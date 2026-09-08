from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class MarketFake:
    def __init__(self) -> None:
        self.history_calls: list[dict[str, object]] = []
        self.quote_calls: list[dict[str, object]] = []
        self.snapshot_calls: list[dict[str, object]] = []

    async def health(self):
        return {"allowed_actions": ["market:read"], "allowed_modes": ["paper"]}

    async def get_quotes(self, instruments, mode="quote"):
        self.quote_calls.append({"instruments": instruments, "mode": mode})
        return {"quotes": [{"symbol": instruments[0], "last_price": 10, "depth": {"buy": [{"price": 9, "quantity": 2, "orders": 1}], "sell": []}}]}

    async def get_candles(self, instrument, interval, lookback):
        return {"instrument": instrument, "candles": [{"close": 10}] * lookback}

    async def get_historical_candles(self, *args, **kwargs):
        self.history_calls.append(dict(kwargs))
        return {"ingest": kwargs["ingest"], "candles": [{"close": 10}]}

    async def get_market_snapshot(self, **kwargs):
        self.snapshot_calls.append(dict(kwargs))
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
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        depth = await client.call_tool("get_market_depth", {"request": {"symbols": ["ABC"]}})
        data = json.loads(depth.content[0].text)["data"]
        assert data["available"] is True
        assert data["depth"][0]["buy"][0]["price"] == 9
        history = await client.call_tool("get_historical_candles", {"request": {"instrument": "ABC"}})
        assert json.loads(history.content[0].text)["data"]["ingest"] is False
    assert fake.history_calls[-1]["ingest"] is False
    assert fake.history_calls[-1]["passthrough"] is False


@pytest.mark.asyncio
async def test_historical_date_bounds_are_timezone_aware_and_passthrough_is_explicit() -> None:
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        result = await client.call_tool("get_historical_candles", {"request": {"instrument": "ABC", "from_date": "2026-09-01", "to_date": "2026-09-02", "passthrough": True}})
        assert json.loads(result.content[0].text)["status"] == "ok"
    call = fake.history_calls[-1]
    assert call["passthrough"] is True
    assert call["ingest"] is False
    assert call["from_date"] == datetime.fromisoformat("2026-09-01T00:00:00+05:30")
    assert call["to_date"] == datetime.fromisoformat("2026-09-02T23:59:59.999999+05:30")


@pytest.mark.asyncio
async def test_historical_offset_bounds_are_preserved() -> None:
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        result = await client.call_tool("get_historical_candles", {"request": {"instrument": "ABC", "from_date": "2026-09-01T00:15:00+04:00", "to_date": "2026-09-02T18:00:00+04:00"}})
        assert json.loads(result.content[0].text)["status"] == "ok"
    call = fake.history_calls[-1]
    assert call["from_date"] == datetime.fromisoformat("2026-09-01T00:15:00+04:00")
    assert call["to_date"] == datetime.fromisoformat("2026-09-02T18:00:00+04:00")


@pytest.mark.asyncio
async def test_quote_modes_do_not_use_execution_mode_policy_argument() -> None:
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        for mode in ("ltp", "quote", "full"):
            result = await client.call_tool("get_quotes", {"request": {"symbols": ["ABC"]}, "mode": mode})
            assert json.loads(result.content[0].text)["status"] == "ok"
            result = await client.call_tool("get_market_snapshot", {"request": {"symbols": ["ABC"]}, "mode": mode})
            assert json.loads(result.content[0].text)["status"] == "ok"
    assert [item["mode"] for item in fake.quote_calls] == ["ltp", "quote", "full"]
    assert [item["mode"] for item in fake.snapshot_calls] == ["ltp", "quote", "full"]


@pytest.mark.asyncio
async def test_request_history_is_explicit_ingestion_and_rejects_passthrough() -> None:
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper", allow_data_refresh=True), client=fake)
    async with Client(server) as client:
        result = await client.call_tool("request_history", {"request": {"instrument": "ABC"}})
        assert json.loads(result.content[0].text)["status"] == "ok"
        assert fake.history_calls[-1]["ingest"] is True
        assert fake.history_calls[-1]["passthrough"] is False
        with pytest.raises(Exception, match="passthrough"):
            await client.call_tool("request_history", {"request": {"instrument": "ABC", "passthrough": True}})
    assert len(fake.history_calls) == 1


@pytest.mark.asyncio
async def test_live_execution_mode_still_denied_for_paper_only_worker() -> None:
    fake = MarketFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=fake)
    async with Client(server) as client:
        with pytest.raises(Exception, match="live"):
            await client.call_tool("create_run", {"request": {"template_id": "t", "account_scope": "a", "execution_mode": "live"}})


def test_history_contract_rejects_naive_inverted_and_conflicting_ranges() -> None:
    from kite_algo_mcp.contracts import HistoricalCandleRequest

    with pytest.raises(ValueError, match="timezone"):
        HistoricalCandleRequest.model_validate({"instrument": "ABC", "from_date": "2026-09-01T00:00:00"})
    with pytest.raises(ValueError, match="not be after"):
        HistoricalCandleRequest.model_validate({"instrument": "ABC", "from_date": "2026-09-02", "to_date": "2026-09-01"})
    with pytest.raises(ValueError, match="lookback_days"):
        HistoricalCandleRequest.model_validate({"instrument": "ABC", "from_date": "2026-09-01", "lookback_days": 2})
    with pytest.raises(ValueError, match="extra"):
        HistoricalCandleRequest.model_validate({"instrument": "ABC", "request_history": True})


@pytest.mark.asyncio
async def test_index_sector_null_handling_is_preserved() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=MarketFake())
    async with Client(server) as client:
        result = await client.call_tool("get_index_constituents", {"request": {"source_list": "nifty50"}})
        members = json.loads(result.content[0].text)["data"]["members"]
        assert members[0]["sector"] == "Financial Services"
        assert members[1]["sector"] is None
