#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Indicator-driven mean-reversion worker.

Warms up historical candles, uses LiveIndicatorEngine for EMA/RSI evaluation
on a streaming candle feed, generates entry signals on oversold conditions,
and places safety-checked orders with managed lifecycle handling.

Defaults to `dry_run`. Live mode requires KITE_ALGO_ENABLE_LIVE=1.
"""

from __future__ import annotations

import os

from kite_algo_worker import (
    AlgoWorkerConfig,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    LiveIndicatorEngine,
    RunConfig,
    candles_to_df,
    equity_market_order,
)

# ── configuration ──────────────────────────────────────────────

SYMBOL = os.getenv("KITE_ALGO_SYMBOL", "NSE:INFY")
TIMEFRAME = os.getenv("KITE_ALGO_TIMEFRAME", "5minute")
WARMUP_CANDLES = int(os.getenv("KITE_ALGO_WARMUP_CANDLES", "100"))
EMA_PERIOD = int(os.getenv("KITE_ALGO_EMA_PERIOD", "20"))
RSI_PERIOD = int(os.getenv("KITE_ALGO_RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("KITE_ALGO_RSI_OVERSOLD", "30"))
QUANTITY = int(os.getenv("KITE_ALGO_QUANTITY", "1"))
PRODUCT = os.getenv("KITE_ALGO_PRODUCT", "CNC")
MAX_ITERATIONS = int(os.getenv("KITE_ALGO_MAX_ITERATIONS", "20"))


def _require_live_ack(execution_mode: str) -> None:
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1.")


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    _require_live_ack(execution_mode)

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_mean_reversion_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.getenv("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    # 1. Verify connectivity and create/recover run
    client.health()

    try:
        client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=strategy_run_id,
            template_id="mean-reversion",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion",
                "entry_surface": "external_algo_worker",
            },
        )

    # 2. Warm up historical candles and build indicator engine
    history = client.get_historical_candles_snapshot(SYMBOL, timeframe=TIMEFRAME)
    if not history.candles or len(history.candles) < WARMUP_CANDLES:
        raise SystemExit(f"Insufficient warmup: {len(history.candles)} candles (need {WARMUP_CANDLES})")

    df = candles_to_df(history)
    engine = LiveIndicatorEngine.from_history(
        df,
        indicators=[
            ("ema", {"source": "close", "period": EMA_PERIOD}),
            ("rsi", {"source": "close", "period": RSI_PERIOD}),
        ],
    )

    # 3. Stream candles and evaluate signals
    config = RunConfig(
        strategy_run_id=strategy_run_id,
        template_id="mean-reversion",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=execution_mode,
    )

    iteration = 0
    entry_placed = False

    with client.run(config) as run:
        for event in client.stream_candles(SYMBOL, interval=TIMEFRAME):
            iteration += 1
            candle = event.get("current") or event
            if not candle:
                continue

            is_complete = bool(candle.get("is_complete"))

            if is_complete:
                values = engine.finalize_candle(candle)
            else:
                values = engine.update_provisional(candle)

            rsi_val = values.get("rsi")
            ema_val = values.get("ema")
            close = candle.get("close")

            if is_complete and not entry_placed:
                if rsi_val is not None and rsi_val < RSI_OVERSOLD and close is not None and close < (ema_val or close):
                    run.log_decision_event(
                        event_type="signal.generated",
                        summary=f"Oversold signal: RSI={rsi_val:.1f}, Close={close}",
                        details={"symbol": SYMBOL, "rsi": rsi_val, "close": close},
                    )

                    safety = run.safety_check()
                    if safety.can_trade:
                        result = run.place_order(
                            equity_market_order(SYMBOL.split(":")[-1], "BUY", QUANTITY, product=PRODUCT),
                            idempotency_key=f"{run.run_id}:entry:candle-{candle.get('ts', '')}",
                            safety_token=safety.safety_token,
                        )
                        print(f"ENTRY: {result}")
                        entry_placed = True
                    else:
                        print(f"BLOCKED: {safety.blocking_reasons}")

            run.heartbeat(metrics={"iteration": iteration, "rsi": rsi_val})

            if iteration >= MAX_ITERATIONS:
                break

        # 4. Exit if an entry was made
        if entry_placed:
            pnl = client.get_run_pnl(run.run_id)
            print(f"FINAL PNL: {pnl['totals']['net_pnl']}")

            client.exit_run(
                run.run_id,
                reason="demo complete",
                idempotency_key=f"{run.run_id}:exit:demo",
                dry_run=execution_mode == "live",
            )


if __name__ == "__main__":
    main()
