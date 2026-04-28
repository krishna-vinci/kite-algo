#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a lightweight Kite Algo worker SDK certification check.")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()

    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.environ.get("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    preview_report = {"skipped": True}
    run_id = os.environ.get("KITE_ALGO_RUN_ID")
    if args.mode == "live" and run_id:
        preview_report = client.preview_order(
            run_id,
            {
                "exchange": os.environ.get("KITE_ALGO_PREVIEW_EXCHANGE", "NSE"),
                "tradingsymbol": os.environ.get("KITE_ALGO_PREVIEW_SYMBOL", "INFY"),
                "transaction_type": "BUY",
                "variety": "regular",
                "product": os.environ.get("KITE_ALGO_PREVIEW_PRODUCT", "CNC"),
                "order_type": "MARKET",
                "quantity": 1,
                "market_protection": -1,
            },
        )

    report = {
        "health": client.health(),
        "funds": client.get_funds(mode=args.mode, account_scope=os.environ.get("KITE_ALGO_ACCOUNT_SCOPE")),
        "quotes": client.get_quotes([os.environ.get("KITE_ALGO_SYMBOL", "NSE:NIFTY 50")], mode="quote"),
        "preview": preview_report,
        "capabilities": {
            "async_client": True,
            "websocket_client": True,
            "preview_order": True,
            "list_orders": True,
            "wait_for_history": True,
        },
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
