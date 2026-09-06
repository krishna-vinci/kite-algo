from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class MarketFake:
    async def health(self):
        return {"allowed_actions": ["market:read"]}

    async def get_quotes(self, instruments, mode="quote"):
        return {"quotes": [{"symbol": instruments[0], "last_price": 10, "depth": {"buy": [{"price": 9, "quantity": 2, "orders": 1}], "sell": []}}]}

    async def get_candles(self, instrument, interval, lookback):
        return {"instrument": instrument, "candles": [{"close": 10}] * lookback}

    async def get_historical_candles(self, *args, **kwargs):
        return {"ingest": kwargs["ingest"], "candles": [{"close": 10}]}

    async def get_market_snapshot(self, **kwargs):
        return {"symbols": kwargs["symbols"], "snapshot": True}

    async def search_tickers(self, *args, **kwargs):
        return {"items": []}

    async def resolve_tickers(self, values):
        return {"items": values}

    async def get_market_calendar(self, *args, **kwargs):
        return {"sessions": []}

    async def get_market_calendar_status(self, **kwargs):
        return {"available": True}

    async def get_index_constituents(self, *args, **kwargs):
        return {"members": [{"symbol": "ABC", "sector": "Financial Services"}, {"symbol": "DEF", "sector": None}]}

    async def get_index_constituent_status(self, *args, **kwargs):
        return {"fresh": True}


@pytest.mark.asyncio
async def test_depth_preserves_real_levels_and_history_is_ingestion_free_by_default() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=MarketFake())
    async with Client(server) as client:
        depth = await client.call_tool("get_market_depth", {"request": {"symbols": ["ABC"]}})
        data = json.loads(depth.content[0].text)["data"]
        assert data["available"] is True
        assert data["depth"][0]["buy"][0]["price"] == 9
        history = await client.call_tool("get_historical_candles", {"request": {"instrument": "ABC"}})
        assert json.loads(history.content[0].text)["data"]["ingest"] is False


@pytest.mark.asyncio
async def test_index_sector_null_handling_is_preserved() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=MarketFake())
    async with Client(server) as client:
        result = await client.call_tool("get_index_constituents", {"request": {"source_list": "nifty50"}})
        members = json.loads(result.content[0].text)["data"]["members"]
        assert members[0]["sector"] == "Financial Services"
        assert members[1]["sector"] is None
