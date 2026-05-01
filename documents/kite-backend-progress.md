# Kite Backend Progress Tracker

Last updated: 2026-05-01

## Scope

Backend-only Kite/Zerodha integration status tracker.
Do not use this file for frontend work.

---

## Recently completed

- Fixed headless Kite Connect login for newly switched broker accounts that land on Zerodha's app authorization page before returning a `request_token`
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

- Added Journal V2 production-validation gate infrastructure:
  - created `tests/journaling/test_v2_db_integration.py` for real Postgres schema-idempotency, live/paper isolation, V2 projection replay idempotency, V1/V2 replay preservation, and note revision concurrency validation
  - expanded `tests/test_journal_v2_router.py` with stricter V2 route environment-scope checks for ID-only reads, strategies/unresolved resolution, analytics, and paper/live comparison boundaries
  - added `scripts/validate_journal_v2_production.py` with schema/API/paper/live-read-only/frontend validation modes plus explicit live-order safety guard for the optional tiny live drill
  - fixed the unrelated `frontend-next` typecheck blocker by adding typed placeholder page shells for generated routes: `alerts`, `algos`, `charts`, `custom-display`, `quick-trade`, and `screeners`
  - current validation result in this environment: focused validation tests pass (`19 passed, 5 skipped, 1 warning`), Docker validation DB integration tests pass (`5 passed, 1 warning`), validation runner schema/API/paper/frontend checks pass against `kite_algo_validation`, and frontend typecheck passes
  - live broker runtime read-only status is connected (`broker.connected=true`, daily token gate ready, websocket `CONNECTED`), but the main DB has no existing `journal_execution_environments` live rows for `kite:XJJ446`; production readiness remains **not yet 100% proven** until a V2-attributed live environment/fill exists and live read-only Journal V2 validation passes
  - optional tiny live trade still requires explicit operator approval before any live order action

- Completed Journal V2 final production batch (Tasks 23–24):
  - added production handoff guide at `docs/journal-v2-developer-guide.md` documenting V2 concepts, backend API contracts, safety rules, and frontend alignment notes
  - completed final Journal V2 verification gate across focused V2 backend suites, worker/paper nearby regression suites, and frontend V2 test files
  - current backend status: Journal V2 environment/identity/episode/intent/timeline/notes/metrics/unresolved primitives are implemented, reviewed, and verified through the planned test matrix
  - remaining UI iteration note: frontend is aligned to Journal V2 data boundaries and basic flows, but final UX decisions for editor behavior, episode layout, note templates, analytics visualizations, and unresolved workflow still require trader/developer feedback before considering UI final
  - typecheck note from final pass: `frontend-next` typecheck currently fails in pre-existing generated `.next/types/validator.ts` imports for unrelated missing pages (`alerts`, `algos`, `charts`, `custom-display`, `quick-trade`, `screeners`)

- Completed backend control-plane Phases 1-3:
  - added authenticated `GET /api/control/strategy-positions`, `POST /api/control/strategies/{strategy_run_id}/exit`, `POST /api/control/strategies/{strategy_run_id}/cancel-orders`, and `POST /api/control/reconcile`
  - snapshot aggregation now reuses paper runtime summaries, non-paper algo-worker runs/P&L, and heartbeat-derived worker health, while keeping a stable manual/unattributed exposure bucket that infers broker account from the current Kite session and avoids double-counting known live strategy exposure
  - control-plane exit reuses existing paper-runtime and worker live exit paths; strategy-scoped cancel intentionally returns a deterministic `409` until broker-safe open-order attribution exists
  - control-plane protection adapters attach `option_runtime` state from option strategy store + algo runtime status and `investing_runtime` state from investing holdings summaries, with metadata fallback and per-strategy degradation when adapter state is unavailable
- Added centralized backend exposure protection for algo-worker runs:
  - workers can declare versioned `runtime_state.backend_protection` on run creation and patch it later through `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/protection`
  - backend evaluator supports position-level and basket-level percent stoploss/target/trailing rules, optional worker-stale exit, and configurable MIS squareoff buffer
  - triggered V1 rules submit conservative attributed strategy exits; position rules provide leg-specific thresholds until a broker-safe leg-only exit primitive exists
  - protection state persists in `runtime_state.backend_protection_state`, including generation, last check, trigger/action, exit submission, and errors
  - control plane displays `backend_worker_protection` alongside existing option/investing protection state
  - Python SDK now includes helper models plus `create_run(..., backend_protection=...)` and `update_backend_protection(...)`, with docs/examples for protected workers
