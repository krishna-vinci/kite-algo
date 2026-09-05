# Algo Worker Development Guide

This is the practical coding guide for building external algo strategies that use Kite Algo for execution, grouping, protection, exits, accounting, and journaling.

The rule is simple:

```text
strategy worker owns decisions
Kite Algo backend owns execution, grouping, attribution, accounting, protection state, and exits
```

Workers must only call the public worker API through the Python SDK. Never call broker internals, database tables, or paper-runtime internals.

## Install

```bash
python3 -m pip install kite-algo-worker==0.7.6
```

## Environment variables

| Variable | Purpose | Safe default |
| --- | --- | --- |
| `KITE_ALGO_API_BASE` | Backend base URL | `http://localhost:18777` |
| `KITE_ALGO_WORKER_TOKEN` | Worker token (sent as `Authorization: Bearer <token>`) | required |
| `KITE_ALGO_ACCOUNT_SCOPE` | Account scope, e.g. `kite:paper-a` or `kite:<broker_id>` | `kite:paper-a` |
| `KITE_ALGO_EXECUTION_MODE` | `dry_run`, `paper`, or `live` | `dry_run` |
| `KITE_ALGO_RUN_ID` | Stable strategy run id for restart recovery | strategy-specific |
| `KITE_ALGO_ENABLE_LIVE` | Explicit live acknowledgement | unset (refuses live without it) |
| `KITE_ALGO_TIMEOUT` | HTTP timeout seconds | `10` |

Store tokens in environment or a secret manager. Never commit raw worker tokens.

## Run lifecycle

Every worker strategy operates under one stable `strategy_run_id` per lifecycle.

### Minimal raw-client worker

```python
import os
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
    token=os.environ["KITE_ALGO_WORKER_TOKEN"],
))

strategy_run_id = os.environ.get("KITE_ALGO_RUN_ID", "run_demo_001")
client.health()

run = client.create_run(
    strategy_run_id=strategy_run_id,
    template_id="demo-strategy",
    account_scope=os.environ.get("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
    execution_mode=os.environ.get("KITE_ALGO_EXECUTION_MODE", "dry_run"),
    metadata={
        "strategy_family": "indicator_strategy",
        "strategy_name": "Demo Worker",
        "entry_surface": "external_algo_worker",
    },
)

order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, f"{strategy_run_id}:entry:001")
```

### Recommended managed-lifecycle worker

For longer-lived workers, prefer `RunConfig` + `client.run(...)` + `ManagedRun`:

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, RunConfig, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:18777", token="kwa_...",
))

config = RunConfig(
    strategy_run_id="run_managed_001",
    template_id="managed-demo",
    account_scope="kite:paper-a",
    execution_mode="paper",
)

with client.run(config) as run:
    safety = run.safety_check()
    if not safety.can_trade:
        raise SystemExit(f"Blocked: {', '.join(safety.blocking_reasons) or safety.run_status}")

    run.place_order(
        equity_market_order("INFY", "BUY", 1),
        idempotency_key=f"{run.run_id}:entry:001",
        safety_token=safety.safety_token,
    )
