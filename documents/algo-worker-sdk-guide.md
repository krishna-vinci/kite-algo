# Algo Worker and Python SDK Guide

This is the best starting document for contributors who want to understand why the worker model exists and why it is a major strength of Kite Algo.

## Worker philosophy

The core rule is:

> strategy code owns decisions; Kite Algo owns execution, attribution, grouped accounting, protection updates, and journal-visible truth.

That rule keeps strategy workers small and replaceable while letting the platform keep the dangerous and stateful parts centralized.

## Why workers exist

Workers make it possible to:

- run strategy logic outside the main backend process
- use local or remote machines for compute-heavy or isolated strategies
- reuse backend-owned market data and execution contracts
- stay inside the same grouped accounting and journaling model as the rest of the platform

## Worker/backend contract

Workers should use only the public worker API, ideally through the Python SDK in `sdk/python/`.

Workers should not:

- call broker internals directly
- write to database tables directly
- invent their own broker attribution tags
- bypass grouped run identity

## Run lifecycle

Every worker strategy should operate under one stable `strategy_run_id` per lifecycle.

Typical flow:

1. `health()` to verify the worker token
2. `create_run(...)` once for the strategy lifecycle (or use `RunConfig` + `client.run(...)`)
3. read quotes, history, ticks, or candles
4. preview/place explicit intents with idempotency keys
5. read grouped run funds and grouped P&L
6. send heartbeats for long-running workers
7. patch backend protection state if the strategy updates thresholds
8. `exit_run(...)` to close the grouped run

## Execution modes

| Mode | What happens |
| --- | --- |
| `dry_run` | Validate logic and payloads without creating paper or live execution |
| `paper` | Use durable backend-owned simulated orders, trades, positions, funds, and grouped P&L |
| `live` | Route through backend-owned broker sessions and live attribution paths |

## Why grouped funds and grouped P&L exist

Brokers expose account-level truth, not strategy-level truth.

Kite Algo adds grouped run accounting so a strategy can answer practical questions such as:

- how much capital is this run using?
- what is this run's current P&L?
- which orders and trades belong to this run?
- is this run flat yet?

That grouped model is what makes remote workers manageable instead of chaotic.

## Worker-safe execution and attribution rules

- workers emit explicit order intents
- the backend injects or derives attribution metadata
- the backend owns grouped order/trade/run truth
- paper mode never writes to the broker
- live mode requires a real broker-backed account scope

## Python SDK role

The SDK is intentionally thin.

It calls public worker endpoints under `/api/algo-workers/worker/*` and does not import backend internals, database logic, or market-runtime internals.

That makes it safer to version, easier to install remotely, and easier for strategy authors to adopt.

### Explicit helper layer (Spec 4)

The SDK now includes an explicit ergonomics layer with no hidden trading behavior:

- `RunConfig`: immutable typed run builder for `create_run(...)` payload parity.
- `client.create_run_from_config(config)`: thin call-through.
- `client.run(config, ...)`: context manager for run/session lifecycle.
- `ManagedRun`: run-bound helper object for explicit calls (`safety_check`, `place_order`, `place_basket`, `patch_risk`, `update_backend_protection`, `exit_run`, `heartbeat`).

Important boundaries:

- `client.run()` owns session claim/heartbeat-on-enter/release lifecycle only.
- It does **not** auto-exit your strategy.
- `ManagedRun` does **not** auto-call `safety_check()`.
- Trading decisions and call ordering stay explicit in worker code.

Use the raw client directly when you want full manual control. Use `RunConfig` + `client.run()` when you want cleaner lifecycle wiring while keeping all safety and mutation calls visible.

Install from PyPI:

```bash
python3 -m pip install kite-algo-worker==0.6.2
```

Pin to an immutable version in production.

