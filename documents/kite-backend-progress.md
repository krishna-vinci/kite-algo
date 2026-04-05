# Kite Backend Progress Tracker

Last updated: 2026-04-05

## Scope

Backend-only Kite/Zerodha integration status tracker.
Do not use this file for frontend work.

---

## Recently completed

- Centralized Kite session handling in `broker_api/kite_session.py`
- Stopped returning broker `access_token` from login responses
- Hardened headless broker login and startup token rotation
- Added Redis-backed global write pacing for order/GTT writes
- Added Redis-backed order idempotency for direct order placement
- Routed strategy order placement through the same throttled write path
- Added broker login health endpoint in `api/routers/auth.py`
- Added runtime status/log metadata support via `runtime_monitor.py`
- Added project-local OpenCode skill at `.opencode/skills/kite-backend-progress/SKILL.md`
- Added backend mutual fund router in `broker_api/kite_mutual_funds.py`

## Newly implemented in current branch

- Added websocket re-architecture documentation for a unified Go market-runtime service in `documents/websocket-runtime/`
- Extended the local `kite-backend-progress` skill so websocket work must also consult and maintain the websocket-runtime docs
- Added initial Go market-runtime scaffold in `market-runtime/` with:
  - config/env loading
  - internal HTTP control-plane endpoints
  - owner-based subscription registry
  - hidden shard allocator
  - Redis tick/status/order-update publishing
  - Postgres-backed system token lookup/watcher
  - `gokiteconnect/v4/ticker` shard wrapper
- Added local Go SDK reference materials in `kite-go/` and cloned source reference in `gokiteconnect/` (local-only reference, git metadata removed)
- Added initial Phase 3 marketwatch cutover support:
  - Go market-runtime now exposes `ws://.../ws/marketwatch` for direct marketwatch clients
  - frontend can point directly at the Go runtime when `VITE_MARKET_RUNTIME_WS_URL` is set
  - removed obsolete Python market-runtime proxy helpers from `api/routers/marketwatch.py` so runtime-backed marketwatch traffic goes directly to Go
  - sunk the legacy Python `/api/ws/marketwatch` websocket path so it now rejects callers instead of serving live market data
- Added owner-lease refresh and runtime-side stale-owner cleanup to reduce market-runtime subscription leaks when disconnect cleanup is imperfect
- Added initial Phase 4 candle cutover support:
  - `broker_api/candle_aggregator.py` now supports consuming `market:ticks` and syncing subscriptions through the market-runtime when `MARKET_RUNTIME_ENABLED=true`
  - candle aggregation no longer needs to own a direct broker websocket connection in runtime-enabled mode
  - token rotation no longer restarts the candle aggregator in runtime-enabled mode
  - runtime tick timestamp normalization was fixed for ISO string payloads from the Go runtime
- Migrated the remaining websocket-dependent backend consumers onto the Go runtime contract:
  - alerts engine now reads runtime tick cache and owns runtime subscriptions through `backend:alerts-engine`
  - position protection engine now reads runtime tick cache and owns runtime subscriptions through `backend:protection-engine`
  - options sessions now converge token ownership through `backend:options-sessions`
  - real-time positions now consume runtime tick flow and maintain dedicated runtime subscriptions for tracked open-position tokens
- Retired Python `WebSocketManager` startup so the Go runtime is now the only intended broker websocket owner
- Added Python runtime bridge handling for:
  - runtime tick cache hydration from Redis
  - runtime status tracking
  - relayed websocket order-update ingestion from `market:order_updates`
  - owner lease refresh for backend runtime consumers
- Added dev wiring for the Go market-runtime in `compose.dev.yml` and `market-runtime/Dockerfile`
- Hardened production deployment wiring for the Go market-runtime:
  - `compose.yml` now includes the runtime service and routes backend/frontend through it
  - `market-runtime/Dockerfile` now has separate `dev` and `production` targets
  - production runtime now runs as a compiled binary instead of `go run`
  - production frontend now proxies `/ws/marketwatch` to the Go runtime
