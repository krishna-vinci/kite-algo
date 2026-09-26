"""Hosted child bootstrap: build the ``ctx`` a hosted strategy's ``main(ctx)`` needs.

A hosted strategy is a single Python file with ``def main(ctx)``. The supervisor
runs ``python -m kite_algo_worker.hosted <source_file>`` with a minimal,
explicitly allowlisted environment (no supervisor/DB/broker credentials). This
module reads that environment, attaches to the existing run with the attach-only
SDK method, and calls the strategy's ``main``.

The uploaded source is imported **here, in the child process only** — never in
the API or the supervisor.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

__all__ = ["ChildContext", "build_context", "load_strategy_main", "run_child"]

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class SessionContext:
    """The one market session a scheduled child may propose within."""

    schedule_id: str
    date: str
    opens_at: str
    closes_at: str
    _next_seq: int = 0

    def next_evaluation_id(self) -> str:
        evaluation_id = f"session:{self.schedule_id}:{self.date}:{self._next_seq}"
        self._next_seq += 1
        return evaluation_id

    def market_open(self, *, now: Optional[datetime] = None) -> bool:
        moment = (now or datetime.now(_IST)).astimezone(_IST)
        if moment.date().isoformat() != self.date:
            return False
        opens = datetime.fromisoformat(self.opens_at).astimezone(_IST).time()
        closes = datetime.fromisoformat(self.closes_at).astimezone(_IST).time()
        return opens <= moment.time() < closes

ENV_BASE_URL = "KITE_ALGO_BASE_URL"
ENV_WORKER_TOKEN = "KITE_ALGO_WORKER_TOKEN"
ENV_RUN_ID = "KITE_ALGO_RUN_ID"
ENV_SESSION_NONCE = "KITE_ALGO_SESSION_NONCE"
ENV_TEMPLATE_ID = "KITE_ALGO_TEMPLATE_ID"
ENV_ACCOUNT_SCOPE = "KITE_ALGO_ACCOUNT_SCOPE"
ENV_MODE = "KITE_ALGO_MODE"
ENV_PARAMS = "KITE_ALGO_PARAMS"
ENV_SCRATCH = "KITE_ALGO_SCRATCH"


@dataclass
class ChildContext:
    """What a hosted strategy's ``main(ctx)`` receives."""

    params: Dict[str, Any]
    client: Any
    run: Any  # ManagedRun (attach-only)
    scratch: Path
    template_id: str
    execution_mode: str
    run_id: str
    _session_cache: Optional[SessionContext] = field(default=None, init=False, repr=False, compare=False)
    _session_cache_built: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def occurrence(self) -> Optional[Dict[str, Any]]:
        """The occurrence this child was launched for, or ``None`` for run-now.

        Read from the platform binding the attach fetched (never chosen here).
        """
        config = getattr(self.run, "config", None)
        return config.hosted_occurrence if config is not None else None

    @property
    def session(self) -> Optional[SessionContext]:
        """The bound market session, or ``None`` for every other job kind.

        Built once and cached: ``next_evaluation_id()`` mints a fresh id each
        call, so re-reading this property must return the same instance
        rather than resetting the sequence to zero.
        """
        if self._session_cache_built:
            return self._session_cache
        self._session_cache_built = True
        occurrence = self.occurrence
        if not occurrence or occurrence.get("evaluation_kind") != "session_occurrence":
            self._session_cache = None
            return None
        schedule_id = occurrence.get("schedule_id")
        if not occurrence.get("session_date") or not occurrence.get("opens_at") or not occurrence.get("closes_at"):
            self._session_cache = None
            return None
        self._session_cache = SessionContext(
            schedule_id=str(schedule_id or ""),
            date=str(occurrence["session_date"]),
            opens_at=str(occurrence["opens_at"]),
            closes_at=str(occurrence["closes_at"]),
        )
        return self._session_cache

    def progress(self, note: Optional[str] = None) -> Dict[str, Any]:
        """Report progress to the hosted attempt (the child's own liveness)."""
        return self.run.progress(note)

    def request_execution(self, plan_id: str, *, idempotency_key: str) -> Dict[str, Any]:
        """Ask the platform to execute a frozen plan of this run (governed)."""
        return self.run.request_execution(plan_id, idempotency_key=idempotency_key)

    def owned_work(self) -> Dict[str, Any]:
        """The strategy's own positions plus its pending execution work."""
        return self.run.owned_work()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required hosted child environment variable {name}")
    return value


def build_context() -> ChildContext:
    from .client import AlgoWorkerConfig, KiteAlgoWorkerClient
    from .run_config import RunConfig

    base_url = _require(ENV_BASE_URL)
    token = _require(ENV_WORKER_TOKEN)
    run_id = _require(ENV_RUN_ID)
    nonce = _require(ENV_SESSION_NONCE)
    template_id = _require(ENV_TEMPLATE_ID)
    account_scope = _require(ENV_ACCOUNT_SCOPE)
    mode = os.environ.get(ENV_MODE) or "paper"
    params = json.loads(os.environ.get(ENV_PARAMS) or "{}")
    scratch = Path(os.environ.get(ENV_SCRATCH) or os.getcwd())

    client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url=base_url, token=token))
    config = RunConfig(template_id=template_id, account_scope=account_scope, execution_mode=mode)
    managed = client.attach_run(run_id, session_nonce=nonce, config=config)
    return ChildContext(
        params=dict(params),
        client=client,
        run=managed,
        scratch=scratch,
        template_id=template_id,
        execution_mode=mode,
        run_id=run_id,
    )


def load_strategy_main(source_path: str):
    """Import the uploaded file in this (child) process and return its ``main``.

    The module is registered in ``sys.modules`` for the duration of the import,
    under the name the loader gives it. That registration is a correctness
    requirement, not bookkeeping: a source that uses
    ``from __future__ import annotations`` (or any quoted annotation) makes
    ``@dataclass`` resolve its string annotations through
    ``sys.modules[cls.__module__]``, and an unregistered module makes that lookup
    return ``None`` -- the import then dies with ``AttributeError: 'NoneType'
    object has no attribute '__dict__'`` before a single line of strategy code
    runs. On the failure path the previous entry is restored (or the name removed
    if there was none), so a half-built module is never left behind.
    """
    path = Path(source_path)
    if not path.is_file():
        raise RuntimeError(f"hosted strategy source not found: {source_path}")
    spec = importlib.util.spec_from_file_location("hosted_strategy", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load hosted strategy source: {source_path}")
    module = importlib.util.module_from_spec(spec)
    module_name = spec.name or "hosted_strategy"
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    main = getattr(module, "main", None)
    if not callable(main):
        raise RuntimeError("hosted strategy must define a callable main(ctx)")
    return main


def run_child(source_path: Optional[str] = None) -> int:
    if source_path is None:
        if len(sys.argv) < 2:
            raise RuntimeError("usage: python -m kite_algo_worker.hosted <source_file>")
        source_path = sys.argv[1]
    main = load_strategy_main(source_path)
    ctx = build_context()
    result = main(ctx)
    if isinstance(result, int):
        return result
    return 0


def _cli() -> int:
    try:
        return run_child()
    except Exception as exc:  # surfaced to the supervisor's captured log
        print(f"hosted child failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess tests
    raise SystemExit(_cli())
