"""Export the reviewed MCP catalog to JSON for the Go adapter.

Reads the Python adapter's catalog (specs) and request models (contracts),
writes mcp/go/internal/catalog/catalog.json plus a golden tool-name fixture
for the Go parity test. Run from repo root:

    python3 mcp/go/tools/export_catalog.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PKG = REPO / "mcp" / "python" / "kite_algo_mcp"


def _load_standalone(module_name: str, path: Path):
    """Load a package module without running kite_algo_mcp/__init__.py.

    catalog.py needs only the stdlib and contracts.py only pydantic; going
    through the package would drag fastmcp into the exporter.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


catalog = _load_standalone("kite_export_catalog", PKG / "catalog.py")
contracts = _load_standalone("kite_export_contracts", PKG / "contracts.py")

TOOL_CATALOG = catalog.TOOL_CATALOG
TOOL_SPECS = catalog.TOOL_SPECS

# Tool name -> contracts request model. Every cataloged tool takes exactly one
# request model; `None` entries take ad-hoc scalar args whose schemas are
# synthesized in _adhoc_schema() below (kept aligned with tools/*.py).
_REQUEST_MODELS: dict[str, str | None] = {
    "get_capabilities": None,
    "list_runs": "RunListRequest",
    "get_run": "RunSelector",
    "get_run_health": "RunSelector",
    "get_funds": None,
    "get_run_funds": "RunSelector",
    "get_account_portfolio": None,
    "search_instruments": "SearchInstrumentsRequest",
    "resolve_instruments": "SymbolRequest",
    "get_quotes": "SymbolRequest",
    "get_market_snapshot": "SymbolRequest",
    "get_market_depth": "SymbolRequest",
    "get_candles": "CandleRequest",
    "get_current_candle": "CandleRequest",
    "get_historical_candles": "HistoricalCandleRequest",
    "request_history": "HistoricalCandleRequest",
    "get_market_calendar": "CalendarRequest",
    "get_market_calendar_status": None,
    "get_index_constituents": "IndexRequest",
    "get_index_status": "IndexRequest",
    "get_fundamentals_features": "FundamentalsScopeRequest",
    "get_fundamentals_statements": "FundamentalsStatementRequest",
    "get_fundamentals_status": "FundamentalsScopeRequest",
    "refresh_fundamentals": "FundamentalsRefreshRequest",
    "create_run": "CreateRunRequest",
    "check_run_safety": "RunSelector",
    "preview_order": "PlaceOrderRequest",
    "preview_basket": "BasketRequest",
    "place_order": "PlaceOrderRequest",
    "place_basket": "BasketRequest",
    "list_orders": "RunSelector",
    "list_trades": "RunSelector",
    "get_order": "OrderActionRequest",
    "get_order_history": "OrderActionRequest",
    "modify_order": "OrderModifyRequest",
    "cancel_order": "OrderActionRequest",
    "list_baskets": "RunSelector",
    "get_basket": None,
    "create_bracket": "BracketRequest",
    "list_brackets": "RunSelector",
    "get_bracket": None,
    "cancel_bracket": None,
    "create_gtt": "GttRequest",
    "list_gtts": None,
    "get_gtt": None,
    "modify_gtt": None,
    "delete_gtt": None,
    "exit_run": "ExitRunRequest",
    "update_run_risk": "RiskRequest",
    "update_run_protection": "ProtectionRequest",
    "get_run_protection": "RunSelector",
    "get_run_pnl": "RunSelector",
    "list_run_timeline": "PageRequest",
    "list_execution_events": "PageRequest",
    "log_run_decision": "DecisionRequest",
    "list_option_expiries": "OptionRequest",
    "get_option_chain": "OptionRequest",
    "get_option_mini_chain": "OptionRequest",
    "get_option_greeks": "OptionRequest",
    "get_option_pcr": "OptionRequest",
    "get_option_max_pain": "OptionRequest",
    "resolve_option_contracts": "OptionRequest",
    "preview_option_strategy": "OptionRunRequest",
    "preview_option_entry": "OptionActionRequest",
    "preview_option_exit": "OptionActionRequest",
    "create_option_run": "OptionCreateRunRequest",
    "enter_option_run": "OptionWriteActionRequest",
    "exit_option_run": "OptionWriteActionRequest",
    "get_option_run_state": "OptionActionRequest",
    "update_option_protection": "OptionActionRequest",
    "get_option_protection": "OptionActionRequest",
    "replay_option_protection": "OptionReplayRequest",
    "calculate_indicator": "IndicatorRequest",
}

_ADHOC_SCHEMAS: dict[str, dict] = {
    "get_account_portfolio": {"account_scope": {"type": "string"}},
    "get_basket": {"strategy_run_id": {"type": "string"}, "basket_execution_id": {"type": "string"}},
    "get_bracket": {"strategy_run_id": {"type": "string"}, "bracket_intent_id": {"type": "string"}},
    "cancel_bracket": {"strategy_run_id": {"type": "string"}, "bracket_intent_id": {"type": "string"}},
    "list_gtts": {},
    "get_gtt": {"trigger_id": {"type": "integer"}},
    "modify_gtt": {"trigger_id": {"type": "integer"}, "request": {"type": "object", "additionalProperties": True}},
    "delete_gtt": {"trigger_id": {"type": "integer"}},
}


def _adhoc_schema(tool: str) -> dict:
    properties = _ADHOC_SCHEMAS[tool]
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def main() -> None:
    missing = [name for name in TOOL_CATALOG if name not in _REQUEST_MODELS]
    if missing:
        raise SystemExit(f"export_catalog: add request-model mapping for: {sorted(missing)}")

    tools = []
    for spec in TOOL_SPECS:
        model_name = _REQUEST_MODELS[spec.name]
        if model_name is not None:
            model_cls = getattr(contracts, model_name, None)
            if model_cls is None:
                raise SystemExit(f"export_catalog: contracts.{model_name} not found")
            input_schema = model_cls.model_json_schema()
        elif spec.name in _ADHOC_SCHEMAS:
            input_schema = _adhoc_schema(spec.name)
        else:
            input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.append({
            "name": spec.name,
            "group": spec.group,
            "required_action": spec.required_action,
            "effect": spec.effect,
            "scope": spec.scope,
            "idempotent": spec.idempotent,
            "description": spec.description,
            "live_only": spec.live_only,
            "reconcile_with": spec.reconcile_with,
            "request_model": model_name,
            "input_schema": input_schema,
        })

    tools.sort(key=lambda entry: entry["name"])
    out = REPO / "mcp/go/internal/catalog/catalog.json"
    out.write_text(json.dumps({"tools": tools}, indent=2, sort_keys=True) + "\n")

    golden = REPO / "tests/mcp/fixtures/go_parity_tool_names.json"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text(json.dumps(sorted(TOOL_CATALOG.keys()), indent=2) + "\n")
    print(f"wrote {len(tools)} tools -> {out}")


if __name__ == "__main__":
    main()
