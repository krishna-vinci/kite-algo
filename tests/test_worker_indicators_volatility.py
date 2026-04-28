from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.indicators import atr, bbands, keltner, ta


def test_atr_preserves_index_and_warmup():
    index = pd.date_range("2026-04-28", periods=4, freq="D")
    frame = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0],
            "low": [8.0, 9.0, 10.0, 11.0],
            "close": [9.0, 10.0, 11.0, 12.0],
        },
        index=index,
    )

    result = atr(frame, period=3)

    assert list(result.index) == list(index)
    np.testing.assert_allclose(result.to_numpy(), [np.nan, np.nan, 2.0, 2.0], equal_nan=True)
    np.testing.assert_allclose(ta.atr(frame, period=3).to_numpy(), result.to_numpy(), equal_nan=True)


def test_bbands_and_keltner_return_indexed_frames():
    index = pd.date_range("2026-04-28", periods=4, freq="D")
    close = pd.Series([1.0, 2.0, 3.0, 4.0], index=index, name="close")
    ohlc = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0],
            "low": [8.0, 9.0, 10.0, 11.0],
            "close": [9.0, 10.0, 11.0, 12.0],
        },
        index=index,
    )

    bands = bbands(close, period=3, multiplier=2.0)
    assert list(bands.columns) == ["upper", "middle", "lower"]
    assert list(bands.index) == list(index)
    np.testing.assert_allclose(
        bands.loc[index[2], ["upper", "middle", "lower"]].to_numpy(),
        [3.632993161855452, 2.0, 0.36700683814454793],
        rtol=1e-12,
    )
    np.testing.assert_allclose(ta.bbands(close, period=3, multiplier=2.0).to_numpy(), bands.to_numpy(), equal_nan=True)

    channel = keltner(ohlc, period=3, multiplier=1.0)
    assert list(channel.columns) == ["upper", "middle", "lower"]
    assert list(channel.index) == list(index)
    np.testing.assert_allclose(channel.loc[index[2], ["upper", "middle", "lower"]].to_numpy(), [12.0, 10.0, 8.0])
    np.testing.assert_allclose(channel.loc[index[3], ["upper", "middle", "lower"]].to_numpy(), [13.0, 11.0, 9.0])
    np.testing.assert_allclose(ta.keltner(ohlc, period=3, multiplier=1.0).to_numpy(), channel.to_numpy(), equal_nan=True)
