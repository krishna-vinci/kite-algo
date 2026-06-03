# Kite Algo Worker Python SDK

Kite Algo is a self-hosted algorithmic trading platform for Zerodha/Kite workflows. The Python SDK is the official client for external strategy workers — it calls only public worker API endpoints under `/api/algo-workers/worker/*` and never touches broker internals, paper-runtime internals, market-runtime internals, or the database directly.

**The contract:** strategy code owns decisions. Kite Algo owns execution, attribution, grouped accounting, protection state, and journal-visible truth.

## Package status and install

```bash
python3 -m pip install kite-algo-worker==0.7.4
```

Extras:

```bash
python3 -m pip install "kite-algo-worker[dataframe]==0.7.4"
python3 -m pip install "kite-algo-worker[indicators]==0.7.4"
```

- base SDK: HTTP/WebSocket clients, typed models, order helpers
- `dataframe` extra: adds `pandas` + `numpy` for `candles_to_df(...)` and `ohlcv_arrays(...)`
- `indicators` extra: adds dataframe dependencies plus the indicator stack and optional `numba`

Pin to an immutable version in production.

## Quick reference

| Family | Primary calls |
| --- | --- |
| Connection + liveness | `AlgoWorkerConfig`, `KiteAlgoWorkerClient`, `health()`, `heartbeat(...)` |
| Run lifecycle | `create_run(...)`, `get_run(...)`, `RunConfig`, `client.run(...)`, `ManagedRun`, `claim_session(...)`, `release_session(...)`, `run_heartbeat(...)` |
| Funds + run state | `get_funds(...)`, `get_run_funds(...)`, `get_run_health_snapshot(...)` |
| Market data | `resolve_ticker(...)`, `search_tickers(...)`, `get_quotes(...)`, `stream_ticks(...)`, `get_candles(...)`, `stream_candles(...)`, `get_historical_candles(...)` |
| Order types | equity/option market, limit, SL, SL-M across regular, AMO, CO, iceberg, auction varieties |
| Order placement | `preview_order(...)`, `place_order(...)`, `cancel_order(...)`, `modify_order(...)`, order builders |
| Basket execution | `preview_basket(...)`, `place_basket(...)` |
| Safety + protection | `safety_check(...)`, `BackendProtection`, `update_backend_protection(...)`, `patch_risk(...)` |
| Grouped P&L + monitoring | `get_run_pnl(...)`, `stream_run_pnl(...)`, `list_timeline(...)`, `log_decision_event(...)`, `stream_timeline(...)` |
| Options namespace | `client.options.*`, resolver helpers, options run lifecycle |
| GTT helpers | `place_gtt(...)`, `list_gtts()`, `get_gtt(...)`, `modify_gtt(...)`, `delete_gtt(...)` |
| Exits + recovery | `exit_run(...)`, `wait_for_history(...)`, `warmup_history(...)`, polling helpers |

## Connection and authentication

### AlgoWorkerConfig

The connection settings for the Kite Algo worker API.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `base_url` | `str` | yes | Backend URL, e.g. `http://localhost:18777` |
| `token` | `str` | yes | Worker token, sent as `Authorization: Bearer <token>` |
| `timeout` | `float` | no | HTTP timeout in seconds (default `10.0`) |
| `api_prefix` | `str` | no | API prefix (default `/api/algo-workers`) |

### Client initialization

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:18777",
    token="kwa_...",
))
```

### Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `KITE_ALGO_API_BASE` | Backend base URL | `http://localhost:18777` |
| `KITE_ALGO_WORKER_TOKEN` | Worker token | required |
| `KITE_ALGO_ACCOUNT_SCOPE` | Account scope, e.g. `kite:paper-a` or `kite:<broker_id>` | `kite:paper-a` |
| `KITE_ALGO_EXECUTION_MODE` | `dry_run`, `paper`, or `live` | `dry_run` |
| `KITE_ALGO_ENABLE_LIVE` | Live-mode acknowledgement | unset (refuses live without it) |
| `KITE_ALGO_TIMEOUT` | HTTP timeout seconds | `10` |

