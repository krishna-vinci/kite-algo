from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal, assert_series_equal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk/python"))

from kite_algo_worker.indicators import cci, macd, rsi, stochastic, ta, williamsr, williams_r


def test_rsi_preserves_index_and_warmup_behavior():
    index = pd.date_range("2026-04-28", periods=6, freq="D")
    series = pd.Series([1, 2, 3, 4, 5, 6], index=index)

    result = rsi(series, period=3)

    expected = pd.Series([np.nan, np.nan, np.nan, 100.0, 100.0, 100.0], index=index, name="rsi")
    assert_series_equal(result, expected)
    assert ta.rsi(series, period=3).equals(result)


def test_macd_returns_frame_with_signal_and_histogram():
    index = pd.RangeIndex(8)
    series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8], index=index)

    result = macd(series, fast_period=2, slow_period=3, signal_period=2)

    expected = pd.DataFrame(
        {
            "macd": [np.nan, np.nan, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
            "signal": [np.nan, np.nan, np.nan, 0.5, 0.5, 0.5, 0.5, 0.5],
            "histogram": [np.nan, np.nan, np.nan, 0.0, 0.0, 0.0, 0.0, 0.0],
        },
        index=index,
    )

    assert_frame_equal(result, expected, check_exact=False, atol=1e-9, rtol=1e-9)
    assert ta.macd(series, fast_period=2, slow_period=3, signal_period=2).equals(result)


def test_stochastic_and_williamsr_preserve_index():
    index = pd.RangeIndex(6)
    frame = pd.DataFrame(
        {
            "high": [10, 11, 12, 13, 14, 15],
            "low": [8, 9, 10, 11, 12, 13],
            "close": [10, 11, 12, 13, 14, 15],
        },
        index=index,
    )

    stoch = stochastic(frame, k_period=3, d_period=2)
    expected_stoch = pd.DataFrame(
        {
            "stoch_k": [np.nan, np.nan, 100.0, 100.0, 100.0, 100.0],
            "stoch_d": [np.nan, np.nan, np.nan, 100.0, 100.0, 100.0],
        },
        index=index,
    )
    assert_frame_equal(stoch, expected_stoch)

    wr = williamsr(frame, period=3)
    expected_wr = pd.Series([np.nan, np.nan, 0.0, 0.0, 0.0, 0.0], index=index, name="williamsr")
    assert_series_equal(wr, expected_wr)
    assert_series_equal(williams_r(frame, period=3), expected_wr)
    assert ta.williamsr(frame, period=3).equals(wr)


def test_cci_uses_typical_price_and_warmup_nan():
    index = pd.RangeIndex(5)
    frame = pd.DataFrame(
        {
            "high": [10, 11, 12, 13, 14],
            "low": [8, 9, 10, 11, 12],
            "close": [9, 10, 11, 12, 13],
        },
        index=index,
    )

    result = cci(frame, period=3)
    expected = pd.Series([np.nan, np.nan, 100.0, 100.0, 100.0], index=index, name="cci")
    assert_series_equal(result, expected)
    assert ta.cci(frame, period=3).equals(result)


def test_momentum_indicators_reject_partial_explicit_ohlc_inputs():
    frame = pd.DataFrame(
        {
            "high": [10, 11, 12],
            "low": [8, 9, 10],
            "close": [9, 10, 11],
        }
    )

    try:
        stochastic(frame, low=frame["low"], k_period=3, d_period=2)
    except ValueError as exc:
        assert "require high, low, and close together" in str(exc)
    else:
        raise AssertionError("expected stochastic to reject partial explicit OHLC inputs")

    try:
        williamsr(frame, close=frame["close"], period=3)
    except ValueError as exc:
        assert "require high, low, and close together" in str(exc)
    else:
        raise AssertionError("expected williamsr to reject partial explicit OHLC inputs")
