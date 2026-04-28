from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from .base import IndicatorInput, TechnicalAnalysis as _BaseTechnicalAnalysis, format_output, normalize_input
from .utils import TechnicalAnalysis as _UtilsTechnicalAnalysis


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


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


def _ema_kernel(values: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(values.shape, np.nan, dtype=float)
    alpha = 2.0 / (float(period) + 1.0)
    for start, end in _finite_segments(values):
        segment = values[start:end]
        if segment.size < period:
            continue
        seed = float(segment[:period].mean())
        seed_index = start + period - 1
        result[seed_index] = seed
        previous = seed
        for idx in range(period, segment.size):
            price = float(segment[idx])
            previous = (price - previous) * alpha + previous
            result[start + idx] = previous
    return result


def _wma_kernel(values: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return result
    weights = np.arange(1, period + 1, dtype=float)
    denom = float(weights.sum())
    for idx in range(period - 1, values.size):
        window = values[idx - period + 1 : idx + 1]
        if np.isnan(window).any():
            continue
        result[idx] = float(np.dot(window, weights) / denom)
    return result


def _vwma_kernel(price: np.ndarray, volume: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    if price.shape != volume.shape:
        raise ValueError("vwma inputs must have matching shape")
    result = np.full(price.shape, np.nan, dtype=float)
    if price.size < period:
        return result
    for idx in range(period - 1, price.size):
        price_window = price[idx - period + 1 : idx + 1]
        volume_window = volume[idx - period + 1 : idx + 1]
        if np.isnan(price_window).any() or np.isnan(volume_window).any():
            continue
        volume_sum = float(volume_window.sum())
        if volume_sum == 0.0:
            continue
        result[idx] = float(np.dot(price_window, volume_window) / volume_sum)
    return result


def _true_range_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    if not (high.shape == low.shape == close.shape):
        raise ValueError("supertrend inputs must have matching shape")
    result = np.full(high.shape, np.nan, dtype=float)
    if high.size == 0:
        return result
    result[0] = float(high[0] - low[0]) if np.isfinite(high[0]) and np.isfinite(low[0]) else np.nan
    for idx in range(1, high.size):
        h = high[idx]
        l = low[idx]
        c_prev = close[idx - 1]
        if np.isnan(h) or np.isnan(l) or np.isnan(c_prev):
            continue
        tr = max(h - l, abs(h - c_prev), abs(c_prev - l))
        result[idx] = float(tr)
    return result


def _atr_kernel(true_range: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(true_range.shape, np.nan, dtype=float)
    for start, end in _finite_segments(true_range):
        segment = true_range[start:end]
        if segment.size < period:
            continue
        seed = float(segment[:period].mean())
        seed_index = start + period - 1
        result[seed_index] = seed
        previous = seed
        for idx in range(period, segment.size):
            current = float(segment[idx])
            previous = ((previous * (period - 1)) + current) / float(period)
            result[start + idx] = previous
    return result


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
        raise TypeError("supertrend requires either high/low/close inputs or a dataframe with OHLC columns")

    if high_input.index is not None and low_input.index is not None and not high_input.index.equals(low_input.index):
        raise ValueError("supertrend inputs must share the same index")
    if high_input.index is not None and close_input.index is not None and not high_input.index.equals(close_input.index):
        raise ValueError("supertrend inputs must share the same index")
    if not (high_input.values.shape == low_input.values.shape == close_input.values.shape):
        raise ValueError("supertrend inputs must have matching shape")
    return high_input, low_input, close_input


def _supertrend_frame(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int,
    multiplier: float,
    index: Optional[pd.Index] = None,
) -> pd.DataFrame:
    period = _validate_period(period)
    multiplier = float(multiplier)
    if multiplier <= 0:
        raise ValueError("multiplier must be greater than zero")

    hl2 = (high + low) / 2.0
    true_range = _true_range_kernel(high, low, close)
    atr = _atr_kernel(true_range, period)
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    direction = np.full(close.shape, np.nan, dtype=float)
    trend = np.full(close.shape, np.nan, dtype=float)
    long_band = np.full(close.shape, np.nan, dtype=float)
    short_band = np.full(close.shape, np.nan, dtype=float)

    first_ready = None
    for idx, value in enumerate(atr):
        if np.isfinite(value):
            first_ready = idx
            break
    if first_ready is None:
        return pd.DataFrame({"supertrend": trend, "direction": direction, "long": long_band, "short": short_band}, index=index)

    direction[first_ready] = 1.0
    trend[first_ready] = lowerband[first_ready]
    long_band[first_ready] = lowerband[first_ready]

    final_upper = upperband.copy()
    final_lower = lowerband.copy()

    for idx in range(first_ready + 1, close.size):
        if np.isnan(final_upper[idx - 1]) or np.isnan(final_lower[idx - 1]):
            continue

        if np.isnan(close[idx]):
            direction[idx] = direction[idx - 1]
            final_upper[idx] = final_upper[idx - 1]
            final_lower[idx] = final_lower[idx - 1]
            if direction[idx] > 0:
                trend[idx] = final_lower[idx]
                long_band[idx] = final_lower[idx]
            elif direction[idx] < 0:
                trend[idx] = final_upper[idx]
                short_band[idx] = final_upper[idx]
            continue

        if close[idx] > final_upper[idx - 1]:
            direction[idx] = 1.0
        elif close[idx] < final_lower[idx - 1]:
            direction[idx] = -1.0
        else:
            direction[idx] = direction[idx - 1]
            if direction[idx] > 0 and np.isfinite(final_lower[idx]) and final_lower[idx] < final_lower[idx - 1]:
                final_lower[idx] = final_lower[idx - 1]
            if direction[idx] < 0 and np.isfinite(final_upper[idx]) and final_upper[idx] > final_upper[idx - 1]:
                final_upper[idx] = final_upper[idx - 1]

        if direction[idx] > 0:
            trend[idx] = final_lower[idx]
            long_band[idx] = final_lower[idx]
        else:
            trend[idx] = final_upper[idx]
            short_band[idx] = final_upper[idx]

    return pd.DataFrame(
        {
            "supertrend": trend,
            "direction": direction,
            "long": long_band,
            "short": short_band,
        },
        index=index,
    )


def sma(data: Any, period: int, column: Optional[str] = None):
    return _BaseTechnicalAnalysis().sma(data, period, column=column)


def ema(data: Any, period: int, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _ema_kernel(normalized.values, period)
    return format_output(values, normalized, name="ema")


def wma(data: Any, period: int, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _wma_kernel(normalized.values, period)
    return format_output(values, normalized, name="wma")


def vwma(data: Any, volume: Any | None = None, period: int = 14, *, column: Optional[str] = None, volume_column: Optional[str] = None):
    price_input = normalize_input(data, column=column)
    if volume is None:
        if not isinstance(data, pd.DataFrame):
            raise TypeError("vwma requires a volume input unless data is a dataframe")
        if volume_column is None:
            raise ValueError("volume_column must be provided when volume is omitted")
        volume_input = normalize_input(data, column=volume_column)
    else:
        volume_input = normalize_input(volume)
    if price_input.index is not None and volume_input.index is not None and not price_input.index.equals(volume_input.index):
        raise ValueError("vwma inputs must share the same index")
    values = _vwma_kernel(price_input.values, volume_input.values, period)
    return format_output(values, price_input, name="vwma")


def supertrend(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    period: int = 10,
    multiplier: float = 3.0,
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
    return _supertrend_frame(high_input.values, low_input.values, close_input.values, period, multiplier, index=high_input.index)


class TechnicalAnalysis(_UtilsTechnicalAnalysis):
    def sma(self, data: Any, period: int, column: Optional[str] = None):
        return sma(data, period, column=column)

    def ema(self, data: Any, period: int, column: Optional[str] = None):
        return ema(data, period, column=column)

    def wma(self, data: Any, period: int, column: Optional[str] = None):
        return wma(data, period, column=column)

    def vwma(self, data: Any, volume: Any | None = None, period: int = 14, *, column: Optional[str] = None, volume_column: Optional[str] = None):
        return vwma(data, volume, period=period, column=column, volume_column=volume_column)

    def supertrend(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        period: int = 10,
        multiplier: float = 3.0,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
    ):
        return supertrend(
            high,
            low,
            close,
            period=period,
            multiplier=multiplier,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )


__all__ = [
    "TechnicalAnalysis",
    "ema",
    "sma",
    "supertrend",
    "vwma",
    "wma",
]
