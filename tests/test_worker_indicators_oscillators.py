from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal, assert_series_equal

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import dpo, ppo, ta


def test_ppo_returns_indexed_frame_with_signal_and_histogram():
    index = pd.RangeIndex(8)
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], index=index, name="close")

    result = ppo(series, fast_period=2, slow_period=3, signal_period=2)

    expected = pd.DataFrame(
        {
            "ppo": [
                np.nan,
                np.nan,
                25.0,
                16.666666666666664,
                12.5,
                10.0,
                8.333333333333332,
                7.142857142857142,
            ],
            "signal": [
                np.nan,
                np.nan,
                np.nan,
                20.833333333333332,
                15.277777777777779,
                11.75925925925926,
                9.475308641975309,
                7.920340975896531,
            ],
            "histogram": [
                np.nan,
                np.nan,
                np.nan,
                -4.166666666666668,
                -2.7777777777777786,
                -1.7592592592592595,
                -1.1419753086419766,
                -0.7774838330393897,
            ],
        },
        index=index,
    )

    assert_frame_equal(result, expected, check_exact=False, rtol=1e-12, atol=1e-12)
    assert_frame_equal(ta.ppo(series, fast_period=2, slow_period=3, signal_period=2), result)


def test_dpo_preserves_index_and_uses_shifted_sma_warmup():
    index = pd.date_range("2026-04-28 09:15", periods=8, freq="5min")
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], index=index, name="close")

    result = dpo(series, period=4)

    expected = pd.Series([np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, 4.5, 4.5], index=index, name="dpo")
    assert_series_equal(result, expected)
    assert_series_equal(ta.dpo(series, period=4), result)