**Live-mode gate:** The worker refuses live execution unless `KITE_ALGO_ENABLE_LIVE=1` is set. This is an intentional safety guard.

### Health check

```python
client.health()
# Returns: {"status": "healthy"}
```

## Run lifecycle

Every worker strategy operates under one stable `strategy_run_id` per lifecycle. The backend owns run identity, execution mode, attribution, and exit state.

### Two lifecycle styles

| Style | When to use | Session handling |
| --- | --- | --- |
| **Raw client** | Simple scripts, one-shot tasks, full manual control | You manage everything explicitly |
| **Managed lifecycle** | Long-running workers, session-aware flows | `client.run(...)` manages claim/heartbeat/release |

Both styles are valid and fully supported. The managed lifecycle is recommended for longer-lived workers because it handles session plumbing, but it never hides trading decisions.

---

### Raw-client lifecycle

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:18777",
    token="kwa_...",
))

# 1. Verify connectivity
client.health()

# 2. Create or recover the run
run = client.create_run(
    strategy_run_id="run_basic_equity_001",
    template_id="basic-equity",
    account_scope="kite:paper-a",
    execution_mode="paper",
    metadata={"strategy_family": "indicator_strategy", "strategy_name": "Basic Equity"},
)

# 3. Place an order
order = equity_market_order("INFY", "BUY", 1)
result = client.place_order(
    run["strategy_run_id"], order,
    idempotency_key="run_basic_equity_001:entry:001",
)

# 4. Read grouped P&L
pnl = client.get_run_pnl("run_basic_equity_001")
print(pnl["totals"]["net_pnl"])

# 5. Exit when done
client.exit_run("run_basic_equity_001", reason="strategy complete", idempotency_key="run_basic_equity_001:exit:001")
```

### create_run(...)

Creates a strategy run or recovers an existing one by `strategy_run_id`.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `template_id` | `str` | yes | Strategy template identifier |
| `account_scope` | `str` | yes | Account scope, e.g. `kite:paper-a` |
| `execution_mode` | `str` | yes | `dry_run`, `paper`, or `live` |
| `strategy_run_id` | `str` | no | Stable run ID; generated if omitted |
| `summary_fields` | `list[dict]` | no | Display fields for the frontend run card |
| `risk_schema` | `list[dict]` | no | Editable risk controls shown in the frontend |
| `allowed_actions` | `list[str]` | no | Allowed worker actions (default: `["edit_risk", "exit_strategy"]`) |
| `runtime_state` | `dict` | no | Initial runtime state, e.g. `{"risk": {...}}` |
| `metadata` | `dict` | no | Strategy metadata; live runs must include `strategy_family`, `strategy_name`, `entry_surface` |
| `backend_protection` | `BackendProtection` | no | Optional backend-owned protection object |

**Response shape:**

```json
{
  "strategy_run_id": "run_basic_equity_001",
  "status": "open",
  "execution_mode": "paper",
  "account_scope": "kite:paper-a",
  "runtime_state": {"risk": {...}},
  "metadata": {"strategy_family": "...", "strategy_name": "..."}
}
```

### get_run(...)

Recover the backend-owned run state after restarts.

```python
run = client.get_run("run_basic_equity_001")
```

Returns the full run payload including `status`, `execution_mode`, `runtime_state`, `metadata`, and health fields.

### Managed lifecycle

For longer-lived workers, use `RunConfig`, `client.run(...)`, and `ManagedRun`.

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, RunConfig, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:18777",
    token="kwa_...",
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

    run.heartbeat(metrics={"last_signal": "entry-001"})
```

**What `client.run(...)` does:**
- claims the worker session (if `claim_session=True`)
- optionally sends an initial heartbeat (if `heartbeat_on_enter=True`)
- yields a `ManagedRun` bound to the session
- releases the session on exit (if `release_on_exit=True`)

