from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class DiscoveryFake:
    async def health(self):
        return {"allowed_actions": ["health:read", "runs:read", "funds:read", "market:read"]}

    async def list_runs(self, **kwargs):
        return {"items": [{"strategy_run_id": "run-1", "execution_mode": "paper"}], "next_cursor": None, "request": kwargs}

    async def get_run(self, run_id):
        return {"strategy_run_id": run_id, "status": "running"}

    async def get_run_health_snapshot(self, run_id):
        return {"strategy_run_id": run_id, "health": "healthy"}

    async def get_funds(self, **kwargs):
        return {"mode": kwargs["mode"], "available": 1000}

    async def get_run_funds(self, run_id):
        return {"strategy_run_id": run_id, "available": 1000}

    async def get_account_portfolio(self, **kwargs):
        return {"account_scope": kwargs.get("account_scope"), "positions": []}


@pytest.mark.asyncio
async def test_capabilities_redact_worker_fields_and_run_discovery_is_bounded() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=DiscoveryFake())
    async with Client(server) as client:
        result = await client.call_tool("get_capabilities", {})
        text = result.content[0].text
        assert "secret" not in text
        payload = json.loads(text)
        assert payload["data"]["indicator_names"]
        runs = await client.call_tool("list_runs", {"request": {"limit": 1}})
        assert json.loads(runs.content[0].text)["data"]["items"][0]["strategy_run_id"] == "run-1"
