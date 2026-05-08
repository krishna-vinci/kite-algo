from __future__ import annotations

from copy import deepcopy
from itertools import count
from typing import Any, Callable

from .models import OptionRunCreateRequest, OptionRunState


class OptionRunStore:
    """Deterministic in-memory run store for canonical option execution state."""

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._runs: dict[str, OptionRunState] = {}
        self._counter = count(1)
        self._id_factory = id_factory or self._next_run_id

    def _next_run_id(self) -> str:
        return f"opt_run_{next(self._counter):06d}"

    def reset(self) -> None:
        self._runs.clear()
        self._counter = count(1)

    def create_run(self, request: OptionRunCreateRequest) -> OptionRunState:
        strategy_run_id = str(request.strategy_run_id or self._id_factory())
        if not strategy_run_id.startswith("opt_run_") and not request.strategy_run_id:
            strategy_run_id = f"opt_run_{strategy_run_id}"
        if strategy_run_id in self._runs:
            raise ValueError(f"Run already exists: {strategy_run_id}")

        run = OptionRunState.from_create_request(request, strategy_run_id=strategy_run_id)
        self._runs[strategy_run_id] = deepcopy(run)
        return deepcopy(run)

    def list_runs(self) -> list[OptionRunState]:
        return [deepcopy(run) for run in self._runs.values()]

    def get_run(self, strategy_run_id: str) -> OptionRunState:
        if not strategy_run_id:
            raise ValueError("strategy_run_id is required")
        try:
            run = self._runs[strategy_run_id]
        except KeyError as exc:
            raise KeyError(f"Option run not found: {strategy_run_id}") from exc
        return deepcopy(run)

    def get_run_in_session(self, session: Any, strategy_run_id: str) -> OptionRunState:
        _ = session
        return self.get_run(strategy_run_id)

    def save_run(self, run: OptionRunState) -> OptionRunState:
        if not run.strategy_run_id:
            raise ValueError("strategy_run_id is required")
        self._runs[run.strategy_run_id] = deepcopy(run)
        return deepcopy(run)

    def record_orders(self, strategy_run_id: str, orders: list[dict]) -> OptionRunState:
        run = self.get_run(strategy_run_id)
        run.orders.extend(deepcopy(orders))
        return self.save_run(run)

    def record_trades(self, strategy_run_id: str, trades: list[dict]) -> OptionRunState:
        run = self.get_run(strategy_run_id)
        run.trades.extend(deepcopy(trades))
        return self.save_run(run)


_DEFAULT_OPTION_RUN_STORE: Any | None = None


def get_option_run_store() -> Any:
    """Return the production option-run store.

    The production default is durable. Tests and isolated route apps should keep
    using FastAPI dependency overrides with ``OptionRunStore`` when they need a
    deterministic in-memory store.
    """

    global _DEFAULT_OPTION_RUN_STORE
    if _DEFAULT_OPTION_RUN_STORE is None:
        from .durable_store import DurableOptionRunStore

        _DEFAULT_OPTION_RUN_STORE = DurableOptionRunStore()
    return _DEFAULT_OPTION_RUN_STORE


def reset_option_run_store(*, durable: bool = True) -> Any:
    global _DEFAULT_OPTION_RUN_STORE
    if durable:
        from .durable_store import DurableOptionRunStore

        _DEFAULT_OPTION_RUN_STORE = DurableOptionRunStore()
    else:
        _DEFAULT_OPTION_RUN_STORE = OptionRunStore()
    return _DEFAULT_OPTION_RUN_STORE