- Fixed the monthly index refresh scheduler so live metric refresh/injection now also runs for `Nifty500` after constituent refreshes, matching the existing `Nifty50`/`NiftyBank` flow
- Added the next trading journal backend slice:
  - new `journaling/service.py` for run orchestration, source links, decision events, benchmark daily-price refresh, and run summary/benchmark comparison queries
  - new `journaling/runtime.py` restart-safe helper that persists projection cursor state and periodically refreshes benchmark data / recent run summaries without Celery
  - new backend-only journal router in `api/routers/journal.py` for create/update run, append decision events, link sources, run detail/list, and summary endpoints
  - wired the journal router into `main.py` and added an OpenAPI tag
  - added operator scripts `scripts/backfill_trading_journal.py` and `scripts/recompute_journal_metrics.py`
  - added focused journal service/router/runtime tests
  - added bounded phase-2 attribution hooks for option-strategy auto-linking, paper runtime journal refs, algo-trigger decision capture, and momentum investment-tag linkage
- Completed the current trading journal backend scope:
  - aggregate journal summaries for day/week/month/year/since-inception
  - aggregate benchmark comparison endpoint and summary alias endpoint used by the frontend
  - calendar, trades, strategies, review queue, rules, and insights backend routes
  - safer review-state validation and paginated run/trade route support
  - stronger option-strategy lifecycle syncing from `option_strategy_runs` into journal state
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
- Added Phase 1 frontend-facing trading-console backend enablers:
  - paper strategy summary now exposes `mode`, `is_open`, `timeline`, and baseline risk-edit capability hints for strategy-grouped operator surfaces
  - new authenticated `PATCH /api/system/paper/strategies/{strategy_id}/risk` route updates canonical option-strategy protection preferences for open paper runs
  - runtime-managed option-strategy risk edits now also patch the linked algo-instance config when an `algo_instance_id` is present
  - `strategies/option_strategy/runtime_updates.py` provides a pure helper for recomputing canonical preview/rules after protection edits
- Completed the first strategy-run unification backend slice for paper strategy management:
  - option strategy executions now return and persist canonical `strategy_run_id` alongside compatibility `strategy_id` / `option_strategy_id`
  - `paper_runtime/service.py` now groups and exits paper strategy activity by `strategy_run_id` first, with compatibility fallback for older records
  - paper strategy summaries now expose backend-driven `allowed_actions`, `risk_schema`, and `summary_fields` instead of relying on fixed option-only risk control payloads
  - manual or ambiguous paper groups are now explicitly blocked from strategy-level edit/exit behavior instead of being guessed into monitored strategy flows
- Live-verified mutual fund provider read endpoints via headless login:
  - `mf_instruments` ✅ returned 7409 rows in live verification
  - `mf_holdings` ✅ returned empty list for current account
  - `mf_orders` ✅ returned empty list for current account
  - `mf_sips` ✅ returned empty list for current account
- Tightened `MFInstrument` model using live provider fields (`plan`, `scheme_type`, `settlement_type`, `dividend_type`, `last_price`, `last_price_date`)
- Evaluated a PostgreSQL `pg_trgm` search migration for instruments, but rejected it because duplicating search state inside Postgres increased DB footprint and risked competing with order/runtime workloads
- Reduced FastAPI startup memory pressure by removing or lazy-loading heavyweight imports that were not needed at app boot (`pandas`, `numpy`, `yfinance`, `scipy`, chart-only modules)
- Completed the live/paper accounting and reconciliation backend spine:
  - live app order placement now accepts required strategy attribution, writes compact broker tags, quotes margin/charges contracts, and persists `live_order_intents`
  - broker trade fills now project into journal execution facts as `live_fill` when attributed and `broker_import` when external/unknown
  - untagged broker-side exits conservatively attach to exactly one matching open live run, otherwise they stay in the imported broker activity bucket
  - dirty order trade sync now triggers best-effort live journal projection without blocking position reconciliation
  - journal summary/runs/trades/strategies filters now carry strategy-family and execution-mode separation through backend and frontend API types
  - `/api/algo-workers` now supports explicitly live-enabled worker tokens/runs and routes live external algo intents through the attributed live order path
- Added the first real external worker SDK slice:
  - `sdk/python/kite_algo_worker` exposes `KiteAlgoWorkerClient`, `AlgoWorkerConfig`, a custom API exception, and broker-shape order builders
  - SDK examples now cover mean-reversion, option baskets, and grouped live exit preview with safe defaults
  - `docs/algo-worker-development-guide.md` is now a full coding guide for dry_run/paper/live worker strategy development and documents all live order fields supported by `PlaceOrderRequest`
  - focused SDK tests validate auth headers, run/intent payloads, idempotency enforcement, exit preview payloads, non-2xx handling, and order-builder compatibility with broker order validation
