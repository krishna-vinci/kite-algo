# Websocket Market Runtime Specification

Last updated: 2026-04-05

## 1) Purpose

The websocket runtime will become the only owner of live Kite market-data websocket infrastructure.

It will present a **single unified market-data service** to:

- frontend clients
- Python APIs
- Python algos
- options sessions
- alerts
- protection engines
- candle aggregation
- real-time position/PnL consumers

## 2) Why this exists

The current Python websocket usage is fragmented:

- `WebSocketManager` owns one ticker path
- `CandleAggregator` owns another ticker path
- alerts and protection engines directly manipulate websocket internals
- multiple modules depend on in-process `latest_ticks`

This makes the system harder to reason about, harder to scale, and more fragile under reconnects or main-app stalls.

The new runtime fixes that by making websocket infrastructure:

- isolated
- centralized
- explicit
- documented

## 3) Service boundary

### Go runtime owns

- Kite websocket connections
- subscription aggregation
- mode aggregation
- reconnect/resubscribe
- in-memory latest tick cache
- Redis hot snapshot publishing
- backend/frontend market websocket fanout
- candle aggregation from the shared live stream
- raw websocket order-update capture and relay

### Python app owns

- login and token persistence
- order placement and trading REST APIs
- canonical order-event processing
- trade sync and reconciliation
- options computation
- alert logic
- protection logic
- FastAPI

## 4) External usage model

Consumers do **not** think in terms of connection 1/2/3.

They only declare:

- `owner_id`
- desired tokens
- desired mode per token

The runtime decides:

- which hidden shard each token belongs to
- whether a new shard must be opened
- what upstream mode is needed
- how to reconnect and recover state

## 5) Hidden shard model

The runtime may use up to 3 Kite websocket connections.

### Limits

- broker hard limit: **3000 tokens per connection**
- runtime soft limit: **2800 tokens per connection**
- max pooled soft capacity: **8400 tokens**

### Shard activation rules

- start with **1 shard only**
- open shard 2 only when shard 1 would exceed the soft limit
- open shard 3 only when shard 2 would exceed the soft limit
- never expose shard identity to callers

### Placement rules

- token placement should be stable
- avoid continuous rebalancing
- once a token is placed on a shard, keep it there unless a controlled rebalance is required
- prefer deterministic placement rules over constant load-based churn

## 6) Subscription semantics

### Owner-based subscription contract

The runtime stores subscriptions by owner.

Example owners:

- `frontend:marketwatch:user42:layout1`
- `frontend:screener:scanner7`
- `options:NIFTY`
- `alerts`
- `protection:strategy:abc`
- `algo:meanreversion:nifty`

### Rules

- one owner may request many tokens
- many owners may request the same token
- upstream subscription must be deduplicated globally
- removing one owner must not unsubscribe a token still needed by another owner
- the effective upstream mode for a token is the highest requested mode across all owners

### Mode precedence

- `full > quote > ltp`

## 7) No direct broker websocket access outside runtime

After cutover:

- no Python module should create its own `KiteTicker`
- no Python module should call raw broker websocket subscribe/unsubscribe/mode APIs
- no frontend should connect directly to Kite websocket

All such usage must go through the market runtime contract.

## 8) Tick data requirements

The runtime must provide a **rich normalized tick shape**, not just a thin LTP overlay.

Minimum useful fields:

- `instrument_token`
- `mode`
- `last_price`
- `change`
- `ohlc`
- `volume` / `volume_traded`
- `last_quantity`
- `buy_quantity`
- `sell_quantity`
- `average_price`
- `last_trade_time`
- `exchange_timestamp`
- `oi`
- `oi_day_low`
- `oi_day_high`
- `depth` when full mode is available
- `received_at`
- `shard_id`

This is required because options, candles, alerts, and protection logic need more than just LTP.

## 9) Order updates

The runtime should capture websocket order updates and relay them onward, but it should **not** become the final truth-processing layer.

### Go runtime responsibility

- receive websocket order updates
- normalize envelope metadata
- publish/relay to Python reliably

### Python responsibility

- canonical ingest
- dedupe
- downstream processing
- reconciliation with broker trades

## 10) Candle aggregation

Candle aggregation should move under the same market runtime so the system does not keep a separate long-lived `KiteTicker` just for candles.

Candles should be derived from the normalized shared tick stream.

## 11) Token rotation

Python remains responsible for obtaining and persisting the active system access token.

The Go runtime should:

- read the active token from the control plane or database-backed contract
- detect token changes
- rotate websocket sessions cleanly
- rehydrate subscriptions after reconnect

## 12) Operational expectations

The runtime should expose clear health/state such as:

- connected / reconnecting / degraded / exhausted
- active shard count
- token counts per shard
- owner count
- mode counts
- tick freshness / lag
- last token rotation timestamp
- reconnect attempts

## 13) Explicitly out of scope for first implementation

- caller-facing shard selection
- caller-facing priority flags
- manual per-use-case lane management
- custom per-owner reservation systems

The first implementation is a **unified pooled design**, not an exposed priority-routing system.
