from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class IndicatorFake:
    """Backend double: the adapter forwards; the worker computes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def health(self):
        return {"allowed_actions": ["market:read"]}

    async def calculate_indicator(self, payload):
        self.calls.append(payload)
        count = len(payload["bars"])
        return {
            "name": payload["name"],
            "timestamps": [f"ts-{index}" for index in range(count)],
            "values": [float(index) for index in range(count)],
            "included_forming": payload["include_forming"],
            "ready": True,
            "warmup_rows": 3,
        }


def _bars(count: int = 80) -> list[dict[str, object]]:
    return [
        {"timestamp": str(index), "open": 100 + index * 0.5, "high": 101 + index * 0.5,
         "low": 99 + index * 0.5, "close": 100.2 + index * 0.5, "volume": 1000 + index}
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_all_allowlisted_indicator_names_forward_to_worker() -> None:
    names = [
        "sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
        "cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
        "vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
    ]
    fake = IndicatorFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        for name in names:
            result = await client.call_tool("calculate_indicator", {"request": {"name": name, "bars": _bars()}})
            assert result.is_error is False, name
            data = json.loads(result.content[0].text)["data"]
            assert data["name"] == name
            assert len(data["timestamps"]) == len(_bars())
            assert "warmup_rows" in data
            forwarded = fake.calls[-1]
            assert forwarded["name"] == name
            assert len(forwarded["bars"]) == len(_bars())
            assert forwarded["period"] == 14


@pytest.mark.asyncio
async def test_indicator_rejects_unknown_fields_and_excludes_forming_bar() -> None:
    fake = IndicatorFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=fake)
    async with Client(server) as client:
        with pytest.raises(Exception, match="Unknown|extra"):
            await client.call_tool("calculate_indicator", {"request": {"name": "sma", "bars": _bars(20), "expression": "x"}})
        bars = _bars(20)
        bars[-1]["is_complete"] = False
        result = await client.call_tool("calculate_indicator", {"request": {"name": "sma", "bars": bars, "period": 3}})
        data = json.loads(result.content[0].text)["data"]
        assert data["included_forming"] is False
        assert len(fake.calls[-1]["bars"]) == 19
        assert len(data["timestamps"]) == 19
