#!/usr/bin/env python3
"""Preview a grouped live exit without placing broker orders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient


def main() -> None:
    strategy_run_id = os.environ["KITE_ALGO_RUN_ID"]
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    run = client.get_run(strategy_run_id)
    if str(run.get("execution_mode", "")).lower() != "live":
        raise SystemExit("This preview is intended for existing live runs only.")

    preview = client.exit_run(
        strategy_run_id,
        reason=os.getenv("KITE_ALGO_EXIT_REASON", "operator live exit preview"),
        idempotency_key=f"{strategy_run_id}:exit-preview:{os.getenv('KITE_ALGO_EXIT_PREVIEW_ID', '001')}",
        dry_run=True,
    )
    print(preview)


if __name__ == "__main__":
    main()
