from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class IndicatorFake:
    async def health(self):
        return {"allowed_actions": ["market:read"]}


def _bars(count: int = 80) -> list[dict[str, object]]:
    return [
        {"timestamp": str(index), "open": 100 + index * 0.5, "high": 101 + index * 0.5,
         "low": 99 + index * 0.5, "close": 100.2 + index * 0.5, "volume": 1000 + index}
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_all_allowlisted_indicator_names_have_bounded_aligned_output() -> None:
    names = [
        "sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
        "cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
        "vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
    ]
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=IndicatorFake())
    async with Client(server) as client:
        for name in names:
            result = await client.call_tool("calculate_indicator", {"request": {"name": name, "bars": _bars()}})
            assert result.is_error is False, name
            data = json.loads(result.content[0].text)["data"]
            assert data["name"] == name
            assert len(data["timestamps"]) == len(_bars())
            assert "warmup_rows" in data


@pytest.mark.asyncio
async def test_indicator_rejects_unknown_fields_and_excludes_forming_bar() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=IndicatorFake())
    async with Client(server) as client:
        with pytest.raises(Exception, match="Unknown|extra"):
            await client.call_tool("calculate_indicator", {"request": {"name": "sma", "bars": _bars(20), "expression": "x"}})
        bars = _bars(20)
        bars[-1]["is_complete"] = False
        result = await client.call_tool("calculate_indicator", {"request": {"name": "sma", "bars": bars, "period": 3}})
        data = json.loads(result.content[0].text)["data"]
        assert data["included_forming"] is False
        assert len(data["timestamps"]) == 19
