from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from .base import IndicatorInput, format_output, normalize_input, _sma_kernel
from .trend import TechnicalAnalysis as _TrendTechnicalAnalysis, _ema_kernel


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


def _rolling_extreme(values: np.ndarray, period: int, *, func) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return result

    for idx in range(period - 1, values.size):
        window = values[idx - period + 1 : idx + 1]
        if np.isnan(window).any():
            continue
        result[idx] = float(func(window))
    return result


def _format_frame(template: IndicatorInput, **columns: np.ndarray) -> pd.DataFrame:
    index = template.index if template.index is not None else None
    return pd.DataFrame(columns, index=index)


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


def _rsi_kernel(values: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(values.shape, np.nan, dtype=float)

    for start, end in _finite_segments(values):
        segment = values[start:end]
        if segment.size <= period:
            continue

        deltas = np.diff(segment)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        if gains.size < period:
            continue

        avg_gain = float(gains[:period].mean())
        avg_loss = float(losses[:period].mean())

        def _rsi_from_avgs(gain: float, loss: float) -> float:
            if loss == 0.0:
                return 100.0 if gain > 0.0 else 50.0
            rs = gain / loss
            return 100.0 - (100.0 / (1.0 + rs))

        result[start + period] = _rsi_from_avgs(avg_gain, avg_loss)

        for offset in range(period, gains.size):
            gain = float(gains[offset])
            loss = float(losses[offset])
            avg_gain = ((avg_gain * (period - 1)) + gain) / float(period)
            avg_loss = ((avg_loss * (period - 1)) + loss) / float(period)
            result[start + offset + 1] = _rsi_from_avgs(avg_gain, avg_loss)

    return result


def _cci_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    if not (high.shape == low.shape == close.shape):
        raise ValueError("cci inputs must have matching shape")

    typical_price = (high + low + close) / 3.0
    sma = _sma_kernel(typical_price, period)
    result = np.full(typical_price.shape, np.nan, dtype=float)

    if typical_price.size < period:
        return result

    for idx in range(period - 1, typical_price.size):
        window = typical_price[idx - period + 1 : idx + 1]
        if np.isnan(window).any() or np.isnan(sma[idx]):
            continue
        mean_deviation = float(np.mean(np.abs(window - sma[idx])))
        if mean_deviation == 0.0:
            result[idx] = 0.0
        else:
            result[idx] = (typical_price[idx] - sma[idx]) / (0.015 * mean_deviation)
    return result


def _williams_r_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    if not (high.shape == low.shape == close.shape):
        raise ValueError("williamsr inputs must have matching shape")

    highest_high = _rolling_extreme(high, period, func=np.max)
    lowest_low = _rolling_extreme(low, period, func=np.min)
    result = np.full(close.shape, np.nan, dtype=float)

    for idx in range(period - 1, close.size):
        if np.isnan(highest_high[idx]) or np.isnan(lowest_low[idx]) or np.isnan(close[idx]):
            continue
        denominator = highest_high[idx] - lowest_low[idx]
        if denominator == 0.0:
            result[idx] = 0.0
        else:
            result[idx] = -100.0 * (highest_high[idx] - close[idx]) / denominator
    return result


def _stochastic_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, k_period: int, d_period: int) -> Tuple[np.ndarray, np.ndarray]:
    k_period = _validate_period(k_period)
    d_period = _validate_period(d_period)
    if not (high.shape == low.shape == close.shape):
        raise ValueError("stochastic inputs must have matching shape")

    highest_high = _rolling_extreme(high, k_period, func=np.max)
    lowest_low = _rolling_extreme(low, k_period, func=np.min)
    raw_k = np.full(close.shape, np.nan, dtype=float)

    for idx in range(k_period - 1, close.size):
        if np.isnan(highest_high[idx]) or np.isnan(lowest_low[idx]) or np.isnan(close[idx]):
            continue
        denominator = highest_high[idx] - lowest_low[idx]
        if denominator == 0.0:
            raw_k[idx] = 0.0
        else:
            raw_k[idx] = 100.0 * (close[idx] - lowest_low[idx]) / denominator

    signal_d = _sma_kernel(raw_k, d_period)
    return raw_k, signal_d


def rsi(data: Any, period: int = 14, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _rsi_kernel(normalized.values, period)
    return format_output(values, normalized, name="rsi")


def macd(data: Any, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    fast_period = _validate_period(fast_period)
    slow_period = _validate_period(slow_period)
    signal_period = _validate_period(signal_period)

    fast_ema = _ema_kernel(normalized.values, fast_period)
    slow_ema = _ema_kernel(normalized.values, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = _ema_kernel(macd_line, signal_period)
    histogram = macd_line - signal_line
    return _format_frame(normalized, macd=macd_line, signal=signal_line, histogram=histogram)


def stochastic(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    k_period: int = 14,
    d_period: int = 3,
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
    raw_k, signal_d = _stochastic_kernel(high_input.values, low_input.values, close_input.values, k_period, d_period)
    return _format_frame(high_input, stoch_k=raw_k, stoch_d=signal_d)


def cci(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    period: int = 20,
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
    values = _cci_kernel(high_input.values, low_input.values, close_input.values, period)
    return format_output(values, high_input, name="cci")


def williamsr(
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
    values = _williams_r_kernel(high_input.values, low_input.values, close_input.values, period)
    return format_output(values, high_input, name="williamsr")


williams_r = williamsr


class TechnicalAnalysis(_TrendTechnicalAnalysis):
    def rsi(self, data: Any, period: int = 14, column: Optional[str] = None):
        return rsi(data, period=period, column=column)

    def macd(
        self,
        data: Any,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        column: Optional[str] = None,
    ):
        return macd(data, fast_period=fast_period, slow_period=slow_period, signal_period=signal_period, column=column)

    def stochastic(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        k_period: int = 14,
        d_period: int = 3,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
    ):
        return stochastic(
            high,
            low,
            close,
            k_period=k_period,
            d_period=d_period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )

    def cci(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        period: int = 20,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
    ):
        return cci(
            high,
            low,
            close,
            period=period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )

    def williamsr(
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
        return williamsr(
            high,
            low,
            close,
            period=period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )


__all__ = [
    "TechnicalAnalysis",
    "cci",
    "macd",
    "rsi",
    "stochastic",
    "williams_r",
    "williamsr",
]
