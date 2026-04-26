#!/usr/bin/env python3
"""Safe option basket worker example.

The default mode is `dry_run`. Live mode requires KITE_ALGO_ENABLE_LIVE=1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, option_market_order


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1 to acknowledge real broker orders.")

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_option_basket_demo_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    short_call = os.getenv("KITE_ALGO_SHORT_CALL", "NIFTY24APR22500CE")
    hedge_call = os.getenv("KITE_ALGO_HEDGE_CALL", "NIFTY24APR22600CE")
    lot_size = int(os.getenv("KITE_ALGO_LOT_SIZE", "50"))

    try:
        client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=strategy_run_id,
            template_id="option-basket-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            summary_fields=[{"key": "structure", "label": "Structure", "value": "bear-call-spread"}],
            risk_schema=[
                {"key": "max_loss", "label": "Max loss", "type": "number", "value": 2500, "editable": False},
                {"key": "premium_stop", "label": "Premium stop", "type": "number", "value": 1.8, "editable": True},
            ],
            runtime_state={"risk": {"max_loss": 2500, "premium_stop": 1.8}},
            metadata={
                "strategy_family": "options_strategy",
                "strategy_name": "Option Basket Demo",
                "entry_surface": "external_algo_worker",
            },
        )

    orders = [
        option_market_order(short_call, "SELL", lot_size),
        option_market_order(hedge_call, "BUY", lot_size),
    ]
    client.place_basket(
        strategy_run_id,
        orders,
        idempotency_key=f"{strategy_run_id}:entry-basket:bear-call-demo-001",
        metadata={"signal": "credit-spread-entry"},
        all_or_none=False,
        dry_run=execution_mode == "live" and os.getenv("KITE_ALGO_PLACE_LIVE_BASKET", "0") != "1",
    )

    client.patch_risk(strategy_run_id, {"premium_stop": 1.6}, reason="premium decay after entry")


if __name__ == "__main__":
    main()
