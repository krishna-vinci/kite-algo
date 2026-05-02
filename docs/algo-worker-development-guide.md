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
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
```

HTTPS form:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
```

Pin live strategy servers to an immutable tag such as `kite-algo-worker-v0.6.0`. Avoid installing from `main` for live workers because a moving branch can change behavior unexpectedly.

Create the tag from the repository root after committing the SDK:

```bash
git tag -a kite-algo-worker-v0.6.0 -m "kite-algo-worker v0.6.0"
git push origin kite-algo-worker-v0.6.0
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
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, ensure_run, live_equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:8000",
    token="kwa_...",
))

run = ensure_run(
    client,
    strategy_run_id="run_mean_reversion_20260425_001",
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    metadata={"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
)

order = live_equity_market_order("INFY", "BUY", 1)
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
6. `resolve_ticker(...)`, `get_quotes(...)`, `get_candles(...)`, `get_historical_candles(...)`, `wait_for_history(...)`, websocket streams, or SSE streams for backend-owned market data.
7. `get_run(...)` after restarts, mutations, and exits.
8. `get_funds(...)` or `get_run_funds(...)` before sizing entries.
9. `get_run_pnl(...)` or `stream_run_pnl(...)` for grouped realtime run P&L.
10. `exit_run(...)` to close the grouped strategy run.

## Hardened core surface

The v0.5.x SDK is intentionally small and production-oriented. Use these public surfaces first:

- `health()` and `heartbeat(...)` for startup and liveness checks
- `create_run(...)`, `get_run(...)`, `get_funds(...)`, `get_run_funds(...)`, `get_run_pnl(...)`, and `stream_run_pnl(...)` for grouped lifecycle/accounting
- `list_orders(...)`, `list_trades(...)`, `preview_order(...)`, and `preview_basket(...)` for order inspection and sizing checks
- `resolve_ticker(...)`, `search_tickers(...)`, `get_quotes(...)`, `stream_ticks(...)`, `get_candles(...)`, `stream_candles(...)`, `get_historical_candles(...)`, and `get_market_snapshot(...)` for backend-owned market data
- `wait_for_history(...)` and the websocket/SSE clients for recovery-friendly realtime workflows

The certification script at `scripts/sdk_worker_certification.py` exercises this core surface and now reports preview output plus a capability summary.

## Options namespace (canonical worker-safe surface)

Worker options SDK calls must use worker-auth-safe routes under:

`/api/algo-workers/worker/options/*`

Use `client.options` for options market + run/protection lifecycle flows. Canonical
option market snapshots (including Greeks/IV) are exposed by backend option sessions.
Those session Greeks are computed from synthetic-forward + Black-76 in backend option
session computation and surfaced through canonical routes/SDK.

Key points:

- Run-level `product` is required for option run creation (`MIS` or `NRML`).
- Market calls should use `client.options.ensure_session/list_expiries/get_chain/get_mini_chain/get_greeks/...`.
- Selection resolution supports exact strike, ATM/ITM/OTM offset, and snapshot-safe
  `delta_target` selection. Delta targeting only uses already-computed session
  Greek fields; it does not recompute Greeks from raw spot in the worker or route.
- Run/protection SDK methods exist: create/list/get run, preview/enter/exit,
  protection get/update/state/replay.
- Production option runs persist through the durable backend run-state store;
  tests may still override routes with the in-memory store for deterministic cases.
- `kite_algo_worker.option_leg(...)` remains only a payload helper and does not
  imply hidden run-level product defaults.

Example:

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, option_leg

client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_..."))

client.options.ensure_session("NIFTY")
expiries = client.options.list_expiries("NIFTY")
greeks = client.options.get_greeks("NIFTY", expiry="nearest")

run = client.options.create_run(
    strategy_name="bull_call_spread",
    product="MIS",  # required at run level
    legs=[
        option_leg("NIFTY26MAY25000CE", "BUY", 75),
        option_leg("NIFTY26MAY25100CE", "SELL", 75),
    ],
)

preview = client.options.preview_entry(run["strategy_run_id"])
enter_result = client.options.enter(run["strategy_run_id"])
protection_state = client.options.get_protection_state(run["strategy_run_id"])
```

For compatibility, generic SDK primitives still exist, but new option strategy
work should prefer the options namespace above.

### Live protection certification

Use `scripts/live_worker_protection_certification.py` to run the generic live protection 100% gate scenarios for worker stale, position stoploss/target, basket stoploss/target, and protection patch mutability.

Required env:

```bash
export KITE_ALGO_API_BASE=http://localhost:8000
export KITE_ALGO_WORKER_TOKEN=kwa_...
export KITE_ALGO_ACCOUNT_SCOPE=kite:YOUR_BROKER_USER_ID
export KITE_ALGO_CONFIRM_LIVE=YES
```

Optional trading env:

```bash
export KITE_ALGO_CERT_SYMBOL=INFY
export KITE_ALGO_CERT_EXCHANGE=NSE
export KITE_ALGO_CERT_PRODUCT=CNC
export KITE_ALGO_CERT_QUANTITY=1
```

Run all scenarios:

```bash
python3 scripts/live_worker_protection_certification.py
```

Run a subset:

```bash
python3 scripts/live_worker_protection_certification.py --scenarios worker_stale,position_stoploss
```

The script emits structured JSON. If a scenario leaves live exposure behind, it will only submit the emergency flatten fallback when `KITE_ALGO_CONFIRM_FLATTEN=YES`. Otherwise it reports the failure loudly and stops.

The SDK maps to public endpoints only:

| SDK method | Worker endpoint |
| --- | --- |
| `health()` | `GET /api/algo-workers/worker/health` |
| `heartbeat(...)` | `POST /api/algo-workers/worker/heartbeat` |
| `create_run(...)` | `POST /api/algo-workers/worker/runs` |
| `get_run(strategy_run_id)` | `GET /api/algo-workers/worker/runs/{strategy_run_id}` |
| `list_orders(strategy_run_id)` / `list_trades(strategy_run_id)` | `GET /api/algo-workers/worker/orders`, `GET /api/algo-workers/worker/trades` |
| `preview_order(...)` / `preview_basket(...)` | `POST /api/algo-workers/worker/runs/{strategy_run_id}/preview/*` |
| `get_funds(...)` | `GET /api/algo-workers/worker/funds` |
| `get_run_funds(strategy_run_id)` | `GET /api/algo-workers/worker/runs/{strategy_run_id}/funds` |
| `get_run_pnl(strategy_run_id)` | `GET /api/algo-workers/worker/runs/{strategy_run_id}/pnl` |
| `stream_run_pnl(strategy_run_id)` | `GET /api/algo-workers/worker/runs/{strategy_run_id}/pnl/stream` |
| `resolve_ticker(...)` / `search_tickers(...)` | `/api/algo-workers/worker/market/instruments/*` |
| `get_quotes(...)` / `stream_ticks(...)` | `POST /api/algo-workers/worker/market/quotes`, `GET /api/algo-workers/worker/market/ticks/stream` |
| `get_candles(...)` / `stream_candles(...)` | `/api/algo-workers/worker/market/candles*` |
| `get_historical_candles(...)` | `GET /api/algo-workers/worker/market/history` |
| `get_market_snapshot(...)` | `POST /api/algo-workers/worker/market/snapshot` |
| `place_order(...)` / `place_basket(...)` | `POST /api/algo-workers/worker/runs/{strategy_run_id}/intents` |
| `patch_risk(...)` | `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/risk` |
| `update_backend_protection(...)` | `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/protection` |
| `exit_run(...)` | `POST /api/algo-workers/worker/runs/{strategy_run_id}/exit` |

## Backend-owned exposure protection

If a worker wants the backend to enforce position, basket, stale-worker, or MIS squareoff protection, send a declarative `BackendProtection` contract during `create_run(...)` or later with `update_backend_protection(...)`.

Current V1 protection exits the attributed strategy through the backend control-plane exit path when a declared rule triggers. Position rules are still useful for leg-specific thresholds, but the submitted safety action is conservative strategy exit until a broker-safe leg-only exit primitive is added.

```python
from kite_algo_worker import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition

protection = BackendProtection(
    positions=[
        ProtectedPosition(
            symbol="NSE:INFY",
            product="CNC",
            side="BUY",
            quantity=1,
            entry_price=1500,
            stoploss_pct=2,
        )
    ],
    basket=BasketProtection(stoploss_pct=4, trailing_activate_pct=3, trailing_drawdown_pct=1),
    operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300, mis_squareoff_buffer_sec=60),
)

client.create_run(
    strategy_run_id=run_id,
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    backend_protection=protection,
)
```

Keep this contract small and explicit. The worker still owns strategy decisions; the backend only owns enforcement. Validation is strict: product must be `CNC`/`MIS`/`NRML`, side must be `BUY`/`SELL`, quantities and entry prices must be positive, stale-worker limits must be `30..86400`, MIS buffer must be `0..3600`, and enabled protection must contain at least one rules object.

## Runtime-backed market data

External workers can consume market data through the worker API/SDK. The backend remains the facade and the Go market-runtime remains the broker websocket owner.

Use the SDK for ticker resolution, quote snapshots, tick streams, candle snapshots, candle streams, and combined market snapshots:

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)
history = client.get_historical_candles(
    "NSE:INFY",
    timeframe="day",
    from_date="2024-01-01T00:00:00Z",
    to_date="2024-12-31T00:00:00Z",
    ingest=True,
    passthrough=False,
)

for event in client.stream_ticks(["NSE:INFY"], mode="quote"):
    for tick in event.get("ticks", []):
        print(tick["last_price"])
```

For new worker code, prefer the websocket SDK clients for ticks, candles, and grouped run P&L instead of building ad hoc SSE loops:

```python
from kite_algo_worker import WorkerWebSocketClient

ws = WorkerWebSocketClient(base_url="ws://localhost:8000", token="kwa_...")

async with ws.stream(symbols=["NSE:INFY"], mode="quote") as stream:
    event = await stream.recv()
```

`get_historical_candles(...)` uses the backend candle facade. With `ingest=True`, the backend may trigger background ingestion for missing DB ranges. With `passthrough=True`, the backend fetches directly from Kite through its controlled system session and returns normalized candles. Use passthrough deliberately because it consumes broker historical-data quota.

The worker market-data contract is intentionally generic. It supports non-option realtime and investing/positional strategies without adding option-chain strategy logic to the base worker layer. Option-chain helpers, expiry/strike selection, Greeks, IV, and spread builders should be added later in a namespaced options layer inside the same SDK package.

Workers must not connect to broker websockets, read Redis, query market-data tables, or manage market-runtime owner leases directly.

## Worker disconnects and restart recovery

External workers own strategy decisions. If a worker goes offline, new decisions stop. Existing broker orders and positions remain active, and the backend still owns fill ingestion, live position projection, grouped P&L, accounting, and grouped exits when requested.

Production workers should be restart-safe:

1. Persist or deterministically derive the same `strategy_run_id`.
2. On startup, call `get_run(...)` and `get_run_pnl(...)`.
3. Rebuild indicator state from `get_historical_candles(...)`, `get_candles(...)`, or `get_market_snapshot(...)`.
4. Reconnect `stream_ticks(...)`, `stream_candles(...)`, and/or `stream_run_pnl(...)`.
5. Resume decisions only after the recovered backend state is understood.

Do not assume the backend will auto-exit positions when a worker disconnects. Emergency failover policies such as `observe_only`, `cancel_open_orders`, `exit_positions`, or backend hard-stop enforcement should be explicit future safety features.

## Realtime run P&L

The worker API now supports grouped run-level P&L snapshots and an SSE stream.

Use:

```python
snapshot = client.get_run_pnl(run_id)

for update in client.stream_run_pnl(run_id, interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
```

The payload is grouped by `strategy_run_id` and keeps paper and live P&L separated.

Response shape:

```json
{
  "strategy_run_id": "run_mean_reversion_20260425_001",
  "execution_mode": "live",
  "status": "open",
  "currency": "INR",
  "totals": {
    "realized_pnl": 1250.0,
    "unrealized_pnl": -180.0,
    "gross_pnl": 1070.0,
    "charges": 42.5,
    "net_pnl": 1027.5
  },
  "legs": [
    {
      "instrument_token": 408065,
      "exchange": "NSE",
      "tradingsymbol": "INFY",
      "product": "CNC",
      "net_quantity": 1,
      "side": "LONG",
      "average_price": 1450.0,
      "last_price": 1462.0,
      "realized_pnl": 0.0,
      "unrealized_pnl": 12.0,
      "gross_pnl": 12.0,
      "charges": 0.0,
      "net_pnl": 12.0,
      "broker_net_quantity": 1,
      "is_stale": false,
      "last_reconciled_at": "2026-04-25T12:34:56Z"
    }
  ],
  "position_count": 1,
  "is_realtime": true,
  "is_stale": false,
  "updated_at": "2026-04-25T12:34:56Z"
}
```

Mode behavior:

- `dry_run`: returns zero totals and no legs.
- `paper`: returns grouped paper run P&L and grouped paper legs.
- `live`: returns grouped attributed live run P&L with charges and live-leg breakdown.

Important notes:

- The backend is the source of truth for grouped P&L.
- Live broker/manual activity stays separate unless safely attributed.
- `is_stale=true` means the backend could not fully confirm live leg mark coverage or broker quantity alignment for one or more open legs.

## Funds and run allocation

Workers can ask the backend for account-level funds and run-level usage before sizing entries:

```python
account_funds = client.get_funds(mode="paper")
run_funds = client.get_run_funds(run_id)

remaining = run_funds["strategy"]["allocation"]["remaining"]
if remaining is not None and remaining < required_notional:
    return  # skip or reduce size
```

`get_funds(...)` returns a worker-safe account funds snapshot for the token account scope. For paper runs this comes from the paper runtime account. For live runs this comes from broker margins through the backend's live Kite session for `kite:<broker_user_id>`.

`get_run_funds(strategy_run_id)` adds strategy/run usage derived from backend-owned grouped P&L legs. Kite does not provide strategy-level funds directly, so V1 derives `gross_exposure`, `net_exposure`, and current P&L from attributed run state. If run metadata includes `allocation_cap` or `allocation_cap_inr`, the response includes remaining allocation using current gross exposure as the usage basis.

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
- Use `live_equity_market_order(...)` so `market_protection` is explicit on market entries.
- Use `preview_order(...)` and `preview_basket(...)` before live worker-side sizing decisions.

## Recommended startup helpers

- `ensure_run(...)` avoids duplicate run creation when a worker restarts.
- `wait_for_history(...)` smooths over first-run history ingestion delays by polling until candles appear.
- `get_run_protection_state(...)` exposes the backend protection generation/state fragment directly from `runtime_state.backend_protection_state`.
- `list_orders(...)` and `list_trades(...)` give grouped run lifecycle visibility without direct broker API access.

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