- Added `broker_user_id` to Kite session persistence so backend state can be keyed by stable broker account identity
- Added canonical order event infrastructure in `broker_api/order_runtime.py`
  - normalized canonical order receipts in Postgres
  - order state projection table
  - trade fill ledger keyed by `trade_id`
- Reworked websocket order update persistence to use the shared canonical ingestion path
- Added background order runtime worker in `main.py` for:
  - pending canonical event processing
  - dirty-order trade sync
  - periodic position reconciliation
- Replaced old in-memory/SSE-coupled live positions path with:
  - durable `account_positions` projection in Postgres
  - Redis base cache + LTP overlay
  - Redis pub/sub position streaming per broker account
- Added operator endpoints for runtime inspection and manual reconcile/process-now flows
- Added mutual fund backend endpoints for orders, SIPs, holdings, and instruments
- Added backend unittest coverage for:
  - advisory lock acquisition retry/session handling
  - trade fill application across reduce/flip/close transitions
  - Redis overlay position PnL math and tick-driven delta publication
  - websocket external subscription union + reconnect resubscribe behavior
  - mutual fund provider shape/error handling
- Added DB-backed integration coverage (Postgres + Redis) for:
  - canonical order-event ingest/process/trade-sync flow
  - reconciliation replacing stale positions and persisting trade ledger entries
  - canonical event processor failure marking rows failed
  - dirty-order sync failure leaving projections dirty
  - Redis overlay write handling real connection-refused failures
- Added a replay-verification runbook and helper script:
  - `documents/order-event-replay-verification.md`
  - `scripts/verify_order_event_replay.py`
- Added position conversion backend support via `POST /positions/convert`
- Fixed websocket external-token convergence so removing an external subscriber now correctly downgrades back to client-requested mode
- Reduced startup DB session lifetime for Phase 3 instrument lookups by switching `InstrumentsRepository` to session-factory usage instead of app-lifetime sessions
- Reduced advisory-lock polling connection pressure by acquiring position-runtime locks with short-lived retry sessions instead of holding one DB session open while polling
- Improved startup degraded-health reporting when broker bootstrap fails by marking broker/app/websocket runtime state as degraded instead of always reporting healthy startup
- Live-verified mutual fund provider read endpoints via headless login:
  - `mf_instruments` ✅ returned 7409 rows in live verification
  - `mf_holdings` ✅ returned empty list for current account
  - `mf_orders` ✅ returned empty list for current account
  - `mf_sips` ✅ returned empty list for current account
- Tightened `MFInstrument` model using live provider fields (`plan`, `scheme_type`, `settlement_type`, `dividend_type`, `last_price`, `last_price_date`)
- Replaced Meilisearch-backed instrument suggestions with a PostgreSQL `pg_trgm` search path:
  - added `instruments_search_index` plus trigram/filter indexes
  - added Alembic migration `20260405_000002_pg_trgm_search_index.py` for existing deployments
  - `/api/instruments/fuzzy-search` now uses parser-driven PostgreSQL ranking with a base SQL fallback
  - compose no longer requires a Meilisearch service for backend instrument search
- Reduced FastAPI startup memory pressure by removing or lazy-loading heavyweight imports that were not needed at app boot (`pandas`, `numpy`, `yfinance`, `scipy`, chart-only modules)

## Skill usage

- Local OpenCode skill path: `.opencode/skills/kite-backend-progress/SKILL.md`
- Use the `kite-backend-progress` skill before backend work when available locally
- The skill is a handoff helper and points agents back to this document
- Keep this tracker updated whenever backend architecture, status, or priorities materially change

---

## Hardening status

### 1) Order write path
Status: **Mostly hardened**

What is in good shape:
- place/modify/cancel/GTT writes go through centralized pacing
- Redis coordinates write rate across workers
- write path fails closed when Redis is required and unavailable
- direct order placement has safer idempotency than before