Fallback for an exact monorepo tag before or instead of a PyPI release:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.2#subdirectory=sdk/python"
```

Release conventions:

- app/product tags: `vX.Y.Z`
- SDK package tags: `kite-algo-worker-vX.Y.Z`

Pushing an SDK tag automatically builds and publishes the package to PyPI through GitHub Actions.

## Core worker endpoint families

| Family | Example methods |
| --- | --- |
| Health + liveness | `health()`, `heartbeat(...)` |
| Run lifecycle | `create_run(...)`, `get_run(...)`, `exit_run(...)` |
| Market data | `get_quotes(...)`, `get_candles(...)`, `get_historical_candles(...)`, `stream_ticks(...)` |
| Execution | `preview_order(...)`, `preview_basket(...)`, `place_order(...)`, `place_basket(...)` |
| Grouped accounting | `get_funds(...)`, `get_run_funds(...)`, `get_run_pnl(...)`, `stream_run_pnl(...)` |
| Risk / protection | `patch_risk(...)`, `update_backend_protection(...)` |
| Options workflows | `client.options.*` |

## Minimal worker shape

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, ensure_run, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:18777",
    token="kwa_...",
))

run = ensure_run(
    client,
    strategy_run_id="run_demo_001",
    template_id="demo-strategy",
    account_scope="kite:paper-a",
    execution_mode="paper",
    metadata={"strategy_family": "demo", "strategy_name": "Demo Worker"},
)

order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, "run_demo_001:entry:001")
```

## Managed run shape (explicit safety + session lifecycle)

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, RunConfig, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:18777", token="kwa_..."))

config = RunConfig(
    strategy_run_id="run_demo_002",
    template_id="demo-strategy",
    account_scope="kite:paper-a",
    execution_mode="paper",
)

with client.run(config) as run:
    safety = run.safety_check()
    if not safety.can_trade:
        return
    run.place_order(
        equity_market_order("INFY", "BUY", 1),
        idempotency_key=f"{run.run_id}:entry:001",
        safety_token=safety.safety_token,
    )
```

## Option resolver helpers (non-deploying)

`resolve_option_contracts(...)` and `resolve_spread(...)` are pure SDK helpers layered on existing worker-safe options routes. They only resolve contracts and construct `OptionExecutionLeg` payloads. They do not create runs, place orders, or enter/exit option runs.

## Grouped funds and run allocation

Workers can read account-level funds and run-level usage before sizing entries:

```python
account_funds = client.get_funds(mode="paper")
run_funds = client.get_run_funds(run_id)

remaining = run_funds["strategy"]["allocation"]["remaining"]
if remaining is not None and remaining < required_notional:
    return  # skip or reduce size
```

`get_funds(...)` returns a worker-safe account funds snapshot for the token account scope. For paper runs this comes from the paper runtime account. For live runs this comes from broker margins through the backend's live Kite session.

`get_run_funds(strategy_run_id)` adds strategy/run usage derived from backend-owned grouped P&L legs. If run metadata includes `allocation_cap` or `allocation_cap_inr`, the response includes remaining allocation using current gross exposure as the usage basis.

## Realtime grouped run P&L

The SDK exposes grouped run-level P&L snapshots and an SSE stream:

```python
snapshot = client.get_run_pnl(run_id)

for update in client.stream_run_pnl(run_id, interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
```

The payload is grouped by `strategy_run_id` and keeps paper and live P&L separated. The backend is the source of truth for grouped P&L. Live broker/manual activity stays separate unless safely attributed.

## Where worker features live in the codebase

| Path | Purpose |
| --- | --- |
| `api/routers/algo_workers.py` | Worker-safe route surface |
| `algo_runtime/` | Grouped run lifecycle, attribution, execution wiring |
| `options/` | Options sessions, strategy flows, protection, and execution helpers |
| `paper_runtime/` | Durable paper execution path |
| `execution_accounting/` | Shared accounting and attribution semantics |
| `sdk/python/kite_algo_worker/` | SDK client and helper surface |
| `sdk/python/examples/` | Example workers |

## Contribution opportunities

High-value improvements include:

- SDK helper ergonomics and typed models
- better examples and recovery patterns
- stronger worker-safe validation on the backend
- grouped funds/P&L clarity and run-allocation helpers
- live-worker guardrails and per-token risk limits
- options namespace polish and canonical options workflows
- non-option systematic strategy examples on the grouped run contract

## Read next

- [`platform-overview.md`](platform-overview.md)
- [`codebase-map.md`](codebase-map.md)
- [`../sdk/python/README.md`](../sdk/python/README.md)
- [`live-paper-accounting-and-worker-live-execution.md`](live-paper-accounting-and-worker-live-execution.md)
