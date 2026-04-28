#!/usr/bin/env python3
"""Live indicator engine example with rebuild semantics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, LiveIndicatorEngine, candles_to_df, warmup_history


def build_engine(client: KiteAlgoWorkerClient, symbol: str, timeframe: str):
    history = warmup_history(client, symbol, timeframe=timeframe, min_candles=50, sleep_seconds=1.0)
    return LiveIndicatorEngine.from_history(
        candles_to_df(history),
        indicators=[
            ("ema", {"source": "close", "period": 9}),
            ("rsi", {"source": "close", "period": 14}),
            ("macd", {"source": "close", "fast_period": 12, "slow_period": 26, "signal_period": 9}),
        ],
    )


def main() -> None:
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )
    symbol = os.getenv("KITE_ALGO_SYMBOL", "NSE:INFY")
    timeframe = os.getenv("KITE_ALGO_TIMEFRAME", "5minute")

    engine = build_engine(client, symbol, timeframe)
    print({"event": "startup", **engine.metadata})

    for event in client.stream_candles(symbol, interval=timeframe):
        candle = event.get("current") or event
        if not candle:
            continue

        if candle.get("is_complete"):
            values = engine.finalize_candle(candle)
            phase = "confirmed"
        else:
            values = engine.update_provisional(candle)
            phase = "provisional"

        print(
            {
                "phase": phase,
                **engine.metadata,
                "ema": values["ema"].value,
                "rsi": values["rsi"].value,
                "macd": values["macd"].value,
            }
        )

        # Restart/reconnect pattern:
        # engine = LiveIndicatorEngine.from_history(candles_to_df(client.get_historical_candles_snapshot(symbol, timeframe=timeframe)), indicators=...)
        # or engine.rebuild(candles_to_df(history), last_stream_candle=candle)


if __name__ == "__main__":
    main()