**What `ManagedRun` does NOT do:**
- it does NOT auto-exit your strategy
- it does NOT auto-call `safety_check()`
- it does NOT make trading decisions

### Session management (raw client)

If you prefer full control, manage sessions explicitly:

```python
claim = client.claim_session("run_basic_equity_001")
nonce = claim["worker_session_nonce"]

client.run_heartbeat("run_basic_equity_001", session_nonce=nonce)

client.release_session("run_basic_equity_001", session_nonce=nonce)
```

## Funds and run state

### get_funds(...)

Account-level funds for the token's account scope. For paper runs this comes from the paper runtime account. For live runs this comes from broker margins through the backend-controlled live Kite session.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `mode` | `str` | no | `"paper"` (default) or `"live"` |
| `account_scope` | `str` | no | Override the token's default account scope |

**Response shape:**

```json
{
  "available": {"cash": 100000.0, "collateral": 0.0, "intraday_payin": 0.0},
  "utilized": {"debits": 0.0, "exposure": 0.0, "m2m": 0.0, "option_premium": 0.0, "span": 0.0, "holding_sales": 0.0, "turnover": 0.0},
  "net": 100000.0
}
```

```python
funds = client.get_funds(mode="paper")
print(funds["net"])
```

### get_run_funds(strategy_run_id)

Run-level usage derived from backend-owned grouped P&L legs plus optional allocation caps.

```python
run_funds = client.get_run_funds("run_basic_equity_001")
remaining = run_funds.get("strategy", {}).get("allocation", {}).get("remaining")
```

**Response shape:**

```json
{
  "strategy": {
    "allocation": {
      "cap": null,
      "cap_inr": null,
      "usage_gross_exposure": 0.0,
      "remaining": null
    }
  }
}
```

If run metadata includes `allocation_cap` or `allocation_cap_inr`, remaining is computed using current gross exposure as the usage basis.

### get_run_health_snapshot(strategy_run_id)

Operational health for a specific run.

```python
snapshot = client.get_run_health_snapshot("run_basic_equity_001")
print(snapshot.health_status, snapshot.session_status, snapshot.recovery_action_required)
```

**Response fields:** `strategy_run_id`, `status`, `execution_mode`, `account_scope`, `heartbeat_age_sec`, `health_status`, `session_status`, `recovery_status`, `recovery_action_required`, `worker_session_claimed_at`, `last_heartbeat_at`.

## Market data

All market data is backend-owned. Workers never open their own broker websocket connections.

### resolve_ticker(...) / search_tickers(...)

```python
instrument = client.resolve_ticker("NSE:INFY")
results = client.search_tickers("NIFTY", exchange="NFO", limit=20)
```

### get_quotes(...)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `instruments` | `list[str\|int]` | yes | Instrument tokens or `EXCH:SYMBOL` strings |
| `mode` | `str` | no | `"quote"` (full), `"ltp"` (last price only), `"full"` (all fields) |

**Response:** a `quotes` dict keyed by instrument identifier, each containing last_price, ohlc, depth, volume, etc.

```python
quotes = client.get_quotes(["NSE:INFY", "NSE:RELIANCE"], mode="quote")
print(quotes["quotes"]["NSE:INFY"]["last_price"])
```

### get_candles(...)

Reads the worker's recent live/cache candle surface. It is not the full
historical-data API; daily warmups such as `interval="day", lookback=366` may be
empty/stale when no live daily cache exists. Use `get_historical_candles(...)`
for historical daily/intraday ranges.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `symbol_or_token` | `str\|int` | yes | Instrument symbol or token |
| `interval` | `str` | no | Candle interval (default `"5minute"`) |
| `lookback` | `int` | no | Number of recent candles (default `50`) |

**Response:** `candles` list, optional `current` in-progress candle, `is_stale` flag.