Still needed:
- basket execution safety needs another pass
- tests for limiter/idempotency/error cases are still missing

### 2) Order websocket + postback recording
Status: **Production-oriented implementation in place**

Current behavior:
- webhook postbacks are checksum-validated and stored in `order_events`
- websocket order updates are stored in `ws_order_events`
- both flows now also write into canonical normalized receipts for downstream processing
- both flows publish SSE events to `orders.events`

Remaining work:
- verify canonical dedupe behavior against real duplicate provider events
- add tests for event lag, retry, replay, and reconciliation drift under database-backed integration paths

### 3) Live positions / live PnL logic
Status: **Production-oriented implementation in place**

Current behavior:
- positions reconcile into durable `account_positions` rows in Postgres
- Redis now acts as a cache/overlay and pub/sub fanout layer instead of sole state store
- websocket LTP updates are no longer tied to active SSE subscribers
- SSE stream exists per broker account via Redis pub/sub

Remaining work:
- verify incremental trade application math against real broker fills, especially for shorts and partial exits
- add tests for restart recovery, duplicate events, and concurrent tick/order flow

Conclusion:
- **Order placement hardening is strong**
- **order-event ingestion and live positions now have production-grade structure**
- **main remaining work is verification, tests, and a few medium-risk operational cleanups**

### 4) WebSocket market-data runtime
Status: **Go runtime now owns websocket infrastructure; live verification/load observation still pending**

Current behavior:

- Go `market-runtime/` is now the only intended broker websocket owner
- Python business modules consume the runtime via owner subscriptions, Redis-backed tick cache, and relayed order updates
- `broker_api/websocket_manager.py` is no longer started by backend startup

Planned target:

- move live websocket infrastructure into a dedicated **Go market-runtime** service
- keep a **unified external model** for frontend and Python consumers
- use up to **3 hidden Kite websocket connections** internally
- enforce a **2800 token soft limit per connection**
- deduplicate subscriptions globally and aggregate modes centrally
- remove direct broker websocket ownership from Python business modules over time

Current implementation progress:

- Go runtime scaffold exists in `market-runtime/`
- hidden shard allocator exists with the documented **2800** soft cap
- marketwatch connects directly to the Go runtime websocket endpoint
- candle aggregation consumes runtime ticks instead of owning a direct broker websocket in runtime mode
- alerts, protection, options sessions, and real-time positions now depend on runtime-backed contracts instead of Python websocket internals
- Python websocket startup has been retired
- remaining work is live end-to-end verification, shard/load validation, and parity observation under real subscriptions

Latest runtime verification notes:

- production runtime memory dropped from roughly `170-180 MiB` to about `8-13 MiB` after switching the production image from `go run` to a compiled binary image
- a synthetic production owner load test at **2200 subscribed tokens** stayed healthy on a single shard and used about `13 MiB` in the hardened production runtime container
- that load test was cleaned up after observation; it did not represent a true live-market tick-throughput run because the token set was synthetic rather than validated broker instruments

Authoritative docs:

- `documents/websocket-runtime/README.md`
- `documents/websocket-runtime/spec.md`
- `documents/websocket-runtime/contracts.md`
- `documents/websocket-runtime/implementation-plan.md`
- `market-runtime/`
- `kite-go/`

---

### 5) Instrument search + backend startup efficiency
Status: **PostgreSQL `pg_trgm` migration implemented; broker-query ranking verification still needed**

Current behavior:

- instrument suggestions no longer depend on Meilisearch at runtime
- PostgreSQL now owns broker-style fuzzy search via `pg_trgm`, `instruments_search_index`, parser-derived filters, and explicit ranking
- search bootstrap runs during app startup to ensure the search index table is populated when instrument source rows already exist
- `/api/instruments/search/health` reports PostgreSQL search-index status; `/api/instruments/meili/health` is currently retained as a backward-compatible alias
- a plain SQL fallback remains available if the trigram-backed path is unavailable or misconfigured
- FastAPI startup no longer eagerly imports several heavy data/chart packages that are not needed for most requests

