from __future__ import annotations

import sys
from pathlib import Path
import types

import numpy as np

try:  # pragma: no cover - exercised when pandas is installed
    import pandas as pd
except ModuleNotFoundError:  # pragma: no cover - local fallback in minimal envs
    class _Index(list):
        def __init__(self, values, name=None):
            super().__init__(values)
            self.name = name

        def equals(self, other):
            return list(self) == list(other) and self.name == getattr(other, "name", None)

    class _Series:
        def __init__(self, data, index=None, name=None):
            self._data = np.asarray(list(data), dtype=object)
            self.index = index if index is not None else _Index(range(len(self._data)))
            self.name = name

        def to_numpy(self, copy=False):
            return self._data.copy() if copy else self._data

        def tolist(self):
            return self._data.tolist()

    class _DataFrame:
        pass

    class _DatetimeIndex(_Index):
        pass

    def _to_datetime(value, errors=None):
        return value

    pd = types.ModuleType("pandas")
    pd.Series = _Series
    pd.DataFrame = _DataFrame
    pd.Index = _Index
    pd.RangeIndex = lambda size: _Index(range(size))
    pd.DatetimeIndex = _DatetimeIndex
    pd.to_datetime = _to_datetime
    sys.modules["pandas"] = pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from kite_algo_worker.indicators import crossunder, crossover, falling, highest, lowest, rising, ta


def _assert_series_like(result, expected):
    assert result.name == expected.name
    assert result.index.equals(expected.index)
    np.testing.assert_allclose(
        np.asarray(result.to_numpy(), dtype=float),
        np.asarray(expected.to_numpy(), dtype=float),
        equal_nan=True,
    )


def test_crossover_and_crossunder_detect_crossings():
    left = np.array([1.0, 2.0, 4.0])
    right = np.array([3.0, 3.0, 3.0])

    assert crossover(left, right).tolist() == [False, False, True]
    assert crossunder(right, left).tolist() == [False, False, True]


def test_highest_and_lowest_preserve_series_index_and_warmup_nan():
    index = pd.Index(["a", "b", "c", "d"], name="bar")
    series = pd.Series([1.0, 4.0, 2.0, 5.0], index=index, name="close")

    highest_result = highest(series, 2)
    lowest_result = lowest(series, 2)

    expected_highest = pd.Series([np.nan, 4.0, 4.0, 5.0], index=index, name="highest")
    expected_lowest = pd.Series([np.nan, 1.0, 2.0, 2.0], index=index, name="lowest")

    _assert_series_like(highest_result, expected_highest)
    _assert_series_like(lowest_result, expected_lowest)


def test_rising_and_falling_use_full_lookback_window():
    index = pd.RangeIndex(5)
    rising_series = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0], index=index, name="close")
    falling_series = pd.Series([3.0, 2.0, 1.0, 2.0, 3.0], index=index, name="close")

    expected_rising = pd.Series([False, False, True, False, False], index=index, name="rising")
    expected_falling = pd.Series([False, False, True, False, False], index=index, name="falling")

    _assert_series_like(rising(rising_series, 2), expected_rising)
    _assert_series_like(falling(falling_series, 2), expected_falling)


def test_ta_facade_exposes_utility_helpers():
    assert ta.crossunder([3, 2, 1], [2, 2, 2]).tolist() == [False, False, True]
    assert ta.highest([1, 4, 2, 5], 2).tolist()[-1] == 5.0
    assert ta.lowest([1, 4, 2, 5], 2).tolist()[-1] == 2.0
    assert ta.rising([1, 2, 3], 2).tolist() == [False, False, True]
    assert ta.falling([3, 2, 1], 2).tolist() == [False, False, True]


def test_crossover_rejects_mismatched_pandas_indexes():
    left = pd.Series([1.0, 2.0], index=pd.Index(["a", "b"]), name="left")
    right = pd.Series([0.5, 1.5], index=pd.Index(["x", "y"]), name="right")

    try:
        crossover(left, right)
    except ValueError as exc:
        assert "same index" in str(exc)
    else:
        raise AssertionError("expected mismatched pandas indexes to be rejected")


def test_sma_coerces_bad_values_consistently_for_series_and_arrays():
    series = pd.Series(["1", "bad", 3], index=pd.RangeIndex(3), name="close")

    array_result = ta.sma(["1", "bad", 3], 1)
    series_result = ta.sma(series, 1)

    assert array_result[0] == 1.0
    assert np.isnan(array_result[1])
    assert array_result[2] == 3.0
    assert series_result.iloc[0] == 1.0
    assert np.isnan(series_result.iloc[1])
    assert series_result.iloc[2] == 3.0


def test_highest_rejects_non_positive_period():
    try:
        highest([1.0, 2.0, 3.0], 0)
    except ValueError as exc:
        assert "greater than zero" in str(exc)
    else:
        raise AssertionError("expected highest to reject a non-positive period")
