"""Fixture invocation coverage for every reviewed MCP catalog tool.

The worker fake deliberately exposes named SDK methods only.  A missing
adapter mapping therefore raises AttributeError instead of looking like a
successful generic fake response.
"""

from __future__ import annotations

import json
import inspect

import pytest
from fastmcp import Client
from kite_algo_worker import AsyncKiteAlgoWorkerClient, AsyncOptionWorkerClient

from kite_algo_mcp.catalog import TOOL_CATALOG
from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


RUN_ID = "run-fixture"
ORDER = {"symbol": "INFY", "transaction_type": "BUY", "quantity": 1}
OPTION_SELECTOR = {"kind": "exact", "option_type": "CE", "strike": 25_000}


class RecordingOptions:
    def __init__(self, owner: "RecordingWorker") -> None:
        self.owner = owner

    async def _record(self, name: str, *args, **kwargs):
        self.owner.calls.append((f"options.{name}", args, kwargs))
        if name == "resolve_contracts":
            return {"resolved": [{"tradingsymbol": "NIFTY25SEP25000CE", "lot_size": 50, "instrument_token": 1, "exchange": "NFO", "option_type": "CE", "strike": 25_000}]}
        return {"method": f"options.{name}"}

    async def list_expiries(self, underlying): return await self._record("list_expiries", underlying)
    async def get_chain(self, underlying, **kwargs): return await self._record("get_chain", underlying, **kwargs)
    async def get_mini_chain(self, underlying, **kwargs): return await self._record("get_mini_chain", underlying, **kwargs)
    async def get_greeks(self, underlying, **kwargs): return await self._record("get_greeks", underlying, **kwargs)
    async def resolve_contracts(self, underlying, payload): return await self._record("resolve_contracts", underlying, payload)
    async def get_pcr(self, underlying, **kwargs): return await self._record("get_pcr", underlying, **kwargs)
    async def get_max_pain(self, underlying, **kwargs): return await self._record("get_max_pain", underlying, **kwargs)
    async def preview_strategy(self, payload): return await self._record("preview_strategy", payload)
    async def preview_run_entry(self, run_id, payload): return await self._record("preview_run_entry", run_id, payload)
    async def preview_exit(self, run_id, payload): return await self._record("preview_exit", run_id, payload)
    async def create_run(self, **kwargs): return await self._record("create_run", **kwargs)
    async def enter(self, run_id, payload, **kwargs): return await self._record("enter", run_id, payload, **kwargs)
    async def exit(self, run_id, payload, **kwargs): return await self._record("exit", run_id, payload, **kwargs)
    async def get_run_state(self, run_id): return await self._record("get_run_state", run_id)
    async def update_protection(self, run_id, protection, **kwargs): return await self._record("update_protection", run_id, protection, **kwargs)
    async def get_protection_state(self, run_id): return await self._record("get_protection_state", run_id)
    async def replay_protection(self, run_id, snapshots): return await self._record("replay_protection", run_id, snapshots)


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.options = RecordingOptions(self)

    async def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return {"method": name, "items": [], "candles": [], "allowed": True}

    async def health(self):
        self.calls.append(("health", (), {}))
        return {"allowed_actions": ["*"], "allowed_modes": ["paper", "dry_run", "live"]}

    async def claim_session(self, run_id):
        await self._record("claim_session", run_id)
        return {"session_nonce": "fixture-nonce"}
    async def release_session(self, run_id, **kwargs): return await self._record("release_session", run_id, **kwargs)
    async def run_heartbeat(self, run_id, **kwargs): return await self._record("run_heartbeat", run_id, **kwargs)
    async def safety_check(self, run_id): return await self._record("safety_check", run_id)
    async def get_run(self, run_id):
        await self._record("get_run", run_id)
        return {"strategy_run_id": run_id, "execution_mode": "paper", "account_scope": "paper-a"}

    async def list_runs(self, **kwargs): return await self._record("list_runs", **kwargs)
    async def get_funds(self, **kwargs): return await self._record("get_funds", **kwargs)
    async def get_run_funds(self, run_id): return await self._record("get_run_funds", run_id)
    async def get_account_portfolio(self, **kwargs): return await self._record("get_account_portfolio", **kwargs)
    async def create_run(self, **kwargs): return await self._record("create_run", **kwargs)
    async def search_tickers(self, *args): return await self._record("search_tickers", *args)
    async def resolve_tickers(self, values): return await self._record("resolve_tickers", values)
    async def get_quotes(self, *args, **kwargs):
        await self._record("get_quotes", *args, **kwargs)
        return {"quotes": [{"symbol": "INFY", "last_price": 100, "depth": {"buy": [], "sell": []}}]}
    async def get_market_snapshot(self, **kwargs): return await self._record("get_market_snapshot", **kwargs)
    async def get_candles(self, *args): return await self._record("get_candles", *args)
    async def calculate_indicator(self, request): return await self._record("calculate_indicator", request)
    async def get_historical_candles(self, *args, **kwargs): return await self._record("get_historical_candles", *args, **kwargs)
    async def get_market_calendar(self, *args, **kwargs): return await self._record("get_market_calendar", *args, **kwargs)
    async def get_market_calendar_status(self, **kwargs): return await self._record("get_market_calendar_status", **kwargs)
    async def get_index_constituents(self, *args): return await self._record("get_index_constituents", *args)
    async def get_index_constituent_status(self, *args): return await self._record("get_index_constituent_status", *args)
    async def get_fundamentals_features(self, **kwargs): return await self._record("get_fundamentals_features", **kwargs)
    async def get_fundamentals_statements(self, *args, **kwargs): return await self._record("get_fundamentals_statements", *args, **kwargs)
    async def get_fundamentals_status(self, **kwargs): return await self._record("get_fundamentals_status", **kwargs)
    async def refresh_fundamentals(self, **kwargs): return await self._record("refresh_fundamentals", **kwargs)
    async def preview_order(self, *args, **kwargs): return await self._record("preview_order", *args, **kwargs)
    async def preview_basket(self, *args, **kwargs): return await self._record("preview_basket", *args, **kwargs)
    async def place_order(self, *args, **kwargs): return await self._record("place_order", *args, **kwargs)
    async def place_basket(self, *args, **kwargs): return await self._record("place_basket", *args, **kwargs)
    async def list_orders(self, *args): return await self._record("list_orders", *args)
    async def list_trades(self, *args): return await self._record("list_trades", *args)
    async def get_order_snapshot(self, *args): return await self._record("get_order_snapshot", *args)
    async def get_order_history(self, *args): return await self._record("get_order_history", *args)
    async def modify_order(self, *args, **kwargs): return await self._record("modify_order", *args, **kwargs)
    async def cancel_order(self, *args, **kwargs): return await self._record("cancel_order", *args, **kwargs)
    async def list_baskets(self, *args, **kwargs): return await self._record("list_baskets", *args, **kwargs)
    async def get_basket(self, *args): return await self._record("get_basket", *args)
    async def create_bracket(self, *args, **kwargs): return await self._record("create_bracket", *args, **kwargs)
    async def list_brackets(self, *args, **kwargs): return await self._record("list_brackets", *args, **kwargs)
    async def get_bracket(self, *args): return await self._record("get_bracket", *args)
    async def cancel_bracket(self, *args, **kwargs): return await self._record("cancel_bracket", *args, **kwargs)
    async def place_gtt(self, *args, **kwargs): return await self._record("place_gtt", *args, **kwargs)
    async def list_gtts(self): return await self._record("list_gtts")
    async def get_gtt(self, *args): return await self._record("get_gtt", *args)
    async def modify_gtt(self, *args, **kwargs): return await self._record("modify_gtt", *args, **kwargs)
    async def delete_gtt(self, *args): return await self._record("delete_gtt", *args)
    async def exit_run(self, *args, **kwargs): return await self._record("exit_run", *args, **kwargs)
    async def patch_risk(self, *args, **kwargs): return await self._record("patch_risk", *args, **kwargs)
    async def update_backend_protection(self, *args, **kwargs): return await self._record("update_backend_protection", *args, **kwargs)
    async def _request(self, method, path, **kwargs):
        assert method == "GET"
        assert path == f"/worker/runs/{RUN_ID}/protection"
        return await self._record("_request", method, path, **kwargs)
    async def get_run_pnl_snapshot(self, *args): return await self._record("get_run_pnl_snapshot", *args)
    async def list_timeline(self, *args, **kwargs): return await self._record("list_timeline", *args, **kwargs)
    async def list_execution_events(self, *args, **kwargs): return await self._record("list_execution_events", *args, **kwargs)
    async def log_decision_event(self, *args, **kwargs): return await self._record("log_decision_event", *args, **kwargs)


