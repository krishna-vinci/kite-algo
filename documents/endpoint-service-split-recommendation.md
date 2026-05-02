# Endpoint service split recommendation

Based on the live OpenAPI spec at `http://192.168.0.128:18777/openapi.json` and the current codebase.

## Live endpoint groups

Current tags and counts:

- `orders` — 29 endpoints
- `Candles` — 17 endpoints
- `Strategies` — 13 endpoints
- `Instruments` — 12 endpoints
- `Alerts` — 11 endpoints
- `Momentum` — 11 endpoints
- `Historical Data` — 7 endpoints
- `Options` — 6 endpoints
- `Authentication` — 5 endpoints
- `Marketwatch` — 3 endpoints
- `Performance` — 3 endpoints
- `Ingestion` — 2 endpoints
- `Market Data` — 2 endpoints
- `User Settings` — 2 endpoints
- `System` — 1 endpoint

---

## Executive recommendation

Do **not** split this into many microservices.

For this repo, the safest and most maintainable target architecture is:

1. **Python Trading Core API**
2. **Python Algo Worker**
3. **Market Runtime service**
4. **Optional Python Data Jobs service** later

This gives you better fault isolation without turning the app into a hard-to-maintain distributed system.

---

## Why the current monolith is risky

Today one FastAPI process owns too many responsibilities:

- login/session bootstrap
- broker websocket lifecycle
- latest tick cache
- candle aggregation
- alerts engine
- position protection engine
- options session management
- REST APIs
- SSE/WebSocket endpoints
- historical ingestion and instruments sync

That means if one startup path or runtime dependency fails, the whole app can fail to start or become unreliable.

This is especially dangerous for your use case because:

- login/session stability affects algos
- market runtime instability affects alerts/strategies
- batch jobs and realtime workloads share one process

---

## Best target architecture

## 1) Python Trading Core API

This stays your main user-facing backend.

### Keep these tags here

- `Authentication`
- `orders`
- `Options`
- `Alerts`
- `Strategies`
- `Momentum`
- `Performance`
- `User Settings`
- `System`

### Why

These are mostly:

- business logic
- request/response APIs
- strategy and risk orchestration
- DB-heavy workflows
- broker REST integrations
- fast-changing product logic

### Notes by tag

#### Authentication
Keep fully in Python.

Endpoints:
- `/api/login_kite`
- `/api/logout_kite`
- `/api/profile_kite`
- `/api/holdings_kite`
- `/api/margins`

These should stay close to session handling and cookies.

#### orders
Keep almost all in Python.

Examples:
- `/api/orders`
- `/api/positions`
- `/api/gtt/triggers*`
- `/api/webhooks/orders/postback`
- `/api/margins/orders`
- `/api/margins/basket`

Reason:

- order placement and strategy logic are tightly coupled
- idempotency and session ownership matter more than raw runtime speed

Possible later move:
- websocket-order-update ingestion can move as part of the Go market runtime, but the order/business APIs should remain Python

#### Options
Keep in Python.

Endpoints:
- `/api/options/sessions`
- `/api/options/session/{underlying}`
- `/api/options/chain/{underlying_symbol}`
- `/api/sse/options/session/{symbol}`

Reason:

- depends on options session logic
- already uses `numpy` + `numba`
- strongly coupled to strategy tooling

#### Alerts
Keep in Python.

Reason:

- business-rule engine
- DB state + trigger state + user action workflow

#### Strategies
Keep in Python.

Reason:

- highly domain-specific
- tightly coupled to orders, positions, options sessions

#### Momentum / Performance
Keep in Python.

Reason:

- DB-heavy, cache-heavy, reporting/business logic

---

## 2) Python Algo Worker

This should own the strategy/risk execution loops that must not take down login or the main API.

### Move runtime responsibilities here

- alerts evaluation loop
- protection/index stoploss execution loop
- momentum/background rebalance execution logic
- strategy state transitions
- reconciliation logic after restart

### Endpoint ownership model

These APIs can still stay publicly exposed from the main API service, but the execution should be delegated to the worker.

#### Backed by worker runtime
- `Alerts`
- `Strategies`
- `Momentum`
- parts of `orders` realtime/reactive flows

### Communication pattern

Do **not** rely on plain Redis pub/sub for important execution.

Recommended:

- **Redis Streams** for commands/events
- **Redis keys/hashes** for hot ephemeral state
- **Postgres** for durable strategy/order state

Examples:

- API writes `pause strategy`, `resume strategy`, `rebalance now`, `arm protection` commands to a stream
- worker consumes with consumer groups
- worker persists state transitions in Postgres
- worker can recover/replay after restart

### Why this exists separately

This solves the immediate pain point:

- algo crash should not break login
- broken strategy loop should not take down auth/UI

---

## 3) Market Runtime service

This is the **best runtime isolation boundary** and the strongest long-term Go candidate.

It should own the realtime infrastructure boundary.

### Primary responsibilities

- single owner of broker websocket connection
- reconnect handling
- token subscription aggregation
- latest tick cache
- Redis publication / internal stream publication
- realtime websocket fan-out
- candle aggregation
- optional websocket order-update intake

### Runtime note

This service can start as Python if needed, but long term it is the best candidate for Go because it is mostly infrastructure/concurrency heavy rather than business-logic heavy.

### Endpoint groups best suited to move here

#### Marketwatch
Best candidate to move.

