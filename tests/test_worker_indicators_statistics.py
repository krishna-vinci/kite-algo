from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_series_equal

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import linreg, ta


def test_linreg_preserves_index_and_matches_linear_series_tail():
    index = pd.date_range("2026-04-28", periods=5, freq="D")
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=index, name="close")

    result = linreg(series, period=3)

    expected = pd.Series([np.nan, np.nan, 3.0, 4.0, 5.0], index=index, name="linreg")
    assert_series_equal(result, expected)
    assert_series_equal(ta.linreg(series, period=3), result)
