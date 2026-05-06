#!/usr/bin/env python3
"""Mean-reversion example with backend-owned protection."""

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
    RunConfig,
    equity_market_order,
)


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_protected_mean_reversion_demo")
    symbol = os.getenv("KITE_ALGO_SYMBOL", "INFY")
    symbol_key = f"NSE:{symbol}"

    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    protection = BackendProtection(
        positions=[
            ProtectedPosition(
                symbol=symbol_key,
                product="CNC",
                side="BUY",
                quantity=1,
                entry_price=1500,
                stoploss_pct=1.8,
                target_pct=2.6,
            )
        ],
        basket=BasketProtection(stoploss_pct=2.5),
        operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300),
    )

    config = (
        RunConfig(
            strategy_run_id=strategy_run_id,
            template_id="protected-mean-reversion-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Protected Mean Reversion Demo",
                "entry_surface": "external_algo_worker",
            },
        )
        .with_backend_protection(protection)
        .with_summary_field("symbol", symbol_key)
    )

    with client.run(config) as run:
        safety = run.safety_check()
        if not safety.can_trade:
            return
        run.place_order(
            equity_market_order(symbol, "BUY", 1),
            idempotency_key=f"{run.run_id}:entry:{symbol}:001",
            safety_token=safety.safety_token,
        )
        run.update_backend_protection(protection, reason="post-entry protection sync")


if __name__ == "__main__":
    main()
