from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .base import TechnicalAnalysis as _BaseTechnicalAnalysis, format_output, normalize_input


def _validate_pair(left: Any, right: Any):
    left_input = normalize_input(left)
    right_input = normalize_input(right)
    if left_input.index is not None and right_input.index is not None and not left_input.index.equals(right_input.index):
        raise ValueError("indicator inputs must share the same index")
    if left_input.values.shape != right_input.values.shape:
        raise ValueError("indicator inputs must have matching shape")
    return left_input, right_input


def _rolling_extreme(values: np.ndarray, period: int, *, func) -> np.ndarray:
    if period <= 0:
        raise ValueError("period must be greater than zero")

    result = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return result

    for idx in range(period - 1, values.size):
        window = values[idx - period + 1 : idx + 1]
        if np.isnan(window).any():
            continue
        result[idx] = float(func(window))
    return result


def _trend_kernel(values: np.ndarray, period: int, *, rising: bool) -> np.ndarray:
    if period <= 0:
        raise ValueError("period must be greater than zero")

    result = np.zeros(values.shape, dtype=bool)
    if values.size <= period:
        return result

    for idx in range(period, values.size):
        window = values[idx - period : idx + 1]
        if np.isnan(window).any():
            continue
        current = window[-1]
        history = window[:-1]
        if rising:
            result[idx] = bool(np.all(current > history))
        else:
            result[idx] = bool(np.all(current < history))
    return result


def _crossunder_kernel(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    if left.shape != right.shape:
        raise ValueError("crossunder inputs must have matching shape")

    result = np.zeros(left.shape, dtype=bool)
    if left.size < 2:
        return result

    for idx in range(1, left.size):
        if np.isnan(left[idx]) or np.isnan(right[idx]) or np.isnan(left[idx - 1]) or np.isnan(right[idx - 1]):
            continue
        result[idx] = bool(left[idx] < right[idx] and left[idx - 1] >= right[idx - 1])
    return result


def _extreme(data: Any, period: int, *, func, name: str, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _rolling_extreme(normalized.values, period, func=func)
    return format_output(values, normalized, name=name)


def crossover(left: Any, right: Any):
    left_input, right_input = _validate_pair(left, right)
    result = _BaseTechnicalAnalysis().crossover(left_input, right_input)
    return result


def crossunder(left: Any, right: Any):
    left_input, right_input = _validate_pair(left, right)
    values = _crossunder_kernel(left_input.values, right_input.values)
    return format_output(values, left_input, name="crossunder")


def highest(data: Any, period: int, column: Optional[str] = None):
    return _extreme(data, period, func=np.max, name="highest", column=column)


def lowest(data: Any, period: int, column: Optional[str] = None):
    return _extreme(data, period, func=np.min, name="lowest", column=column)


def rising(data: Any, period: int, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _trend_kernel(normalized.values, period, rising=True)
    return format_output(values, normalized, name="rising")


def falling(data: Any, period: int, column: Optional[str] = None):
    normalized = normalize_input(data, column=column)
    values = _trend_kernel(normalized.values, period, rising=False)
    return format_output(values, normalized, name="falling")


class TechnicalAnalysis(_BaseTechnicalAnalysis):
    def crossunder(self, left: Any, right: Any):
        return crossunder(left, right)

    def highest(self, data: Any, period: int, column: Optional[str] = None):
        return highest(data, period, column=column)

    def lowest(self, data: Any, period: int, column: Optional[str] = None):
        return lowest(data, period, column=column)

    def rising(self, data: Any, period: int, column: Optional[str] = None):
        return rising(data, period, column=column)

    def falling(self, data: Any, period: int, column: Optional[str] = None):
        return falling(data, period, column=column)


__all__ = [
    "TechnicalAnalysis",
    "crossover",
    "crossunder",
    "falling",
    "highest",
    "lowest",
    "rising",
]