```python
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=20)
for c in candles["candles"]:
    print(c["open"], c["high"], c["low"], c["close"])
```

### get_historical_candles(...)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `symbol_or_token` | `str\|int` | yes | Instrument |
| `timeframe` | `str` | no | `"day"`, `"5minute"`, etc. |
| `from_date` | `str` | no | ISO 8601 start |
| `to_date` | `str` | no | ISO 8601 end |
| `lookback_days` | `int` | no | Convenience range in days when `from_date` is omitted |
| `ingest` | `bool` | no | Trigger background ingestion for missing DB ranges |
| `passthrough` | `bool` | no | Fetch directly from Kite via the controlled system session |

**Response:** `candles`, `source` (e.g. `"db"`, `"kite_passthrough"`), `ingestion` metadata.

```python
history = client.get_historical_candles(
    "NSE:INFY", timeframe="day",
    lookback_days=366,
    passthrough=True,
)
```

### Streaming

```python
# Real-time tick stream (SSE)
for event in client.stream_ticks(["NSE:INFY"], mode="quote"):
    for tick in event.get("ticks", []):
        print(tick["last_price"])

# Real-time candle stream (SSE)
for event in client.stream_candles("NSE:INFY", interval="5minute"):
    candle = event.get("current") or event
    if candle:
        print(candle["close"], candle["is_complete"])
```

## Order types

### Order type reference

| order_type | Required fields | Optional fields | Notes |
| --- | --- | --- | --- |
| `MARKET` | exchange, tradingsymbol, transaction_type, product, quantity | disclosed_quantity, market_protection | Default variety is `regular` |
| `LIMIT` | ...plus `price` | disclosed_quantity | |
| `SL` | ...plus `price`, `trigger_price` | disclosed_quantity | |
| `SL-M` | ...plus `trigger_price` | disclosed_quantity, market_protection | `price` is `0` or omitted |

### Variety reference

| variety | Behavior | Extra fields |
| --- | --- | --- |
| `regular` | Standard day order | — |
| `amo` | After-market order, placed outside market hours | — |
| `co` | Cover order with mandatory stoploss | `squareoff`, `stoploss`, `trailing_stoploss` |
| `iceberg` | Sliced large order | `iceberg_legs` (2–10), `iceberg_quantity` |
| `auction` | Auction participation | `auction_number` |

### Product guide

| product | Applicable to |
| --- | --- |
| `CNC` | Equity delivery |
| `MIS` | Intraday equity and F&O |
| `NRML` | F&O carry-forward |
| `MTF` | Margin trading facility (equity) |

### Validity guide

| validity | Meaning | Extra field |
| --- | --- | --- |
| `DAY` | Valid for the day (default) | — |
| `IOC` | Immediate or cancel | — |
| `TTL` | Time-to-live in minutes | `validity_ttl` (1–365) |

### Field constraints

- `quantity` must be a positive integer
- `price` is required for `LIMIT` and `SL`; omit or `0` for `MARKET` and `SL-M`
- `trigger_price` is required for `SL` and `SL-M`
- `disclosed_quantity` cannot exceed `quantity`
- `market_protection` allowed only for `MARKET` and `SL-M`; values: `-1` or `0..100`
- `iceberg_legs`: `2..10`
- AMO cannot be combined with iceberg
- CO orders require `squareoff` and `stoploss`; `trailing_stoploss` is optional

**Never send `tag`, `tags`, or `attribution`.** The backend injects attribution metadata.

## Order placement

### preview_order(...)

Dry-run order validation without placing.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| strategy_run_id | `str` | yes | Run identifier |
| order | `dict` | yes | Order payload dict |
| metadata | `dict` | no | Signal/context metadata |

```python
preview = client.preview_order(
    "run_basic_equity_001",
    {"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
     "variety": "regular", "product": "CNC", "order_type": "MARKET", "quantity": 1},
)
```

**Response:** margin estimate, charges breakdown, validation warnings.

