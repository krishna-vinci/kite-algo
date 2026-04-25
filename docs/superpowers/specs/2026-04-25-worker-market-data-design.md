# Worker Market Data V1 Design

Date: 2026-04-25

## Goal

Make external algo workers easy to use for robust realtime strategy development without making workers own broker websocket infrastructure.

The worker SDK should give strategy code the same simple platform contract for market data that it now has for execution, grouped P&L, grouped exits, and lifecycle state.

The v1 scope is deliberately generic:

- ticker/instrument resolution
- quote/tick snapshots
- runtime-backed tick streaming
- candle history/current-candle snapshots
- candle streaming
- combined market snapshot bundles for ergonomic strategy loops

Options-specific chain selection, strike discovery, Greeks, IV, and spread builders are deferred to a later option worker layer inside the same SDK.

## Current architecture context

The Go `market-runtime/` is the only intended broker websocket owner. It owns Kite websocket connections, deduplicates token subscriptions globally, handles shard allocation, writes latest ticks to Redis, and publishes tick events.

Existing contracts already available to reuse:

- latest tick Redis key: `market:tick:{instrument_token}`
- tick pub/sub channel: `market:ticks`
- runtime owner subscription API: `PUT /internal/market-runtime/subscriptions/{owner_id}`
- candle aggregator Redis/current-candle support and realtime candle pub/sub
- Python bridge in `broker_api/market_runtime_client.py`
- worker auth/lifecycle router in `api/routers/algo_workers.py`
- SDK client in `sdk/python/kite_algo_worker/client.py`

The missing piece is a worker-authenticated facade over those runtime-backed contracts, plus SDK methods that make strategy code simple.

## Design principle

Workers own decisions. Kite Algo owns infrastructure and source-of-truth contracts.

Workers must not:

- connect to broker websockets directly
- import backend internals
- read Redis directly
- query database tables directly
- manually manage market-runtime owner leases

Workers should only call `/api/algo-workers/worker/*`, preferably via the SDK.

## V1 scope

### 1. Ticker/instrument resolution

Workers need a simple way to convert human strategy inputs into stable market-data identifiers.

Worker API endpoints:

```text
GET  /api/algo-workers/worker/market/instruments/resolve?symbol=NSE:INFY
GET  /api/algo-workers/worker/market/instruments/search?query=INFY&exchange=NSE&limit=20
POST /api/algo-workers/worker/market/instruments/resolve
```

SDK methods:

```python
client.resolve_ticker("NSE:INFY")
client.search_tickers("INFY", exchange="NSE", limit=20)
client.resolve_tickers(["NSE:INFY", "BSE:RELIANCE"])
```

Returned shape:

```json
{
  "symbol": "NSE:INFY",
  "instrument_token": 408065,
  "exchange": "NSE",
  "tradingsymbol": "INFY",
  "name": "INFOSYS",
  "instrument_type": "EQ",
  "segment": "NSE",
  "tick_size": 0.05,
  "lot_size": 1,
  "expiry": null,
  "strike": null
}
```

Symbols use `EXCHANGE:TRADINGSYMBOL`. Raw integer `instrument_token` inputs are also accepted in market-data calls.

### 2. Quote snapshots

Quote snapshots return latest runtime-backed tick state for one or more instruments.

Worker API endpoint:

```text
POST /api/algo-workers/worker/market/quotes
```

Request:

```json
{
  "symbols": ["NSE:INFY"],
  "instrument_tokens": [408065],
  "mode": "quote"
}
```

SDK:

```python
quotes = client.get_quotes(["NSE:INFY", 408065], mode="quote")
```

Response includes normalized tick fields available from the runtime cache:

```json
{
  "quotes": [
    {
      "instrument_token": 408065,
      "symbol": "NSE:INFY",
      "exchange": "NSE",
      "tradingsymbol": "INFY",
      "mode": "quote",
      "last_price": 1462.0,
      "change": 0.42,
      "ohlc": {"open": 1450.0, "high": 1468.0, "low": 1445.0, "close": 1455.9},
      "volume": 123456,
      "last_quantity": 10,
      "average_price": 1458.2,
      "buy_quantity": 1000,
      "sell_quantity": 900,
      "last_trade_time": "2026-04-25T09:30:00+05:30",
      "exchange_timestamp": "2026-04-25T09:30:01+05:30",
      "received_at": "2026-04-25T04:00:01.120000+00:00",
      "is_stale": false,
      "age_ms": 420
    }
  ],
  "missing": []
}
```

