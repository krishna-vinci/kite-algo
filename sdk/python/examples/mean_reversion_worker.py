#!/usr/bin/env python3
"""Safe mean-reversion worker example.

Defaults to `dry_run`, so broker orders are never placed unless you explicitly set
KITE_ALGO_EXECUTION_MODE=live and KITE_ALGO_ENABLE_LIVE=1 with a live-enabled token.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, equity_market_order


def _require_live_ack(execution_mode: str) -> None:
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1 to acknowledge real broker orders.")


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    _require_live_ack(execution_mode)

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_mean_reversion_demo_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.getenv("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    client.health()
    try:
        client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=strategy_run_id,
            template_id="mean-reversion-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            summary_fields=[
                {"key": "symbol", "label": "Symbol", "value": os.getenv("KITE_ALGO_SYMBOL", "INFY")},
                {"key": "style", "label": "Style", "value": "mean-reversion"},
            ],
            risk_schema=[
                {"key": "stop_loss_pct", "label": "Stop loss %", "type": "number", "value": 1.2, "editable": True},
                {"key": "target_pct", "label": "Target %", "type": "number", "value": 2.4, "editable": True},
                {"key": "max_quantity", "label": "Max quantity", "type": "number", "value": 1, "editable": False},
            ],
            runtime_state={"risk": {"stop_loss_pct": 1.2, "target_pct": 2.4, "max_quantity": 1}},
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion Demo",
                "entry_surface": "external_algo_worker",
                "worker_version": "example-1",
            },
        )

    symbol = os.getenv("KITE_ALGO_SYMBOL", "INFY")
    order = equity_market_order(symbol, "BUY", 1, product=os.getenv("KITE_ALGO_PRODUCT", "CNC"))
    client.place_order(
        strategy_run_id,
        order,
        idempotency_key=f"{strategy_run_id}:entry:{symbol}:demo-bar-001",
        metadata={"signal": "zscore-cross", "zscore": -2.1},
    )

    client.patch_risk(strategy_run_id, {"stop_loss_pct": 1.0}, reason="volatility contraction")
    client.heartbeat(worker_id="mean-reversion-demo", metrics={"last_signal": "demo-bar-001"})

    # Keep examples safe: preview exits for live, close dry_run/paper demo runs only when explicitly requested.
    if os.getenv("KITE_ALGO_EXIT_DEMO", "0") == "1":
        client.exit_run(
            strategy_run_id,
            reason="demo complete",
            idempotency_key=f"{strategy_run_id}:exit:demo-complete",
            dry_run=execution_mode == "live",
        )


if __name__ == "__main__":
    main()
