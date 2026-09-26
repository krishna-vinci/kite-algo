# kite-algo platform reference: architecture, flow, capabilities, gaps

**Verified against:** `development` @ `0a10c50` (2026-09-26). Updated for the Phase 0 fixes through `2502123`.
**Method:** four read-only code audits, with the high-impact claims re-checked by hand. Every statement cites
`file:line`. No tests or services were run for this document.
**Audience:** every future agent (Claude, Codex, others) and the owner. Read this before planning or claiming what
the platform can or cannot do.

**Labels**

| Label | Meaning |
| --- | --- |
| **EXISTS** | A reachable production path exists. |
| **PARTIAL** | It exists with the stated limit. |
| **MISSING** | Searched for and not found. The search is named. |
| **POSSIBLE BUG** | Found by reading the code. Not proven by running it. Confirm with a test before fixing. |

**Maintenance rule:** when you change behaviour described here, update the matching line in the same change. When
you find a line that is wrong, fix it and say so in your report. Stale capability claims have already caused wrong
plans.

---

## 0. One-screen summary

- **What it is.** A self-hosted trading platform for one owner's Zerodha Kite account. It covers market data,
  alerts and screeners, a journal and analytics, paper trading, and **hosted strategies**: the owner's Python code
  runs in a sandboxed child process and trades only through a governed pipeline.
- **The governed pipeline.** Strategy code never places orders:
  1. The strategy submits a **proposal** (a desired state).
  2. The platform compiles it into an immutable **plan**.
  3. **Admission** checks risk limits, margin and freshness.
  4. A **reservation** holds capacity.
  5. An **approval** (the owner) or a **standing grant** (autonomous mode) authorises it.
  6. An **execution request** is created and the **dispatcher** sends it to the paper or live executor.
  7. **Ingestion** records fills, and **attribution** updates the book.
- **Strong today:**
  - hedge-first option entry with a fill gate; short-first staged exits
  - option resize and roll through generations
  - staged CNC financing (sell before buy)
  - bounded LIMIT orders for gated live legs
  - approval pinning to version, catalog and plan hash
  - protection ownership per option run
  - repair, owner exit and cancel-pending
  - per-strategy attribution and account truth
  - a 5-second server-side protection loop: SL, target and trailing %, basket rules, worker-stale exit, MIS square-off
- **Weak or missing today:**
  - **Intraday cadence.** Schedules fire at most once a day. A run-now loop that makes many decisions is allowed
    by the code, but no test proves it.
  - **Index, premium and MTM option stops never fire**, because nothing ever writes their metrics.
  - **No position Greeks.**
  - **No freeze-quantity slicing.**
  - **A live daily loss budget blocks all live admissions** once it is set.
  - **No market-hours gate** in the pipeline.
  - **The supervisor runs one strategy child at a time.**
  - **Logs arrive only after the child exits.**
  - **No real live order has been placed yet.** Everything live has been proven with a fake broker only.

---

## 1. Deployment topology

| Service | Definition | Role | Port (host:container) |
| --- | --- | --- | --- |
| postgres 16 | `compose.yml:2-30` | primary database | 15432:5432 |
| redis 7 (AOF) | `compose.yml:32-45` | tick and candle pub/sub, caches | 16379:6379 |
| market-runtime (Go) | `compose.yml:47-86` | the **only** Kite websocket owner; Redis fan-out; instrument lookup | 18780:8780 |
| finance-app (FastAPI) | `compose.yml:88-142` | API plus **all** in-process background loops | 18777:8777 |
| frontend-next (Next.js) | `compose.yml:144-177` | UI; rewrites `/api/*` to the backend and `/ws/*` to market-runtime (`frontend-next/next.config.ts:13-26`) | 13000:3000 |
| alerts-worker | `compose.worker.yml:11-58` | workflow, screener and universe evaluation, plus notification delivery | — |
| strategy-runner | `compose.supervisor.yml:13-67`, `Dockerfile.supervisor` | hosted-strategy supervisor; has **no DB credentials** and talks only to the lifecycle API | — |
| mcp-go | `compose.mcp-go.yml:8-34` (profile `mcp`) | Go MCP adapter, 73 tools | 18789:8788 |

- **Migrations.** `alembic upgrade head` runs before uvicorn (`compose.yml:123`, `Dockerfile:42`). The current head
  is `20260926_000051_live_approval_binding`.
  - **PARTIAL:** lifespan startup also executes `backend/schema.sql` (`backend/app/bootstrap.py:48-61,296`,
    `backend/app/database.py:26-33`). There are two schema paths.
- **Deploy.** Build all images first, then recreate `finance-app` first so it migrates, then the rest. See
  `documents/hosted-strategies-b2-c1-deployment-2026-09-26.md`.

**Background loops in finance-app** (`backend/app/bootstrap.py`, `backend/app/background.py`):

| Loop | Cadence | Evidence |
| --- | --- | --- |
| order runtime (order events, dirty-order sync) | 1 s | bootstrap.py:408-461 |
| position reconcile | 30 s | bootstrap.py:408-461 |
| position subscription sync | 10 s | bootstrap.py:463-504 |
| system token watcher | 30–60 s | bootstrap.py:506-534 |
| account ingest | 60 s | background.py:51-54 |
| **worker protection** | **5 s** | background.py:122-167 |
| stale-run recovery | 30 s | background.py:167 |
| exiting-run recovery | 10 s | background.py:191 |
| bracket executor | 1 s | background.py:215 |
| **strategy schedule loop** | 60 s | background.py:295-350 |
| **hosted execution dispatcher** | 5 s, 10 requests per pass | strategies/execution_dispatcher.py:25-39 |
| live outcome consumer (ingestion, release, LIMIT timeout sweep) | 15 s | strategies/live_ingestion.py:122 |
| candle aggregator (1m–60m and day) | continuous | bootstrap.py:556-579 |
| journal runtime | 60 s | journaling/runtime.py:22-29 |

**Daily jobs (IST):**

