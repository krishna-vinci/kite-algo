from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..contracts import IndicatorRequest
from ..server import MCPRuntime
from .common import args_model, register_tool


def _indicator_result(request: IndicatorRequest) -> dict[str, Any]:
    # Imports are local so the MCP package can still expose read-only tools
    # with a clear dependency error when optional numerical dependencies are
    # unavailable.
    import pandas as pd
    from kite_algo_worker.indicators import TechnicalAnalysis

    frame = pd.DataFrame([bar.model_dump(mode="python") for bar in request.bars])
    timestamps = frame["timestamp"].tolist() if "timestamp" in frame else [None] * len(frame)
    ta = TechnicalAnalysis()
    name = request.name
    if name == "sma":
        value = ta.sma(frame, request.period, column="close")
    elif name == "ema":
        value = ta.ema(frame, request.period, column="close")
    elif name == "wma":
        value = ta.wma(frame, request.period, column="close")
    elif name == "vwma":
        value = ta.vwma(frame, period=request.period, column="close", volume_column="volume")
    elif name == "supertrend":
        value = ta.supertrend(frame, period=request.period, multiplier=request.multiplier)
    elif name == "rsi":
        value = ta.rsi(frame, request.period, column="close")
    elif name == "macd":
        value = ta.macd(frame, request.fast_period, request.slow_period, request.signal_period, column="close")
    elif name == "ppo":
        value = ta.ppo(frame, request.fast_period, request.slow_period, request.signal_period, column="close")
    elif name == "dpo":
        value = ta.dpo(frame, request.period, column="close")
    elif name == "stochastic":
        value = ta.stochastic(frame, k_period=request.period)
    elif name == "cci":
        value = ta.cci(frame, period=request.period)
    elif name == "williams_r":
        value = ta.williamsr(frame, period=request.period)
    elif name == "linreg":
        value = ta.linreg(frame, request.period, column="close")
    elif name == "atr":
        value = ta.atr(frame, period=request.period)
    elif name == "bbands":
        value = ta.bbands(frame, request.period, request.multiplier, column="close")
    elif name == "keltner":
        value = ta.keltner(frame, period=request.period, multiplier=request.multiplier)
    elif name == "adx":
        value = ta.adx(frame, period=request.period)
    elif name == "aroon":
        value = ta.aroon(frame, period=request.period)
    elif name == "sar":
        value = ta.sar(frame)
    elif name == "obv":
        value = ta.obv(frame, price_column="close", volume_column="volume")
    elif name == "vwap":
        value = ta.vwap(frame)
    elif name == "mfi":
        value = ta.mfi(frame, period=request.period)
    elif name in {"crossover", "crossunder"}:
        if frame["open"].isna().all():
            raise ValueError(f"{name} requires open values as the second aligned series")
        value = getattr(ta, name)(frame["close"], frame["open"])
    elif name in {"highest", "lowest", "rising", "falling"}:
        value = getattr(ta, name)(frame, request.period, column="close")
    else:  # pragma: no cover - Literal validation makes this unreachable
        raise ValueError(f"unsupported indicator {name}")

    if hasattr(value, "to_dict") and hasattr(value, "columns"):
        values: Any = value.to_dict(orient="list")
    elif hasattr(value, "tolist"):
        values = value.tolist()
    else:
        values = value
    return {
        "name": name,
        "timestamps": timestamps,
        "values": values,
        "included_forming": request.include_forming,
        "ready": _ready(values),
        "warmup_rows": _warmup_rows(values),
    }


def _ready(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_ready(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and value[-1] is not None and not (isinstance(value[-1], float) and value[-1] != value[-1])
    return value is not None


def _warmup_rows(value: Any) -> int:
    if isinstance(value, dict):
        return max((_warmup_rows(item) for item in value.values()), default=0)
    if not isinstance(value, list):
        return 0
    count = 0
    for item in value:
        if item is None or (isinstance(item, float) and item != item):
            count += 1
        else:
            break
    return count


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def calculate_indicator(request: IndicatorRequest) -> Any:
        values = args_model(request)
        async def operation(_lease: Any) -> Any:
            return _indicator_result(request)
        return await runtime.invoke("calculate_indicator", values, operation)

    register_tool(server, runtime, "calculate_indicator", calculate_indicator)
