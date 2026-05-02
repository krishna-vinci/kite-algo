# Go fit assessment for kite-algo

## Executive summary

Go is **not strictly needed right now** for this app.

After reviewing the codebase, the best conclusion is:

- keep **Python** as the main application language
- consider **Go only for a narrow real-time data-plane service**
- the strongest Go candidates are:
  - market data ingestion
  - websocket subscription aggregation and fan-out
  - candle aggregation
  - possibly order-update intake as part of the same feed service

Everything else in the repo is still better served by Python for now, especially:

- strategy logic
- alert/protection engines
- request/response REST APIs
- DB-heavy workflows
- options analytics already accelerated with `numpy` + `numba`

If you adopt Go, the recommended approach is **hybrid**, not rewrite.

Updated shape:

- **Python Trading Core API**
- **Python Algo Worker**
- **Market Runtime** as its own boundary
- consider **Go for Market Runtime specifically**, not for the whole app

---

## Current architecture

This repository is a Python-first trading platform built around a single FastAPI process that also owns multiple long-lived runtime services.

### Main startup/orchestration layer

File: `main.py`

The app startup currently initializes and coordinates:

- database/bootstrap logic
- system Kite token resolution / refresh flow
- `WebSocketManager`
- `AlertsEngine`
- `PositionProtectionEngine`
- `CandleAggregator`
- options session infrastructure
- Redis / Postgres / Meilisearch integrations

So the monolith is doing both:

- **control-plane work**: auth, REST endpoints, settings, orchestration
- **data-plane work**: live feed ingestion, state mutation, streaming fan-out

That mixed responsibility is the main reason Go could help in a limited area.

---

## Broker SDK comparison

Sources reviewed:

- Go SDK: `github.com/zerodha/gokiteconnect/v4`
- Python SDK: `pykiteconnect v4`

### Python SDK fit

The Python SDK already matches how this repo is written:

- `KiteConnect` is natural to create per request/session
- `KiteTicker` supports callback-based streaming
- `KiteTicker.connect(threaded=True)` supports threaded mode
- reconnect callbacks and order-update callbacks are available

That aligns with existing code in:

- `broker_api/broker_api.py`
- `broker_api/kite_orders.py`
- `broker_api/websocket_manager.py`
- `broker_api/candle_aggregator.py`

### Go SDK fit

The Go SDK also supports the key broker capabilities needed here:

- session generation / auth
- REST methods for orders, positions, holdings, quotes, historical data
- ticker callbacks
- reconnect handling
- order update callbacks
- configurable HTTP client / timeout behavior

### Practical conclusion from SDK docs

There is **no SDK capability gap** forcing a move to Go.

So the decision should be based on:

- concurrency needs
- runtime isolation
- operational simplicity under feed load
- whether the current Python hot path is becoming a bottleneck

Not on broker library support.

---

## Repo hotspot analysis

## 1) `broker_api/websocket_manager.py` — strongest Go candidate

This file is the clearest place where Go fits.

It currently handles:

- a long-lived `KiteTicker`
- `threaded=True` execution
- callback-thread to asyncio handoff
- per-client websocket connection state
- subscription aggregation across clients
- tick cache maintenance in `latest_ticks`
- Redis overlay writes
- database updates from live tick flow
- order-update persistence
- real-time position updates
- tick fan-out to frontend clients

Why this matters:

- it is highly concurrent
- it is stateful
- it is latency-sensitive
- it mixes broker I/O, cache writes, DB side effects, and frontend delivery in one place

This is exactly the kind of service Go is good at.

## 2) `broker_api/candle_aggregator.py` — very good Go candidate

This component:

- opens another `KiteTicker`
- runs threaded callbacks
- aggregates multiple intervals in memory
- writes forming candles to Redis
- publishes/persists completed candles

That is a classic stream-processing workload.

If you build a Go sidecar, this should either:

- move with the websocket/tick service, or
- be the first limited extraction if you want a smaller experiment

## 3) Dual ticker ownership is a structural smell

Right now the repo has at least two different long-lived ticker consumers:

- `broker_api/websocket_manager.py`
- `broker_api/candle_aggregator.py`

That suggests the app would benefit from a cleaner market-data boundary regardless of language.

Go would help here, but even in Python this is a sign that the market-data path should eventually be consolidated.

## 4) `broker_api/options_sessions.py` — hot path, but not first Go target

This code does periodic options session computation and already uses:

