"""Server-side indicator computation for worker tokens.

Single source of numerical truth: the SDK's ``TechnicalAnalysis``.  This
service exists so MCP adapters and other thin clients never need pandas
themselves — they forward candles and receive computed series.
"""
from __future__ import annotations

from typing import Any

import math

import numpy as np
import pandas as pd

from kite_algo_worker.indicators import TechnicalAnalysis

from backend.api.schemas.worker_indicators import WorkerIndicatorRequest


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


def _jsonable(value: Any) -> Any:
    """Convert pandas outputs into JSON types the way the adapter did.

    Numpy scalars become Python numbers and non-finite floats (NaN/inf from
    warmup windows) become null, matching the adapter's serialization rules.
    """
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def compute_indicator(payload: WorkerIndicatorRequest) -> dict[str, Any]:
    frame = pd.DataFrame([bar.model_dump(mode="python") for bar in payload.bars])
    timestamps = frame["timestamp"].tolist() if "timestamp" in frame else [None] * len(frame)
    ta = TechnicalAnalysis()
    name = payload.name
    if name == "sma":
        value = ta.sma(frame, payload.period, column="close")
    elif name == "ema":
        value = ta.ema(frame, payload.period, column="close")
    elif name == "wma":
        value = ta.wma(frame, payload.period, column="close")
    elif name == "vwma":
        value = ta.vwma(frame, period=payload.period, column="close", volume_column="volume")
    elif name == "supertrend":
        value = ta.supertrend(frame, period=payload.period, multiplier=payload.multiplier)
    elif name == "rsi":
        value = ta.rsi(frame, payload.period, column="close")
    elif name == "macd":
        value = ta.macd(frame, payload.fast_period, payload.slow_period, payload.signal_period, column="close")
    elif name == "ppo":
        value = ta.ppo(frame, payload.fast_period, payload.slow_period, payload.signal_period, column="close")
    elif name == "dpo":
        value = ta.dpo(frame, payload.period, column="close")
    elif name == "stochastic":
        value = ta.stochastic(frame, k_period=payload.period)
    elif name == "cci":
        value = ta.cci(frame, period=payload.period)
    elif name == "williams_r":
        value = ta.williamsr(frame, period=payload.period)
    elif name == "linreg":
        value = ta.linreg(frame, payload.period, column="close")
    elif name == "atr":
        value = ta.atr(frame, period=payload.period)
    elif name == "bbands":
        value = ta.bbands(frame, payload.period, payload.multiplier, column="close")
    elif name == "keltner":
        value = ta.keltner(frame, period=payload.period, multiplier=payload.multiplier)
    elif name == "adx":
        value = ta.adx(frame, period=payload.period)
    elif name == "aroon":
        value = ta.aroon(frame, period=payload.period)
    elif name == "sar":
        value = ta.sar(frame)
    elif name == "obv":
        value = ta.obv(frame, price_column="close", volume_column="volume")
    elif name == "vwap":
        value = ta.vwap(frame)
    elif name == "mfi":
        value = ta.mfi(frame, period=payload.period)
    elif name in {"crossover", "crossunder"}:
        if frame["open"].isna().all():
            raise ValueError(f"{name} requires open values as the second aligned series")
        value = getattr(ta, name)(frame["close"], frame["open"])
    elif name in {"highest", "lowest", "rising", "falling"}:
        value = getattr(ta, name)(frame, payload.period, column="close")
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
        "values": _jsonable(values),
        "included_forming": payload.include_forming,
        "ready": _ready(values),
        "warmup_rows": _warmup_rows(values),
    }
