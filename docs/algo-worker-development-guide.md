# Algo Worker Development Guide

This is the practical coding guide for building external algo strategies that use Kite Algo for execution, grouping, risk edits, exits, accounting, and journaling.

The rule is simple:

```text
strategy worker owns decisions
Kite Algo backend owns execution, grouping, attribution, accounting, and exits
```

Workers should only call the public worker API, preferably through the Python SDK in `sdk/python/kite_algo_worker`. Workers must not call broker internals, database tables, paper-runtime internals, or manually craft broker attribution.

## Install/use the SDK

### Recommended: install from a Git tag on remote strategy servers

Once the SDK changes are committed and tagged, remote servers can install the exact SDK version directly from Git:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.1.0#subdirectory=sdk/python"
```

HTTPS form:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.1.0#subdirectory=sdk/python"
```

Pin live strategy servers to an immutable tag such as `kite-algo-worker-v0.1.0`. Avoid installing from `main` for live workers because a moving branch can change behavior unexpectedly.

Create the tag from the repository root after committing the SDK:

```bash
git tag -a kite-algo-worker-v0.1.0 -m "kite-algo-worker v0.1.0"
git push origin kite-algo-worker-v0.1.0
```

### Local development install

From a strategy project or virtualenv:

```bash
python3 -m pip install -e /path/to/kite-algo/sdk/python
```

During local development from this repository:

```bash
export PYTHONPATH="$PWD/sdk/python:$PYTHONPATH"
```

Minimal worker:

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:8000",
    token="kwa_...",
))

run = client.create_run(
    strategy_run_id="run_mean_reversion_20260425_001",
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    runtime_state={"risk": {"stop_loss_pct": 1.2}},
    metadata={"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
)

order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, "run_mean_reversion_20260425_001:entry:001")
```

## Environment variables

Recommended variables for examples and production workers:

| Variable | Purpose | Safe default |
| --- | --- | --- |
| `KITE_ALGO_API_BASE` | Backend base URL, for example `http://localhost:8000` | `http://localhost:8000` |
| `KITE_ALGO_WORKER_TOKEN` | Raw worker token. Sent as `Authorization: Bearer <token>` | required |
| `KITE_ALGO_ACCOUNT_SCOPE` | Account scope such as `kite:paper-a` or live `kite:<broker_user_id>` | `kite:paper-a` |
| `KITE_ALGO_EXECUTION_MODE` | `dry_run`, `paper`, or `live` | `dry_run` in examples |
| `KITE_ALGO_RUN_ID` | Stable strategy run id for restart recovery | strategy-specific |
| `KITE_ALGO_ENABLE_LIVE` | Explicit live acknowledgement in examples | unset / false |
| `KITE_ALGO_TIMEOUT` | HTTP timeout seconds | `10` |

Store tokens in environment or a secret manager. Never commit raw worker tokens.

## Worker API and SDK lifecycle

All strategy activity should happen under one stable `strategy_run_id` per strategy lifecycle.

1. `health()` at startup to verify the token.
2. `create_run(...)` once per strategy lifecycle. Reuse the same `strategy_run_id` after restarts.
3. `place_order(...)` or `place_basket(...)` with explicit idempotency keys for every intent.
4. `patch_risk(...)` whenever stops, targets, model thresholds, or exposure controls change.
5. `heartbeat(...)` from long-running workers.
6. `get_run(...)` after restarts, mutations, and exits.
7. `exit_run(...)` to close the grouped strategy run.

The SDK maps to public endpoints only:

| SDK method | Worker endpoint |
| --- | --- |
| `health()` | `GET /api/algo-workers/worker/health` |
| `heartbeat(...)` | `POST /api/algo-workers/worker/heartbeat` |
| `create_run(...)` | `POST /api/algo-workers/worker/runs` |
| `get_run(strategy_run_id)` | `GET /api/algo-workers/worker/runs/{strategy_run_id}` |
| `place_order(...)` / `place_basket(...)` | `POST /api/algo-workers/worker/runs/{strategy_run_id}/intents` |
| `patch_risk(...)` | `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/risk` |
| `exit_run(...)` | `POST /api/algo-workers/worker/runs/{strategy_run_id}/exit` |

## Execution modes: dry_run vs paper vs live

### `dry_run`

- Does not place broker orders.
- Accepts and stores worker intent payloads so you can verify strategy decisions and request shapes.
- Good for first local development and CI-style smoke runs.

### `paper`

- Does not place broker orders.
- Sends orders to the paper runtime and proves strategy behavior, grouping, risk patching, exits, and grouped P&L.
- Use paper before enabling any live worker token.

### `live`

- Places real broker orders only when the worker token explicitly allows `live`, the run uses a real broker account scope such as `kite:AB1234`, and live metadata is present.
- Proves broker order placement, fills, margin/charges, and live journaling only after real validation.
- Keep live enablement environment-gated in worker code. The repository also includes `scripts/live_worker_e2e_validation.py` for explicit live validation.

