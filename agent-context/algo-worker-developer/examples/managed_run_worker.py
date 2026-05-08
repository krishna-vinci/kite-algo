#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Managed-lifecycle worker example.

This example is aligned with the current development-branch helper surface.
It shows the explicit session-aware workflow without hiding the trading decisions.
"""

from __future__ import annotations

import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, RunConfig, equity_market_order


def _require_live_ack(execution_mode: str) -> None:
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1 to acknowledge real broker orders.")


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    _require_live_ack(execution_mode)

    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.getenv("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    config = RunConfig(
        strategy_run_id=os.getenv("KITE_ALGO_RUN_ID", "run_managed_demo_v1"),
        template_id="managed-demo",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=execution_mode,
        metadata={
            "strategy_family": "indicator_strategy",
            "strategy_name": "Managed Run Demo",
            "entry_surface": "external_algo_worker",
        },
    )

    with client.run(config) as run:
        health = run.get_health_snapshot()
        print({"run_id": run.run_id, "health_status": health.health_status, "session_status": health.session_status})

        safety = run.safety_check()
        if not safety.can_trade:
            raise SystemExit(f"Run cannot trade: {', '.join(safety.blocking_reasons) or safety.run_status}")

        run.log_decision_event(
            event_type="signal.generated",
            summary="Demo managed worker generated an entry signal",
            details={"signal": "demo-entry", "symbol": os.getenv("KITE_ALGO_SYMBOL", "INFY")},
        )

        order = equity_market_order(
            os.getenv("KITE_ALGO_SYMBOL", "INFY"),
            "BUY",
            int(os.getenv("KITE_ALGO_QUANTITY", "1")),
            product=os.getenv("KITE_ALGO_PRODUCT", "CNC"),
        )
        result = run.place_order(
            order,
            idempotency_key=f"{run.run_id}:entry:001",
            safety_token=safety.safety_token,
            metadata={"signal": "demo-entry"},
        )
        print(result)

        run.heartbeat(worker_id="managed-run-demo", metrics={"last_action": "entry"})


if __name__ == "__main__":
    main()
