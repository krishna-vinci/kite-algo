import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import ema, sma, supertrend, ta, vwma, wma  # noqa: E402


def test_module_aliases_expose_trend_indicators():
    series = pd.Series([1.0, 2.0, 3.0], name="close")
    volume = pd.Series([1.0, 1.0, 1.0], name="volume")
    frame = pd.DataFrame({"high": [2.0, 3.0, 4.0], "low": [1.0, 2.0, 3.0], "close": [1.5, 2.5, 3.5]})

    pd.testing.assert_series_equal(ta.ema(series, 3), ema(series, 3))
    pd.testing.assert_series_equal(ta.wma(series, 3), wma(series, 3))
    pd.testing.assert_series_equal(ta.vwma(series, volume, 3), vwma(series, volume, 3))
    pd.testing.assert_frame_equal(ta.supertrend(frame, period=3), supertrend(frame, period=3))


def test_sma_ema_and_wma_preserve_index_and_warmup():
    index = pd.RangeIndex(5)
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=index, name="close")

    sma_result = sma(series, 3)
    ema_result = ema(series, 3)
    wma_result = wma(series, 3)

    expected_sma = pd.Series([np.nan, np.nan, 2.0, 3.0, 4.0], index=index, name="sma")
    expected_ema = pd.Series([np.nan, np.nan, 2.0, 3.0, 4.0], index=index, name="ema")
    expected_wma = pd.Series([np.nan, np.nan, 14.0 / 6.0, 20.0 / 6.0, 26.0 / 6.0], index=index, name="wma")

    pd.testing.assert_series_equal(sma_result, expected_sma)
    pd.testing.assert_series_equal(ema_result, expected_ema)
    pd.testing.assert_series_equal(wma_result, expected_wma)


def test_vwma_uses_volume_weights_and_resets_after_nan():
    index = pd.RangeIndex(7)
    price = pd.Series([1.0, 2.0, 3.0, np.nan, 5.0, 6.0, 7.0], index=index, name="close")
    volume = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0], index=index, name="volume")

    result = vwma(price, volume, 3)

    expected = pd.Series(
        [
            np.nan,
            np.nan,
            14.0 / 6.0,
            np.nan,
            np.nan,
            np.nan,
            110.0 / 18.0,
        ],
        index=index,
        name="vwma",
    )

    pd.testing.assert_series_equal(result, expected)


def test_supertrend_returns_expected_frame_for_trending_data():
    index = pd.date_range("2026-04-28 09:15", periods=5, freq="5min")
    frame = pd.DataFrame(
        {
            "high": [11.0, 12.0, 13.0, 14.0, 15.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
        },
        index=index,
    )

    result = supertrend(frame, period=3, multiplier=2.0)

    assert list(result.columns) == ["supertrend", "direction", "long", "short"]
    assert result.index.equals(index)
    pd.testing.assert_series_equal(
        result["supertrend"],
        pd.Series([np.nan, np.nan, 8.0, 9.0, 10.0], index=index, name="supertrend"),
    )
    pd.testing.assert_series_equal(
        result["direction"],
        pd.Series([np.nan, np.nan, 1.0, 1.0, 1.0], index=index, name="direction"),
    )
    pd.testing.assert_series_equal(
        result["long"],
        pd.Series([np.nan, np.nan, 8.0, 9.0, 10.0], index=index, name="long"),
    )
    pd.testing.assert_series_equal(
        result["short"],
        pd.Series([np.nan, np.nan, np.nan, np.nan, np.nan], index=index, name="short"),
    )


def test_supertrend_preserves_index_when_period_exceeds_history():
    index = pd.date_range("2026-04-28 09:15", periods=2, freq="5min")
    frame = pd.DataFrame(
        {
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.0, 11.0],
        },
        index=index,
    )

    result = supertrend(frame, period=3, multiplier=2.0)

    assert result.index.equals(index)
    assert result["supertrend"].isna().all()
    assert result["direction"].isna().all()


def test_supertrend_carries_prior_state_across_nan_close():
    index = pd.date_range("2026-04-28 09:15", periods=5, freq="5min")
    frame = pd.DataFrame(
        {
            "high": [11.0, 12.0, 13.0, 14.0, 15.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "close": [10.0, 11.0, 12.0, np.nan, 14.0],
        },
        index=index,
    )

    result = supertrend(frame, period=3, multiplier=2.0)

    assert result.loc[index[2], "direction"] == 1.0
    assert result.loc[index[3], "direction"] == 1.0
    assert result.loc[index[3], "supertrend"] == result.loc[index[2], "supertrend"]
