from __future__ import annotations

from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from .base import IndicatorInput, normalize_input
from .statistics import TechnicalAnalysis as _StatisticsTechnicalAnalysis
from .trend import _atr_kernel


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


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


def _resolve_hl_inputs(
    high: Any,
    low: Any | None = None,
    *,
    high_column: str = "high",
    low_column: str = "low",
) -> Tuple[IndicatorInput, IndicatorInput]:
    if low is not None:
        high_input = normalize_input(high)
        low_input = normalize_input(low)
    elif isinstance(high, pd.DataFrame):
        high_input = normalize_input(high, column=high_column)
        low_input = normalize_input(high, column=low_column)
    else:
        raise TypeError("indicator requires either high/low inputs or a dataframe with high/low columns")

    if high_input.index is not None and low_input.index is not None and not high_input.index.equals(low_input.index):
        raise ValueError("indicator inputs must share the same index")
    if high_input.values.shape != low_input.values.shape:
        raise ValueError("indicator inputs must have matching shape")
    return high_input, low_input


def _format_frame(template: IndicatorInput, **columns: np.ndarray) -> pd.DataFrame:
    index = template.index if template.index is not None else None
    return pd.DataFrame(columns, index=index)


def _directional_movement(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    tr = np.full(high.shape, np.nan, dtype=float)
    dm_plus = np.full(high.shape, np.nan, dtype=float)
    dm_minus = np.full(high.shape, np.nan, dtype=float)

    valid = np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
    start = None
    for idx, is_valid in enumerate(valid):
        if is_valid and start is None:
            start = idx
            tr[idx] = high[idx] - low[idx]
            dm_plus[idx] = 0.0
            dm_minus[idx] = 0.0
            continue
        if not is_valid:
            start = None
            continue
        if start is None:
            continue

        up_move = high[idx] - high[idx - 1]
        down_move = low[idx - 1] - low[idx]
        tr[idx] = max(high[idx] - low[idx], abs(high[idx] - close[idx - 1]), abs(low[idx] - close[idx - 1]))
        dm_plus[idx] = up_move if up_move > down_move and up_move > 0.0 else 0.0
        dm_minus[idx] = down_move if down_move > up_move and down_move > 0.0 else 0.0

    return tr, dm_plus, dm_minus


def adx(
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
    period = _validate_period(period)

    tr, dm_plus, dm_minus = _directional_movement(high_input.values, low_input.values, close_input.values)
    atr = _atr_kernel(tr, period)
    smoothed_plus = _atr_kernel(dm_plus, period)
    smoothed_minus = _atr_kernel(dm_minus, period)

    plus_di = np.full(atr.shape, np.nan, dtype=float)
    minus_di = np.full(atr.shape, np.nan, dtype=float)
    valid = np.isfinite(atr) & (atr != 0.0) & np.isfinite(smoothed_plus) & np.isfinite(smoothed_minus)
    plus_di[valid] = (smoothed_plus[valid] / atr[valid]) * 100.0
    minus_di[valid] = (smoothed_minus[valid] / atr[valid]) * 100.0

    dx = np.full(atr.shape, np.nan, dtype=float)
    di_sum = plus_di + minus_di
    dx_valid = np.isfinite(plus_di) & np.isfinite(minus_di) & (di_sum != 0.0)
    dx[dx_valid] = (np.abs(plus_di[dx_valid] - minus_di[dx_valid]) / di_sum[dx_valid]) * 100.0
    adx_values = _atr_kernel(dx, period)

    return _format_frame(high_input, plus_di=plus_di, minus_di=minus_di, adx=adx_values)


def aroon(
    high: Any,
    low: Any | None = None,
    period: int = 14,
    *,
    high_column: str = "high",
    low_column: str = "low",
):
    high_input, low_input = _resolve_hl_inputs(high, low, high_column=high_column, low_column=low_column)
    period = _validate_period(period)

    aroon_up = np.full(high_input.values.shape, np.nan, dtype=float)
    aroon_down = np.full(low_input.values.shape, np.nan, dtype=float)
    lookback = period + 1

    for idx in range(lookback - 1, high_input.values.size):
        high_window = high_input.values[idx - lookback + 1 : idx + 1]
        low_window = low_input.values[idx - lookback + 1 : idx + 1]
        if np.isnan(high_window).any() or np.isnan(low_window).any():
            continue
        highest_pos = 0
        lowest_pos = 0
        for window_idx in range(lookback):
            if high_window[window_idx] > high_window[highest_pos]:
                highest_pos = window_idx
            if low_window[window_idx] < low_window[lowest_pos]:
                lowest_pos = window_idx
        bars_since_high = (lookback - 1) - highest_pos
        bars_since_low = (lookback - 1) - lowest_pos
        aroon_up[idx] = 100.0 * (period - bars_since_high) / float(period)
        aroon_down[idx] = 100.0 * (period - bars_since_low) / float(period)

    return _format_frame(high_input, aroon_up=aroon_up, aroon_down=aroon_down)


def sar(
    high: Any,
    low: Any | None = None,
    acceleration: float = 0.02,
    maximum: float = 0.2,
    *,
    high_column: str = "high",
    low_column: str = "low",
):
    high_input, low_input = _resolve_hl_inputs(high, low, high_column=high_column, low_column=low_column)
    acceleration = float(acceleration)
    maximum = float(maximum)
    if acceleration <= 0.0 or maximum <= 0.0:
        raise ValueError("acceleration and maximum must be greater than zero")
    if acceleration > maximum:
        raise ValueError("acceleration cannot be greater than maximum")

    sar_values = np.full(high_input.values.shape, np.nan, dtype=float)
    trend = np.full(high_input.values.shape, np.nan, dtype=float)
    valid = np.isfinite(high_input.values) & np.isfinite(low_input.values)

    start = None
    af = acceleration
    ep = np.nan
    for idx, is_valid in enumerate(valid):
        if is_valid and start is None:
            start = idx
            sar_values[idx] = low_input.values[idx]
            trend[idx] = 1.0
            af = acceleration
            ep = high_input.values[idx]
            continue
        if not is_valid:
            start = None
            continue
        if start is None:
            continue

        prev_sar = sar_values[idx - 1]
        prev_trend = trend[idx - 1]
        current_sar = prev_sar + (af * (ep - prev_sar))

        if prev_trend > 0:
            if low_input.values[idx] <= current_sar:
                trend[idx] = -1.0
                sar_values[idx] = ep
                ep = low_input.values[idx]
                af = acceleration
            else:
                trend[idx] = 1.0
                if high_input.values[idx] > ep:
                    ep = high_input.values[idx]
                    af = min(af + acceleration, maximum)
                if idx - 2 >= start:
                    current_sar = min(current_sar, low_input.values[idx - 1], low_input.values[idx - 2])
                else:
                    current_sar = min(current_sar, low_input.values[idx - 1])
                sar_values[idx] = current_sar
        else:
            if high_input.values[idx] >= current_sar:
                trend[idx] = 1.0
                sar_values[idx] = ep
                ep = high_input.values[idx]
                af = acceleration
            else:
                trend[idx] = -1.0
                if low_input.values[idx] < ep:
                    ep = low_input.values[idx]
                    af = min(af + acceleration, maximum)
                if idx - 2 >= start:
                    current_sar = max(current_sar, high_input.values[idx - 1], high_input.values[idx - 2])
                else:
                    current_sar = max(current_sar, high_input.values[idx - 1])
                sar_values[idx] = current_sar

    return _format_frame(high_input, sar=sar_values, trend=trend)


class TechnicalAnalysis(_StatisticsTechnicalAnalysis):
    def adx(
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
        return adx(
            high,
            low,
            close,
            period=period,
            high_column=high_column,
            low_column=low_column,
            close_column=close_column,
        )

    def aroon(
        self,
        high: Any,
        low: Any | None = None,
        period: int = 14,
        *,
        high_column: str = "high",
        low_column: str = "low",
    ):
        return aroon(high, low, period=period, high_column=high_column, low_column=low_column)

    def sar(
        self,
        high: Any,
        low: Any | None = None,
        acceleration: float = 0.02,
        maximum: float = 0.2,
        *,
        high_column: str = "high",
        low_column: str = "low",
    ):
        return sar(
            high,
            low,
            acceleration=acceleration,
            maximum=maximum,
            high_column=high_column,
            low_column=low_column,
        )


__all__ = ["TechnicalAnalysis", "adx", "aroon", "sar"]
