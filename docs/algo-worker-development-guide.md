# Algo Worker Development Guide

This guide is the contract for building fully automated strategies outside the main app while still using Kite Algo for account scope, paper execution, position grouping, risk edits, exits, and journaling attribution.

The main app owns execution. The worker owns decisions.

## Current Boundary

The v1 worker API is intentionally limited to:

- `paper`
- `dry_run`

Live order routing is not enabled through worker tokens yet. That is deliberate. It lets us prove strategy grouping, idempotency, risk updates, paper P/L, exits, and journal attribution before opening a live broker path.

## Mental Model

Every automated strategy should create one `strategy_run_id` and send all orders, risk updates, and exits through that run.

Use this shape:

```text
algo worker process
  -> creates strategy run
  -> receives or computes signals
  -> submits idempotent order intents
  -> patches run risk when the strategy changes stops or targets
  -> exits the strategy run

Kite Algo backend
  -> authenticates worker token
  -> validates paper/dry-run scope
  -> sends paper orders to the paper runtime
  -> attributes every trade to strategy_run_id
  -> closes the run as one grouped strategy
```

Do not let a worker call broker or paper-runtime internals directly. The worker should only call the worker API.

## Token Setup

Open the Kite Algo settings page and use `Algo worker access`.

Recommended token setup:

- `name`: a short worker name, for example `mean-reversion-paper`
- `account_scope`: the paper account this worker can use, for example `kite:paper-a`
- `allowed_templates`: optional comma-separated templates, for example `mean-reversion, option-master`

The raw token is shown once. Store it in the worker environment:

```bash
export KITE_ALGO_API_BASE="http://localhost:8000"
export KITE_ALGO_WORKER_TOKEN="kwa_..."
```

The worker must send:

```text
Authorization: Bearer $KITE_ALGO_WORKER_TOKEN
```

## API Lifecycle

### 1. Health Check

```bash
curl "$KITE_ALGO_API_BASE/api/algo-workers/worker/health" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN"
```

Use this at startup. Fail fast if the token is invalid, revoked, expired, or scoped incorrectly.

### 2. Create a Strategy Run

Create one run before submitting orders.

```bash
curl -X POST "$KITE_ALGO_API_BASE/api/algo-workers/worker/runs" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_run_id": "run_mean_reversion_20260424_001",
    "template_id": "mean-reversion",
    "account_scope": "kite:paper-a",
    "execution_mode": "paper",
    "summary_fields": [
      {"key": "symbol", "label": "Symbol", "value": "NIFTY 50"},
      {"key": "regime", "label": "Regime", "value": "mean-reversion"}
    ],
    "risk_schema": [
      {"key": "stop_loss_pct", "label": "Stop loss %", "type": "number", "value": 1.2, "editable": true},
      {"key": "target_pct", "label": "Target %", "type": "number", "value": 2.4, "editable": true},
      {"key": "max_position_qty", "label": "Max quantity", "type": "number", "value": 10, "editable": false}
    ],
    "allowed_actions": ["edit_risk", "exit_strategy"],
    "runtime_state": {
      "risk": {
        "stop_loss_pct": 1.2,
        "target_pct": 2.4,
        "max_position_qty": 10
      }
    },
    "metadata": {
      "worker_version": "1.0.0"
    }
  }'
```

Use stable `strategy_run_id` values. If the worker restarts, it should recover the existing run instead of creating unrelated positions.

### 3. Submit an Order Intent

All order placement goes through `intents`. Every intent must have an `idempotency_key`.

```bash
curl -X POST "$KITE_ALGO_API_BASE/api/algo-workers/worker/runs/run_mean_reversion_20260424_001/intents" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "intent_type": "place_order",
    "idempotency_key": "run_mean_reversion_20260424_001:entry:1",
    "payload": {
      "order": {
        "symbol": "NIFTY 50",
        "side": "BUY",
        "quantity": 1,
        "order_type": "MARKET"
      }
    },
    "metadata": {
      "signal": "zscore-cross"
    }
  }'
```

For multi-leg or option strategies, submit a basket:

```json
{
  "intent_type": "place_basket",
  "idempotency_key": "run_option_master_001:entry-basket:1",
  "payload": {
    "basket": {
      "orders": [
        {"symbol": "NIFTY24APR22500CE", "side": "SELL", "quantity": 50, "order_type": "MARKET"},
        {"symbol": "NIFTY24APR22600CE", "side": "BUY", "quantity": 50, "order_type": "MARKET"}
      ]
    }
  }
}
```

If the same `idempotency_key` is retried, the backend returns the previously stored result instead of placing a duplicate paper order.

### 4. Patch Risk

Use this when the strategy changes a stop, target, trail, model confidence threshold, max quantity, or any other run-level control.

```bash
curl -X PATCH "$KITE_ALGO_API_BASE/api/algo-workers/worker/runs/run_mean_reversion_20260424_001/risk" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patch": {
      "stop_loss_pct": 0.8,
      "target_pct": 1.8
    },
    "reason": "volatility contraction"
  }'
```

The patch updates both:

- `runtime_state.risk`
- matching values in `risk_schema`

That keeps backend state and UI-editable risk fields aligned.

### 5. Read Run State

```bash
curl "$KITE_ALGO_API_BASE/api/algo-workers/worker/runs/run_mean_reversion_20260424_001" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN"
```

Use this after restarts and after order or risk mutations.

### 6. Exit the Strategy