## Strategy run metadata

For live runs, metadata must include:

```json
{
  "strategy_family": "indicator_strategy",
  "strategy_name": "Mean Reversion",
  "entry_surface": "external_algo_worker"
}
```

Valid `strategy_family` values:

- `options_strategy`
- `indicator_strategy`
- `investment_strategy`
- `discretionary_strategy`

`entry_surface` is optional but recommended for auditability.

## Full supported order catalog

Worker live orders must match `broker_api.kite_orders.PlaceOrderRequest`. The SDK order builders produce that shape.

### Supported values

| Field | Supported values / notes |
| --- | --- |
| `exchange` | `NSE`, `BSE`, `NFO`, `CDS`, `MCX` |
| `tradingsymbol` | Broker trading symbol, for example `INFY` or `NIFTY24APR22500CE` |
| `transaction_type` | `BUY`, `SELL` |
| `variety` | `regular`, `amo`, `co`, `iceberg`, `auction` |
| `product` | `CNC`, `MIS`, `NRML`, `MTF` |
| `order_type` | `MARKET`, `LIMIT`, `SL`, `SL-M` |
| `quantity` | Positive integer |
| `price` | Required for `LIMIT` and `SL`; omit for `MARKET`; omit or `0` for `SL-M` |
| `trigger_price` | Required for `SL` and `SL-M` |
| `validity` | `DAY`, `IOC`, `TTL` |
| `validity_ttl` | Required when `validity=TTL`; backend validates `1..365` |
| `disclosed_quantity` | Optional; cannot exceed `quantity` |
| `market_protection` | Optional; allowed for `MARKET` and `SL-M`; `-1` or `0..100` |
| `autoslice` | Optional boolean |
| `iceberg_legs` | Optional integer `2..10` |
| `iceberg_quantity` | Optional positive integer |
| `auction_number` | Optional string for auction orders |
| `squareoff` | Optional cover-order squareoff value |
| `stoploss` | Optional cover-order stoploss value |
| `trailing_stoploss` | Optional cover-order trailing stoploss value |

Do **not** send `tag`, `tags`, or `attribution`. The backend injects compact broker tags and durable live attribution for the strategy run.

### SDK order helpers

```python
from kite_algo_worker import (
    market_order,
    limit_order,
    sl_order,
    sl_m_order,
    option_market_order,
    equity_market_order,
    OrderBuilder,
)
```

Helpers:

- `market_order(exchange, tradingsymbol, transaction_type, product, quantity, variety="regular", ...)`
- `limit_order(exchange, tradingsymbol, transaction_type, product, quantity, price, variety="regular", ...)`
- `sl_order(exchange, tradingsymbol, transaction_type, product, quantity, price, trigger_price, variety="regular", ...)`
- `sl_m_order(exchange, tradingsymbol, transaction_type, product, quantity, trigger_price, variety="regular", ...)`
- `option_market_order(tradingsymbol, transaction_type, quantity, product="NRML", exchange="NFO", variety="regular", ...)`
- `equity_market_order(tradingsymbol, transaction_type, quantity, product="CNC", exchange="NSE", variety="regular", ...)`

All helpers accept the optional fields listed above except `price` / `trigger_price` where the order type controls them.

## Equity examples

```python
from kite_algo_worker import equity_market_order, limit_order

entry = equity_market_order("INFY", "BUY", 1, product="CNC")
client.place_order(run_id, entry, f"{run_id}:entry:INFY:20260425T091500")

limit_exit = limit_order("NSE", "INFY", "SELL", "CNC", 1, price=1510.50)
client.place_order(run_id, limit_exit, f"{run_id}:target:INFY:20260425T100000")
```

## Option examples

```python
from kite_algo_worker import option_market_order

buy_call = option_market_order("NIFTY24APR22500CE", "BUY", 50)
client.place_order(run_id, buy_call, f"{run_id}:long-call:001")
```

## Basket examples

Use baskets for spreads, hedges, and multi-leg strategies. Every leg remains grouped under the same `strategy_run_id`.

```python
orders = [
    option_market_order("NIFTY24APR22500CE", "SELL", 50),
    option_market_order("NIFTY24APR22600CE", "BUY", 50),
]

client.place_basket(
    run_id,
    orders,
    idempotency_key=f"{run_id}:entry-basket:credit-spread:001",
    metadata={"signal": "credit-spread-entry"},
    all_or_none=False,
    dry_run=False,
)
```

For live basket previews, pass `dry_run=True` to preview broker margin/charges without placing broker orders.

## Stop loss / SL-M / LIMIT examples

