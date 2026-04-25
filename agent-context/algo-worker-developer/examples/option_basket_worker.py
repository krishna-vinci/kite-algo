#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Copyable option basket worker skeleton."""

from __future__ import annotations

import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, option_market_order


def main() -> None:
    mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    if mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode without KITE_ALGO_ENABLE_LIVE=1")

    run_id = os.getenv("KITE_ALGO_RUN_ID", "run_option_basket_demo_001")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ["KITE_ALGO_API_BASE"],
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    try:
        client.get_run(run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=run_id,
            template_id="option-basket-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=mode,
            summary_fields=[{"key": "structure", "label": "Structure", "value": "bear-call-spread"}],
            risk_schema=[{"key": "premium_stop", "label": "Premium stop", "type": "number", "value": 1.8, "editable": True}],
            runtime_state={"risk": {"premium_stop": 1.8}},
            metadata={"strategy_family": "options_strategy", "strategy_name": "Option Basket Demo", "entry_surface": "external_algo_worker"},
        )

    orders = [
        option_market_order(os.getenv("KITE_ALGO_SHORT_CALL", "NIFTY24APR22500CE"), "SELL", 50),
        option_market_order(os.getenv("KITE_ALGO_HEDGE_CALL", "NIFTY24APR22600CE"), "BUY", 50),
    ]
    client.place_basket(run_id, orders, idempotency_key=f"{run_id}:entry-basket:demo-001", metadata={"signal": "credit-spread-entry"})
    client.patch_risk(run_id, {"premium_stop": 1.6}, reason="premium decay after entry")


if __name__ == "__main__":
    main()