- Added grouped algo-worker run P&L snapshot/stream support:
  - `/api/algo-workers/worker/runs/{strategy_run_id}/pnl` now returns backend-owned grouped run totals plus per-leg breakdown for `dry_run`, `paper`, and `live`
  - `/api/algo-workers/worker/runs/{strategy_run_id}/pnl/stream` now exposes SSE updates so remote workers can consume grouped P&L that feels realtime
  - live run P&L is reconstructed from attributed live fills plus current account position marks, keeping charges separated and flagging stale coverage when broker mark/quantity alignment is incomplete
  - paper run P&L reuses grouped paper strategy state via `strategy_run_id` instead of requiring workers to infer grouped P&L locally
- Added generic runtime-backed algo-worker market-data primitives:
  - worker endpoints now expose ticker resolution/search, quote snapshots, tick SSE streams, candle snapshots, candle SSE streams, and combined market snapshot bundles under `/api/algo-workers/worker/market/*`
  - SDK methods now wrap those endpoints so external workers can build non-option realtime strategies without broker websockets, Redis access, database access, or backend internals
  - option-chain discovery, strike/expiry selection, Greeks/IV, and spread builders are explicitly deferred to a later namespaced option worker layer inside the same SDK package
- Added worker-safe funds and run-allocation snapshots:
  - `/api/algo-workers/worker/funds` returns account funds from paper runtime or broker margins through the backend-controlled live Kite session
  - `/api/algo-workers/worker/runs/{strategy_run_id}/funds` adds derived run exposure/P&L and optional allocation-cap remaining calculations for worker position sizing
  - SDK methods `get_funds()` and `get_run_funds()` expose these snapshots without letting workers call broker APIs directly
- Added worker-safe historical candle access for investing/positional strategies:
  - `/api/algo-workers/worker/market/history` wraps the existing robust backend candle facade under worker auth
  - SDK method `get_historical_candles()` supports symbol/token lookup, timeframe ranges, backend background ingestion, and deliberate Kite passthrough via the backend-controlled system session
  - realtime worker market streams remain SSE-based (`stream_ticks`, `stream_candles`, `stream_run_pnl`), not raw WebSocket connections from workers
- Extended the algo-worker API and Python SDK core refresh:
  - added worker order lifecycle routes for grouped live order/trade inspection plus cancel/modify actions under worker auth
  - added worker preview routes for live order margin/charges inspection and dry-run basket previews
  - added worker websocket routes for tick streams, candle streams, and grouped run P&L streams with worker-token auth and stream-specific permission checks
  - refreshed the Python SDK with typed exceptions/models, sync order lifecycle + preview methods, an async client, websocket clients, and helper ergonomics such as `ensure_run(...)`, `wait_for_history(...)`, and `live_equity_market_order(...)`
  - updated the worker development guide and added `scripts/sdk_worker_certification.py` for lightweight worker SDK certification checks
- Completed the generic live protection 100% gate for algo workers:
  - added `sdk/python/kite_algo_worker/live_protection_certification.py` with pure threshold/verdict helpers and `scripts/live_worker_protection_certification.py` for strict ultra-small live protection drills
  - updated `docs/algo-worker-development-guide.md` with live protection certification usage and safety gates
  - fixed a live-db compatibility gap where some running environments were missing `canonical_order_events.processing_started_at`, which prevented canonical event processing/trade-fill projection and made live worker P&L stay empty even for filled attributed orders
  - added runtime self-heal for that schema compatibility in `broker_api/order_runtime.py` and moved startup stuck-row refresh into the guarded worker loop in `main.py` so the order runtime worker cannot die silently before entering its retry loop
  - re-validated the full generic live protection surface with real tiny broker drills for worker-stale exit, position stoploss, basket stoploss, position target, basket target, and live protection patch mutability
- Completed Plan A of options-core production closure:
  - added semantic expiry selectors for explicit, nearest, current-week, next-week, and current-month resolution
  - completed canonical options market routes for session, chain, mini-chain, Greeks, selection resolve, PCR, and max-pain
  - added worker-token-protected options market proxy routes under `/api/algo-workers/worker/options/*`
  - updated the Python worker SDK options namespace so market calls use worker-safe routes
  - added deterministic route, SDK, auth-boundary, and market edge-case tests; combined Plan A regression passed with `46 passed, 1 warning`