```

**What `client.run(...)` does:** manages session claim/heartbeat/release plumbing.

**What it does NOT do:** auto-trade, auto-exit, or make decisions for you.

## Safety rules

- Always call `safety_check()` before guarded trade actions
- If `can_trade` is false, stop. Read `blocking_reasons` to understand why
- If a `safety_token` is present, pass it to the guarded action
- If the token is rejected (expired), reacquire safety state — don't blindly retry
- Use deterministic idempotency keys for every order intent
- Start in `dry_run`, then `paper`, then `live` only after explicit validation
- Never send broker `tag`, `tags`, or `attribution`
- Close grouped strategies through `client.exit_run(...)`, not ad-hoc exit orders

## Core action payload guidance

### create_run(...)

| Field | Required | Description |
| --- | --- | --- |
| `template_id` | yes | Strategy template identifier |
| `account_scope` | yes | e.g. `kite:paper-a` |
| `execution_mode` | yes | `dry_run`, `paper`, or `live` |
| `strategy_run_id` | no | Stable run ID (generated if omitted) |
| `metadata` | no | Must include `strategy_family`, `strategy_name`, `entry_surface` for live runs |

**Response:** `strategy_run_id`, `status`, `execution_mode`, `runtime_state`, `metadata`.

### safety_check(strategy_run_id)

**Response:** `can_trade` (bool), `safety_token` (str|null), `blocking_reasons` (list[str]), `run_status` (str).

### place_order(...)

| Field | Required | Description |
| --- | --- | --- |
| `strategy_run_id` | yes | Run identifier |
| order payload | yes | Dict with exchange, tradingsymbol, transaction_type, product, order_type, quantity |
| `idempotency_key` | yes | Deterministic key to prevent duplicates |
| `safety_token` | no | From `safety_check()` |
| `session_nonce` | no | From `claim_session()` |

### get_run_pnl(strategy_run_id)

**Response:** `totals` (net_pnl, gross_pnl, charges), `legs` (per-leg breakdown), `is_stale` (bool).

### exit_run(strategy_run_id)

| Field | Required | Description |
| --- | --- | --- |
| `reason` | no | Human-readable reason |
| `idempotency_key` | no | Deterministic exit key |
| `dry_run` | no | Preview exit without placing orders (safe for live) |

## Order builder summary

| Helper | Use for |
| --- | --- |
| `equity_market_order(symbol, side, qty)` | Simple equity entry/exit |
| `limit_order(exchange, symbol, side, product, qty, price)` | Limit orders |
| `sl_order(exchange, symbol, side, product, qty, price, trigger)` | Stop-loss orders |
| `sl_m_order(exchange, symbol, side, product, qty, trigger)` | Stop-loss market orders |
| `option_market_order(symbol, side, qty)` | Option market orders |
| `amo_market_order(exchange, symbol, side, product, qty)` | After-market market orders |
| `amo_limit_order(exchange, symbol, side, product, qty, price)` | After-market limit orders |

All helpers accept optional fields: `variety`, `validity`, `disclosed_quantity`, `market_protection`, and variety-specific fields (CO: `squareoff`/`stoploss`; iceberg: `iceberg_legs`/`iceberg_quantity`).

## Protection essentials

Backend protection objects declare thresholds the backend enforces automatically.

**Products:** `CNC`, `MIS`, `NRML`. **Sides:** `BUY`, `SELL`. Quantities and prices must be positive. Stale-worker limits: `30..86400` seconds.

Use `update_backend_protection(...)` and `patch_risk(...)` to adjust thresholds at runtime.

## Options flow summary

Prefer `client.options.*` and resolver helpers over manual option payload construction.

```python
leg = resolve_offset_leg(client.options, underlying="NIFTY", product="MIS",
    expiry="current_week", option_type="CE", offset="ATM", transaction_type="BUY")

option_run = client.options.create_run(strategy_name="Call Entry", product="MIS",
    legs=[leg.model_dump(exclude_none=True)])

client.options.enter(option_run["strategy_run_id"], safety_token=safety.safety_token)
```

## Timeline, health, and GTT at a glance

- `log_decision_event(...)` — record worker decisions to the run timeline
- `list_timeline(...)` — read execution/decision/protection event history
- `get_run_health_snapshot(...)` — operational health (heartbeat age, session status, recovery status)
- `place_gtt(...)` / `list_gtts()` / `get_gtt(...)` / `modify_gtt(...)` / `delete_gtt(...)` — account-scoped GTT management

## Live-safety rules

- start in `dry_run`, then `paper`, then `live` only after explicit validation
- require `KITE_ALGO_ENABLE_LIVE=1` as an intentional live-mode gate
- for live exits: use `dry_run=True` first to preview, then commit
- never send broker `tag`, `tags`, or `attribution`
- close grouped strategies through `client.exit_run(...)`

## Examples in this pack

- `examples/basic_equity_worker.py` — raw-client baseline
- `examples/managed_run_worker.py` — managed lifecycle example
- `examples/mean_reversion_worker.py` — indicator-driven strategy
- `examples/signal_driven_worker.py` — external decision integration
- `examples/option_basket_worker.py` — options spread example
- `examples/live_exit_preview.py` — safe live exit preview

These files are copied from the canonical SDK examples.
