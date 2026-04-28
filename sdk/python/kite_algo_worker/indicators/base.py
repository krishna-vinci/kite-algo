from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class IndicatorInput:
    values: np.ndarray
    index: Optional[pd.Index]
    kind: str
    name: Optional[str] = None


def _coerce_series(data: Any, column: Optional[str] = None) -> pd.Series:
    if isinstance(data, pd.Series):
        return data

    if isinstance(data, pd.DataFrame):
        if column is not None:
            if column not in data.columns:
                raise KeyError(f"column '{column}' not found in dataframe input")
            return data[column]
        if len(data.columns) != 1:
            raise ValueError("dataframe input must contain exactly one column or specify column=")
        return data.iloc[:, 0]

    raise TypeError("indicator input must be a pandas Series or DataFrame")


def _coerce_numeric_1d(values: Any) -> np.ndarray:
    array = np.asarray(values, dtype=object)
    if array.ndim != 1:
        raise ValueError("indicator input must be one-dimensional")
    result = np.empty(array.shape[0], dtype=float)
    for idx, value in enumerate(array):
        try:
            result[idx] = float(value)
        except (TypeError, ValueError):
            result[idx] = np.nan
    return result


def normalize_input(data: Any, column: Optional[str] = None) -> IndicatorInput:
    if isinstance(data, IndicatorInput):
        return data

    if isinstance(data, pd.Series):
        return IndicatorInput(
            values=_coerce_numeric_1d(data.to_numpy(copy=False)),
            index=data.index,
            kind="series",
            name=data.name,
        )

    if isinstance(data, pd.DataFrame):
        series = _coerce_series(data, column=column)
        return IndicatorInput(
            values=_coerce_numeric_1d(series.to_numpy(copy=False)),
            index=series.index,
            kind="dataframe",
            name=series.name,
        )

    return IndicatorInput(values=_coerce_numeric_1d(data), index=None, kind="array", name=None)


def format_output(values: Any, template: IndicatorInput, *, name: Optional[str] = None):
    array = np.asarray(values)
    if template.kind in {"series", "dataframe"} and template.index is not None:
        return pd.Series(array, index=template.index, name=name or template.name)
    return array


class BaseIndicator:
    @staticmethod
    def validate_input(data: Any, column: Optional[str] = None) -> IndicatorInput:
        return normalize_input(data, column=column)

    @staticmethod
    def format_output(values: Any, template: IndicatorInput, *, name: Optional[str] = None):
        return format_output(values, template, name=name)


def _sma_kernel(values: np.ndarray, period: int) -> np.ndarray:
    if period <= 0:
        raise ValueError("period must be greater than zero")

    result = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return result

    for idx in range(period - 1, values.size):
        window = values[idx - period + 1 : idx + 1]
        if np.isnan(window).any():
            continue
        result[idx] = float(window.mean())
    return result


def _crossover_kernel(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    if left.shape != right.shape:
        raise ValueError("crossover inputs must have matching shape")

    result = np.zeros(left.shape, dtype=bool)
    if left.size < 2:
        return result

    for idx in range(1, left.size):
        if np.isnan(left[idx]) or np.isnan(right[idx]) or np.isnan(left[idx - 1]) or np.isnan(right[idx - 1]):
            continue
        result[idx] = bool(left[idx] > right[idx] and left[idx - 1] <= right[idx - 1])
    return result


class TechnicalAnalysis(BaseIndicator):
    def sma(self, data: Any, period: int, column: Optional[str] = None):
        normalized = self.validate_input(data, column=column)
        values = _sma_kernel(normalized.values, period)
        return self.format_output(values, normalized, name="sma")

    def crossover(self, left: Any, right: Any):
        left_input = self.validate_input(left)
        right_input = self.validate_input(right)
        if left_input.index is not None and right_input.index is not None and not left_input.index.equals(right_input.index):
            raise ValueError("crossover pandas inputs must share the same index")
        values = _crossover_kernel(left_input.values, right_input.values)
        return self.format_output(values, left_input, name="crossover")


__all__ = [
    "BaseIndicator",
    "IndicatorInput",
    "TechnicalAnalysis",
    "format_output",
    "normalize_input",
]