- Completed Plan B of options-core production closure:
  - added explicit run-level option product validation and removed the hidden SDK option-leg product default
  - added canonical option run store/lifecycle primitives with created, previewed, entered, partial-entry, cleanup-required, exiting, partial-exit, and exited states
  - replaced canonical execution route scaffolds with real grouped run creation, entry preview, enter, exit preview, exit, orders/trades/state behavior
  - replaced protection route scaffolds with real protection config, evaluated state, and replay/debug behavior
  - added worker-token-protected options run/protection proxy routes and SDK wrappers for run/protection workflows
  - verified protection recommendations only target actually open/completed legs and use run-level product plus market protection for market exits
  - confirmed existing option session Greek math is synthetic-forward/Black-76 based and new canonical routes only expose those computed snapshot fields; combined Plan B regression passed with `63 passed, 1 warning`
- Completed Plan C of options-core production closure:
  - added deterministic end-to-end options lifecycle integration tests covering market → strategy preview → run creation → entry/exit/protection flows
  - added `greeks_source` metadata so canonical Greeks responses expose `synthetic_forward_black76` when session snapshots include forward/sigma context
  - strengthened worker-token auth boundary tests for market, run, and protection routes while confirming canonical `/api/options/*` routes do not depend on worker auth
  - added resource behavior coverage for bounded mini-chain output, service-level window validation, snapshot resource-error pass-through, and protection runtime isolation from market/session recomputation
  - clarified legacy compatibility ownership in old option routers/selectors and documented the completed worker options namespace in the SDK guide
  - final focused options closure regression passed with `100 passed, 1 warning`; adjacent worker/runtime regression passed with `51 passed, 1 skipped, 1 warning`
- Added durable canonical options run-state persistence slice:
  - new Postgres `public.option_run_states` table for canonical `OptionRunState` payloads (status/legs/protection/metadata/orders/trades/leg lifecycle)
  - added `DurableOptionRunStore` with the same run-store contract as in-memory (`create_run`, `list_runs`, `get_run`, `save_run`, `record_orders`, `record_trades`)
  - added DB startup compatibility helper for `option_run_states` table/index creation in older environments
  - added fake-session tests for insert/get/update/append behavior and commit/rollback/close safety without requiring a real Postgres instance
- Completed the options-core 100% production gate core backend items:
  - production canonical option routes now use `DurableOptionRunStore` by default while tests can still override with deterministic in-memory stores
  - added explicit Redis v1 JSON option-chain snapshot key/channel contract while preserving legacy Redis writes
  - added middleware-equivalent app-auth coverage proving canonical `/api/options/*` requires app auth and worker options routes remain worker-token protected
  - implemented snapshot-safe `delta_target` / `target_delta` contract selection from existing session delta fields without recomputing Greeks from raw spot
  - added restart/recovery tests proving durable option run status, protection config, orders, trades, and leg lifecycle survive a new store instance
  - hardened durable order/trade append with row locking and rejected malformed option instrument tokens instead of coercing them to `0`
  - added best-effort startup prewarm for the options Black-76/IV math engine and confirmed current mini-chain/session APIs already expose custom window/cadence controls with `cadence_sec >= 1`
  - updated the worker development guide with durable option run state and delta-target selection semantics
- Hardened paper-mode isolation and parity for algo workers/runtime:
  - added centralized account-scope parsing so paper/live routing no longer relies on scattered string heuristics
  - added a shared execution attribution builder for paper/dry-run flows so canonical run identity wins over caller metadata
  - worker run access now requires owning `token_id`, not just matching scope/template
  - paper grouped run P&L now uses a dedicated `PaperRunStateService`, includes charges/staleness metadata, and paper exits stay open/blocked when reconciliation is unsafe instead of falsely closing runs
  - paper journaling can now auto-resolve/create a per-run journal entry keyed by `strategy_run_id` plus account scope when explicit journal ids are missing
- Updated worker token scope handling + paper accounting parity:
  - live-bound worker tokens can now create/access paper and same-account live/dry_run scopes while preserving strict token_id run ownership and cross-live-account isolation
  - worker paper run P&L payloads now serialize charges/net from the paper run-state source-of-truth instead of hardcoding zero charges
  - paper fill-time margin release now prefers persisted position `margin_in_use` when present so blocked funds clear to zero after fully closed paper exposure

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
Status: **Production-oriented implementation in place and re-validated against real worker-filled live drills**

