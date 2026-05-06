# Websocket Market Runtime Contracts

Last updated: 2026-04-05

This file describes the first-pass contracts that other parts of the repo should target.

## 1) Control-plane API

The Go runtime should expose a small internal control-plane API.

Initial recommendation: internal HTTP.

## 2) Subscription API

### Set or replace owner subscriptions

`PUT /internal/market-runtime/subscriptions/{owner_id}`

Request body:

```json
{
  "tokens": {
    "256265": "full",
    "260105": "ltp"
  }
}
```

Rules:

- this replaces the full desired set for that owner
- omitted tokens are removed for that owner
- upstream effective state is recalculated by the runtime
- owners are expected to be refreshed periodically while the caller connection remains active
- synthetic/internal load-test owners should be deleted explicitly after verification so runtime status returns to the real production baseline

### Delete owner subscriptions

`DELETE /internal/market-runtime/subscriptions/{owner_id}`

### Read owner subscriptions

`GET /internal/market-runtime/subscriptions/{owner_id}`

## 3) Runtime status API

`GET /internal/market-runtime/status`

Suggested response fields:

```json
{
  "status": "healthy",
  "active_shards": 1,
  "soft_limit_per_shard": 2800,
  "hard_limit_per_shard": 3000,
  "owners": 17,
  "effective_tokens": 1422,
  "shards": [
    {
      "shard_id": 1,
      "status": "connected",
      "tokens": 1422,
      "modes": {
        "ltp": 1200,
        "quote": 150,
        "full": 72
      }
    }
  ]
}
```

## 4) Redis hot snapshot contract

### Per-token latest snapshot

Key:

`market:tick:{instrument_token}`

Suggested payload:

```json
{
  "instrument_token": 256265,
  "mode": "full",
  "last_price": 24850.35,
  "change": 0.42,
  "ohlc": {
    "open": 24790.0,
    "high": 24910.0,
    "low": 24720.0,
    "close": 24746.0
  },
  "volume": 0,
  "last_quantity": 0,
  "buy_quantity": 0,
  "sell_quantity": 0,
  "average_price": 0,
  "last_trade_time": null,
  "exchange_timestamp": "2026-04-05T09:45:10+05:30",
  "oi": null,
  "oi_day_low": null,
  "oi_day_high": null,
  "depth": null,
  "received_at": "2026-04-05T04:15:10.120000+00:00",
  "shard_id": 1
}
```

### Runtime status key

Key:

`market:status`

### Owner subscription summary

Key:

`market:owner:{owner_id}`

## Owner lease behavior

The first implementation assumes owner subscriptions are **leased**, not permanent.

- the runtime keeps owner subscriptions in memory
- active clients/proxies should periodically refresh the same owner subscription set
- stale owners may be garbage-collected by the runtime after the lease TTL expires

This reduces long-lived subscription leaks if disconnect cleanup fails.

### Effective token aggregate summary

Key:

`market:token:{instrument_token}:agg`

## 5) Redis stream / pubsub contract

### Tick updates

Recommended stream or pubsub channel:

- `market:ticks`

Payload should contain one or more normalized ticks.

### Order updates

Recommended channel or stream:

- `market:order_updates`

Payload should contain:

- normalized envelope metadata
- raw order update body from broker websocket
- receipt timestamp

Python canonical ingestion can subscribe to this and persist/process it.

### Runtime status changes

Recommended channel:

- `market:status:events`

Use for:

- connected
- reconnecting
- token rotated
- shard opened
- shard exhausted
- degraded

## 6) Frontend websocket contract

The Go runtime exposes the primary client websocket endpoint.

Endpoint:

`GET /ws/market`

For this repo, the implemented route is:

`GET /ws/marketwatch`

Client messages:

```json
{ "action": "set_subscriptions", "owner_id": "frontend:marketwatch:user42:layout1", "tokens": { "256265": "full", "260105": "ltp" } }
```

```json
{ "action": "clear_subscriptions", "owner_id": "frontend:marketwatch:user42:layout1" }
```

Server messages should be explicit event envelopes such as:

```json
{ "type": "status", "status": "connected", "active_shards": 1 }
```

```json
{ "type": "snapshot", "ticks": { "256265": { "instrument_token": 256265, "last_price": 24850.35, "mode": "full" } } }
```

```json
{ "type": "ticks", "ticks": [ { "instrument_token": 256265, "last_price": 24851.05, "mode": "full" } ] }
```

## 7) Python consumer contract

Python services should eventually consume one or both of:

- Redis latest snapshot keys for pull reads
- Redis stream/pubsub for push updates

Examples:

- options sessions read `market:tick:{token}` or in-process cache fed from `market:ticks`
- real-time positions consume `market:ticks`
- alerts/protection read latest snapshots and register subscriptions through the control-plane API
- websocket order updates are relayed through `market:order_updates`, then ingested by Python canonical order-runtime processing

## Legacy compatibility

The Python `/api/ws/marketwatch` route is no longer a live market-data path.

It now only rejects legacy callers and tells them to connect directly to the Go websocket endpoint at `/ws/marketwatch`.

Python `WebSocketManager` is retired as a runtime owner. Backend consumers must use the market-runtime contract instead of any direct broker websocket path.

## 8) Non-goals for first contract version

- no caller-specified shard id
- no caller-specified priority level
- no public broker-specific semantics exposed to clients

The system remains unified from the caller point of view.