Endpoints:
- `/api/marketwatch/nifty50/overlay-snapshot`
- websocket marketwatch flow behind frontend

Why:

- directly backed by live tick cache
- tied to websocket ingestion and latest-tick overlay

Keep in Python initially:
- `/api/nifty50`
- `/api/marketwatch/nifty50/finalize-baseline`

Those are more data/admin oriented.

#### Candles realtime side
Very strong candidate to move.

Endpoints:
- `/api/candles/stream/{identifier}`
- `/api/candles/aggregator/start`
- `/api/candles/aggregator/status`
- `/api/candles/aggregator/stop`

Why:

- stream processing workload
- long-lived connections
- in-memory interval state
- strong need for runtime isolation

#### Market Data
Partial candidate.

Endpoints:
- `/api/ltp`
- `/api/quote/ohlc`

These can stay in Python at first, but if you build a Go market runtime, you may eventually move the hot quote/LTP path there or back it entirely from its cache.

#### orders realtime stream sub-area
Only the realtime infrastructure piece is a Go candidate.

Possible later move:
- `/api/orders/events/stream`
- `/api/ws/orders/updates/status`
- `/api/ws/orders/updates/enable`
- `/api/ws/orders/updates/disable`
- `/api/ws/orders/events`

Only move these if they become part of a unified realtime event service.

Do **not** move order placement/modification/cancellation endpoints.

---

## 4) Optional Python Data Jobs service

This should be separated only after the market runtime split, or kept as a worker process if that is simpler.

### Move or isolate these tags later

- `Instruments`
- `Historical Data`
- `Ingestion`
- historical/admin parts of `Candles`

### Why

These endpoints are batch/reference-data oriented and should not compete with market-hour runtime services.

### Candidate endpoints

#### Instruments
- `/api/instruments/sync-and-reindex`
- `/api/instruments/fuzzy-search`

#### Historical Data
- `/api/fetch_historical_data`
- `/api/update_historical_data`
- `/api/fetch_indices_historical_data`
- `/api/update_indices_historical_data`
- `/api/clear_historical_data`
- `/api/historical_data_progress`

#### Ingestion
- `/api/ingest-stock-data`
- `/api/update-nifty50-data`

#### Candles data/admin side
- `/api/candles/{identifier}`
- `/api/candles/{identifier}/coverage`
- `/api/candles/{identifier}/cache`
- `/api/candles/historical/*`
- `/api/candles/ingestion/*`
- `/api/candles/user/watchlist`

---

## What should not be split too early

### Don’t create a service per tag

That would be overkill for this repo.

For example, these should stay together in Python:

- `orders`
- `Options`
- `Alerts`
- `Strategies`

They share too much state and logic.

### Don’t keep multiple broker websocket owners forever

Right now, the codebase already has websocket-heavy realtime logic spread across:

- `broker_api/websocket_manager.py`
- `broker_api/candle_aggregator.py`

Long term, the new Go service should become the **single realtime owner**.

---

## Recommended migration order

## Phase 1 — stabilize without major split

Before moving code:

- keep the public API shape the same
- make startup more fault-tolerant
- ensure noncritical services can fail without blocking login
- separate startup concerns into optional/lazy components
- improve health checks and metrics

Goal:

- login should survive even if candles or ingestion is broken
- algos should not depend on every module starting successfully

## Phase 2 — split API and algo worker

Move execution loops out of the API process first:

- alerts engine runtime
- strategy runtime
- momentum/rebalance execution runtime
- protection runtime

But keep the public REST API shape unchanged.

Important:

- use **Redis Streams**, not plain pub/sub, for important commands/events
- persist execution state in Postgres
- add startup reconciliation for in-flight orders and active strategies

## Phase 3 — extract market runtime

First move:

- broker websocket ownership
- marketwatch realtime overlay
- candle aggregation

This gives the biggest stability win.

## Phase 4 — isolate data jobs

Later, move or isolate:

- instruments sync
- historical backfills
- ingestion jobs
- Meilisearch maintenance

---

## Strongest Go migration candidates by tag

### Migrate to Go first

1. `Marketwatch` realtime portion
2. `Candles` realtime portion
3. internal market websocket/subscription runtime
4. optional websocket order-update ingestion/runtime later

### Keep in Python

1. `Authentication`
2. `orders` business APIs
3. `Options`
4. `Alerts`
5. `Strategies`
6. `Momentum`
7. `Performance`
8. `User Settings`

### Move later or isolate as worker/admin service

1. `Instruments`
2. `Historical Data`
3. `Ingestion`
4. candles historical/admin endpoints

---

## Final answer

If your goal is **efficiency + stability + easier recovery when one subsystem breaks**, the optimal path for this codebase is:

- **not** “rewrite to Go”
- **not** “many microservices”
- **yes** to a **hybrid split**:
  - **Python Trading Core API**
  - **Python Algo Worker**
  - **Market Runtime**
  - **optional Python Data Jobs** later

That gives you the best balance of:

- operational safety
- simpler maintenance
- better fault isolation
- improved realtime efficiency
- minimal complexity for a solo/non-expert maintainer

### Important refinement

If you only split `API + worker` but leave broker websocket ownership and tick processing inside the API, you only solve part of the problem.

The strongest long-term boundary is:

- **algos isolated from API**
- **market/tick runtime isolated from both**

So the revised recommended end state is:

1. API survives worker crash
2. algos survive API restart where possible
3. broker websocket/tick runtime is not tied to login/auth startup