BASE_ORDER = {"symbol": "INFY", "transaction_type": "BUY", "quantity": 1}
CASES = {
    "get_capabilities": {}, "list_runs": {"request": {"limit": 1}}, "get_run": {"request": {"strategy_run_id": RUN_ID}},
    "get_run_health": {"request": {"strategy_run_id": RUN_ID}}, "get_funds": {}, "get_run_funds": {"request": {"strategy_run_id": RUN_ID}},
    "get_account_portfolio": {}, "search_instruments": {"request": {"query": "INFY"}}, "resolve_instruments": {"request": {"symbols": ["INFY"]}},
    "get_quotes": {"request": {"symbols": ["INFY"]}}, "get_market_snapshot": {"request": {"symbols": ["INFY"]}}, "get_market_depth": {"request": {"symbols": ["INFY"]}},
    "get_candles": {"request": {"instrument": "INFY"}}, "get_current_candle": {"request": {"instrument": "INFY"}},
    "get_historical_candles": {"request": {"instrument": "INFY"}}, "request_history": {"request": {"instrument": "INFY"}},
    "get_market_calendar": {"request": {"from_date": "2026-09-01", "to_date": "2026-09-02"}}, "get_market_calendar_status": {},
    "get_index_constituents": {"request": {"source_list": "nifty50"}}, "get_index_status": {"request": {"source_list": "nifty50"}},
    "get_fundamentals_features": {"request": {"symbols": ["INFY"]}}, "get_fundamentals_statements": {"request": {"symbol": "INFY", "dataset": "quarterly"}},
    "get_fundamentals_status": {"request": {"symbols": ["INFY"]}}, "refresh_fundamentals": {"request": {"symbols": ["INFY"]}},
    "create_run": {"request": {"template_id": "template", "account_scope": "paper-a"}}, "check_run_safety": {"request": {"strategy_run_id": RUN_ID}},
    "preview_order": {"request": {"strategy_run_id": RUN_ID, "idempotency_key": "fixture-order-1", "order": BASE_ORDER}},
    "preview_basket": {"request": {"strategy_run_id": RUN_ID, "idempotency_key": "fixture-basket-1", "orders": [BASE_ORDER]}},
    "place_order": {"request": {"strategy_run_id": RUN_ID, "idempotency_key": "fixture-order-1", "order": BASE_ORDER}},
    "place_basket": {"request": {"strategy_run_id": RUN_ID, "idempotency_key": "fixture-basket-1", "orders": [BASE_ORDER]}},
    "list_orders": {"request": {"strategy_run_id": RUN_ID}}, "list_trades": {"request": {"strategy_run_id": RUN_ID}},
    "get_order": {"request": {"strategy_run_id": RUN_ID, "order_id": "order-1"}}, "get_order_history": {"request": {"strategy_run_id": RUN_ID, "order_id": "order-1"}},
    "modify_order": {"request": {"strategy_run_id": RUN_ID, "order_id": "order-1", "quantity": 1}}, "cancel_order": {"request": {"strategy_run_id": RUN_ID, "order_id": "order-1"}},
    "list_baskets": {"request": {"strategy_run_id": RUN_ID}}, "get_basket": {"strategy_run_id": RUN_ID, "basket_execution_id": "basket-1"},
    "create_bracket": {"request": {"strategy_run_id": RUN_ID, "idempotency_key": "fixture-bracket-1", "entry_order": BASE_ORDER, "stoploss": {**BASE_ORDER, "order_type": "SL-M", "trigger_price": 90}}},
    "list_brackets": {"request": {"strategy_run_id": RUN_ID}}, "get_bracket": {"strategy_run_id": RUN_ID, "bracket_intent_id": "bracket-1"}, "cancel_bracket": {"strategy_run_id": RUN_ID, "bracket_intent_id": "bracket-1"},
    "create_gtt": {"request": {"type": "single", "tradingsymbol": "INFY", "trigger_values": [100], "last_price": 99, "orders": [{"tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1, "price": 100}]}},
    "list_gtts": {}, "get_gtt": {"trigger_id": 1}, "modify_gtt": {"trigger_id": 1, "request": {"type": "single", "tradingsymbol": "INFY", "trigger_values": [100], "last_price": 99, "orders": [{"tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1, "price": 100}]}}, "delete_gtt": {"trigger_id": 1},
    "exit_run": {"request": {"strategy_run_id": RUN_ID}}, "update_run_risk": {"request": {"strategy_run_id": RUN_ID, "max_daily_loss": 100}}, "update_run_protection": {"request": {"strategy_run_id": RUN_ID, "stoploss_pct": 1.0}},
    "get_run_protection": {"request": {"strategy_run_id": RUN_ID}}, "get_run_pnl": {"request": {"strategy_run_id": RUN_ID}}, "list_run_timeline": {"request": {"strategy_run_id": RUN_ID}}, "list_execution_events": {"request": {"strategy_run_id": RUN_ID}}, "log_run_decision": {"request": {"strategy_run_id": RUN_ID, "decision_type": "signal", "summary": "fixture"}},
    "list_option_expiries": {"request": {"underlying": "NIFTY"}}, "get_option_chain": {"request": {"underlying": "NIFTY"}}, "get_option_mini_chain": {"request": {"underlying": "NIFTY"}}, "get_option_greeks": {"request": {"underlying": "NIFTY"}}, "get_option_pcr": {"request": {"underlying": "NIFTY"}}, "get_option_max_pain": {"request": {"underlying": "NIFTY"}}, "resolve_option_contracts": {"request": {"underlying": "NIFTY", "selector": OPTION_SELECTOR}},
    "preview_option_strategy": {"request": {"strategy_name": "fixture", "underlying": "NIFTY", "expiry": "2026-09-24", "legs": [OPTION_SELECTOR]}}, "preview_option_entry": {"request": {"strategy_run_id": RUN_ID}}, "preview_option_exit": {"request": {"strategy_run_id": RUN_ID}},
    "create_option_run": {"request": {"strategy_name": "fixture", "strategy_run_id": RUN_ID, "account_scope": "paper-a", "execution_mode": "paper", "underlying": "NIFTY", "expiry": "2026-09-24", "legs": [OPTION_SELECTOR]}},
    "enter_option_run": {"request": {"strategy_run_id": RUN_ID, "execution_mode": "paper", "account_scope": "paper-a", "idempotency_key": "fixture-option-1"}}, "exit_option_run": {"request": {"strategy_run_id": RUN_ID, "execution_mode": "paper", "account_scope": "paper-a", "idempotency_key": "fixture-option-2"}}, "get_option_run_state": {"request": {"strategy_run_id": RUN_ID}},
    "update_option_protection": {"request": {"strategy_run_id": RUN_ID}, "stoploss_pct": 1.0}, "get_option_protection": {"request": {"strategy_run_id": RUN_ID}}, "replay_option_protection": {"request": {"strategy_run_id": RUN_ID, "metric_snapshots": [{"spot": 100}]}},
    "calculate_indicator": {"request": {"name": "sma", "bars": [{"close": 10, "open": 9, "high": 11, "low": 8, "volume": 100}, {"close": 11, "open": 10, "high": 12, "low": 9, "volume": 100}]}},
}


EXPECTED_METHODS = {
    "get_capabilities": None,
    "search_instruments": "search_tickers", "resolve_instruments": "resolve_tickers", "get_quotes": "get_quotes", "get_market_snapshot": "get_market_snapshot", "get_market_depth": "get_quotes", "get_candles": "get_candles", "get_current_candle": "get_candles", "get_historical_candles": "get_historical_candles", "request_history": "get_historical_candles", "get_market_calendar": "get_market_calendar", "get_market_calendar_status": "get_market_calendar_status", "get_index_constituents": "get_index_constituents", "get_index_status": "get_index_constituent_status", "get_fundamentals_features": "get_fundamentals_features", "get_fundamentals_statements": "get_fundamentals_statements", "get_fundamentals_status": "get_fundamentals_status", "refresh_fundamentals": "refresh_fundamentals", "list_runs": "list_runs", "get_run": "get_run", "get_run_health": "get_run", "get_funds": "get_funds", "get_run_funds": "get_run_funds", "get_account_portfolio": "get_account_portfolio", "create_run": "create_run", "check_run_safety": "safety_check", "preview_order": "preview_order", "preview_basket": "preview_basket", "place_order": "place_order", "place_basket": "place_basket", "list_orders": "list_orders", "list_trades": "list_trades", "get_order": "get_order_snapshot", "get_order_history": "get_order_history", "modify_order": "modify_order", "cancel_order": "cancel_order", "list_baskets": "list_baskets", "get_basket": "get_basket", "create_bracket": "create_bracket", "list_brackets": "list_brackets", "get_bracket": "get_bracket", "cancel_bracket": "cancel_bracket", "create_gtt": "place_gtt", "list_gtts": "list_gtts", "get_gtt": "get_gtt", "modify_gtt": "modify_gtt", "delete_gtt": "delete_gtt", "exit_run": "exit_run", "update_run_risk": "patch_risk", "update_run_protection": "update_backend_protection", "get_run_protection": "get_run_protection_state", "get_run_pnl": "get_run_pnl_snapshot", "list_run_timeline": "list_timeline", "list_execution_events": "list_execution_events", "log_run_decision": "log_decision_event", "list_option_expiries": "options.list_expiries", "get_option_chain": "options.get_chain", "get_option_mini_chain": "options.get_mini_chain", "get_option_greeks": "options.get_greeks", "get_option_pcr": "options.get_pcr", "get_option_max_pain": "options.get_max_pain", "resolve_option_contracts": "options.resolve_contracts", "preview_option_strategy": "options.preview_strategy", "preview_option_entry": "options.preview_run_entry", "preview_option_exit": "options.preview_exit", "create_option_run": "options.create_run", "enter_option_run": "options.enter", "exit_option_run": "options.exit", "get_option_run_state": "options.get_run_state", "update_option_protection": "options.update_protection", "get_option_protection": "options.get_protection_state", "replay_option_protection": "options.replay_protection", "calculate_indicator": "calculate_indicator",
}


@pytest.mark.asyncio
async def test_every_catalog_tool_has_a_real_fixture_invocation() -> None:
    assert set(CASES) == set(TOOL_CATALOG)
    assert set(EXPECTED_METHODS) == set(TOOL_CATALOG)
    fake = RecordingWorker()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="live", allow_data_refresh=True), client=fake)
    async with Client(server) as client:
        for name, arguments in CASES.items():
            first_call = len(fake.calls)
            result = await client.call_tool(name, arguments)
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "ok", (name, payload)
            invocation_calls = fake.calls[first_call:]
            for sdk_name, sdk_args, sdk_kwargs in invocation_calls:
                owner = AsyncOptionWorkerClient if sdk_name.startswith("options.") else AsyncKiteAlgoWorkerClient
                sdk_method = getattr(owner, sdk_name.removeprefix("options."))
                inspect.signature(sdk_method).bind(None, *sdk_args, **sdk_kwargs)
            # This optional SDK convenience method is absent from the current
            # release; exercise the adapter's actual transport fallback.
            method = "_request" if name == "get_run_protection" else EXPECTED_METHODS[name]
            if method is not None:
                matching = [call for call in invocation_calls if call[0] == method]
                assert len(matching) == 1, (name, invocation_calls)
            if any(call[0] == "claim_session" for call in invocation_calls):
                assert method is not None, name
                assert matching[0][2].get("session_nonce") == "fixture-nonce", name
                releases = [call for call in invocation_calls if call[0] == "release_session"]
                assert len(releases) == 1, name
                assert releases[0][2]["session_nonce"] == "fixture-nonce", name