| Time | Job |
| --- | --- |
| 02:00 | fundamentals |
| 05:45 | exchange calendar |
| 06:30 | index refresh check (runs monthly) |
| 07:00 | instruments |
| 08:00 | token refresh, retried every 30 s |
| 16:00 | daily candle finalisation |

Sources: `schedulers.py:50-320`, `broker_api.py:1079`, `daily_candle_finalization.py:346`.

## 2. Market data

- **market-runtime (Go): EXISTS.** Uses `gokiteconnect/v4` (`market-runtime/go.mod:8`).
  - Writes `SET market:tick:{token}` (TTL 900 s) and `PUBLISH market:ticks`, `market:order_updates` and
    `market:status` (every 10 s) (`internal/service/redis.go:47-83`).
  - Shards: up to 3, with 2800 tokens each. Owner subscriptions have a 90 s lease. The access token is polled from
    Postgres every 30 s (`internal/config/config.go:34-42`).
  - **No auth** on its HTTP or websocket endpoints (`internal/service/http.go:16-113`, `marketwatch_ws.go:20`).
- **Candles: EXISTS.** `backend/broker_api/market/candle_aggregator.py` consumes ticks and writes
  `candle:{token}:{interval}:current|latest`, then publishes `realtime_candles:*` (`:242,365-415`).
- **Option chain and Greeks: EXISTS, started lazily.**
  - An `OptionsSession` per underlying recomputes every **5 s** (`backend/broker_api/options/options_sessions.py:45-55`)
    over a strike window of ±12 (`backend/options/api/market_router.py:19-20`). Expiries refresh every 60 s.
  - Sessions start only via `POST /api/options/sessions` (`market_router.py:50`). There is no auto-start at boot. A
    missing session returns `OPTION_SESSION_NOT_FOUND`.
  - **Model:** Black-76 on a synthetic forward `F = S + C_atm − P_atm`. There is **one IV per expiry**, inverted
    from the ATM call, and it is applied to every strike, so there is no smile or skew (`options_sessions.py:324-395,604-640`).
    Kernels are in `options_greeks.py:50-300`. Theta is per day; vega is per 1%.
  - Time to expiry is anchored at 15:30 **IST** (fixed in Phase 0, `391adf0`; it was UTC, which overstated T by
    about 5.5 hours).
  - **Freshness limits:** 10 s at plan freeze and **5 s** before a live send (`backend/options/market/freshness.py:17-18`).
    The 5 s limit equals the session cadence, so expect occasional stale refusals.
  - **Endpoints:** expiries, chain, mini-chain, greeks, selection/resolve, PCR, max-pain, SSE stream
    (`market_router.py:78-160`). Worker mirrors are in `backend/options/api/worker_options_router.py:159-252`.
- **Calendar: PARTIAL.** Covers NSE CM holidays only (`backend/broker_api/market/nse_calendar_source.py`,
  `exchange_calendar.py`). There are no MCX or CDS sessions.

## 3. Broker integration

- **Login: EXISTS, headless only.** Uses `KITE_USER_ID`, `KITE_PASSWORD` and a TOTP secret
  (`backend/broker_api/session/kite_auth.py:17-172`). There is one `system` session.
  - The app **will not boot without Kite** (`bootstrap.py:307-372,718-729`).
  - The access token is stored in plain text (`kite_session.py:24`).
- **Order rate limiting: EXISTS.** Every Kite write goes through `run_kite_write_action` → `KiteWriteThrottler`
  (`backend/broker_api/orders/service.py:45-163`). That covers place (`:359`), modify (`:563`), cancel (`:584`), the
  per-order basket calls, GTT (`:944`, `:1026`, `:1055`) and mutual funds (`kite_mutual_funds.py:173-243`).
  - **How it works:** a global slot scheduler in Redis. A Lua script reserves the next free slot, spaced
    `1/rate` apart (`:58-105`). The rate is `KITE_WRITE_OPS_PER_SEC`, default 9, with a hard cap of 10 and a floor
    of 1 (`:46-47,154`).
  - **Excess orders queue, they are not dropped.** A write whose slot is later waits for it: the 11th order in a
    burst goes in the next second. The lock is shared across processes through Redis, so all services share one
    budget.
  - **Limits on the queue:** a write whose wait would exceed `KITE_WRITE_LIMIT_MAX_WAIT_SECONDS` (default 30 s) is
    refused with 503 "Order queue is too long". Redis is required by default (`KITE_WRITE_LIMIT_REQUIRE_REDIS=true`);
    there is a local-lock fallback (`:78-87`).
- **`PlaceOrderRequest`** (`backend/broker_api/orders/models.py:55-65`) has the fields `market_protection`,
  `autoslice`, and `iceberg_legs` (2–10).
  - Live F&O orders set `autoslice=true`; equity and paper sends do not. Kite child slices are linked back to the
    submitted parent through its `autoslice:<parent>` tag so fills remain step-attributed.
- **Broker REST routes: PARTIAL.** Their decorators were lost by accident in the `c598871` refactor.
  - Phase 0 (`15ab018`) **restored the read-only ones** under `/api`: orders, trades, positions, margins, charges,
    trigger range, realtime positions (initialize, realtime, stream, reconcile), order-runtime status, GTT reads and
    order-event reads. `tests/broker_api/test_orders_routes.py` checks which routes are on.
  - The **write** routes stay **unregistered by owner decision**: place, modify, cancel, basket, convert, GTT writes,
    process-now, ws enable/disable and postback. Hosted trading goes through the governed pipeline.
- **Order updates** arrive over the websocket through `market:order_updates`. Live orders flow internally through
  the worker routes (`backend/api/routers/worker_execution.py`), the bracket executor and MCP.

---

## 4. Hosted strategy lifecycle

### 4.1 Data model (`backend/strategies/models.py`)

- **`hosted_strategies`** (`:39-97`)
  - `default_execution_mode`: paper, dry_run or live.
  - `default_job_kind`: continuous or finite.
  - `authorization_mode`: `approval_based` (default) or `autonomous`.
  - `max_duration_s` and `progress_deadline_s` are capped at 7 days by the API (`backend/api/schemas/strategies.py:38-39`).
  - `stale_exit_policy`: none or `exit_on_worker_stale`.