```python
from kite_algo_worker import limit_order, sl_order, sl_m_order

target = limit_order("NSE", "INFY", "SELL", "CNC", 1, price=1510.50)
stop_limit = sl_order("NSE", "INFY", "SELL", "CNC", 1, price=1489.50, trigger_price=1490.00)
stop_market = sl_m_order("NSE", "INFY", "SELL", "CNC", 1, trigger_price=1490.00, market_protection=-1)

client.place_order(run_id, target, f"{run_id}:target:001")
client.place_order(run_id, stop_limit, f"{run_id}:stop-limit:001")
client.place_order(run_id, stop_market, f"{run_id}:stop-market:001")
```

## Grouped live exit behavior

Always exit with `exit_run(...)`; do not place ad-hoc manual exit orders from the worker.

For live runs, grouped `/exit` is broker-aware:

1. Reconciles live broker positions first.
2. Reads attributed open live legs for that `strategy_run_id`.
3. Builds reducing market exit orders for the grouped live legs.
4. Validates broker net position can cover the attributed strategy quantity.
5. Places the exit basket through the same attributed live order path unless `dry_run=True`.
6. Closes the run only after projected live fills prove the strategy is flat.

If exit orders are submitted but fills are still pending, the run status becomes `exiting`. Keep monitoring, allow order/trade sync to project fills, and call `exit_run(...)` again to confirm flat closure.

Preview without broker placement:

```python
preview = client.exit_run(run_id, reason="operator preview", idempotency_key=f"{run_id}:exit-preview:001", dry_run=True)
```

## Risk patching for dynamic stops/ML models

Use `patch_risk(...)` for dynamic stops, targets, trailing distances, model confidence thresholds, volatility regimes, and max exposure changes.

```python
client.patch_risk(
    run_id,
    {"trailing_stop_pct": 0.75, "model_confidence_min": 0.68},
    reason="model regime changed",
)
```

The backend updates `runtime_state.risk` and matching `risk_schema` values, so operator views and runtime state stay aligned. The worker does not need database access.

## Idempotency key rules

Every order intent must have an explicit idempotency key. Keys must be 8 to 160 characters. The SDK raises `ValueError` if `place_order` or `place_basket` is called without one or with a key outside that length range.

Good keys are deterministic and include the run, action, instrument or basket, and signal/time bucket:

```text
{strategy_run_id}:entry:{symbol}:{bar_timestamp}
{strategy_run_id}:entry-basket:{structure}:{signal_id}
{strategy_run_id}:scaleout:{leg}:{signal_id}
{strategy_run_id}:exit:{reason}:{signal_id}
```

If the same key is retried, the backend returns the stored result instead of placing a duplicate order. Do not generate random keys for retryable signals.

## Recovery after worker restart

Persist locally:

- `strategy_run_id`
- last processed signal/bar id
- idempotency keys already emitted
- strategy-local model/risk state needed to continue decisions

On restart:

1. Create the SDK client and call `health()`.
2. Call `get_run(strategy_run_id)`.
3. If the run is `open`, resume from the last persisted signal id.
4. If the run is `exiting`, call `exit_run(..., dry_run=True)` or `exit_run(...)` after broker sync to confirm flat closure.
5. If the run is `closed` or `failed`, do not submit more intents; create a new lifecycle run if the strategy should start again.

## What backend owns vs what worker owns

Backend owns:

- worker token authentication and scoping
- dry_run/paper/live mode enforcement
- live order validation and broker placement
- compact broker tags and live attribution
- paper runtime execution
- grouped live and paper exits
- live/paper journal separation
- margin/charges contracts
- order/fill projection into journal facts
- keeping broker/manual activity separate as `broker_import` unless safely attributed by reconciliation

Worker owns:

- signals and strategy decisions
- stable `strategy_run_id` selection
- deterministic idempotency keys
- run metadata, summary fields, and initial risk schema
- risk patches when strategy controls change
- heartbeat and restart recovery

## What not to do

- Do not call broker APIs directly from external workers.
- Do not call backend database tables or paper-runtime internals.
- Do not send `tag`, `tags`, or `attribution`; the backend injects attribution.
- Do not mix multiple unrelated strategy lifecycles into one `strategy_run_id`.
- Do not use random idempotency keys for retryable order intents.
- Do not enable live before dry_run and paper behavior are proven.
- Do not assume live `/exit` is closed until the backend confirms flat projected fills.
- Do not manually merge live and paper P&L; the backend keeps them separated.

## Runnable SDK examples

Examples live in `sdk/python/examples/`:

- `mean_reversion_worker.py` — create run, place an equity order, patch risk, heartbeat, optional exit.
- `option_basket_worker.py` — create run, submit an option basket, patch risk.
- `live_exit_preview.py` — preview grouped live exit with `dry_run=True`.

All examples default to safe behavior (`dry_run` or preview). Live order placement requires explicit environment acknowledgement.