Exit the whole grouped strategy run, not one loose position.

```bash
curl -X POST "$KITE_ALGO_API_BASE/api/algo-workers/worker/runs/run_mean_reversion_20260424_001/exit" \
  -H "Authorization: Bearer $KITE_ALGO_WORKER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "target reached"}'
```

After exit, the run is closed. Closed runs cannot be risk-edited.

## Python Worker Skeleton

```python
import os
import time
from dataclasses import dataclass

import requests


@dataclass
class AlgoConfig:
    api_base: str
    token: str
    account_scope: str
    strategy_run_id: str
    template_id: str


class KiteAlgoWorkerClient:
    def __init__(self, config: AlgoConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        })

    def health(self) -> dict:
        return self._request("GET", "/api/algo-workers/worker/health")

    def create_run(self, risk: dict) -> dict:
        return self._request("POST", "/api/algo-workers/worker/runs", json={
            "strategy_run_id": self.config.strategy_run_id,
            "template_id": self.config.template_id,
            "account_scope": self.config.account_scope,
            "execution_mode": "paper",
            "risk_schema": [
                {"key": key, "label": key.replace("_", " ").title(), "type": "number", "value": value, "editable": True}
                for key, value in risk.items()
            ],
            "runtime_state": {"risk": risk},
        })

    def place_order(self, order: dict, key: str) -> dict:
        return self._request("POST", f"/api/algo-workers/worker/runs/{self.config.strategy_run_id}/intents", json={
            "intent_type": "place_order",
            "idempotency_key": key,
            "payload": {"order": order},
        })

    def patch_risk(self, patch: dict, reason: str) -> dict:
        return self._request("PATCH", f"/api/algo-workers/worker/runs/{self.config.strategy_run_id}/risk", json={
            "patch": patch,
            "reason": reason,
        })

    def exit_run(self, reason: str) -> dict:
        return self._request("POST", f"/api/algo-workers/worker/runs/{self.config.strategy_run_id}/exit", json={
            "reason": reason,
        })

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.session.request(method, f"{self.config.api_base}{path}", timeout=10, **kwargs)
        response.raise_for_status()
        return response.json()


def main() -> None:
    config = AlgoConfig(
        api_base=os.environ["KITE_ALGO_API_BASE"],
        token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        account_scope=os.environ.get("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        strategy_run_id=os.environ.get("KITE_ALGO_RUN_ID", "run_mean_reversion_local"),
        template_id="mean-reversion",
    )
    client = KiteAlgoWorkerClient(config)
    client.health()
    client.create_run({"stop_loss_pct": 1.2, "target_pct": 2.4})

    last_signal_id = None
    while True:
        signal = compute_signal()
        if signal and signal["id"] != last_signal_id:
            client.place_order(signal["order"], key=f"{config.strategy_run_id}:signal:{signal['id']}")
            last_signal_id = signal["id"]
        if signal and "risk_patch" in signal:
            client.patch_risk(signal["risk_patch"], reason="model update")
        time.sleep(1)


def compute_signal():
    return None


if __name__ == "__main__":
    main()
```

## Strategy Patterns

### Mean Reversion

Use one run per traded symbol or portfolio bucket. Put editable stop, target, max position size, and cooldown in `risk_schema`. Use deterministic idempotency keys for entries and exits:

```text
{strategy_run_id}:entry:{bar_timestamp}
{strategy_run_id}:scaleout:{bar_timestamp}
{strategy_run_id}:exit:{reason}:{bar_timestamp}
```

### Momentum

Use `runtime_state.risk` for trend stop, trailing stop distance, pyramiding limit, and signal freshness. Patch risk whenever the trailing stop or max exposure changes.

### Option Strategy Master

Treat the option page as a master strategy. The master run can submit baskets for iron condors, spreads, long calls, long puts, and naked trades. Each leg stays attributed to the same `strategy_run_id`.

Use `risk_schema` for premium stop, premium target, trailing premium stop, max loss, and manual-edit controls. The worker decides which fields are editable.

### Machine Learning Strategy

The ML model can run on a separate high-RAM machine. It should still use the same worker contract:

- create a run
- submit order or basket intents
- patch risk when model confidence or volatility regime changes
- exit the run when the model invalidates the trade

Dynamic stops work through `PATCH /risk`. The model does not need direct database access.

## Production Rules

- Use one `strategy_run_id` per strategy lifecycle.
- Persist `strategy_run_id` and the last signal id locally so restarts do not duplicate trades.
- Use idempotency keys for every intent.
- Scope tokens to paper account and templates when possible.
- Send heartbeat from long-running workers.
- Never store the raw token in source control.
- Never share one token across unrelated workers.
- Close runs through `/exit`; do not leave grouped strategy positions open.
- Treat risk edits as run-level state, not loose order metadata.
- Run every new strategy in `dry_run`, then `paper`, before any future live-gated path.

## Architecture Gaps To Close Next

The current API is enough to develop and paper-test isolated algos. The next production hardening steps are:

1. Add a worker market-data stream or snapshot endpoint so remote workers do not need their own duplicate quote plumbing.
2. Add first-class worker journal events for signal, model version, feature snapshot, and decision explanation.
3. Add run recovery helpers, for example list open runs by token/template/account.
4. Add live-mode gating later with explicit broker permission, limits, kill switch, and audit trail.
5. Add SDK wrappers so strategy code can import a small client instead of writing raw HTTP calls.
