from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .base import format_output, normalize_input
from .oscillators import TechnicalAnalysis as _OscillatorTechnicalAnalysis


def _validate_period(period: int) -> int:
    period = int(period)
    if period <= 0:
        raise ValueError("period must be greater than zero")
    return period


def _linreg_kernel(values: np.ndarray, period: int) -> np.ndarray:
    period = _validate_period(period)
    result = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return result

    x = np.arange(period, dtype=float)
    sum_x = float(x.sum())
    sum_x2 = float((x * x).sum())
    denominator = (period * sum_x2) - (sum_x * sum_x)

    for idx in range(period - 1, values.size):
        window = values[idx - period + 1 : idx + 1]
        if np.isnan(window).any():
            continue
        sum_y = float(window.sum())
        sum_xy = float((x * window).sum())
        if denominator == 0.0:
            result[idx] = float(window[-1])
            continue
        slope = ((period * sum_xy) - (sum_x * sum_y)) / denominator
        intercept = (sum_y - (slope * sum_x)) / float(period)
        result[idx] = (slope * (period - 1)) + intercept
    return result


def linreg(data: Any, period: int = 14, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _linreg_kernel(normalized.values, period)
    return format_output(values, normalized, name="linreg")


class TechnicalAnalysis(_OscillatorTechnicalAnalysis):
    def linreg(self, data: Any, period: int = 14, column: Optional[str] = None):
        return linreg(data, period=period, column=column)


__all__ = ["TechnicalAnalysis", "linreg"]
