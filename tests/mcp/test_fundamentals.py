from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class FundamentalsFake:
    async def health(self):
        return {"allowed_actions": ["fundamentals:read", "fundamentals:write"]}

    async def get_fundamentals_features(self, **kwargs):
        return {"symbols": kwargs["symbols"], "rows": [{"symbol": "ABC", "pe": None}]}

    async def get_fundamentals_statements(self, *args, **kwargs):
        return {"symbol": args[0], "rows": []}

    async def get_fundamentals_status(self, **kwargs):
        return {"symbols": kwargs["symbols"], "fresh": False}

    async def refresh_fundamentals(self, **kwargs):
        return {"job_id": "sync-1", "symbols": kwargs["symbols"]}


@pytest.mark.asyncio
async def test_fundamental_scope_is_explicit_and_missing_values_remain_null() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=FundamentalsFake())
    async with Client(server) as client:
        result = await client.call_tool("get_fundamentals_features", {"request": {"symbols": ["abc"]}})
        data = json.loads(result.content[0].text)["data"]
        assert data["symbols"] == ["ABC"]
        assert data["rows"][0]["pe"] is None
        with pytest.raises(Exception, match="Unknown tool"):
            await client.call_tool("refresh_fundamentals", {"request": {"symbols": ["ABC"]}})


@pytest.mark.asyncio
async def test_refresh_requires_opt_in_profile() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper", allow_data_refresh=True), client=FundamentalsFake())
    async with Client(server) as client:
        assert any(tool.name == "refresh_fundamentals" for tool in await client.list_tools())
