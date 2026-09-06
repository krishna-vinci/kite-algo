from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import ConfigurationError, MCPConfig, load_config
from kite_algo_mcp.server import create_server


class FakeClient:
    def __init__(self, *, actions=None):
        self.actions = actions or {"health:read", "market:read", "runs:read", "funds:read"}
        self.closed = False

    async def health(self):
        return {"status": "ok", "allowed_actions": sorted(self.actions), "worker_token": "must-not-leak"}

    async def search_tickers(self, query, exchange=None, limit=20):
        return {"items": [{"symbol": query.upper(), "exchange": exchange or "NSE"}], "limit": limit}

    async def close(self):
        self.closed = True


def test_config_rejects_missing_and_non_loopback_http() -> None:
    with pytest.raises(ConfigurationError):
        load_config({})
    with pytest.raises(ConfigurationError):
        MCPConfig(api_url="http://example.com", worker_token="x")
    with pytest.raises(ConfigurationError):
        MCPConfig(api_url="https://user:pass@example.com", worker_token="x")


@pytest.mark.asyncio
async def test_in_process_protocol_lists_tools_and_returns_result() -> None:
    fake = FakeClient()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert "search_instruments" in names
        assert "place_order" not in names
        result = await client.call_tool("search_instruments", {"request": {"query": "reliance"}})
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "ok"
        assert payload["data"]["items"][0]["symbol"] == "RELIANCE"
        assert "secret" not in result.content[0].text


@pytest.mark.asyncio
async def test_policy_denies_direct_dispatch_when_backend_revokes_action() -> None:
    fake = FakeClient(actions={"health:read"})
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        with pytest.raises(Exception, match="backend_action_denied"):
            await client.call_tool("search_instruments", {"request": {"query": "reliance"}})


def test_server_construction_does_not_start_transport() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=FakeClient())
    assert server is not None