Remaining work:

- verify top broker-style query ordering against real expected results (`nifty`, `bank nifty`, strike + CE/PE flows, common typos)
- tune ranking/alias logic if Swagger/manual validation shows regressions versus previous Meilisearch behavior
- consider follow-up cleanup to remove now-dead Meilisearch helper code and obsolete environment variables once rollout confidence is high

Expected operational effect:

- removes the separate Meilisearch container memory footprint from normal backend deployments
- reduces FastAPI baseline RSS by avoiding unnecessary scientific/chart imports at startup

---

## Next backend priorities

### Priority 0: build the unified websocket market runtime

Status: **Implemented architecturally; verification and live load validation remain**

Target direction:

- build a dedicated Go market-runtime service
- keep websocket usage unified for callers
- hide internal shard selection from callers
- lazily scale from 1 to 3 Kite websocket connections
- use a **2800 token soft limit** per connection

Current implementation status:

- Go service scaffold exists in `market-runtime/`
- hidden shard allocator exists and enforces the 2800 soft cap
- internal control-plane endpoints exist for owner subscription management
- Redis publishing and Postgres token lookup are wired
- marketwatch now uses the direct Go websocket path
- candle aggregation uses the runtime-backed path
- alerts, protection, options sessions, and live positions now use runtime-backed contracts
- Python `WebSocketManager` startup has been removed
- live shard verification and runtime load observation are still pending

Implementation docs:

- `documents/websocket-runtime/README.md`
- `documents/websocket-runtime/spec.md`
- `documents/websocket-runtime/contracts.md`
- `documents/websocket-runtime/implementation-plan.md`

### Priority 1: finish order-event hardening

Files:
- `broker_api/kite_orders.py`
- `broker_api/websocket_manager.py`
- `schema.sql`

Recommended work:
- unify normalized order-event shape across webhook + websocket
- add durable dedupe key / processing ledger for websocket order events
- store enough metadata to identify partial-fill deltas safely
- publish one canonical downstream event shape for fills/status changes
- add replay/reconciliation path from stored events or fresh broker fetch

### Priority 2: rebuild live positions around safe state transitions

Files:
- `broker_api/kite_orders.py`
- `broker_api/websocket_manager.py`
- `main.py`

Recommended work:
- stop storing the full session position map as a single read-modify-write blob
- use per-session lock and/or per-position Redis keys/hash updates
- separate "tracked sessions" from "active SSE subscribers"
- support multiple SSE subscribers per session cleanly
- apply order fills as deltas from canonical processed events
- add periodic reconciliation with Kite positions/trades

### Priority 3: implement mutual funds endpoints cleanly
Status: **Implemented, read paths live-verified, write/get-by-id paths still partially unverified**

Target capabilities from Kite API:
- list mutual fund instruments ✅
- list mutual fund orders ✅
- get mutual fund order by id ✅
- place mutual fund order ✅
- cancel mutual fund order ✅
- list mutual fund SIPs ✅
- place mutual fund SIP ✅
- modify mutual fund SIP ✅
- cancel mutual fund SIP ✅
- list mutual fund holdings ✅

Implementation notes:
- create a dedicated backend router/service instead of mixing MF into unrelated files
- keep request/response models explicit
- use the same auth/session pattern as other Kite routes
- use the same structured error handling style
- apply write pacing to MF write endpoints if they count against broker write limits

Current file:
- `broker_api/kite_mutual_funds.py`

Open question:
- read endpoints verified live for instruments/orders/sips/holdings
- write endpoints were intentionally not executed live because they are side-effecting real broker actions
- get-by-id response shapes still need live confirmation once a real MF order or SIP exists in the account

### Priority 4: tests

Minimum high-value tests:
- Redis write limiter behavior
- order idempotency claim/replay/conflict behavior
- webhook checksum validation + duplicate insert behavior
- websocket event dedupe behavior
- live position delta application and reconciliation behavior

