#!/usr/bin/env python3
"""Batch dataframe + indicator workflow example."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, candles_to_df, ohlcv_arrays, ta


def main() -> None:
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )
    symbol = os.getenv("KITE_ALGO_SYMBOL", "NSE:INFY")
    timeframe = os.getenv("KITE_ALGO_TIMEFRAME", "5minute")

    history = client.get_historical_candles_snapshot(symbol, timeframe=timeframe)
    df = candles_to_df(history)
    arrays = ohlcv_arrays(df)

    df["ema_fast"] = ta.ema(df["close"], period=9)
    df["ema_slow"] = ta.ema(df["close"], period=21)
    df["rsi_14"] = ta.rsi(df["close"], period=14)
    df["atr_14"] = ta.atr(df, period=14)
    macd = ta.macd(arrays.close, fast_period=12, slow_period=26, signal_period=9)

    latest = df.iloc[-1]
    signal = "flat"
    if latest["ema_fast"] > latest["ema_slow"] and latest["rsi_14"] > 55:
        signal = "bullish"
    elif latest["ema_fast"] < latest["ema_slow"] and latest["rsi_14"] < 45:
        signal = "bearish"

    print(
        {
            "symbol": symbol,
            "rows": len(df),
            "last_close": float(latest["close"]),
            "ema_fast": float(latest["ema_fast"]),
            "ema_slow": float(latest["ema_slow"]),
            "rsi_14": float(latest["rsi_14"]),
            "macd_histogram": float(macd.iloc[-1]["histogram"]),
            "complete": bool(arrays.is_complete[-1]),
            "signal": signal,
        }
    )


if __name__ == "__main__":
    main()