### place_order(...)

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| strategy_run_id | `str` | yes | Run identifier |
| order | `dict` | yes | Order payload |
| idempotency_key | `str` | yes | Deterministic key to prevent duplicates |
| metadata | `dict` | no | Signal/context metadata |
| safety_token | `str` | no | Token from `safety_check()` (managed lifecycle) |
| session_nonce | `str` | no | From `claim_session()` (raw-client managed session) |

```python
result = client.place_order(
    "run_basic_equity_001",
    {"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
     "variety": "regular", "product": "CNC", "order_type": "MARKET", "quantity": 1},
    idempotency_key="run_basic_equity_001:entry:001",
)
```

**Response:** order acceptance payload including `order_id` and `status`.

**Idempotency:** Use deterministic keys per intent (e.g. `"run_id:entry:symbol:bar_index"`). Replaying the same key returns the original result without placing a duplicate order.

### cancel_order(...) / modify_order(...) / inspection

```python
client.cancel_order("run_basic_equity_001", "250508000001")
client.modify_order("run_basic_equity_001", "250508000001", {"quantity": 2})
orders = client.list_orders("run_basic_equity_001")
trades = client.list_trades("run_basic_equity_001")
```

## Basket execution

Place multiple orders as a single basket.

### preview_basket(...)

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| strategy_run_id | `str` | yes | Run identifier |
| orders | `list[dict]` | yes | List of order payloads |
| metadata | `dict` | no | Context metadata |
| all_or_none | `bool` | no | If `True`, all legs must be valid (default `False`) |

### place_basket(...)

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| strategy_run_id | `str` | yes | Run identifier |
| orders | `list[dict]` | yes | List of order payloads |
| idempotency_key | `str` | yes | Deterministic basket key |
| metadata | `dict` | no | Context metadata |
| all_or_none | `bool` | no | Require all legs to succeed |
| dry_run | `bool` | no | Preview only, do not place |
| safety_token | `str` | no | From `safety_check()` |
| session_nonce | `str` | no | From `claim_session()` |

```python
from kite_algo_worker import equity_market_order

basket = client.place_basket(
    "run_basic_equity_001",
    [
        equity_market_order("INFY", "BUY", 1),
        equity_market_order("RELIANCE", "BUY", 1),
    ],
    idempotency_key="run_basic_equity_001:basket:001",
    all_or_none=True,
)
```

**Response:** basket acceptance with per-leg results.

**Guardrails:**
- `all_or_none=True` means the entire basket is rejected if any leg fails validation
- `dry_run=True` previews the basket without placing any orders — useful for live operations
- Basket keys should be as deterministic as single-order keys

## Safety and protection

### safety_check(strategy_run_id)

Gatekeeper for guarded trade actions. Must be called before placing orders in managed-lifecycle or session-aware flows.

**Response fields:**

| Field | Type | Description |
| --- | --- | --- |
| `can_trade` | `bool` | Whether the run is allowed to trade |
| `safety_token` | `str\|null` | Opaque token; pass to the guarded action if present |
| `token_expires_at` | `str\|null` | ISO 8601 expiry of the safety token |
| `blocking_reasons` | `list[str]` | Human-readable reasons why trading is blocked |
| `run_status` | `str` | Current run status |
| `generic_protection` | `dict` | Backend protection state |
| `options_protection` | `dict` | Options-specific protection state |
| `evaluated_at` | `str` | ISO 8601 timestamp |

### Safety flow

```python
# Raw-client flow
claim = client.claim_session("run_managed_001")
safety = client.safety_check("run_managed_001")

if not safety.can_trade:
    raise SystemExit(f"Blocked: {safety.blocking_reasons}")

client.place_order(
    "run_managed_001",
    equity_market_order("INFY", "BUY", 1),
    idempotency_key="run_managed_001:entry:001",
    safety_token=safety.safety_token,
    session_nonce=claim["worker_session_nonce"],
)
```

