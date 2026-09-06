import sys
from pathlib import Path

import pandas as pd

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import candles_to_df, ohlcv_arrays  # noqa: E402
from kite_algo_worker.models import WorkerCandle, WorkerHistoricalCandles  # noqa: E402


def test_candles_to_df_sorts_and_dedupes():
    payload = {
        "candles": [
            {"ts": "2026-04-28T09:20:00+05:30", "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 200, "is_complete": True},
            {"ts": "2026-04-28T09:15:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 100, "oi": 10, "is_complete": True},
            {"ts": "2026-04-28T09:15:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100.75, "volume": 110, "oi": 11, "is_complete": False},
        ]
    }

    df = candles_to_df(payload)

    assert list(df.index.astype(str)) == ["2026-04-28 09:15:00+05:30", "2026-04-28 09:20:00+05:30"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "oi", "is_complete"]
    assert df.iloc[0]["close"] == 100.75
    assert df.iloc[0]["oi"] == 11
    assert not bool(df.iloc[0]["is_complete"])


def test_candles_to_df_accepts_typed_history_and_defaults_optional_columns():
    history = WorkerHistoricalCandles(
        symbol="NSE:SBIN",
        interval="5minute",
        current=WorkerCandle(ts="2026-04-28T09:20:00+05:30", open=101, high=102, low=100, close=101.5, volume=200),
        candles=[WorkerCandle(ts="2026-04-28T09:15:00+05:30", open=100, high=101, low=99, close=100.5, volume=100, oi=None, is_complete=True)],
    )

    df = candles_to_df(history)

    assert list(df.index.astype(str)) == ["2026-04-28 09:15:00+05:30", "2026-04-28 09:20:00+05:30"]
    assert "oi" in df.columns
    assert pd.isna(df.iloc[1]["oi"])
    assert bool(df.iloc[1]["is_complete"])


def test_ohlcv_arrays_extracts_numpy_views():
    df = candles_to_df(
        {
            "candles": [
                {"ts": "2026-04-28T09:15:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "oi": 10, "is_complete": True}
            ]
        }
    )

    arrays = ohlcv_arrays(df)

    assert arrays.close.tolist() == [1.5]
    assert arrays.oi.tolist() == [10]
    assert arrays.is_complete.tolist() == [True]


def test_ohlcv_arrays_normalizes_dataframe_input_and_string_flags():
    df = pd.DataFrame(
        [
            {"ts": "2026-04-28T09:15:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "is_complete": "false"},
            {"ts": "2026-04-28T09:20:00+05:30", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 120, "is_complete": "true"},
        ]
    )

    arrays = ohlcv_arrays(df)

    assert list(arrays.index.astype(str)) == ["2026-04-28 09:15:00+05:30", "2026-04-28 09:20:00+05:30"]
    assert arrays.is_complete.tolist() == [False, True]
    assert arrays.oi is None


def test_sdk_quote_response_preserves_depth_and_unavailable_state():
    from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient

    payload = {
        "quotes": [
            {
                "symbol": "NSE:INFY",
                "depth": {
                    "buy": [{"price": 1525.45, "quantity": 100, "orders": 3}],
                    "sell": [{"price": 1525.55, "quantity": 80, "orders": 2}],
                },
                "depth_available": True,
                "depth_unavailable_reason": None,
            },
            {
                "symbol": "NSE:SBIN",
                "depth": None,
                "depth_available": False,
                "depth_unavailable_reason": "depth_not_supplied_by_feed",
            },
        ],
        "missing": [],
    }
    client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    client._request = lambda *_args, **_kwargs: payload

    response = client.get_quotes(["NSE:INFY", "NSE:SBIN"], mode="full")

    assert response["quotes"][0]["depth"]["buy"][0]["orders"] == 3
    assert response["quotes"][1]["depth"] is None
    assert response["quotes"][1]["depth_unavailable_reason"] == "depth_not_supplied_by_feed"
