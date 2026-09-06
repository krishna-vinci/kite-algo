from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class WorkflowOptions:
    async def list_expiries(self, underlying):
        return {"expiries": ["2026-09-10"]}


class WorkflowFake:
    def __init__(self):
        self.options = WorkflowOptions()
        self.submissions = 0

    async def health(self):
        return {"allowed_actions": ["health:read", "market:read", "fundamentals:read", "runs:read", "runs:create", "intents:submit", "runs:exit"]}

    async def get_index_constituents(self, source_list, **kwargs):
        return {"source_list": source_list, "members": [{"symbol": "ABC", "sector": "Financial Services"}]}

    async def get_fundamentals_features(self, **kwargs):
        return {"rows": [{"symbol": symbol, "pe": None} for symbol in kwargs["symbols"]]}

    async def get_candles(self, instrument, interval, lookback):
        return {"candles": [{"timestamp": str(index), "close": 10 + index, "open": 9 + index, "high": 11 + index, "low": 8 + index, "volume": 10} for index in range(lookback)]}

    async def create_run(self, **kwargs):
        return {"strategy_run_id": "run-1", "execution_mode": kwargs["execution_mode"]}

    async def preview_basket(self, *args, **kwargs):
        return {"preview": True}

    async def claim_session(self, run_id):
        return {"session_nonce": "hidden"}

    async def release_session(self, run_id, *, session_nonce):
        pass

    async def run_heartbeat(self, run_id, *, session_nonce, status):
        pass

    async def safety_check(self, run_id):
        return {"allowed": True}

    async def place_basket(self, *args, **kwargs):
        self.submissions += 1
        return {"status": "accepted", "basket_execution_id": "basket-1"}

    async def list_orders(self, run_id):
        return {"orders": []}

    async def exit_run(self, *args, **kwargs):
        return {"status": "exited"}


@pytest.mark.asyncio
async def test_research_then_explicit_paper_execution_workflow() -> None:
    fake = WorkflowFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=fake)
    async with Client(server) as client:
        index = await client.call_tool("get_index_constituents", {"request": {"source_list": "nifty500"}})
        members = json.loads(index.content[0].text)["data"]["members"]
        symbols = [members[0]["symbol"]]
        fundamentals = await client.call_tool("get_fundamentals_features", {"request": {"symbols": symbols}})
        assert symbols[0] in fundamentals.content[0].text
        candles = await client.call_tool("get_candles", {"request": {"instrument": symbols[0], "lookback": 30}})
        bars = json.loads(candles.content[0].text)["data"]["candles"]
        indicator = await client.call_tool("calculate_indicator", {"request": {"name": "sma", "period": 3, "bars": bars}})
        assert "warmup_rows" in indicator.content[0].text
        await client.call_tool("create_run", {"request": {"template_id": "template", "account_scope": "paper", "execution_mode": "paper"}})
        preview = await client.call_tool("preview_basket", {"request": {"strategy_run_id": "run-1", "idempotency_key": "basket-preview-1", "orders": [{"symbol": "ABC", "transaction_type": "BUY", "quantity": 1}]}})
        assert json.loads(preview.content[0].text)["data"]["preview"] is True
        assert fake.submissions == 0
        await client.call_tool("place_basket", {"request": {"strategy_run_id": "run-1", "idempotency_key": "basket-submit-1", "orders": [{"symbol": "ABC", "transaction_type": "BUY", "quantity": 1}]}})
        assert fake.submissions == 1