**Token expiry:** If a `safety_token` is rejected (expired), reacquire it via a new `safety_check()` call. Do not retry with the stale token.

### BackendProtection objects

```python
from kite_algo_worker import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition

protection = BackendProtection(
    positions=[
        ProtectedPosition(
            symbol="NSE:INFY", product="CNC", side="BUY",
            quantity=1, entry_price=1500.0, stoploss_pct=2.0,
        )
    ],
    basket=BasketProtection(stoploss_pct=4.0),
    operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300),
)
```

**Protection constraints:**
- `product`: `CNC`, `MIS`, or `NRML`
- `side`: `BUY` or `SELL`
- quantities and prices must be positive
- stale-worker limit: `30..86400` seconds
- MIS squareoff buffer: `0..3600` seconds

### update_backend_protection(...) / patch_risk(...)

```python
client.update_backend_protection("run_managed_001", protection, reason="rebalance")
client.patch_risk("run_managed_001", {"stop_loss_pct": 1.0}, reason="volatility adjustment")
```

## Grouped P&L and monitoring

### get_run_pnl(strategy_run_id)

Backend-owned grouped run P&L snapshot.

```python
pnl = client.get_run_pnl("run_managed_001")
print(pnl["totals"]["net_pnl"])
```

**Response fields:**

| Field | Description |
| --- | --- |
| `totals` | `net_pnl`, `gross_pnl`, `charges`, `brokerage`, `taxes` |
| `legs` | Per-leg breakdown (empty for dry_run) |
| `is_stale` | `True` if the backend cannot fully confirm live marks |

`dry_run` returns zero totals and no legs. `paper` returns grouped paper run P&L. `live` returns grouped attributed live P&L with charges and live-leg breakdown.

### stream_run_pnl(strategy_run_id, interval_seconds=1.0)

SSE stream of grouped run-level P&L updates.

```python
for update in client.stream_run_pnl("run_managed_001", interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
```

### list_timeline(strategy_run_id, **params)

Worker-visible run event history.

| Parameter | Type | Description |
| --- | --- | --- |
| `after_cursor` | `int` | Paginate after this cursor |
| `limit` | `int` | Max events to return |
| `event_kind` | `str` | Filter: `"execution"`, `"decision"`, `"protection"` |

```python
timeline = client.list_timeline_snapshot("run_managed_001", limit=20)
for event in timeline.events:
    print(event.event_type, event.summary)
```

### log_decision_event(strategy_run_id, **payload)

Record a worker decision to the backend-owned timeline.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `event_type` | `str` | yes | e.g. `"signal.generated"`, `"risk.updated"` |
| `summary` | `str` | yes | One-line description |
| `details` | `dict` | no | Structured context |
| `related_resource_type` | `str` | no | Linked resource type |
| `related_resource_id` | `str` | no | Linked resource ID |

```python
client.log_decision_event(
    "run_managed_001",
    event_type="signal.generated",
    summary="EMA crossover detected on INFY 5min",
    details={"symbol": "INFY", "fast_ema": 1450.1, "slow_ema": 1448.3},
)
```

### stream_timeline(strategy_run_id, **params)

SSE stream of timeline events.

```python
for event in client.stream_timeline("run_managed_001"):
    print(event["event_type"], event.get("summary"))
```

## Options workflows

Worker options flows use the `client.options` namespace and resolver helpers. This keeps option selection backend-backed and avoids ad-hoc strike logic in every worker.

### Options session and market data

```python
client.options.ensure_session("NIFTY")
expiries = client.options.list_expiries("NIFTY")
chain = client.options.get_chain("NIFTY", expiry="current_week")
greeks = client.options.get_greeks("NIFTY", expiry="nearest")
```

### Resolver helpers

Backend-backed helpers that resolve contracts into `OptionExecutionLeg` payloads.