- `asyncio.to_thread`
- `numpy`
- `numba`
- vectorized Black-76 calculations from `broker_api/options_greeks.py`

Important detail:

- `broker_api/options_greeks.py` already uses `@njit(..., nogil=True)`

That means the heaviest numerical work is already pushed closer to compiled-speed execution.

So while this path is performance-sensitive, it is **not the best first reason to introduce Go**.

## 5) `alerts/engine.py` and `strategies/indexstoploss/index_stoploss_algo.py`

These engines run frequent evaluation loops (~500 ms) and depend on:

- `latest_ticks`
- DB refreshes
- strategy state
- order-execution orchestration

These are important, but they are mostly **business-rule engines**, not pure infrastructure services.

They should stay in Python first.

## 6) `strategies/momentum.py` and `broker_api/performance_logic.py`

These are mostly:

- DB-heavy
- Redis-heavy
- broker REST-heavy
- business/reporting oriented

These do **not** show a strong reason for Go.

## 7) Streaming endpoints are tied to the monolith

Streaming currently exists in multiple places:

- `broker_api/options_router.py` (SSE / websocket session streaming)
- `broker_api/candles_api.py` (SSE candles)
- `broker_api/kite_orders.py` (SSE positions/orders)
- `api/routers/marketwatch.py` and frontend marketwatch flow

This means the API server is also responsible for long-lived stream delivery. That is fine for now, but if client counts grow, Go would help most on the **market-data-facing streams**, not necessarily on business SSE endpoints.

---

## Where Go fits best

## Best fit: market data gateway / feed service

Recommended Go service responsibilities:

- own the broker websocket connection
- manage reconnects
- aggregate token subscriptions across clients/consumers
- maintain in-memory latest tick state
- publish ticks to Redis / Streams / NATS / gRPC
- optionally serve websocket fan-out directly
- ingest order updates from the same ticker channel

Why this is the best fit:

- high-concurrency
- mostly infrastructure logic
- long-lived connections
- low business-rule density
- clean service boundary

## Very good fit: candle aggregation

A Go service can also own:

- tick-to-candle reduction
- interval bucket state
- Redis current/latest candle updates
- completed candle publication
- optional persistence enqueueing

This is likely the easiest high-value extraction.

## Good fit later: real-time quote/session cache service

If the app grows, a Go service could also centralize:

- hot LTP cache
- quote normalization
- fan-out to multiple downstream Python services

But this should come after the main feed/candle boundary is proven.

---

## Where Python should stay

## 1) FastAPI APIs and orchestration

Keep in Python:

- `main.py`
- API routers under `api/`
- existing FastAPI request/response flows

Reason:

- already idiomatic
- not the main bottleneck
- easy to evolve quickly

## 2) Strategy engines and risk logic

Keep in Python:

- `alerts/engine.py`
- `strategies/indexstoploss/*`
- `strategies/momentum.py`
- `strategies/strike_selector.py`

Reason:

- rapid iteration matters more than raw concurrency
- business logic is easier to maintain in Python
- many flows combine DB state, broker REST, and domain rules

## 3) Options analytics / quant logic

Keep in Python first:

- `broker_api/options_sessions.py`
- `broker_api/options_greeks.py`

Reason:

- already optimized with `numpy` + `numba`
- high risk to reimplement and validate in Go
- Python is still the strongest ecosystem for this class of code

## 4) DB/business workflows

Keep in Python:

- `broker_api/performance_logic.py`
- large parts of `broker_api/broker_api.py`
- user/session/order orchestration in `broker_api/kite_orders.py`

Reason:

- these are not feed-engine bottlenecks
- migration cost would be high for little gain

---

## Migration options

## Option A — Stabilize the modular monolith first

Best if:

- current production load is still manageable
- there is no hard evidence of Python/event-loop contention
- you want fastest feature velocity

What to do first:

- add better metrics on tick latency
- measure event-loop lag
- measure Redis and DB write latency under live feed load
- measure websocket fan-out latency / connected client count pressure

## Option B — Split API and Algo Worker first

Create:

- `kite-api` for auth, REST, CRUD, settings, order APIs
- `kite-worker` for alerts, protection, strategy execution loops, rebalance runtime

Use:

- **Redis Streams** for commands/events
- **Redis keys/hashes** for hot ephemeral state
- **Postgres** for durable strategy/order state

Avoid using plain Redis pub/sub as the only backbone for important execution.

### Pros