- **`hosted_strategy_versions`** (`:100-130`) are immutable. Each holds:
  - `source`, limited to 256 KiB and hashed; it is never executed by the API.
  - `parameters_schema` (JSON Schema 2020-12, with no remote refs).
  - `capabilities_snapshot` with `data`, `trade` and `notify`. The default is data-only.
  - `risk_policy` (`backend/strategies/service.py:128-318,541-629`).
- **Per-version risk policy:** `max_loss_inr`, `notional_limit_inr`, `margin_limit_inr`, `protection`,
  `expiry_policy`, `allowed_structure_families` and `naked_permitted` (`backend/strategies/risk_policy.py:79-87`).
  - Effective policy = min(declared, operator, platform ceiling).
  - The operator policy is applied to **option** plans only (`admission.py:494`).
  - **PARTIAL:** there is no UI to author it; it is API only.
- **`hosted_strategy_schedules`** (`:133-215`): one per strategy. Kinds are daily, weekly, monthly and calendar.
- **`strategy_jobs`** (`:218-335`):
  - Lease, epoch and attempt fencing.
  - Status: queued, starting, running, fencing, recovery_required, stopped, failed or hung.
  - `identity_json` holds the bound evaluation.
- **`hosted_execution_grants`** (`:403-516`): standing authorisation. A grant is bound to the version, source hash,
  account, environment and policy hash, and there is one active grant per (strategy, account, environment).
- **`hosted_execution_requests`** (`:519-622`): requested → awaiting_approval or queued → dispatching → executed,
  refused, rejected or dispatch_unresolved.
- **`account_scope`**: paper needs a paper scope; live needs `kite:<ID>`. It must be listed in
  `HOSTED_STRATEGY_ACCOUNT_SCOPES`, which is an exact-match allowlist and denies by default
  (`backend/api/services/hosted_strategy_authz.py:10-66`).

### 4.2 API and UI

- **Routes** live in `backend/api/routers/strategies.py`:
  - CRUD: `:452-924`
  - versions: `:2642-2684`
  - jobs, run now, stop, logs and reconciliation: `:2776-3296`
  - schedule: `:716-833`
  - admission, reservation, approval and execute: `:1131-2445`
  - authorization and grants: `:1873-1993`
  - execution requests, approve and reject: `:2009-2088`
  - option runs, settlement, repair and rolls: `:1259-1693`
  - Owner actions are in `strategy_owner_actions.py` and option runs in `strategy_option_runs.py`.
  - The supervisor lifecycle is in `backend/api/routers/hosted_lifecycle.py`.
- **UI** (`frontend-next/features/strategies/components/*`):
  - A composer with a source textarea, readiness check, a schema field builder, capabilities and job kind.
  - A params form generated from the schema (`hosted-params-editor.tsx`).
  - The detail page has: new version, run now, an authorization panel (mode, grants, admission policy), an
    execution-requests panel with approve and reject, a schedule panel and an options panel.
    - The options panel covers runs, repair, exit, cancel-pending, flatten and stop (`hosted-options-panel.tsx`).
  - The job detail page shows stop, logs (polled every 5 s), notifications and reconciliation.

### 4.3 Runtime (supervisor)

- **Isolation: EXISTS.**
  - The child runs as uid/gid 10002 in its own process group.
  - rlimits: 2 GiB address space, **1 h CPU**, 256 files, 128 processes (`backend/strategies/supervisor_process.py:197-205,406-435`).
  - The environment is an allowlist (`supervisor.py:595-628`).
  - The source file is read-only after a hash check.
  - The image carries the SDK plus pandas, numpy and numba (`Dockerfile.supervisor`).
- **Lease, heartbeat and progress.**
  - Lease 120 s, heartbeat 30 s, progress poll 5 s (`supervisor.py:98-101`).
  - A child is stale after `progress_deadline_s` plus a 30 s grace.
  - Reaching `max_duration_s` means a fail-closed fence (`:961-1007`).
  - The child token expires at `max_duration_s` + 300 s (`hosted_lifecycle.py:476`).
- **After a crash or restart:** there is no reattach and no replay. Children are terminated and fenced on start
  (`supervisor.py:14-19,494-536`). Most ends go to `recovery_required`. Only a finite job that exits 0 with trade
  capability auto-continues (`continuation.py:450-472`).
- **Stop:** SIGTERM, then SIGKILL after 10 s (`supervisor.py:1038-1043`). **Stopping does not flatten or cancel
  orders** (`strategies.py:3202-3203`).
- **Concurrency: PARTIAL.** One child at a time per runner (`supervisor.py:720,1091-1102`). The `concurrency`
  setting is validated but not used, and compose pins one container.
- **Logs: PARTIAL.** They ship after the child terminates, are capped at 256 KiB, and are not streamed live
  (`hosted_lifecycle.py:60-61`, `strategies.py:3276-3279`). Near-live signals are `ctx.progress` and
  `log_decision_event`.
- **`continuous` vs `finite`: `continuous` is effectively a label.** The supervisor never reads `job_kind`. The
  only difference is that continuous jobs never auto-continue (`continuation.py:454-456`).
- **Many decisions per job:**
  - **EXISTS for run_now jobs.** They have no bound evaluation, so the child can mint fresh `evaluation_id`s
    (`backend/api/routers/worker_proposals.py:225-280`; test at `tests/strategies/test_proposals.py:776-794`).
  - **MISSING for scheduled jobs.** A scheduled job is bound to one evaluation: `EVALUATION_IDENTITY_MISMATCH`,
    plus `UNIQUE(strategy_id, evaluation_id)`.
  - **No test or example runs a periodic market-hours loop.**
- **Execution is tied to the live attempt.** The dispatcher refuses a request unless the originating job is still
  running with a live lease and the same run, token, epoch and attempt
  (`backend/strategies/execution_requests.py:1659-1696`). An approval that lands after the child has exited is
  refused, which is why the examples poll and call `ctx.progress` while they wait.

