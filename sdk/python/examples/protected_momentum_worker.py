#!/usr/bin/env python3
"""Momentum basket example with backend-owned basket/stale protection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    BackendProtection,
    BasketProtection,
    KiteAlgoWorkerClient,
    OperationalProtection,
    ProtectedPosition,
    equity_market_order,
)


def main() -> None:
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )
    run_id = os.getenv("KITE_ALGO_RUN_ID", "run_protected_momentum_demo")
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    symbols = [item.strip().upper() for item in os.getenv("KITE_ALGO_SYMBOLS", "INFY,TCS,RELIANCE").split(",") if item.strip()]

    protection = BackendProtection(
        positions=[
            ProtectedPosition(
                symbol=f"NSE:{symbol}",
                product="CNC",
                side="BUY",
                quantity=1,
                entry_price=1000,
                stoploss_pct=3,
            )
            for symbol in symbols
        ],
        basket=BasketProtection(stoploss_pct=5, target_pct=8, trailing_activate_pct=6, trailing_drawdown_pct=2),
        operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=600, mis_squareoff_buffer_sec=120),
    )

    client.create_run(
        strategy_run_id=run_id,
        template_id="protected-momentum-demo",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=execution_mode,
        backend_protection=protection,
        metadata={
            "strategy_family": "indicator_strategy",
            "strategy_name": "Protected Momentum Demo",
            "entry_surface": "external_algo_worker",
        },
    )

    orders = [equity_market_order(symbol, "BUY", 1) for symbol in symbols]
    client.place_basket(
        run_id,
        orders,
        idempotency_key=f"{run_id}:rebalance:001",
        metadata={"signal": "monthly-rebalance"},
        dry_run=execution_mode != "paper",
    )

    client.update_backend_protection(run_id, protection, reason="rebalance protection refresh")


if __name__ == "__main__":
    main()
