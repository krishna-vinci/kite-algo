#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Signal-driven worker — integrates external decisions into Kite Algo.

This pattern is for workers whose alpha logic lives elsewhere: a precomputed
signal file, an external model API, a webhook, or a custom rules engine.

The worker reads an external decision, translates it into the standard worker
lifecycle (run check → safety check → place/skip order → read P&L), and stays
explicitly backend-owned for execution and accounting.

Defaults to `dry_run`. Live mode requires KITE_ALGO_ENABLE_LIVE=1.
"""

from __future__ import annotations

import json
import os

from kite_algo_worker import (
    AlgoWorkerConfig,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    equity_market_order,
)

# ── configuration ──────────────────────────────────────────────

SIGNAL_SOURCE = os.getenv("KITE_ALGO_SIGNAL_SOURCE", "env")  # "env" or "file"
QUANTITY = int(os.getenv("KITE_ALGO_QUANTITY", "1"))
PRODUCT = os.getenv("KITE_ALGO_PRODUCT", "CNC")


def _require_live_ack(execution_mode: str) -> None:
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1.")


def _read_signal_from_env() -> dict:
    """Read an external signal from environment variables.

    Expected env vars:
      KITE_ALGO_SIGNAL_SYMBOL  (e.g. INFY)
      KITE_ALGO_SIGNAL_ACTION  (BUY, SELL, or SKIP)
    """
    return {
        "symbol": os.getenv("KITE_ALGO_SIGNAL_SYMBOL", "INFY"),
        "action": os.getenv("KITE_ALGO_SIGNAL_ACTION", "SKIP").upper(),
    }


def _read_signal_from_file(path: str) -> dict:
    """Read an external signal from a JSON file.

    Expected JSON shape:
      {"symbol": "INFY", "action": "BUY", "reason": "custom model v2"}
    """
    with open(path) as f:
        return json.load(f)


def _read_signal() -> dict:
    if SIGNAL_SOURCE == "file":
        path = os.environ.get("KITE_ALGO_SIGNAL_FILE", "/tmp/signal.json")
        return _read_signal_from_file(path)
    return _read_signal_from_env()


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    _require_live_ack(execution_mode)

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_signal_driven_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.getenv("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    # 1. Verify connectivity and create/recover run
    client.health()

    try:
        client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        client.create_run(
            strategy_run_id=strategy_run_id,
            template_id="signal-driven",
            account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
            execution_mode=execution_mode,
            metadata={
                "strategy_family": "discretionary_strategy",
                "strategy_name": "Signal-Driven Demo",
                "entry_surface": "external_algo_worker",
            },
        )

    # 2. Read the external signal
    signal = _read_signal()
    action = signal.get("action", "SKIP").upper()
    symbol = signal.get("symbol", "INFY")
    reason = signal.get("reason", "external signal")

    print(f"SIGNAL: {symbol} → {action} ({reason})")

    # 3. Translate signal into the worker lifecycle
    if action == "SKIP":
        print("SKIP: signal action is SKIP, no order placed")
        return

    if action not in ("BUY", "SELL"):
        raise SystemExit(f"Invalid signal action: {action}")

    # 4. Safety check before placing
    claim = client.claim_session(strategy_run_id)
    safety = client.safety_check(strategy_run_id)

    if not safety.can_trade:
        raise SystemExit(f"BLOCKED: {safety.blocking_reasons}")

    # 5. Log the decision and place the order
    client.log_decision_event(
        strategy_run_id,
        event_type="external_signal.received",
        summary=f"External signal: {action} {symbol} ({reason})",
        details=signal,
    )

    order = equity_market_order(symbol, action, QUANTITY, product=PRODUCT)
    result = client.place_order(
        strategy_run_id,
        order,
        idempotency_key=f"{strategy_run_id}:entry:{symbol}:{reason}",
        safety_token=safety.safety_token,
        session_nonce=claim["worker_session_nonce"],
    )
    print(f"ORDER: {result}")

    # 6. Read grouped P&L
    pnl = client.get_run_pnl(strategy_run_id)
    print(f"PNL: {pnl['totals']['net_pnl']}")

    client.release_session(strategy_run_id, session_nonce=claim["worker_session_nonce"])


if __name__ == "__main__":
    main()
