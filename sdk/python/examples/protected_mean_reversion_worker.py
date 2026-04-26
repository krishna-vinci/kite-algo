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
    KiteAlgoWorkerError,
    OperationalProtection,
    ProtectedPosition,
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

    try:
        client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=strategy_run_id,
            template_id="protected-mean-reversion-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            backend_protection=protection,
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Protected Mean Reversion Demo",
                "entry_surface": "external_algo_worker",
            },
        )

    client.place_order(
        strategy_run_id,
        equity_market_order(symbol, "BUY", 1),
        idempotency_key=f"{strategy_run_id}:entry:{symbol}:001",
    )

    client.update_backend_protection(strategy_run_id, protection, reason="post-entry protection sync")


if __name__ == "__main__":
    main()