Recently added tests:
- candle aggregator runtime-mode timestamp parsing / candle update tests
- order runtime fill application transitions (reduce/flip/close)
- Redis overlay position PnL + tick delta publication
- websocket external token union downgrade/resubscribe behavior
- mutual fund provider shape/error mapping
- advisory lock retry/session acquisition behavior
- Postgres-backed canonical ingest/process/trade-sync integration flow
- Postgres-backed reconcile stale-row replacement flow
- processor failure and dirty-sync failure integration cases
- real Redis connection-refused overlay failure handling
- order-event replay verification workflow and script

---

## Kite API coverage snapshot

This is a practical repo-status view, not a marketing claim.

| Area | Status | Notes |
|---|---|---|
| Authentication/session bootstrap | Implemented | Custom backend-managed login flow exists; headless automation is working but depends on Zerodha web flow stability |
| User profile | Implemented | `profile_kite` |
| Holdings | Implemented | `holdings_kite` |
| Positions | Implemented | raw `positions`, real-time position layer, and product conversion endpoint are present |
| Orders: place/list/history/trades/modify/cancel | Implemented | strongest backend area after recent hardening |
| Basket orders | Implemented | needs another hardening pass |
| GTT triggers | Implemented | create/list/get/modify/delete |
| Margins | Implemented | account margins, order margins, basket margins |
| Market quotes | Implemented | LTP and OHLC/quote-style endpoints exist |
| Historical candles | Implemented | strong support plus local storage/aggregator flows |
| Instruments | Implemented | sync/search/resolve paths exist |
| WebSocket market data | Implemented, planned re-architecture | current Python manager is production-used today; unified Go market-runtime replacement is now the intended target |
| Order postbacks | Implemented | checksum validation and persistence exist |
| WebSocket order updates | Implemented | canonical ingestion + trade-sync/reconciliation structure now in place |
| Live positions/PnL derived from ticks + fills | Implemented | durable projection + Redis overlay in place; test verification still needed |
| Mutual funds | Implemented | dedicated router added; needs live-response verification |
| Publisher/apps/other non-core docs areas | Not implemented / not relevant here | low priority for this repo right now |

Rough summary:
- **Core trading + market data coverage is strong**
- **Order-event ingestion and live positions now have production-grade structure**
- **Main remaining work is test coverage and verification under real/provider edge cases**

---

## Answer to “did we finish hardening?”

Short answer: **Structurally yes for the backend runtime paths, and baseline unit/integration coverage now exists, but some live-provider verification is still pending.**

What is done:
- login/session hardening
- global broker write pacing
- safer direct order idempotency
- canonical order-event ingestion and processing pipeline
- durable live position projection with reconciliation and Redis overlay
- backend mutual fund endpoints

What is not done:
- live duplicate/replay verification against a real captured order-event bundle
- upstream broker token invalidation during logout/session teardown
- live MF get-by-id/write-path verification when a safe real sample exists

Update after latest verification:
- mutual fund read endpoints now have live-provider verification
- baseline Postgres/Redis integration coverage now exists for canonical runtime + failure paths
- remaining verification gap is mainly live duplicate/replay edge cases and live MF get-by-id/write-path shapes
- attempted live duplicate/replay verification on 2026-04-05, but the current Kite account had `orders() == 0` and `trades() == 0`, and runtime raw event tables were empty, so there was no real captured order-event bundle available to replay without placing a new live order

If a new agent picks this up, the correct next task is:

1. continue implementing and hardening `market-runtime/`, starting with live shard verification and direct or optimized marketwatch/candle runtime streaming
2. add backend tests for order runtime, websocket flow, and live positions
3. migrate alerts, protection, and options consumers off direct Python websocket ownership
4. verify live duplicate/replay order-event behavior against real provider events
5. verify MF get-by-id shapes when a real order/SIP exists, without executing unsafe side-effecting writes unless explicitly approved
6. refresh this tracker and the websocket-runtime docs after each material step
