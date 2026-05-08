#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Basic raw-client worker example.

Defaults to `dry_run`, so broker orders are never placed unless you explicitly set
KITE_ALGO_EXECUTION_MODE=live and KITE_ALGO_ENABLE_LIVE=1 with a live-enabled token.
"""

from __future__ import annotations

import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, equity_market_order


def _require_live_ack(execution_mode: str) -> None:
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1 to acknowledge real broker orders.")


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    _require_live_ack(execution_mode)

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_basic_equity_demo_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
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
            template_id="basic-equity-demo",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            metadata={
                "strategy_family": "indicator_strategy",
                "strategy_name": "Basic Equity Demo",
                "entry_surface": "external_algo_worker",
            },
        )

    symbol = os.getenv("KITE_ALGO_SYMBOL", "INFY")
    order = equity_market_order(symbol, "BUY", int(os.getenv("KITE_ALGO_QUANTITY", "1")), product=os.getenv("KITE_ALGO_PRODUCT", "CNC"))
    result = client.place_order(
        strategy_run_id,
        order,
        idempotency_key=f"{strategy_run_id}:entry:{symbol}:001",
        metadata={"signal": "demo-entry"},
    )
    print(result)

    pnl = client.get_run_pnl(strategy_run_id)
    print(pnl["totals"]["net_pnl"])


if __name__ == "__main__":
    main()