The endpoint reads runtime cache first. It may register/refresh a short-lived worker owner subscription when requested symbols are not already warm.

### 3. Tick stream

Tick streams expose runtime-backed updates to workers as SSE. SSE keeps the SDK simple and avoids making external workers implement websocket protocols.

Worker API endpoint:

```text
GET /api/algo-workers/worker/market/ticks/stream?symbols=NSE:INFY&mode=quote
```

POST-first streaming can be added later if query-string length becomes a real issue. V1 should also support comma-separated symbols and tokens for practical use.

SDK:

```python
for event in client.stream_ticks(["NSE:INFY", 408065], mode="quote"):
    print(event["ticks"])
```

Behavior:

- backend resolves symbols to tokens
- backend creates an owner id such as `worker:{token_id}:market:{stream_id}`
- backend registers desired runtime subscriptions
- backend yields an initial snapshot event
- backend filters `market:ticks` events to requested tokens
- backend refreshes owner lease during the stream
- backend deletes owner subscription on clean disconnect
- if cleanup fails, runtime owner lease expiry still removes stale subscriptions

SSE events:

```text
event: snapshot
data: {"ticks": [...], "missing": []}

event: ticks
data: {"ticks": [...]}

: heartbeat
```

### 4. Candle snapshots

Candle snapshots are needed for candle-driven strategies and indicator calculation.

Worker API endpoint:

```text
GET /api/algo-workers/worker/market/candles?symbol=NSE:INFY&interval=5minute&lookback=100
```

SDK:

```python
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=100)
current = client.get_current_candle("NSE:INFY", interval="5minute")
```

Response:

```json
{
  "symbol": "NSE:INFY",
  "instrument_token": 408065,
  "interval": "5minute",
  "candles": [
    {
      "ts": "2026-04-25T09:15:00+05:30",
      "open": 1450.0,
      "high": 1460.0,
      "low": 1448.0,
      "close": 1458.0,
      "volume": 10000,
      "oi": null,
      "is_complete": true
    }
  ],
  "current": {
    "ts": "2026-04-25T09:30:00+05:30",
    "open": 1458.0,
    "high": 1462.0,
    "low": 1457.0,
    "close": 1461.5,
    "volume": 1200,
    "oi": null,
    "is_complete": false
  }
}
```

Implementation should reuse existing DB-backed candle query paths and Redis current-candle/cache paths rather than creating a new candle store.

### 5. Candle stream

Candle streams expose forming and closed candle updates.

Worker API endpoint:

```text
GET /api/algo-workers/worker/market/candles/stream?symbol=NSE:INFY&interval=5minute
```

SDK:

```python
for candle_event in client.stream_candles("NSE:INFY", interval="5minute"):
    if candle_event["candle"]["is_complete"]:
        handle_closed_candle(candle_event["candle"])
```

Behavior:

- backend resolves symbol/token
- backend ensures runtime subscription is active for candle aggregation if needed
- backend yields current candle snapshot
- backend relays updates from the existing realtime candle pub/sub channel
- backend sends heartbeat comments when idle

### 6. Combined market snapshot bundle

For easy strategy startup and loop code, add one generic bundle endpoint. It returns raw data only; it does not calculate strategy signals.

Worker API endpoint:

```text
POST /api/algo-workers/worker/market/snapshot
```

SDK:

```python
snapshot = client.get_market_snapshot(
    symbols=["NSE:INFY", "NSE:RELIANCE"],
    candles=[
        {"symbol": "NSE:INFY", "interval": "5minute", "lookback": 50},
        {"symbol": "NSE:RELIANCE", "interval": "15minute", "lookback": 20},
    ],
    mode="quote",
)
```

Response:

```json
{
  "quotes": [...],
  "candles": [...],
  "missing": [],
  "updated_at": "2026-04-25T04:00:01.120000+00:00"
}
```

This is the convenience layer that keeps workers pleasant without turning the backend into an indicator framework.

## Explicit non-goals for v1

V1 does not include:

- option chain discovery
- ATM/ITM/OTM selection helpers
- expiry helpers
- Greeks, IV, max pain, PCR, or option analytics
- spread/iron-condor/straddle builders
- RSI/EMA/VWAP or other indicator helpers
- strategy-specific signal fields

Those can be built by workers from the raw tick/candle primitives or added later in specialized modules.

## Later stage: option worker layer

Options should remain in the same SDK package but in a separate namespace/module so the generic worker contract stays simple.

Possible later SDK shape:

```python
from kite_algo_worker import KiteAlgoWorkerClient
from kite_algo_worker.options import OptionSelector, build_credit_spread

chain = client.options.get_chain("NIFTY", expiry="nearest")
legs = OptionSelector(chain).around_atm(width=100).short_strangle()
orders = build_credit_spread(legs, quantity=50)
```

Possible later worker API namespace:

```text
/api/algo-workers/worker/options/chain
/api/algo-workers/worker/options/expiries
/api/algo-workers/worker/options/strikes
```

This keeps one worker SDK and one auth model while avoiding option-specific complexity in the generic market-data surface.

## Error handling and safety

All worker market-data endpoints should use existing worker bearer-token auth and action gating.

Suggested action gates:

- `market:read` for snapshots/search/candles
- `market:stream` for tick/candle streams

Initial tokens may include these actions by default only if that matches current worker-token policy; otherwise admin token creation should expose them explicitly.

Error behavior:

- invalid symbol: `404` or returned in `missing`, depending on single vs batch endpoint
- invalid interval/mode: `422`
- runtime unavailable: controlled `503` for snapshots, SSE `event: error` for streams
- Redis/candle cache unavailable: controlled `503` or stale/missing data with `is_stale=true`, depending on endpoint
- stream disconnect: best-effort runtime owner cleanup

Data freshness:

- include `received_at`, `exchange_timestamp`, `age_ms`, and `is_stale`
- make staleness threshold configurable, defaulting to a conservative small value for live ticks
- never silently label stale data as realtime

## Worker disconnect behavior

External workers are the strategy decision engines. If a worker process stops, new strategy decisions stop until it restarts. Broker orders and positions that already exist continue to exist, and the backend should continue ingesting fills, projecting live positions, and exposing grouped run P&L.

V1 should make this behavior explicit and restart-safe rather than pretending the backend can continue a custom strategy by itself:

- missed heartbeats should make the run visibly stale/unhealthy
- market-data stream disconnects should release runtime subscriptions through best-effort cleanup and runtime owner lease expiry
- restarted workers should be able to recover by reusing the same `strategy_run_id`, calling `get_run(...)`, `get_run_pnl(...)`, `get_candles(...)`, and reconnecting streams
- backend-owned emergency failover policies such as `cancel_open_orders`, `exit_positions`, or hard stop/target enforcement are valuable, but should be a later explicit safety feature instead of hidden v1 behavior

The v1 implementation should document this clearly and avoid silently auto-exiting live positions unless the worker explicitly calls the grouped exit API or a future configured failover policy says to do so.

## Implementation units

The implementation should be split into small units:

1. `worker_market_data` service/helper module
   - resolve inputs into instrument metadata
   - read runtime tick snapshots
   - manage owner subscriptions for worker streams
   - normalize quote payloads

2. Worker router additions in `api/routers/algo_workers.py` or a new included router
   - keep the existing worker auth model
   - expose market-data endpoints under `/worker/market/*`

3. SDK additions
   - `resolve_ticker`, `search_tickers`, `resolve_tickers`
   - `get_quotes`, `stream_ticks`
   - `get_candles`, `get_current_candle`, `stream_candles`
   - `get_market_snapshot`

4. Documentation and examples
   - update `docs/algo-worker-development-guide.md`
   - update `sdk/python/README.md`
   - add a simple realtime mean-reversion/candle example

5. Tests
   - worker API auth and payload tests
   - SDK request and SSE parsing tests
   - resolver behavior tests
   - runtime unavailable / stale-data tests

## Testing strategy

Minimum automated coverage:

- ticker resolver accepts `EXCHANGE:TRADINGSYMBOL` and raw tokens
- quote snapshot returns normalized data and missing tokens
- tick stream sends snapshot, tick events, heartbeat, and cleanup
- candle query returns historical/current shape
- candle stream parses SSE in SDK
- invalid mode/interval validation
- runtime unavailable returns controlled error
- worker token without market action cannot access market endpoints

Manual/live verification:

- resolve `NSE:INFY`
- request quote snapshot and confirm runtime cache fields
- stream ticks for one liquid equity during market hours
- request `5minute` candles and confirm latest/current candle shape
- run a sample paper worker using stream ticks + order intent + run P&L

## Success criteria

The feature is successful when a worker can implement a non-option realtime strategy with only SDK calls:

```python
client.resolve_ticker("NSE:INFY")
client.get_candles("NSE:INFY", interval="5minute", lookback=50)
for tick in client.stream_ticks(["NSE:INFY"], mode="quote"):
    # decide
    # place_order(...)
    # monitor get_run_pnl(...)
```

No worker should need broker websocket code, Redis access, database access, or backend internal imports.