### 4.4 Scheduling (`backend/strategies/scheduling.py`)

- **Kinds: EXISTS.** daily, weekly, monthly and calendar, each with one `at_time` (`:59,128-133,246-338`). Misfire
  grace is 3600 s and overlap is deferred (`:64-70,109-116`).
- **At most one run per strategy per day.** There is one schedule per strategy, and occurrences are keyed by date.
- **MISSING:**
  - intraday or interval schedules
  - holiday awareness (daily fires every calendar day, including weekends; `:261-333`)
  - use of `window_end` / `squareoff_at` (stored but never read; `service.py:507-508`)
  - setting a manual pause (`manual_paused_at` is only ever set to None; `repository.py:1941,1976`)

### 4.5 SDK available to strategy code (`sdk/python/kite_algo_worker`)

- **Entry point.** `main(ctx)` receives a `ChildContext` with params, client, run, scratch, `execution_mode`,
  `progress`, `request_execution` and `owned_work` (`hosted.py:36-58,134-144`).
- **Data** (`market:read`/`market:stream`, from the `data` capability):
  - quotes, candles, history, indicators, index constituents, calendar and ticker search
  - SSE and websocket streams
  - universes (`endpoint_manifest.py:48-141,182-183`)
- **Options reads:** `get_chain`, `get_greeks`, `list_expiries`, `get_mini_chain`, `resolve_contracts`, the leg,
  delta and spread resolvers, `get_pcr`, `get_max_pain` and `ensure_session` (`options/client.py:31-172`).
- **Trading** (from the `trade` capability): `submit_proposal`, `request_execution`, `execution_requests`,
  `owned_work` and `submit_and_request_execution` (`managed_run.py:197-262`). Funds and positions reads come with it.
  - **Raw orders and option mutations are refused** for hosted children (`HOSTED_RAW_MUTATION_FORBIDDEN`,
    `backend/api/services/hosted_attempt.py:356-385`).
- **Notify:** `ctx.run.notify(...)` (`managed_run.py:89-112`).
- **Scheduled occurrence: EXISTS.** Fixed in Phase 0 (`cd9f048`): `attach_run` merges the server's
  `runtime_state`. A scheduled child reads `ctx.occurrence` (`job_id`, `evaluation_id`, `evaluation_kind`,
  `occurrence_key`, `due_at`) and proposes with that `evaluation_id` and `evaluation_kind="scheduled_occurrence"`.
  For run-now children it is `None`.

### 4.6 Examples (`examples/hosted_platform/`, `examples/hosted_acceptance/`)

| Example | What it shows |
| --- | --- |
| `index_indicator_strategy.py` | An index plus indicator signal trades one instrument through proposal → request → wait |
| `index_universe_equal_weight.py` | An owner universe driving an equal-weight rebalance |
| `nifty500_momentum.py` | A momentum portfolio with a breadth gate; calendar-aware |
| `options_index_setup_adjustment.py` | An index setup plus premiums and Greeks, with one bounded adjustment |
| `options_dynamic_straddle.py` | A straddle with wings: entry, delta-threshold resize, days-to-expiry roll, exit. **One decision per run.** |
| `run_phase5_acceptance.py` | The acceptance harness (real API, supervisor child, disposable Postgres). Evidence is in `evidence/` (an audit record; never delete). |
| `simple_entry_exit.py` | Entry and exit in one job, kept alive with a keep-alive loop |

---

## 5. Proposal → plan → admission → execution

### 5.1 Proposals (`backend/strategies/proposals.py`)

- **Identity.** `UNIQUE(strategy_id, evaluation_id)` (`attribution_models.py:477`). Evaluation kinds are
  `scheduled_occurrence` and `run_now`.
- **Idempotency.** The same payload hash is an idempotent retry. A different payload for the same id is refused
  with `PROPOSAL_EVALUATION_CONFLICT` (`:459-492`).
- **Capital basis.** `target_weights` freezes `capital_basis_inr` from the admission policy's `allocation_inr`
  (`:365-457`).
- **Target kinds** (`compiler/__init__.py:54-60`): `single_instrument`, `intent_bundle`, `target_weights`,
  `target_futures` and `option_structure`.

### 5.2 Compilers (`backend/strategies/compiler/`)

- **`single_instrument`.** Needs a pinned, active instrument. MIS overnight is refused. Lot multiple and maximum
  are **not** checked (`single_instrument.py:29-108`).
- **`intent_bundle`.** EQ legs only, with no cap on leg count. **Paper only**: live refuses it with
  `LIVE_PLAN_KIND_UNSUPPORTED`.
- **`target_weights`.** Needs a universe revision, reference prices, a capital basis and a cash buffer.
  - The `WeightsPortfolioCompiler.compile` limits have no production caller; admission enforces the equivalents.
- **`target_futures`.**
  - Takes whole lots, FUT only, an expiry, and a catalog lot size.
  - `FREEZE_LIMIT_EXCEEDED` fires **only if the payload declares `freeze_quantity`**, because the catalog has no
    freeze column (`futures.py:177-255`).
  - Supports a roll with `open_new`/`close_old`.
- **`option_structure`.**
  - Legs are given as direct coordinates or through a `selection` policy.
  - Leg quantity = `lot_size × ratio × structure_units`, with no upper bound.
  - Expiry policies, the naked flag, and exit/adjust references (`option_structure.py:56-574`).
  - **No freeze check.**
- **Tick size** is validated only for live gated LIMIT orders (`live_limit_orders.py:39`).

### 5.3 Admission and risk (`backend/strategies/admission.py`)

- **Operator policy (`StrategyAdmissionPolicy`).** Every field is optional: null means not enforced (`:175-187,1154-1226`).

