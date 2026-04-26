#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Copyable mean-reversion worker skeleton.

Safe default: dry_run. Set KITE_ALGO_ENABLE_LIVE=1 before live mode.
"""

from __future__ import annotations

import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, equity_market_order


def require_live_ack(mode: str) -> None:
    if mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode without KITE_ALGO_ENABLE_LIVE=1")


def main() -> None:
    mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    require_live_ack(mode)

    run_id = os.getenv("KITE_ALGO_RUN_ID", "run_mean_reversion_demo_001")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ["KITE_ALGO_API_BASE"],
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    client.health()

    try:
        client.get_run(run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=run_id,
            template_id="mean-reversion-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=mode,
            summary_fields=[{"key": "symbol", "label": "Symbol", "value": "INFY"}],
            risk_schema=[
                {"key": "stop_loss_pct", "label": "Stop loss %", "type": "number", "value": 1.2, "editable": True},
                {"key": "target_pct", "label": "Target %", "type": "number", "value": 2.4, "editable": True},
            ],
            runtime_state={"risk": {"stop_loss_pct": 1.2, "target_pct": 2.4}},
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion Demo",
                "entry_surface": "external_algo_worker",
            },
        )

    order = equity_market_order("INFY", "BUY", 1)
    client.place_order(run_id, order, f"{run_id}:entry:INFY:demo-001", metadata={"signal": "zscore-cross"})
    client.patch_risk(run_id, {"stop_loss_pct": 1.0}, reason="volatility contraction")
    client.heartbeat(worker_id="mean-reversion-demo", metrics={"last_signal": "demo-001"})


if __name__ == "__main__":
    main()
