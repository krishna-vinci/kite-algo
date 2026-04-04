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

## Newly implemented in current branch (not yet fully verified)

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
- add tests for event lag, retry, replay, and reconciliation drift

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
- improve app degraded-health reporting when broker startup bootstrap fails

Conclusion:
- **Order placement hardening is strong**
- **order-event ingestion and live positions now have production-grade structure**
- **main remaining work is verification, tests, and a few medium-risk operational cleanups**

---

## Next backend priorities

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
Status: **Implemented, needs verification against live API behavior**

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
- verify exact live SDK/provider response shapes for all MF endpoints and tighten response models once confirmed

### Priority 4: tests

Minimum high-value tests:
- Redis write limiter behavior
- order idempotency claim/replay/conflict behavior
- webhook checksum validation + duplicate insert behavior
- websocket event dedupe behavior
- live position delta application and reconciliation behavior

---

## Kite API coverage snapshot

This is a practical repo-status view, not a marketing claim.

| Area | Status | Notes |
|---|---|---|
| Authentication/session bootstrap | Implemented | Custom backend-managed login flow exists; headless automation is working but depends on Zerodha web flow stability |
| User profile | Implemented | `profile_kite` |
| Holdings | Implemented | `holdings_kite` |
| Positions | Implemented | raw `positions` plus separate in-app real-time position layer |
| Orders: place/list/history/trades/modify/cancel | Implemented | strongest backend area after recent hardening |
| Basket orders | Implemented | needs another hardening pass |
| GTT triggers | Implemented | create/list/get/modify/delete |
| Margins | Implemented | account margins, order margins, basket margins |
| Market quotes | Implemented | LTP and OHLC/quote-style endpoints exist |
| Historical candles | Implemented | strong support plus local storage/aggregator flows |
| Instruments | Implemented | sync/search/resolve paths exist |
| WebSocket market data | Implemented | subscription manager is present and production-used |
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

Short answer: **Structurally yes for the backend runtime paths, but verification/test hardening is still pending.**

What is done:
- login/session hardening
- global broker write pacing
- safer direct order idempotency
- canonical order-event ingestion and processing pipeline
- durable live position projection with reconciliation and Redis overlay
- backend mutual fund endpoints

What is not done:
- tests for these critical paths
- live provider-shape verification for mutual funds and some event edge cases
- a few medium-risk operational cleanups

If a new agent picks this up, the correct next task is:

1. add backend tests for order runtime, websocket flow, and live positions
2. verify mutual fund endpoint behavior against live provider responses
3. clean up remaining medium-risk operational issues
4. refresh this tracker after verification results