| Refusal | Condition |
| --- | --- |
| `ADMISSION_POLICY_MISSING` | Live needs a policy with `allocation_inr` |
| `ALLOCATION_EXCEEDED` | Post-plan allocation over the limit |
| `INSTRUMENT_NOTIONAL_EXCEEDED` | Per-instrument notional over the limit |
| `GROSS_NOTIONAL_EXCEEDED` | Gross notional over the limit |
| `MAX_OPEN_INSTRUMENTS_EXCEEDED` | Too many open instruments |
| `ORDER_RATE_EXCEEDED` | Counts **admissions (reservations)** in `admission_window_seconds`, default 3600, against `admissions_per_window`, which has no default. It does not count orders. |

- **Option risk gate** (`:488-730`):
  - A strategy without a declared policy is refused with `STRATEGY_RISK_POLICY_MISSING`.
  - Other refusals: `OPTION_STRUCTURE_FAMILY_NOT_ALLOWED`, `OPTION_EXPIRY_POLICY_NOT_ALLOWED`,
    `OPTION_NAKED_NOT_PERMITTED`, `OPTION_MAX_LOSS_EXCEEDED`, `STRATEGY_NOTIONAL_LIMIT_EXCEEDED`, `MARGIN_INSUFFICIENT`.
  - Reducing exits and adjusts skip this gate.
- **Live margin and funds.** Evidence must be no older than `ADMISSION_MARGIN_MAX_AGE_SECONDS` (60 s). The basket
  margin is `required_margin_inr`. Cash below the requirement is refused, except for a staged CNC plan (`:1332-1385`).
- **EXISTS: `daily_loss_budget_inr` and the account-wide `account_daily_loss_cap_inr`.**
  - Strategy budget: admission is handed today's realized P&L for the strategy/environment, computed from its
    attributed confirmed fills with an average-cost fold (`backend/strategies/daily_loss.py`; the fill's own IST
    calendar day decides what is "today"), minus order-level charge estimates when the fill source records them. An
    exposure-INCREASING plan is refused with `DAILY_LOSS_BUDGET_EXCEEDED` once the realized loss reaches the budget;
    reductions are never blocked. Unreadable evidence still refuses with `DAILY_LOSS_BUDGET_UNAVAILABLE`.
  - Account cap: the optional `account_daily_loss_cap_inr` (platform live settings, migration `20260926_000053`)
    is tested against the broker's own day P&L, summed from the reconciled `account_positions` book
    (`realized_pnl` + unrealised, the same `pnl` the realtime positions service publishes). An exposure-increasing
    LIVE plan is refused with `ACCOUNT_DAILY_LOSS_CAP_REACHED` once the account loss reaches the cap; unreadable
    evidence with a cap set refuses too (fail closed), and reductions are never blocked.
  - `GET /api/platform/status` surfaces the cap state as `risk: {day_pnl_inr, cap_inr, cap_reached}`.
  - There is **no automatic flatten** here; a reached cap only refuses new exposure.
- **MISSING:**
  - A market-hours or holiday gate (`:1426-1430` says so).
  - A per-plan or per-day **count** cap on orders, trades or legs. The searches for `max_orders`, `max_trades` and
    `max_legs` found none. Order **rate** is limited to ≤10 Kite writes per second, with excess queued (§3).
- **Inert inputs:**
  - Futures peak margin: no caller passes `peak_capacity_inr`.
  - Paper funds at admission: `paper_funds` is never passed; the paper runtime checks funds per order instead.
  - The reconciliation freeze `is_frozen_coordinate` has no non-test caller.

### 5.4 Reservations, approvals, authorization, requests

- **Reservations** (`reservations.py`):
  - Postgres takes an account advisory lock.
  - Refusal: `CAPACITY_EXCEEDED`.
  - Valid for 900 s.
- **Approvals** (`approvals.py:356-600`):
  - An approval pins the plan hash, exposure snapshot, reconciliation version, catalog generation, version id,
    source hash, policy hash and the option run and generation.
  - Any mismatch is refused by name. Valid for 900 s.
  - **Not pinned to price.** Price is bounded only at live release.
- **Authorization** (`execution_authorization.py:1-18,831-992`):
  - `autonomous` mode plus an active grant means requests queue without a click.
  - Refusals: `GRANT_*`, and `AUTHORIZATION_MODE_NOT_AUTONOMOUS`.
  - A live grant is refused while `HOSTED_LIVE_ENABLED` is off.
- **Requests and dispatcher** (`execution_requests.py`, `execution_dispatcher.py`):
  - Requests are idempotent per (owner, plan, key).
  - The dispatcher's claims time out after 900 s.
  - An unproven claim becomes `dispatch_unresolved` and is never replayed.
  - The dispatcher is on unless `HOSTED_EXECUTION_DISPATCH_ENABLED` is falsy.

### 5.5 Paper executor (`backend/strategies/execution.py`, `backend/paper_runtime/`)

- **Steps.** Each leg's step is target minus the attributed current position, floored to the lot. Every step is a
  MARKET order (`:3149`).
- **Sequencing.**
  - CNC rebalance sells before it buys.
  - Options go hedge-first, with `OPTION_HEDGE_NOT_FILLED`.
  - Rolls acquire first.
  - A plan can execute only once (`PLAN_ALREADY_EXECUTED`).
- **Paper fills** (`paper_runtime/service.py`):
  - Fills at the opposite depth, or LTP.
  - Slippage is 0.
  - Lot multiples and funds are enforced.
  - Partial fills are opt-in: `PAPER_PARTIAL_FILL_RATIO` defaults to 1.0, a full fill.
- **Paper margin and charges are heuristics** (`margin_engine.py:28-36`, `charges.py:21-27`). There is no expiry
  or T+1 settlement.

### 5.6 Live (`backend/strategies/live_*.py`)

- **Gates:**
  - `HOSTED_LIVE_ENABLED`: off by default; truthy values only (`live_settings.py:19-37`).
  - `HOSTED_STRATEGY_ACCOUNT_SCOPES`.
  - **Lane gate** `HOSTED_LIVE_LANES` (committed `25beed9`):
    - Default deny.
    - Refuses `LIVE_LANE_NOT_ENABLED` for exposure-increasing plans and releases; reductions are never blocked.
