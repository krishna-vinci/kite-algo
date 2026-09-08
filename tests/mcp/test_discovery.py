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
        assert payload["data"]["available_data_tools"] == []
        runs = await client.call_tool("list_runs", {"request": {"limit": 1}})
        assert json.loads(runs.content[0].text)["data"]["items"][0]["strategy_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_capabilities_reports_effective_refresh_tools_after_backend_intersection() -> None:
    server = create_server(
        MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="read", allow_data_refresh=True),
        client=DiscoveryFake(),
    )
    async with Client(server) as client:
        result = await client.call_tool("get_capabilities", {})
        payload = json.loads(result.content[0].text)["data"]
        assert payload["data_refresh_enabled"] is True
        assert payload["available_data_tools"] == ["request_history", "refresh_fundamentals"]
        assert payload["available_trade_tools"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["read", "paper", "live"])
@pytest.mark.parametrize("refresh", [False, True])
async def test_tool_listing_keeps_refresh_and_trade_gates_independent(profile: str, refresh: bool) -> None:
    server = create_server(
        MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile=profile, allow_data_refresh=refresh),
        client=DiscoveryFake(),
    )
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert ("request_history" in names) is refresh
    assert ("refresh_fundamentals" in names) is refresh
    assert ("place_order" in names) is (profile in {"paper", "live"})
    assert ("create_gtt" in names) is (profile == "live")
