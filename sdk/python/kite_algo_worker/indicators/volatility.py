from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from .base import IndicatorInput, _sma_kernel, format_output, normalize_input
from .trend import TechnicalAnalysis as _TrendTechnicalAnalysis, _atr_kernel, _ema_kernel, _true_range_kernel


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


def _validate_multiplier(multiplier: float) -> float:
    multiplier = float(multiplier)
    if multiplier <= 0:
        raise ValueError("multiplier must be greater than zero")
    return multiplier


def _finite_segments(values: np.ndarray) -> Tuple[Tuple[int, int], ...]:
    segments = []
    start = None
    for idx, value in enumerate(values):
        if np.isfinite(value):
            if start is None:
                start = idx
        elif start is not None:
            segments.append((start, idx))
            start = None
    if start is not None:
        segments.append((start, values.size))
    return tuple(segments)


def _resolve_ohlc_inputs(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
) -> Tuple[IndicatorInput, IndicatorInput, IndicatorInput]:
    if low is not None and close is not None:
        high_input = normalize_input(high)
        low_input = normalize_input(low)
        close_input = normalize_input(close)
    elif low is not None or close is not None:
        raise ValueError("explicit OHLC inputs require high, low, and close together")
    elif isinstance(high, pd.DataFrame):
        high_input = normalize_input(high, column=high_column)
        low_input = normalize_input(high, column=low_column)
        close_input = normalize_input(high, column=close_column)
    else:
        raise TypeError("indicator requires either high/low/close inputs or a dataframe with OHLC columns")

    if high_input.index is not None and low_input.index is not None and not high_input.index.equals(low_input.index):
        raise ValueError("indicator inputs must share the same index")
    if high_input.index is not None and close_input.index is not None and not high_input.index.equals(close_input.index):
        raise ValueError("indicator inputs must share the same index")
    if not (high_input.values.shape == low_input.values.shape == close_input.values.shape):
        raise ValueError("indicator inputs must have matching shape")
    return high_input, low_input, close_input


def _bbands_kernel(values: np.ndarray, period: int, multiplier: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    period = _validate_period(period)
    multiplier = _validate_multiplier(multiplier)
    middle = _sma_kernel(values, period)
    upper = np.full(values.shape, np.nan, dtype=float)
    lower = np.full(values.shape, np.nan, dtype=float)

    if values.size < period:
        return upper, middle, lower

    for start, end in _finite_segments(values):
        segment = values[start:end]
        if segment.size < period:
            continue
        for idx in range(period - 1, segment.size):
            window = segment[idx - period + 1 : idx + 1]
            if np.isnan(window).any() or np.isnan(middle[start + idx]):
                continue
            stddev = float(window.std(ddof=0))
            center = float(middle[start + idx])
            upper[start + idx] = center + (multiplier * stddev)
            lower[start + idx] = center - (multiplier * stddev)
    return upper, middle, lower


def _keltner_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int, multiplier: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    period = _validate_period(period)
    multiplier = _validate_multiplier(multiplier)
    middle = _ema_kernel(close, period)
    atr = _atr_kernel(_true_range_kernel(high, low, close), period)
    upper = middle + (multiplier * atr)
    lower = middle - (multiplier * atr)
    return upper, middle, lower


def atr(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    period: int = 14,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
):
    high_input, low_input, close_input = _resolve_ohlc_inputs(
        high,
        low,
        close,
        high_column=high_column,
        low_column=low_column,
        close_column=close_column,
    )
    values = _atr_kernel(_true_range_kernel(high_input.values, low_input.values, close_input.values), period)
    return format_output(values, high_input, name="atr")


def bbands(data: Any, period: int = 20, multiplier: float = 2.0, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    upper, middle, lower = _bbands_kernel(normalized.values, period, multiplier)
    index = normalized.index if normalized.index is not None else None
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower}, index=index)


def keltner(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    period: int = 20,
    multiplier: float = 2.0,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
):
    high_input, low_input, close_input = _resolve_ohlc_inputs(
        high,
        low,
        close,
        high_column=high_column,
        low_column=low_column,
        close_column=close_column,
    )
    upper, middle, lower = _keltner_kernel(high_input.values, low_input.values, close_input.values, period, multiplier)
    index = high_input.index if high_input.index is not None else None
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower}, index=index)


class TechnicalAnalysis(_TrendTechnicalAnalysis):
    def atr(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        period: int = 14,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
    ):
        return atr(
            high,
            low,
            close,
            period=period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )

    def bbands(self, data: Any, period: int = 20, multiplier: float = 2.0, column: Optional[str] = None):
        return bbands(data, period=period, multiplier=multiplier, column=column)

    def keltner(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        period: int = 20,
        multiplier: float = 2.0,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
    ):
        return keltner(
            high,
            low,
            close,
            period=period,
            multiplier=multiplier,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )


__all__ = ["TechnicalAnalysis", "atr", "bbands", "keltner"]