- **Lanes** (`live_sequence.py:351-356`, `live_service.py:1342-1347`):

  | Lane | Plan kinds |
  | --- | --- |
  | `cnc` | `single_instrument` CNC, `target_weights` |
  | `mis` | `single_instrument` with MIS |
  | `futures` | `target_futures` / roll |
  | `options` | `option_structure` |

  - Single, MIS and futures take **one leg** (`LIVE_PLAN_COMPOUND_UNSUPPORTED`).
- **Quotes** must be ≤5 s old (`live_adapter.py:93,1187-1206`).
- **Order types.**
  - Gated dependent legs use a **bounded LIMIT** (`live_limit_orders.py`): drift is 0.5%
    (`LIVE_OPTION_LIMIT_MAX_DRIFT_PCT` / `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT`), passive tick rounding, and a 10 s
    timeout (`LIVE_GATED_LIMIT_TIMEOUT_SECONDS`).
  - The timeout is swept by the 15 s consumer, so a LIMIT can live longer than 10 s.
  - **Immediate and protective legs are MARKET** (`live_adapter.py:2110-2112`).
- **Staged CNC financing** (`financing.py`): increases are authorised only against confirmed reductions, and funds
  are read under the account lock.
- **Ingestion, ledger and repair.** `live_ingestion.py` handles broker fills through the barrier, attribution and
  consumption. `live_lane_ledger.py` records lane-owned fill effects. `live_repair.py` offers abandon residual and
  abandon staged dependent, and never re-sends.
- **Weight sizing: one rule.** `financing.weight_target_quantity` (weight × buffered capital / price, floored to
  the lot) is shared by admission, paper and live. Fixed in Phase 0 (`3ceed0e`); live used to round a sub-lot
  weight up to 1 lot.
- **Freeze-quantity slicing: via Kite autoslice.** Every step remains one API order for its full quantity, with
  `autoslice=true` on live F&O sends (`live_adapter.py:2104-2125`); Kite creates the child orders and the ingestion
  ledger aggregates parent/slice fills by cumulative quantity.

### 5.7 Attribution, accounting, owner actions

- **Attribution.** A quantity-only projection per (account, strategy, environment, identity, product)
  (`attribution_models.py:211-226`). It is **not per run** and carries **no P&L**.
- **Account truth.** Broker = Σ attributed + manual (`account_truth.py:1-40`).
- **Per-run P&L** exists only for paper display (`paper_runtime/run_state.py`).
- **Strategy realized P&L** is derived on demand for the daily-loss controls
  (`backend/strategies/daily_loss.py`): today's attributed fills folded at average cost. It is not stored on the
  projection.
- **Owner actions** (`backend/api/services/owner_actions.py`):

| Action | Paper | Live | Notes |
| --- | --- | --- | --- |
| Cancel pending | EXISTS | EXISTS | Reductions and protective legs cannot be cancelled |
| Dead-submission disposition | EXISTS | EXISTS | |
| Option owner exit | EXISTS | EXISTS | Via `option_run_repair.py:427-621` |
| Flatten | EXISTS | **MISSING for non-option books** | Refuses `FLATTEN_LIVE_NONOPTION_UNSUPPORTED` |

---

## 6. Options subsystem

- **Expiry selectors: EXISTS.** `nearest`, `current_week`, `next_week`, `current_month` (the last expiry of the
  month) or an explicit date (`backend/options/market/expiry_selectors.py:33-86`). There is no `next_month`.
- **Relative strikes (ATM ± N, ITMn/OTMn, `delta_target`): PARTIAL.**
  - They are resolved by `resolve_selection` (`backend/options/market/service.py:115-240`,
    `selection.py:16-223`), exposed to workers at `.../selection/resolve`, and wrapped by the SDK helpers
    `resolve_offset_leg`, `resolve_delta_leg` and `resolve_spread` (`sdk/.../options/resolvers.py:97-157`).
  - **A strategy resolves first and then proposes concrete legs.** A `selection` leg inside a proposal is refused
    with `SELECTION_POLICY_UNRESOLVABLE`, because production injects no `chain_resolver` (`proposals.py:241-243`,
    `option_structure.py:499-508`).
  - The examples use a point offset from spot in strategy code.
  - **Lot size.** When a leg omits `lot_size`, the resolver uses the contract's lot size from the chain snapshot.
    If none is available it refuses with `OPTION_SELECTION_LOT_SIZE_UNAVAILABLE`. Fixed in Phase 0 (`391adf0`); it
    used to default to 1.
- **Legacy structure templates.** `backend/options/strategy/compiler.py` defines nine templates for preview only
  (`POST /api/options/strategies/preview`): buy_call, sell_put, bull and bear spreads, short and long
  straddle/strangle, iron condor. It also sets default index and premium rule boundaries.
- **Option runs** (`backend/options/execution/`):
  - `OptionRunState` and the status lifecycle, including `adjusting` and `settled` (`models.py:12-142`,
    `lifecycle.py`).
  - Durable store with CAS on status and generation (`durable_store.py:409-506`).
  - A generation history capped at 10 (`execution.py:2380-2413`).
- **Adjust: EXISTS for paper and live.**
  - **Resize** keeps the same expiry and changes the quantity.
  - **Roll** is triggered when the desired expiry differs from the held one (`execution.py:1644-1652`). It acquires
    first, SELL acquires are hedge-gated, and the old generation is released behind
    `RULE_OPTION_ROLL_RELEASE_GATE` (`live_sequence.py:129-132,895-920`). It completes only when the released legs
    are proven flat.
  - Admissibility refuses, each by name: not owned, adjust already in flight, stale basis, short left unhedged,
    owner unknown (`plan_binding.py:1191-1300`).
  - **The strategy decides when to roll.** There is no platform scheduler for rolls or expiry escalation:
    `OptionExpiryPolicy.check` is used only at entry (`live_adapter.py:781-812`), and the futures
    `check_expiry_cutoff` has no caller.
- **Hedge fill gate**: `backend/options/protection/hedge_gate.py:82-100`, called at `live_service.py:953-990`.
- **Staged exit**: shorts close first, and the stage is made durable before the send
  (`backend/options/protection/staged_exit.py`, `exit_builder.py:50`).
