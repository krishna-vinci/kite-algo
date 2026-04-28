from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import mfi, obv, ta, vwap


def test_obv_preserves_index_and_steps_with_price_direction():
    index = pd.date_range("2026-04-28", periods=4, freq="D")
    frame = pd.DataFrame(
        {
            "close": [10.0, 11.0, 11.0, 9.0],
            "volume": [100.0, 200.0, 300.0, 400.0],
        },
        index=index,
    )

    result = obv(frame, price_column="close", volume_column="volume")

    assert list(result.index) == list(index)
    np.testing.assert_allclose(result.to_numpy(), [0.0, 200.0, 200.0, -200.0])
    np.testing.assert_allclose(ta.obv(frame, price_column="close", volume_column="volume").to_numpy(), result.to_numpy())


def test_vwap_and_mfi_return_expected_series():
    index = pd.date_range("2026-04-28", periods=5, freq="D")
    frame = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 11.0, 10.0],
            "low": [10.0, 11.0, 12.0, 11.0, 10.0],
            "close": [10.0, 11.0, 12.0, 11.0, 10.0],
            "volume": [1.0, 1.0, 1.0, 1.0, 1.0],
        },
        index=index,
    )

    vwap_result = vwap(frame, high_column="high", low_column="low", close_column="close", volume_column="volume")
    assert list(vwap_result.index) == list(index)
    np.testing.assert_allclose(vwap_result.to_numpy(), [10.0, 10.5, 11.0, 11.0, 10.8])
    np.testing.assert_allclose(
        ta.vwap(frame, high_column="high", low_column="low", close_column="close", volume_column="volume").to_numpy(),
        vwap_result.to_numpy(),
    )

    mfi_result = mfi(frame, period=3, high_column="high", low_column="low", close_column="close", volume_column="volume")
    assert list(mfi_result.index) == list(index)
    np.testing.assert_allclose(mfi_result.to_numpy(), [np.nan, np.nan, np.nan, 67.64705882352942, 36.36363636363637], equal_nan=True)
    np.testing.assert_allclose(
        ta.mfi(frame, period=3, high_column="high", low_column="low", close_column="close", volume_column="volume").to_numpy(),
        mfi_result.to_numpy(),
        equal_nan=True,
    )


def test_vwap_supports_price_volume_positional_mode_and_dataframe_volume_override():
    index = pd.date_range("2026-04-28", periods=3, freq="D")
    price = pd.Series([10.0, 11.0, 12.0], index=index)
    volume = pd.Series([1.0, 2.0, 3.0], index=index)

    positional = vwap(price, volume)
    np.testing.assert_allclose(positional.to_numpy(), [10.0, 10.666666666666666, 11.333333333333334])

    frame = pd.DataFrame(
        {
            "high": [12.0, 13.0, 14.0],
            "low": [8.0, 9.0, 10.0],
            "close": [10.0, 11.0, 12.0],
        },
        index=index,
    )

    frame_with_override = vwap(frame, volume=volume)
    np.testing.assert_allclose(frame_with_override.to_numpy(), [10.0, 10.666666666666666, 11.333333333333334])
