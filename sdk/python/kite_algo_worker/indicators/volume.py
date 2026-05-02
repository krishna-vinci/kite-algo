from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from .base import IndicatorInput, format_output, normalize_input
from .trend import TechnicalAnalysis as _TrendTechnicalAnalysis


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


def _finite_segments(mask: np.ndarray) -> Tuple[Tuple[int, int], ...]:
    segments = []
    start = None
    for idx, value in enumerate(mask):
        if bool(value):
            if start is None:
                start = idx
        elif start is not None:
            segments.append((start, idx))
            start = None
    if start is not None:
        segments.append((start, mask.size))
    return tuple(segments)


def _resolve_price_volume_inputs(
    price: Any,
    volume: Any | None = None,
    *,
    price_column: str = "close",
    volume_column: str = "volume",
) -> Tuple[IndicatorInput, IndicatorInput]:
    if volume is not None:
        price_input = normalize_input(price, column=price_column) if isinstance(price, pd.DataFrame) else normalize_input(price)
        volume_input = normalize_input(volume)
    elif isinstance(price, pd.DataFrame):
        price_input = normalize_input(price, column=price_column)
        volume_input = normalize_input(price, column=volume_column)
    else:
        raise TypeError("indicator requires a volume input unless data is a dataframe")

    if price_input.index is not None and volume_input.index is not None and not price_input.index.equals(volume_input.index):
        raise ValueError("indicator inputs must share the same index")
    if price_input.values.shape != volume_input.values.shape:
        raise ValueError("indicator inputs must have matching shape")
    return price_input, volume_input


def _resolve_ohlcv_inputs(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    volume: Any | None = None,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
    volume_column: str = "volume",
) -> Tuple[IndicatorInput, IndicatorInput, IndicatorInput, IndicatorInput]:
    if low is not None and close is not None and volume is not None:
        high_input = normalize_input(high)
        low_input = normalize_input(low)
        close_input = normalize_input(close)
        volume_input = normalize_input(volume)
    elif low is not None or close is not None or volume is not None:
        raise ValueError("explicit OHLCV inputs require high, low, close, and volume together")
    elif isinstance(high, pd.DataFrame):
        high_input = normalize_input(high, column=high_column)
        low_input = normalize_input(high, column=low_column)
        close_input = normalize_input(high, column=close_column)
        volume_input = normalize_input(high, column=volume_column)
    else:
        raise TypeError("indicator requires either OHLCV inputs or a dataframe with OHLCV columns")

    if high_input.index is not None and low_input.index is not None and not high_input.index.equals(low_input.index):
        raise ValueError("indicator inputs must share the same index")
    if high_input.index is not None and close_input.index is not None and not high_input.index.equals(close_input.index):
        raise ValueError("indicator inputs must share the same index")
    if high_input.index is not None and volume_input.index is not None and not high_input.index.equals(volume_input.index):
        raise ValueError("indicator inputs must share the same index")
    if not (high_input.values.shape == low_input.values.shape == close_input.values.shape == volume_input.values.shape):
        raise ValueError("indicator inputs must have matching shape")
    return high_input, low_input, close_input, volume_input