- **Protection ownership**: one owner row per run with an `owner_epoch` CAS (`ownership.py:340-737`). The entry
  claim is at `plan_binding.py:2199-2217`.
  - **PARTIAL:** `transfer()` has no production caller.
- **Repair** (`repair.py`) covers partial entry, partial exit, cleanup and a finished adjust. An ambiguous run
  escalates.
- **Position Greeks: MISSING.** `strategy_option_runs.py:719-727` returns `available=False` explicitly. Only the
  example sums delta, on the worker side.
- **UI.** The options panel is part of the strategy detail page. There is **no chain viewer, strike picker or
  payoff chart**; `/options` redirects to `/strategies`.

## 7. Protection (risk exits)

- **Generic runtime: EXISTS.** A 5 s loop (`WORKER_PROTECTION_ENABLED`, `WORKER_PROTECTION_INTERVAL_SECONDS`).
  - Rules (`backend/api/services/protection.py:117-613`):
    - per position: SL %, target % and trailing SL %
    - basket: SL %, target %, trailing activate/drawdown
    - `worker_stale` exit
    - MIS square-off (NSE 15:20, NFO 15:25 by default; `background.py:20-35`)
  - **All rules are a percentage of average price.** There are no rupee, underlying or Greek rules.
  - Live prices come from `realtime_positions_service` (`worker_protection.py:427-483`).
  - A durable claim precedes any submit. Structures exit through `StagedStructureExit`; others exit the whole book
    (`protection_runtime.py:58-313,633-784`).
  - Exit orders are MARKET without `market_protection`; live F&O exits use Kite autoslice (`:719-733`).
- **Option rule vocabulary: PARTIAL, effectively dead.**
  - The metrics are `index_ltp`, `combined_premium`, `combined_premium_change_pct`, `strategy_mtm` and
    `open_quantity` (`backend/options/protection/models.py:24-30`).
  - **Nothing writes `metadata["protection_metrics"]`.** Only `open_quantity` is derived (`metrics.py:23-66`), and a
    missing metric is skipped silently (`evaluator.py:6-30`).
  - A triggered verdict only **blocks** increases (`OPTION_ADJUSTMENT_PROTECTION_ACTIVE`,
    `OPTIONS_PROTECTION_TRIGGERED`). It never places an exit.
  - `bridge.py` has no importers, and its vocabulary doesn't match the runtime.
  - **The only underlying stop today is strategy code** that reads spot and proposes an exit, and that dies with the
    worker.
- **Owner policy with generic position rules: fixed** in Phase 0 (`2502123`). It was confirmed by
  `test_generic_owner_rules_do_not_block_a_resize`: metric-less rules made every resize-up or roll acquire
  "unreadable". `_option_protection_block` now skips metric-less rules; an unsupported option metric still fails
  closed.

## 8. Other features

- **Alerts and workflows: EXISTS** (`backend/workflows`).
  - Clocks: `ltp` or `candle_close`.
  - Operators: gt/lt, crosses, within, rises/falls %, breaks previous high/low.
  - Indicators: sma, ema, wma, rsi, macd, atr, bollinger, supertrend, vwap_session, volume.
  - Also all/any/not groups, sequences, breadth, pairs, relative strength, fundamentals, and a canvas editor
    (`models.py`, `registry.py:20-233`).
  - **Alerts only notify. They never trade.**
- **Screeners and universes: EXISTS.** Screeners run on NSE-calendar schedules with rank and top-N attachments.
  Universes are resolved every 300 s (`backend/screeners`, `worker_entry.py:896-951`).
- **Notifications: EXISTS, outbound only.** Telegram `sendMessage` and ntfy (`backend/notifications/adapters/`).
  There are **no inbound commands**.
- **Journal and analytics: EXISTS.** Daily, weekly and monthly journal; episodes with notes; analytics for
  summary, strategy, equity, costs and paper-vs-live; the live journal projector (`backend/journaling`,
  `api/routers/{journal,analytics}.py`).
- **Paper runtime: EXISTS.** Fills, margin and charge heuristics, and account reset/upsert
  (`/api/system/paper/accounts/...`).
- **Algo runtime: PARTIAL.** `backend/algo_runtime` is an in-process, tick- and candle-driven kernel with a
  registry, snapshots (candles, indicators, option reads) and triggers. **No algo is registered in production code**;
  `register(` is called only in tests.
- **MCP: EXISTS.** A Go adapter with 73 tools. The read, paper and live profiles are enforced
  (`mcp/go/internal/policy/policy.go:119-220`).
  - **PARTIAL:** the bearer token is optional and compared in non-constant time. The allowed hosts/origins are not
    implemented. The profile defaults to `live` in compose (`compose.mcp-go.yml:21`).
- **Auth.** A single admin user; JWT access 15 min and refresh 14 days, in httpOnly cookies (`backend/app/auth.py`).
  - Worker tokens are hashed and scoped (account, modes, actions, templates).
  - **Gaps:**
    - market-runtime and the `/ws` path are unauthenticated.
    - Postgres and Redis are host-published with default credentials.

## 9. Frontend map (`frontend-next/app/(app)`)

**Left rail:** Dashboard, Alerts, Strategies, Journal, Settings.

| Route | What it shows |
| --- | --- |
| `/dashboard` | Paper net, live control P&L, broker positions, health |
| `/alerts` | List, create, detail, edit, canvas, operations |
| `/alerts/screeners/*`, `/alerts/universes/*` | Screeners and universes |
| `/strategies` | List |
| `/strategies/new` | Composer |
| `/strategies/[id]` | Detail |
| `/strategies/[id]/jobs/[jobId]` | Job detail |
| `/journal/*` | Daily, week, month, episodes, analytics |
| `/settings` | Reference data, worker access, workspace, and an APIs placeholder |

- `/trading`, `/options` and `/custom-display` redirect to `/strategies`. `/paper` redirects to `/strategies?mode=paper`.
- The paper/live `WorkspaceMode` comes from `?mode=` or session storage.
- **No manual order entry. No settings for live trading behaviour.**