Current behavior:
- positions reconcile into durable `account_positions` rows in Postgres
- Redis now acts as a cache/overlay and pub/sub fanout layer instead of sole state store
- websocket LTP updates are no longer tied to active SSE subscribers
- SSE stream exists per broker account via Redis pub/sub
- live worker P&L fallback recovered correctly after fixing canonical-event projection on a stale live DB that was missing `processing_started_at`

Remaining work:
- verify incremental trade application math against real broker fills, especially for shorts and partial exits
- add tests for restart recovery, duplicate events, and concurrent tick/order flow

Conclusion:
- **Order placement hardening is strong**
- **order-event ingestion and live positions now have production-grade structure**
- **generic live worker protection has now been live-proven for the agreed pre-options gate**
- **main remaining work is broader verification, tests, and a few medium-risk operational cleanups outside that gate**

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
Status: **Meilisearch remains active; alternate search engine evaluation is deferred**

Current behavior:

- instrument suggestions still run through Meilisearch while search-engine replacement is evaluated
- PostgreSQL should remain focused on canonical backend data (orders, positions, runtime state, etc.) and should not host a duplicated search index for instruments
- FastAPI startup no longer eagerly imports several heavy data/chart packages that are not needed for most requests

Remaining work:

- verify top broker-style query ordering against real expected results (`nifty`, `bank nifty`, strike + CE/PE flows, common typos)
- if search-engine evaluation resumes later, require a measured RAM win and acceptable broker-style ranking before replacing Meilisearch

Expected operational effect:

- reduces FastAPI baseline RSS by avoiding unnecessary scientific/chart imports at startup
- keeps Postgres isolated from instrument-search experiments so order/runtime workloads stay unaffected

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
- live worker drill on 2026-04-29 verified that the SDK can stream fresh GOLDM data after subscription, compute indicators from worker-safe historical candles, preview live orders, and place real broker orders from worker runs
- the same live drill also exposed a critical backend-control-plane gap for generic worker protection: a stale-worker protection exit closed the run `live_protection_idea_1777436466` as already flat even though the broker-side `IDEA` `MIS` buy had filled and remained open until a second manual worker sell flattened it
- live worker order/trade inspection is not yet trustworthy because real broker orders/trades for those runs were filtered out of `/api/algo-workers/worker/orders` and `/api/algo-workers/worker/trades`, while `order_state_projection` also remained stuck at `PLACED` for both filled order IDs
- likely root cause from the drill: live worker fills were not linked into `journal_source_links` / `journal_execution_facts` for the worker `strategy_run_id`, so `_list_live_strategy_open_legs_sync(...)` returned no legs and `_exit_live_worker_run(...)` treated the run as flat
- same-day fixes corrected the worker safety path by:
  - deriving worker live open legs from `live_order_intents` + trade fills instead of journal tables
  - recovering durable worker attribution from broker order ids and client-order-ref tags, including canonical order-event tag recovery when direct backfill lags
  - adding a direct-broker defer guard so a run is not falsely marked flat just because local attribution/projection tables are behind
  - adding `market_protection=-1` to backend-generated live market exit orders
  - adding a live worker P&L fallback when journal linkage is missing
- same-day live re-validation then succeeded for:
  - grouped live entry/exit round-trip on `live_final_exit_1777441545`
  - full backend stale-worker auto-exit on `live_stale_auto_1777441609` with real broker fill, trigger at `heartbeat_age_sec=31`, attributed exit order, final closed run, and flat broker account
- generic worker stoploss/target logic remains strongly unit-tested but not fully forced against real market movement in the same live session; stale-worker protection is the best live-validated generic path so far

If a new agent picks this up, the correct next task is:

1. add optional worker conveniences that remain API-only, such as listing recoverable open runs by token/template/account and structured decision/journal events
2. extract template-owned builders for `build_summary_fields(run)`, `build_risk_schema(run)`, and `build_allowed_actions(run)` so non-option strategies can use the same backend contract cleanly
3. implement one non-option systematic algo on that contract and verify it in paper mode using the SDK
4. carry the same run/capability contract into the live strategy-management path
5. continue implementing and hardening `market-runtime/`, starting with live shard verification and direct or optimized marketwatch/candle runtime streaming
6. add backend tests for order runtime, websocket flow, and live positions
7. verify live duplicate/replay order-event behavior against real provider events
8. verify MF get-by-id shapes when a real order/SIP exists, without executing unsafe side-effecting writes unless explicitly approved
9. refresh this tracker and the websocket-runtime docs after each material step