def _obv_kernel(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    if close.shape != volume.shape:
        raise ValueError("obv inputs must have matching shape")

    result = np.full(close.shape, np.nan, dtype=float)
    mask = np.isfinite(close) & np.isfinite(volume)
    for start, end in _finite_segments(mask):
        result[start] = 0.0
        for idx in range(start + 1, end):
            delta = close[idx] - close[idx - 1]
            if delta > 0:
                result[idx] = result[idx - 1] + volume[idx]
            elif delta < 0:
                result[idx] = result[idx - 1] - volume[idx]
            else:
                result[idx] = result[idx - 1]
    return result


def _vwap_kernel(price: np.ndarray, volume: np.ndarray) -> np.ndarray:
    if price.shape != volume.shape:
        raise ValueError("vwap inputs must have matching shape")

    result = np.full(price.shape, np.nan, dtype=float)
    mask = np.isfinite(price) & np.isfinite(volume)
    for start, end in _finite_segments(mask):
        numerator = 0.0
        denominator = 0.0
        for idx in range(start, end):
            numerator += float(price[idx] * volume[idx])
            denominator += float(volume[idx])
            if denominator != 0.0:
                result[idx] = numerator / denominator
    return result


def _mfi_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    if not (high.shape == low.shape == close.shape == volume.shape):
        raise ValueError("mfi inputs must have matching shape")

    result = np.full(close.shape, np.nan, dtype=float)
    mask = np.isfinite(high) & np.isfinite(low) & np.isfinite(close) & np.isfinite(volume)
    for start, end in _finite_segments(mask):
        segment_size = end - start
        if segment_size <= period:
            continue

        typical = (high[start:end] + low[start:end] + close[start:end]) / 3.0
        raw_money_flow = typical * volume[start:end]
        positive = np.zeros(segment_size, dtype=float)
        negative = np.zeros(segment_size, dtype=float)

        for idx in range(1, segment_size):
            if typical[idx] > typical[idx - 1]:
                positive[idx] = raw_money_flow[idx]
            elif typical[idx] < typical[idx - 1]:
                negative[idx] = raw_money_flow[idx]

        for idx in range(period, segment_size):
            pos_sum = float(positive[idx - period + 1 : idx + 1].sum())
            neg_sum = float(negative[idx - period + 1 : idx + 1].sum())
            if pos_sum == 0.0 and neg_sum == 0.0:
                result[start + idx] = 50.0
            elif neg_sum == 0.0:
                result[start + idx] = 100.0
            elif pos_sum == 0.0:
                result[start + idx] = 0.0
            else:
                money_ratio = pos_sum / neg_sum
                result[start + idx] = 100.0 - (100.0 / (1.0 + money_ratio))
    return result


def obv(price: Any, volume: Any | None = None, *, price_column: str = "close", volume_column: str = "volume"):
    price_input, volume_input = _resolve_price_volume_inputs(price, volume, price_column=price_column, volume_column=volume_column)
    values = _obv_kernel(price_input.values, volume_input.values)
    return format_output(values, price_input, name="obv")


def vwap(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    volume: Any | None = None,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
    volume_column: str = "volume",
):
    if not isinstance(high, pd.DataFrame) and volume is None and close is None and low is not None:
        volume = low
        low = None

    if isinstance(high, pd.DataFrame) and low is None and close is None:
        high_input = normalize_input(high, column=high_column)
        low_input = normalize_input(high, column=low_column)
        close_input = normalize_input(high, column=close_column)
        volume_input = normalize_input(volume) if volume is not None else normalize_input(high, column=volume_column)
        if high_input.index is not None and volume_input.index is not None and not high_input.index.equals(volume_input.index):
            raise ValueError("vwap inputs must share the same index")
        if not (high_input.values.shape == low_input.values.shape == close_input.values.shape == volume_input.values.shape):
            raise ValueError("vwap inputs must have matching shape")
        price_input = IndicatorInput(
            values=(high_input.values + low_input.values + close_input.values) / 3.0,
            index=high_input.index,
            kind=high_input.kind,
            name="typical_price",
        )
    elif volume is not None and low is None and close is None:
        price_input, volume_input = _resolve_price_volume_inputs(high, volume, price_column=close_column, volume_column=volume_column)
    else:
        high_input, low_input, close_input, volume_input = _resolve_ohlcv_inputs(
            high,
            low,
            close,
            volume,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
            volume_column=volume_column,
        )
        price_input = IndicatorInput(
            values=(high_input.values + low_input.values + close_input.values) / 3.0,
            index=high_input.index,
            kind=high_input.kind,
            name="typical_price",
        )

    if price_input.index is not None and volume_input.index is not None and not price_input.index.equals(volume_input.index):
        raise ValueError("vwap inputs must share the same index")
    values = _vwap_kernel(price_input.values, volume_input.values)
    return format_output(values, price_input, name="vwap")


def mfi(
    high: Any,
    low: Any | None = None,
    close: Any | None = None,
    volume: Any | None = None,
    period: int = 14,
    *,
    high_column: str = "high",
    low_column: str = "low",
    close_column: str = "close",
    volume_column: str = "volume",
):
    high_input, low_input, close_input, volume_input = _resolve_ohlcv_inputs(
        high,
        low,
        close,
        volume,
        high_column=high_column,
        low_column=low_column,
        close_column=close_column,
        volume_column=volume_column,
    )
    values = _mfi_kernel(high_input.values, low_input.values, close_input.values, volume_input.values, period)
    return format_output(values, high_input, name="mfi")


class TechnicalAnalysis(_TrendTechnicalAnalysis):
    def obv(self, price: Any, volume: Any | None = None, *, price_column: str = "close", volume_column: str = "volume"):
        return obv(price, volume, price_column=price_column, volume_column=volume_column)

    def vwap(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        volume: Any | None = None,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
        volume_column: str = "volume",
    ):
        return vwap(
            high,
            low,
            close,
            volume,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
            volume_column=volume_column,
        )

    def mfi(
        self,
        high: Any,
        low: Any | None = None,
        close: Any | None = None,
        volume: Any | None = None,
        period: int = 14,
        *,
        high_column: str = "high",
        low_column: str = "low",
        close_column: str = "close",
        volume_column: str = "volume",
    ):
        return mfi(
            high,
            low,
            close,
            volume,
            period=period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
            volume_column=volume_column,
        )


__all__ = ["TechnicalAnalysis", "mfi", "obv", "vwap"]