```python
from kite_algo_worker import (
    resolve_option_leg,
    resolve_offset_leg,
    resolve_delta_leg,
    resolve_spread,
    SpreadLegSelection,
    SpreadSpec,
)

# Single leg by explicit selection
leg = resolve_option_leg(
    client.options, underlying="NIFTY", product="MIS", expiry="current_week",
    selection={"option_type": "CE", "strike": 25000},
    transaction_type="BUY", lots=1,
)

# By offset from ATM
leg = resolve_offset_leg(
    client.options, underlying="NIFTY", product="MIS", expiry="current_week",
    option_type="CE", offset="ATM", transaction_type="BUY",
)

# By delta target
leg = resolve_delta_leg(
    client.options, underlying="NIFTY", product="MIS", expiry="current_week",
    option_type="CE", delta_target=0.30, transaction_type="SELL",
)

# Spread
spread = resolve_spread(
    client.options, underlying="NIFTY", product="MIS",
    spec=SpreadSpec(
        spread_type="vertical_call_spread", expiry="current_week",
        legs=[
            SpreadLegSelection(selection={"option_type": "CE", "moneyness": "ATM"}, transaction_type="SELL"),
            SpreadLegSelection(selection={"option_type": "CE", "moneyness": "+1_strike"}, transaction_type="BUY"),
        ],
    ),
)
```

**Important:** The resolver helpers construct payloads but do NOT auto-create runs or place trades. You still own the run creation and entry/exit decisions explicitly.

### Options run lifecycle

```python
# Create
option_run = client.options.create_run(
    strategy_name="Bull Call Spread",
    product="MIS",
    legs=[leg.model_dump(exclude_none=True) for leg in spread],
    protection={"stoploss_pct": 2.0},
)

# Preview entry
preview = client.options.preview_entry(option_run["strategy_run_id"])

# Enter (safety-checked)
enter_result = client.options.enter(
    option_run["strategy_run_id"],
    safety_token=safety.safety_token,
    session_nonce=run.session_nonce,
)

# Protection state
state = client.options.get_protection_state(option_run["strategy_run_id"])

# Exit
client.options.exit(option_run["strategy_run_id"])
```

## GTT helpers

Account-scoped GTT (Good Till Triggered) helpers. These are worker-safe backend passthrough calls — they do not create local state or hidden automation.

### place_gtt(...)

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `type` | `str` | yes | `"single"` or `"two-leg"` |
| `condition` | `dict` | yes | Trigger condition (exchange, tradingsymbol, trigger_values, last_price) |
| `orders` | `list[dict]` | yes | Orders to place when triggered |

**Response:** `trigger_id` (int).

```python
result = client.place_gtt_snapshot({
    "type": "single",
    "condition": {
        "exchange": "NSE", "tradingsymbol": "INFY",
        "trigger_values": [1450.0], "last_price": 1448.0,
    },
    "orders": [{
        "exchange": "NSE", "tradingsymbol": "INFY",
        "transaction_type": "BUY", "quantity": 1,
        "order_type": "LIMIT", "product": "CNC", "price": 1450.0,
    }],
})

trigger = client.get_gtt_snapshot(result.trigger_id)
print(trigger.id, trigger.status)
```

### list / get / modify / delete

```python
triggers = client.list_gtts()
client.get_gtt(123)
client.modify_gtt(123, {"condition": {"trigger_values": [1460.0], "last_price": 1455.0}})
client.delete_gtt(123)
```

## Exits and recovery

### exit_run(...)

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| strategy_run_id | `str` | yes | Run identifier |
| reason | `str` | no | Human-readable reason |
| idempotency_key | `str` | no | Deterministic exit key |
| dry_run | `bool` | no | Preview exit without placing orders (safe for live) |

```python
client.exit_run(
    "run_basic_equity_001",
    reason="target reached",
    idempotency_key="run_basic_equity_001:exit:target",
)
```

For live runs: use `dry_run=True` first to preview the exit without sending live orders.

### Recovery helpers

Small polling helpers for warmup and recovery flows.