- fixes the immediate issue where algo crash can take down login/API
- much simpler than full microservices
- still keeps one repo and one main data model

### Cons

- does not fully isolate broker websocket/tick runtime if that remains inside API
- restart reconciliation for in-flight orders becomes mandatory

## Option C — Extract only candle aggregation to Go

Pros:

- narrow scope
- easy correctness testing
- useful performance isolation

Cons:

- leaves `WebSocketManager` complexity in Python
- does not solve the biggest concurrency hotspot

## Option D — Separate Market Runtime boundary

Create a dedicated service for:

- broker websocket ownership
- subscription aggregation
- latest tick state
- marketwatch realtime
- candle aggregation
- optional order-update intake

This service can start in Python if needed, but it is the strongest long-term Go candidate.

### Pros

- isolates the actual realtime hot path
- prevents tick/runtime failures from affecting login/auth
- cleanest long-term architecture

### Cons

- more design work than API/worker split alone
- requires clear contracts for hot state and events

## Option E — Extract market-data ingestion + fan-out to Go

Pros:

- highest-value extraction
- cleanest use of Go strengths
- separates data-plane from control-plane

Cons:

- requires an internal protocol / contract
- more moving parts operationally

## Option F — Build one consolidated Go market-data service

This service would absorb both:

- `broker_api/websocket_manager.py`
- `broker_api/candle_aggregator.py`

Long-term this is probably the best hybrid shape.

But it is **too big as a first migration unless you already know Python is the constraint**.

---

## Risks and tradeoffs

## Added system complexity

Hybrid systems add:

- deployment complexity
- service-to-service contracts
- distributed debugging overhead
- more monitoring requirements

If current scale does not justify it, this becomes negative ROI.

## Trading correctness risk

The real-time path is correctness-sensitive:

- subscription semantics
- reconnect semantics
- order update delivery
- tick freshness
- candle accuracy
- duplicate/out-of-order handling

That means a Go migration should be staged carefully.

## Go is not obviously better for the whole app

This codebase is not a pure low-latency matching engine.

A lot of it is:

- product logic
- data shaping
- DB access
- API composition
- strategy experimentation

Python is still the better default for those parts.

## Existing Python optimizations reduce urgency

The repo already uses strong Python-side optimizations in important areas:

- threaded broker websocket mode
- async orchestration
- Redis buffering
- `asyncio.to_thread`
- `numpy`
- `numba`

So “rewrite in Go for speed” is too broad and not well-supported by the codebase review.

---

## Recommendation

### Final recommendation

Use a **hybrid architecture only if you want to isolate the market-data hot path**.

My repo-specific recommendation is:

1. **Do not rewrite the app in Go.**
2. **Keep Python as the main backend and business-logic layer.**
3. **First split API from algo execution.**
4. **Then isolate the Market Runtime boundary.**
5. **Introduce Go for Market Runtime only if its complexity/load justifies it.**
6. If you adopt Go, start with one of these two:
   - **first choice:** market-data ingestion + subscription aggregation + fan-out
   - **second choice:** candle aggregation sidecar

### Confidence assessment

- **Go clearly useful:** realtime ingest/fan-out/candle processing
- **Go maybe useful later:** quote cache / stream gateway
- **Go not needed now:** most REST, auth, strategy, DB-heavy, and quant parts

So the answer to “where does Go fit?” is:

> Go fits best as a **real-time infrastructure sidecar**, not as the main language for this app.

And the answer to “is it needed at all?” is:

> **Not yet, unless your current feed/streaming path is under real load pressure or becoming operationally messy.**

---

## Suggested next experiments

Before building anything in Go, measure these in the current Python system:

- tick ingest rate
- callback-thread to asyncio handoff latency
- flush loop delay in `WebSocketManager`
- Redis overlay write latency
- DB write latency from live-feed paths
- websocket/SSE fan-out latency
- CPU and memory during market hours
- reconnect recovery time
- number of concurrent marketwatch/options/candles stream clients

Before a Go rewrite, first test the simpler operational split:

- move strategy/alert/protection loops into a worker
- keep login/auth in API
- use durable state and replayable commands

If those metrics still show pain in the realtime feed path, build this proof-of-concept next:

### Suggested POC

A **Go market-data sidecar** that:

- connects to `KiteTicker`
- manages subscriptions
- publishes normalized ticks to Redis
- optionally computes candles
- exposes health/status metrics

Keep Python unchanged except for consuming the new stream.

That will tell you very quickly whether Go is actually helping this app.