## 10. Configuration

- **`.env.example`**: DB, Redis, Kite credentials plus TOTP, JWT and admin credentials, alerts, Telegram, ntfy,
  MCP and the market-runtime URL.
  - Stale entries: `MEILI_*`, `VITE_*`, and `MARKET_RUNTIME_ENABLED=false`, which would crash the app if compose
    didn't override it.
- **Trading behaviour is env-only and not in `.env.example`:**

| Setting | Default |
| --- | --- |
| `HOSTED_LIVE_ENABLED` | off |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | deny |
| `HOSTED_LIVE_LANES` | deny |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | on |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | 60 |
| `ADMISSION_RISK_*` ceilings | none |
| `LIVE_OPTION_LIMIT_MAX_DRIFT_PCT` | 0.005 |
| `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` | 0.005 |
| `LIVE_GATED_LIMIT_TIMEOUT_SECONDS` | 10 |
| `PAPER_PARTIAL_FILL_RATIO` | 1.0 |
| `SCHEDULE_MISFIRE_GRACE_SECONDS` | 3600 |
| `FUTURES_EXPIRY_WARNING_DAYS` | 5 |
| `WORKER_PROTECTION_*` | — |
| `ACCOUNT_INGEST_*` | — |
| `KITE_WRITE_OPS_PER_SEC` | 9 |
| `HOSTED_SUPERVISOR_*` | — |

- **Hard-coded:** quote max age 5 s; approval and reservation validity 900 s.
- **Settings stored in the DB or UI:** marketwatch subscriptions, worker tokens, notification channels, paper
  account balance and risk, admission policy, authorization mode and grants.
- **Agents never edit `.env*`.**

---

## 11. Capability matrix by strategy type

| Type | Plan kind / lane | Paper | Live | Decision cadence today | Protection |
| --- | --- | --- | --- | --- | --- |
| Portfolio / investing (equal weight, momentum) | `target_weights` / cnc | EXISTS | EXISTS (fake broker only), staged financing | Once-a-day schedule fits | Basket % rules |
| Equity signal, delivery | `single_instrument` CNC / cnc | EXISTS | EXISTS (fake broker only), one leg | **Needs intraday.** Only a run-now loop, not proven | Position % rules |
| Intraday equity | `single_instrument` MIS / mis | EXISTS | EXISTS (fake broker only), one leg | Needs intraday (same gap) | % rules plus MIS square-off |
| Futures | `target_futures` / futures | EXISTS | EXISTS (fake broker only), one leg, roll state machine | Needs intraday (same gap) | % rules; freeze refused only if declared |
| Options | `option_structure` / options | EXISTS | EXISTS (fake broker only): hedge gate, LIMIT, roll, repair | Needs intraday (same gap) | % rules, staged exit; **index, premium and MTM rules dead**; no Greeks |
| Order bundle | `intent_bundle` | EXISTS | MISSING | — | — |

## 12. Known gaps, ranked by production risk

1. **No real broker round trip yet.** Every live claim is fake-broker proven (C2 not started).
2. **Freeze limits.** Nothing slices orders, including protective exits. `autoslice` exists in the order model but
   is never set.
3. **Option index, premium and MTM stops are dead.** No position Greeks. No rupee or underlying rule in the generic
   runtime.
4. **A live daily loss budget blocks all live plans.** There is no account-wide loss cap and no single kill switch.
   Stopping a job does not flatten, and live non-option flatten is refused.
5. **Intraday cadence.** Schedules fire once a day. The run-now loop is unproven. `continuous` is only a label. One
   child per runner. The 1 h CPU limit. Logs only after exit. Execution dies with the attempt.
6. **No market-hours or holiday gate** in the pipeline or the scheduler.
7. **Relative legs are not part of the frozen plan**, because no `chain_resolver` is injected.
8. **Phase 0 fixed** (2026-09-26): Greeks T in IST; the one weight-sizing rule; the resolver lot size;
   owner-policy rules blocking adjusts; scheduled children reading their occurrence; read-only broker routes; and
   the live resize test flake (chain freshness in the test).
9. **Pre-existing test debt:**
   - `tests/options` is fully green (225 passed) after the Phase 0 test repair (`e4375d9`).
   - `tests/strategies/test_execution_dispatcher.py` fails from event-loop pollution when run with the whole
     directory; the file passes on its own.
10. **Security hygiene:**
    - market-runtime and `/ws` have no auth.
    - Postgres and Redis use default credentials on host ports.
    - The MCP token is optional.
    - The manual order write routes are off by decision; read-only routes are restored.
11. **UX:**
    - Trading behaviour lives in env.
    - Risk policy is API-only.
    - No chain or payoff UI.
    - No inbound Telegram commands.
    - No count cap on orders per plan or day. Only the rate limit (≤10/s, queued) exists.

## 13. Corrections to earlier claims (2026-09-26 session)

- **"No relative legs."** Wrong as stated. ATM ± N, ITM/OTM and delta targeting exist as a resolve helper and in
  the SDK. They are only missing *inside* the frozen proposal.
- **"No weekly/monthly roll."** Wrong. The expiry selectors and the governed option roll exist. What is missing is
  a platform-driven roll or expiry scheduler.
- **"Orders capped at 10."** The owner means the **order rate limit**: at most 10 Kite writes per second, with the
  rest queued into the next slots (see §3). That exists and applies to every order path. There is **no** cap on
  the total number of orders per plan or per day.

## 14. Working conventions

- Testing policy, PostgreSQL rules and never deleting evidence: see `AGENTS.md`. For the repo map and ownership
  lanes, see `CONTRIBUTING.md`.
- Runbooks per live lane: `documents/runbooks/README.md` and `hosted-live-{cnc,mis,futures,options}.md`.
- Plan and progress: `documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md`.
- Competitive plan: `documents/openalgo-competitive-production-plan-2026-09-26.md`.
- Orchestration (Claude plans, Codex implements): `.claude/skills/codex-orchestrate/SKILL.md`.
