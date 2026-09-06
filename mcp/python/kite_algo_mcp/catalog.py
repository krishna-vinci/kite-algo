"""The reviewed public MCP catalog.

Keeping this list separate from registration makes omissions visible in code
review and lets policy use exactly the same metadata as discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def _read(name: str, group: str, action: str = "market:read", *, scope: str = "none", description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "read", scope, True, description or f"Bounded {name.replace('_', ' ')} read.")  # type: ignore[arg-type]


def _write(name: str, group: str, action: str, *, scope: str = "run", live_only: bool = False, idempotent: bool = False, description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "trade_write", scope, idempotent, description or f"Explicit {name.replace('_', ' ')} write.", live_only)  # type: ignore[arg-type]


def _data(name: str, group: str, action: str = "fundamentals:write", *, description: str = "") -> ToolSpec:
    return ToolSpec(name, group, action, "data_write", "none", True, description or f"Explicit {name.replace('_', ' ')} data refresh.")


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _read("get_capabilities", "discovery", "health:read", description="Return redacted worker capabilities and maintained constants."),
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
    _data("request_history", "market", "market:write", description="Explicitly request bounded historical ingestion."),
    _read("get_market_calendar", "market"),
    _read("get_market_calendar_status", "market"),
    _read("get_index_constituents", "market"),
    _read("get_index_status", "market"),
    _read("get_fundamentals_features", "fundamentals", "fundamentals:read"),
    _read("get_fundamentals_statements", "fundamentals", "fundamentals:read"),
    _read("get_fundamentals_status", "fundamentals", "fundamentals:read"),
    _data("refresh_fundamentals", "fundamentals"),
    _write("create_run", "runs", "runs:create", scope="run", idempotent=True),
    _read("check_run_safety", "runs", "runs:read", scope="run"),
    _read("preview_order", "orders", "intents:submit", scope="run"),
    _read("preview_basket", "orders", "intents:submit", scope="run"),
    _write("place_order", "orders", "intents:submit", scope="run", idempotent=True),
    _write("place_basket", "orders", "intents:submit", scope="run", idempotent=True),
    _read("list_orders", "orders", "runs:read", scope="run"),
    _read("list_trades", "orders", "runs:read", scope="run"),
    _read("get_order", "orders", "runs:read", scope="run"),
    _read("get_order_history", "orders", "runs:read", scope="run"),
    _write("modify_order", "orders", "intents:submit", scope="run", idempotent=True),
    _write("cancel_order", "orders", "intents:submit", scope="run", idempotent=True),
    _read("list_baskets", "orders", "runs:read", scope="run"),
    _read("get_basket", "orders", "runs:read", scope="run"),
    _write("create_bracket", "orders", "intents:submit", scope="run", idempotent=True),
    _read("list_brackets", "orders", "runs:read", scope="run"),
    _read("get_bracket", "orders", "runs:read", scope="run"),
    _write("cancel_bracket", "orders", "intents:submit", scope="run", idempotent=True),
    _write("create_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=True),
    _read("list_gtts", "orders", "gtt:read", scope="account", description="Inspect live account-level GTTs."),
    _read("get_gtt", "orders", "gtt:read", scope="account"),
    _write("modify_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=True),
    _write("delete_gtt", "orders", "gtt:write", scope="account", live_only=True, idempotent=True),
    _write("exit_run", "runs", "runs:exit", scope="run", idempotent=True),
    _write("update_run_risk", "runs", "risk:update", scope="run", idempotent=True),
    _write("update_run_protection", "runs", "risk:update", scope="run", idempotent=True),
    _read("get_run_protection", "runs", "runs:read", scope="run"),
    _read("get_run_pnl", "runs", "runs:read", scope="run"),
    _read("list_run_timeline", "runs", "runs:read", scope="run"),
    _read("list_execution_events", "runs", "runs:read", scope="run"),
    _write("log_run_decision", "runs", "runs:log", scope="run", idempotent=True),
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
    _write("create_option_run", "options", "runs:create", scope="run", idempotent=True),
    _write("enter_option_run", "options", "intents:submit", scope="run", idempotent=True),
    _write("exit_option_run", "options", "runs:exit", scope="run", idempotent=True),
    _read("get_option_run_state", "options", "runs:read", scope="run"),
    _write("update_option_protection", "options", "risk:update", scope="run", idempotent=True),
    _read("get_option_protection", "options", "runs:read", scope="run"),
    _read("replay_option_protection", "options", "market:read", scope="run", description="Pure simulation of option protection against supplied snapshots."),
    _read("calculate_indicator", "indicators", "market:read", description="Calculate one allowlisted SDK indicator over bounded supplied candles."),
)


TOOL_CATALOG = {spec.name: spec for spec in TOOL_SPECS}

if len(TOOL_CATALOG) != len(TOOL_SPECS):  # pragma: no cover - import invariant
    raise RuntimeError("duplicate MCP tool name in catalog")


def specs_for_group(group: str) -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOL_SPECS if spec.group == group)


__all__ = ["Effect", "ToolSpec", "TOOL_SPECS", "TOOL_CATALOG", "specs_for_group"]
