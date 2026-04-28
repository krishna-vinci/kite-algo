#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, ensure_run, live_equity_market_order  # noqa: E402


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise SystemExit(f"Missing required environment variable: {name}")
    return str(value).strip()


class WorkerApi:
    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        allow_404 = bool(kwargs.pop("allow_404", False))
        response = self.session.request(method, f"{self.base_url}{path}", timeout=20, **kwargs)
        if response.status_code == 404 and allow_404:
            return {"status_code": 404}
        try:
            payload = response.json()
        except Exception:
            payload = {"text": response.text}
        if response.status_code >= 400:
            raise SystemExit(json.dumps({"error": "request_failed", "status_code": response.status_code, "payload": payload}, indent=2))
        return payload


def _print_step(name: str, payload: Dict[str, Any]) -> None:
    print(json.dumps({"step": name, "payload": payload}, indent=2, sort_keys=True))


def _ensure_run(api: WorkerApi, *, run_id: str, account_scope: str, mode: str, template_id: str) -> Dict[str, Any]:
    existing = api.request("GET", f"/api/algo-workers/worker/runs/{run_id}", allow_404=True)
    if existing.get("status_code") != 404:
        return existing
    return api.request(
        "POST",
        "/api/algo-workers/worker/runs",
        json={
            "strategy_run_id": run_id,
            "template_id": template_id,
            "account_scope": account_scope,
            "execution_mode": mode,
            "risk_schema": [
                {"key": "max_quantity", "label": "Max Quantity", "type": "number", "value": 1, "editable": False},
                {"key": "validation_only", "label": "Validation Only", "type": "boolean", "value": True, "editable": False},
            ],
            "runtime_state": {"risk": {"max_quantity": 1}, "validation": {"created_by": "live_worker_e2e_validation"}},
            "metadata": {
                "strategy_family": "indicator_strategy",
                "strategy_name": "Live Worker E2E Validation",
                "entry_surface": "external_algo_worker_validation",
            },
        },
    )


def _sdk_client() -> KiteAlgoWorkerClient:
    return KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=_env("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=_env("KITE_ALGO_WORKER_TOKEN"),
            timeout=float(os.environ.get("KITE_ALGO_TIMEOUT", "20")),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Kite Algo worker dry-run/live execution wiring.")
    parser.add_argument("--mode", choices=["dry_run", "live"], default=os.environ.get("KITE_ALGO_E2E_MODE", "dry_run"))
    parser.add_argument("--place-live-order", action="store_true", help="Actually submit the live order intent. Requires KITE_ALGO_CONFIRM_LIVE=YES.")
    parser.add_argument("--exercise-exit", action="store_true", help="Call live /exit dry_run after the order step.")
    args = parser.parse_args()

    if args.mode == "live" and (not args.place_live_order or os.environ.get("KITE_ALGO_CONFIRM_LIVE") != "YES"):
        raise SystemExit("Live mode requires --place-live-order and KITE_ALGO_CONFIRM_LIVE=YES.")

    api = WorkerApi(base_url=_env("KITE_ALGO_API_BASE", "http://localhost:8000"), token=_env("KITE_ALGO_WORKER_TOKEN"))
    client = _sdk_client()
    account_scope = _env("KITE_ALGO_ACCOUNT_SCOPE")
    run_id = os.environ.get("KITE_ALGO_RUN_ID") or f"run_live_e2e_{int(time.time())}"
    template_id = os.environ.get("KITE_ALGO_TEMPLATE_ID", "live-worker-e2e")
    exchange = os.environ.get("KITE_ALGO_E2E_EXCHANGE", "NSE")
    symbol = os.environ.get("KITE_ALGO_E2E_SYMBOL", "INFY")
    product = os.environ.get("KITE_ALGO_E2E_PRODUCT", "CNC")
    quantity = int(os.environ.get("KITE_ALGO_E2E_QUANTITY", "1"))
    transaction_type = os.environ.get("KITE_ALGO_E2E_TRANSACTION_TYPE", "BUY").strip().upper()

    _print_step("health", api.request("GET", "/api/algo-workers/worker/health"))
    run = ensure_run(
        client,
        strategy_run_id=run_id,
        template_id=template_id,
        account_scope=account_scope,
        execution_mode=args.mode,
        metadata={
            "strategy_family": "indicator_strategy",
            "strategy_name": "Live Worker E2E Validation",
            "entry_surface": "external_algo_worker_validation",
        },
    ) if args.mode == "live" else _ensure_run(api, run_id=run_id, account_scope=account_scope, mode=args.mode, template_id=template_id)
    _print_step("run", run)

    order = live_equity_market_order(
        symbol,
        transaction_type,
        quantity,
        product=product,
        exchange=exchange,
        market_protection=int(os.environ.get("KITE_ALGO_E2E_MARKET_PROTECTION", "-1")),
    )
    preview = client.preview_order(run_id, order, metadata={"validation": True, "script": "scripts/live_worker_e2e_validation.py"}) if args.mode == "live" else None
    if preview is not None:
        _print_step("entry_preview", preview)

    intent = api.request(
        "POST",
        f"/api/algo-workers/worker/runs/{run_id}/intents",
        json={
            "intent_type": "place_order",
            "idempotency_key": f"{run_id}:entry:validation:1",
            "payload": {"order": order},
            "metadata": {"validation": True, "script": "scripts/live_worker_e2e_validation.py"},
        },
    )
    _print_step("entry_intent", intent)

    if args.exercise_exit:
        exit_result = api.request(
            "POST",
            f"/api/algo-workers/worker/runs/{run_id}/exit",
            json={
                "reason": "live worker e2e validation",
                "idempotency_key": f"{run_id}:exit:validation:1",
                "dry_run": True,
            },
        )
        _print_step("exit_dry_run", exit_result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
