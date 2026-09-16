"""The reviewed public MCP catalog.

Keeping this list separate from registration makes omissions visible in code
review and lets policy use exactly the same metadata as discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


Effect = Literal["read", "data_write", "trade_write"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: str
    required_action: str
    effect: Effect = "read"
    scope: Literal["none", "run", "account"] = "none"
    idempotent: bool = True
    description: str = ""
    live_only: bool = False
    reconcile_with: str | None = None


def _read(name: str, group: str, action: str = "market:read", *, scope: str = "none", description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "read", scope, True, description or f"Bounded {name.replace('_', ' ')} read.")  # type: ignore[arg-type]


def _write(name: str, group: str, action: str, *, scope: str = "run", live_only: bool = False, idempotent: bool = False, description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "trade_write", scope, idempotent, description or f"Explicit {name.replace('_', ' ')} write.", live_only)  # type: ignore[arg-type]


def _data(name: str, group: str, action: str = "market:read", *, description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "data_write", "none", True, description or f"Explicit {name.replace('_', ' ')} data refresh.")


TOOL_SPECS: tuple[ToolSpec, ...] = (
    # /worker/health is authenticated by the bearer token but has no separate
    # worker action gate.  An empty action keeps the health response usable as
    # the source of the remaining dynamic capability checks.
    _read("get_capabilities", "discovery", "", description="Return redacted worker capabilities and maintained constants."),
    _read("list_runs", "discovery", "runs:read", scope="none"),
    _read("get_run", "discovery", "runs:read", scope="run"),
    _read("get_run_health", "discovery", "runs:read", scope="run"),
    _read("get_funds", "discovery", "funds:read", scope="account"),
    _read("get_run_funds", "discovery", "funds:read", scope="run"),
    _read("get_account_portfolio", "discovery", "funds:read", scope="account"),
    _read("search_instruments", "market"),
    _read("resolve_instruments", "market"),
    _read("get_quotes", "market"),
    _read("get_market_snapshot", "market"),
    _read("get_market_depth", "market"),
    _read("get_candles", "market"),
    _read("get_current_candle", "market"),
    _read("get_historical_candles", "market"),
    # The backend deliberately uses market:read for both cached history reads
    # and the explicit ingestion request; MCP's data-refresh profile remains
    # the additional side-effect gate.
    _data("request_history", "market", "market:read", description="Explicitly request bounded historical ingestion."),
    _read("get_market_calendar", "market"),
    _read("get_market_calendar_status", "market"),
    _read("get_index_constituents", "market"),
    _read("get_index_status", "market"),
    # Fundamentals worker routes share the backend market:read gate.
    _read("get_fundamentals_features", "fundamentals", "market:read"),
    _read("get_fundamentals_statements", "fundamentals", "market:read"),
    _read("get_fundamentals_status", "fundamentals", "market:read"),
    _data("refresh_fundamentals", "fundamentals", "market:read"),
    # Server-generated run IDs are not a retry key; callers must reconcile
    # list_runs/get_run after an uncertain create response.
    _write("create_run", "runs", "runs:create", scope="run", idempotent=False),
    _read("check_run_safety", "runs", "runs:read", scope="run"),
    _read("preview_order", "orders", "intents:submit", scope="run"),
    _read("preview_basket", "orders", "intents:submit", scope="run"),
    _write("place_order", "orders", "intents:submit", scope="run", idempotent=True),
    _write("place_basket", "orders", "intents:submit", scope="run", idempotent=True),
    _read("list_orders", "orders", "runs:read", scope="run"),
    _read("list_trades", "orders", "runs:read", scope="run"),
    _read("get_order", "orders", "runs:read", scope="run"),
    _read("get_order_history", "orders", "runs:read", scope="run"),
    # These endpoints target an existing broker order but accept no retry key;
    # a timeout must therefore be reconciled, not automatically replayed.
    _write("modify_order", "orders", "intents:submit", scope="run", idempotent=False),
    _write("cancel_order", "orders", "intents:submit", scope="run", idempotent=False),
    _read("list_baskets", "orders", "runs:read", scope="run"),
    _read("get_basket", "orders", "runs:read", scope="run"),
    # The worker backend accepts a key for attribution but currently allocates
    # a fresh bracket intent on every request; do not advertise this as
    # idempotent until the backend deduplicates it.
    _write("create_bracket", "orders", "intents:submit", scope="run", idempotent=False),
    _read("list_brackets", "orders", "runs:read", scope="run"),
    _read("get_bracket", "orders", "runs:read", scope="run"),
    _write("cancel_bracket", "orders", "intents:submit", scope="run", idempotent=False),
    # Provider GTT creation has no worker idempotency key and can create a
    # second trigger after an uncertain response.
    _write("create_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=False),
    _read("list_gtts", "orders", "gtt:read", scope="account", description="Inspect live account-level GTTs."),
    _read("get_gtt", "orders", "gtt:read", scope="account"),
    # Broker GTT routes have no worker idempotency key.  The same applies to
    # order cancellation above and to these state-changing account actions.
    _write("modify_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=False),
    _write("delete_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=False),
    _write("exit_run", "runs", "runs:exit", scope="run", idempotent=False),
    _write("update_run_risk", "runs", "risk:update", scope="run", idempotent=False),
    _write("update_run_protection", "runs", "risk:update", scope="run", idempotent=False),
    _read("get_run_protection", "runs", "runs:read", scope="run"),
    _read("get_run_pnl", "runs", "runs:read", scope="run"),
    _read("list_run_timeline", "runs", "runs:read", scope="run"),
    _read("list_execution_events", "runs", "runs:read", scope="run"),
    _write("log_run_decision", "runs", "runs:log", scope="run", idempotent=False),
    _read("list_option_expiries", "options", "market:read"),
    _read("get_option_chain", "options", "market:read"),
    _read("get_option_mini_chain", "options", "market:read"),
    _read("get_option_greeks", "options", "market:read"),
    _read("get_option_pcr", "options", "market:read"),
    _read("get_option_max_pain", "options", "market:read"),
    _read("resolve_option_contracts", "options", "market:read"),
    _read("preview_option_strategy", "options", "intents:submit", scope="run"),
    _read("preview_option_entry", "options", "intents:submit", scope="run"),
    _read("preview_option_exit", "options", "intents:submit", scope="run"),
    # Option state routes currently do not deduplicate the supplied key; keep
    # the key for attribution/reconciliation but do not advertise retry safety.
    _write("create_option_run", "options", "runs:create", scope="run", idempotent=False),
    _write("enter_option_run", "options", "intents:submit", scope="run", idempotent=False),
    _write("exit_option_run", "options", "runs:exit", scope="run", idempotent=False),
    _read("get_option_run_state", "options", "runs:read", scope="run"),
    _write("update_option_protection", "options", "risk:update", scope="run", idempotent=False),
    _read("get_option_protection", "options", "runs:read", scope="run"),
    _read("replay_option_protection", "options", "market:read", scope="run", description="Pure simulation of option protection against supplied snapshots."),
    _read("calculate_indicator", "indicators", "market:read", description="Calculate one allowlisted SDK indicator over bounded supplied candles."),
)


_RECONCILIATION_TOOLS = {
    "create_run": "get_run",
    "place_order": "get_order",
    "place_basket": "get_basket",
    "modify_order": "get_order",
    "cancel_order": "get_order",
    "create_bracket": "get_bracket",
    "cancel_bracket": "get_bracket",
    "create_gtt": "get_gtt",
    "modify_gtt": "get_gtt",
    "delete_gtt": "get_gtt",
    "exit_run": "get_run",
    "update_run_risk": "get_run",
    "update_run_protection": "get_run_protection",
    "log_run_decision": "list_run_timeline",
    "create_option_run": "get_option_run_state",
    "enter_option_run": "get_option_run_state",
    "exit_option_run": "get_option_run_state",
    "update_option_protection": "get_option_protection",
}

TOOL_CATALOG = {
    spec.name: replace(spec, reconcile_with=_RECONCILIATION_TOOLS.get(spec.name))
    for spec in TOOL_SPECS
}

if len(TOOL_CATALOG) != len(TOOL_SPECS):  # pragma: no cover - import invariant
    raise RuntimeError("duplicate MCP tool name in catalog")


def specs_for_group(group: str) -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOL_SPECS if spec.group == group)


__all__ = ["Effect", "ToolSpec", "TOOL_SPECS", "TOOL_CATALOG", "specs_for_group"]
