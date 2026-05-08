from __future__ import annotations

import sys
from pathlib import Path

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import LiveIndicatorEngine  # noqa: E402
from kite_algo_worker.indicators.live import IndicatorValue  # noqa: E402
from kite_algo_worker.models import WorkerCandle, WorkerHistoricalCandles  # noqa: E402


def _history_frame():
    import pandas as pd

    return pd.DataFrame(
        [
            {"ts": "2026-04-28T09:15:00+05:30", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0, "is_complete": True},
            {"ts": "2026-04-28T09:20:00+05:30", "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.0, "volume": 110.0, "is_complete": True},
            {"ts": "2026-04-28T09:25:00+05:30", "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.0, "volume": 120.0, "is_complete": True},
        ]
    )


def test_live_indicator_engine_tracks_provisional_and_confirmed_values():
    engine = LiveIndicatorEngine.from_history(_history_frame(), indicators=[("ema", {"source": "close", "period": 3})])

    assert isinstance(engine.confirmed_values["ema"], IndicatorValue)
    assert engine.confirmed_values["ema"].confirmed is True
    assert engine.confirmed_values["ema"].ready is True
    assert engine.confirmed is True

    provisional = engine.update_provisional(
        {
            "ts": "2026-04-28T09:30:00+05:30",
            "open": 13.0,
            "high": 14.0,
            "low": 12.0,
            "close": 13.0,
            "volume": 130.0,
            "is_complete": False,
        }
    )
    confirmed = engine.finalize_candle(
        {
            "ts": "2026-04-28T09:30:00+05:30",
            "open": 13.0,
            "high": 14.0,
            "low": 12.0,
            "close": 13.0,
            "volume": 130.0,
            "is_complete": True,
        }
    )

    assert provisional["ema"].confirmed is False
    assert provisional["ema"].ready is True
    assert confirmed["ema"].confirmed is True
    assert confirmed["ema"].ready is True
    assert confirmed["ema"].value == provisional["ema"].value
    assert engine.confirmed is True
    assert engine.provisional_values == {}


def test_live_indicator_engine_initializes_from_history_with_incomplete_current():
    history = WorkerHistoricalCandles.model_validate(
        {
            "interval": "5minute",
            "candles": [
                {"ts": "2026-04-28T09:15:00+05:30", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0, "is_complete": True},
                {"ts": "2026-04-28T09:20:00+05:30", "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.0, "volume": 110.0, "is_complete": True},
            ],
            "current": {"ts": "2026-04-28T09:25:00+05:30", "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.0, "volume": 120.0, "is_complete": False},
        }
    )

    engine = LiveIndicatorEngine.from_history(history, indicators=[("ema", {"source": "close", "period": 3})])

    assert engine.confirmed_values["ema"].ready is False
    assert engine.confirmed_values["ema"].confirmed is True
    assert engine.provisional_values["ema"].ready is True
    assert engine.provisional_values["ema"].confirmed is False
    assert engine.values["ema"].ts == "2026-04-28T09:25:00+05:30"
    assert engine.confirmed is False


def test_live_indicator_engine_rebuilds_from_history_and_stream():
    history = _history_frame()
    indicators = [
        ("ema", {"source": "close", "period": 3}),
        ("macd", {"source": "close", "fast_period": 2, "slow_period": 3, "signal_period": 2}),
    ]
    last_stream_candle = {
        "ts": "2026-04-28T09:30:00+05:30",
        "open": 13.0,
        "high": 14.0,
        "low": 12.0,
        "close": 13.0,
        "volume": 130.0,
        "is_complete": False,
    }

    engine = LiveIndicatorEngine.from_history(history, indicators=indicators)
    rebuilt = engine.rebuild(history, last_stream_candle=last_stream_candle)

    fresh = LiveIndicatorEngine.from_history(history, indicators=indicators)
    expected = fresh.update_provisional(last_stream_candle)

    assert rebuilt["ema"].value == expected["ema"].value
    assert rebuilt["macd"].value == expected["macd"].value
    assert rebuilt["macd"].ready is True
    assert engine.values["macd"].confirmed is False


def test_live_indicator_engine_rebuild_respects_worker_candle_completion_flag():
    history = _history_frame()
    engine = LiveIndicatorEngine.from_history(history, indicators=[("ema", {"source": "close", "period": 3})])

    rebuilt = engine.rebuild(
        history,
        last_stream_candle=WorkerCandle(
            ts="2026-04-28T09:30:00+05:30",
            open=13.0,
            high=14.0,
            low=12.0,
            close=13.0,
            volume=130.0,
            is_complete=True,
        ),
    )

    assert rebuilt["ema"].confirmed is True
    assert engine.confirmed is True


def test_live_indicator_engine_rebuild_treats_string_false_as_provisional():
    history = _history_frame()
    engine = LiveIndicatorEngine.from_history(history, indicators=[("ema", {"source": "close", "period": 3})])

    rebuilt = engine.rebuild(
        history,
        last_stream_candle={
            "ts": "2026-04-28T09:30:00+05:30",
            "open": 13.0,
            "high": 14.0,
            "low": 12.0,
            "close": 13.0,
            "volume": 130.0,
            "is_complete": "false",
        },
    )

    assert rebuilt["ema"].confirmed is False
    assert engine.confirmed is False
