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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["ChildContext", "build_context", "load_strategy_main", "run_child"]

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

    def progress(self, note: Optional[str] = None) -> Dict[str, Any]:
        """Report progress to the hosted attempt (the child's own liveness)."""
        return self.run.progress(note)


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
    """Import the uploaded file in this (child) process and return its ``main``."""
    path = Path(source_path)
    if not path.is_file():
        raise RuntimeError(f"hosted strategy source not found: {source_path}")
    spec = importlib.util.spec_from_file_location("hosted_strategy", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load hosted strategy source: {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
