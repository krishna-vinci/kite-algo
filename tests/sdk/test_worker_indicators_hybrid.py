from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import adx, aroon, sar, ta


def test_adx_returns_indexed_frame_with_expected_warmup():
    index = pd.RangeIndex(5)
    frame = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0, 14.0],
            "low": [8.0, 9.0, 10.0, 11.0, 12.0],
            "close": [9.0, 10.0, 11.0, 12.0, 13.0],
        },
        index=index,
    )

    result = adx(frame, period=3)

    expected = pd.DataFrame(
        {
            "plus_di": [np.nan, np.nan, 33.33333333333333, 38.88888888888889, 42.592592592592595],
            "minus_di": [np.nan, np.nan, 0.0, 0.0, 0.0],
            "adx": [np.nan, np.nan, np.nan, np.nan, 100.0],
        },
        index=index,
    )

    assert_frame_equal(result, expected, check_exact=False, rtol=1e-12, atol=1e-12)
    assert_frame_equal(ta.adx(frame, period=3), result)


def test_aroon_returns_expected_up_and_down_values():
    index = pd.RangeIndex(6)
    frame = pd.DataFrame(
        {
            "high": [1.0, 2.0, 3.0, 2.0, 1.0, 2.0],
            "low": [4.0, 3.0, 2.0, 1.0, 2.0, 1.0],
        },
        index=index,
    )

    result = aroon(frame, period=3)

    expected = pd.DataFrame(
        {
            "aroon_up": [np.nan, np.nan, np.nan, 66.66666666666667, 33.333333333333336, 0.0],
            "aroon_down": [np.nan, np.nan, np.nan, 100.0, 66.66666666666667, 33.333333333333336],
        },
        index=index,
    )

    assert_frame_equal(result, expected, check_exact=False, rtol=1e-12, atol=1e-12)
    assert_frame_equal(ta.aroon(frame, period=3), result)


def test_sar_returns_sar_and_trend_columns():
    index = pd.RangeIndex(5)
    frame = pd.DataFrame(
        {
            "high": [2.0, 3.0, 4.0, 5.0, 6.0],
            "low": [1.0, 2.0, 3.0, 4.0, 5.0],
        },
        index=index,
    )

    result = sar(frame, acceleration=0.02, maximum=0.2)

    expected = pd.DataFrame(
        {
            "sar": [1.0, 1.0, 1.0, 1.18, 1.4856],
            "trend": [1.0, 1.0, 1.0, 1.0, 1.0],
        },
        index=index,
    )

    assert_frame_equal(result, expected, check_exact=False, rtol=1e-12, atol=1e-12)
    assert_frame_equal(ta.sar(frame, acceleration=0.02, maximum=0.2), result)
