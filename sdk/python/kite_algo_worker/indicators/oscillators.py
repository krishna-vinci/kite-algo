from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .base import IndicatorInput, _sma_kernel, format_output, normalize_input
from .momentum import TechnicalAnalysis as _MomentumTechnicalAnalysis
from .trend import _ema_kernel


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


def _format_frame(template: IndicatorInput, **columns: np.ndarray) -> pd.DataFrame:
    index = template.index if template.index is not None else None
    return pd.DataFrame(columns, index=index)


def ppo(
    data: Any,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    column: Optional[str] = None,
):
    normalized = normalize_input(data, column=column)
    fast_period = _validate_period(fast_period)
    slow_period = _validate_period(slow_period)
    signal_period = _validate_period(signal_period)
    if fast_period >= slow_period:
        raise ValueError("fast_period must be less than slow_period")

    fast_ema = _ema_kernel(normalized.values, fast_period)
    slow_ema = _ema_kernel(normalized.values, slow_period)

    ppo_line = np.full(normalized.values.shape, np.nan, dtype=float)
    valid = np.isfinite(fast_ema) & np.isfinite(slow_ema) & (slow_ema != 0.0)
    ppo_line[valid] = ((fast_ema[valid] - slow_ema[valid]) / slow_ema[valid]) * 100.0

    signal_line = _ema_kernel(ppo_line, signal_period)
    histogram = ppo_line - signal_line
    return _format_frame(normalized, ppo=ppo_line, signal=signal_line, histogram=histogram)


def dpo(data: Any, period: int = 21, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    period = _validate_period(period)

    sma_values = _sma_kernel(normalized.values, period)
    barsback = (period // 2) + 1
    result = np.full(normalized.values.shape, np.nan, dtype=float)

    for idx in range(barsback, normalized.values.size):
        baseline_idx = idx - barsback
        if np.isnan(normalized.values[idx]) or np.isnan(sma_values[baseline_idx]):
            continue
        result[idx] = normalized.values[idx] - sma_values[baseline_idx]

    return format_output(result, normalized, name="dpo")


class TechnicalAnalysis(_MomentumTechnicalAnalysis):
    def ppo(
        self,
        data: Any,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        column: Optional[str] = None,
    ):
        return ppo(data, fast_period=fast_period, slow_period=slow_period, signal_period=signal_period, column=column)

    def dpo(self, data: Any, period: int = 21, column: Optional[str] = None):
        return dpo(data, period=period, column=column)


__all__ = ["TechnicalAnalysis", "dpo", "ppo"]