```python
from kite_algo_worker import wait_for_history, warmup_history, wait_for_terminal_order_state

# Poll until historical candles are available
history = wait_for_history(client, "NSE:INFY", timeframe="day", attempts=10)

# Poll until minimum candle count for indicator warmup
history = warmup_history(client, "NSE:INFY", timeframe="5minute", min_candles=50)

# Poll until an order reaches a terminal state
order = wait_for_terminal_order_state(client, "run_basic_equity_001", "250508000001")
```

Additional helpers: `wait_for_fresh_candle(...)`, `wait_for_quotes(...)`, `preview_then_place_order(...)`.

### Typical restart flow

1. `get_run(strategy_run_id)` — recover backend-owned run state
2. `warmup_history(...)` or `get_historical_candles(...)` — rebuild data context
3. rebuild local indicator state from historical candles
4. reconnect `stream_ticks(...)`, `stream_candles(...)`, or `stream_run_pnl(...)`

## Order builder helpers

Thin convenience functions that produce standard order payloads. Use these instead of hand-writing full order dicts.

### Equity

```python
from kite_algo_worker import equity_market_order

equity_market_order("INFY", "BUY", 1)                         # CNC, NSE, regular
equity_market_order("INFY", "BUY", 1, product="MIS")           # intraday
equity_market_order("INFY", "BUY", 1, variety="amo")           # after-market
```

### Generic builders

```python
from kite_algo_worker import market_order, limit_order, sl_order, sl_m_order

market_order("NSE", "INFY", "BUY", "CNC", 1)
limit_order("NSE", "INFY", "BUY", "CNC", 1, price=1450.0)
sl_order("NSE", "INFY", "BUY", "CNC", 1, price=1450.0, trigger_price=1445.0)
sl_m_order("NSE", "INFY", "BUY", "CNC", 1, trigger_price=1445.0)
```

### Options and AMO

```python
from kite_algo_worker import option_market_order, amo_limit_order, amo_market_order

option_market_order("NIFTY26MAY25000CE", "BUY", 75, product="NRML")
amo_limit_order("NSE", "INFY", "BUY", "CNC", 1, price=1450.0)
amo_market_order("NSE", "INFY", "BUY", "CNC", 1)
```

### Full control

```python
from kite_algo_worker import OrderBuilder

OrderBuilder("NSE", "INFY", "BUY", "CNC", 1) \
    .market() \
    .regular() \
    .build()
```

All helpers accept optional fields: `disclosed_quantity`, `market_protection`, `validity`, `validity_ttl`, `autoslice`, and variety-specific fields.

## Scenario-driven examples

Start with these canonical examples to understand the different worker patterns:

| Example | Pattern | File |
| --- | --- | --- |
| Basic equity | Raw-client baseline | `examples/basic_equity_worker.py` |
| Managed lifecycle | Session-aware worker | `examples/managed_run_worker.py` |
| Mean reversion | Indicator-driven strategy | `examples/mean_reversion_worker.py` |
| Options spread | Options workflow | `examples/option_basket_worker.py` |
| Live exit preview | Operational safety | `examples/live_exit_preview.py` |
| Signal-driven | External decision integration | `examples/signal_driven_worker.py` |

All examples default to `dry_run` mode and require explicit live acknowledgement (`KITE_ALGO_ENABLE_LIVE=1`). They are runnable against a local or remote Kite Algo backend using environment variables.

## Release conventions

- App/product tags: `vX.Y.Z`
- SDK package tags: `kite-algo-worker-vX.Y.Z`

The SDK has its own semantic version stream. Backend changes do not force SDK releases.

### Publishing a new SDK release

1. Update `sdk/python/pyproject.toml`
2. Run `python scripts/check_worker_sdk_version_refs.py`
3. Run the SDK test suite
4. Build from `sdk/python` and run `twine check`
5. Create and push the matching `kite-algo-worker-vX.Y.Z` tag

The repo uses GitHub Actions trusted publishing for PyPI.
