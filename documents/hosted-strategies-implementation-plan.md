# Platform-hosted Python strategies — implementation plan (bounded)

**Status:** EXECUTED (2026-09-15). Every slice in this plan was implemented,
tested and deployed; the per-slice commits, the live data-only acceptance
evidence and the remaining limits are recorded in
`documents/hosted-strategies-supervisor-slice-report.md` §12 and the release
manifest §9. Kept as the plan of record.
**Design:** `documents/hosted-strategies-design.md` (this plan implements only that design).
**Explicitly out of scope (whole plan):** Go rewrite, MCP, live hosted execution, general backtesting,
custom dependency environments, advanced signal consumption, a universal strategy engine, and any
change to alerts/screener evaluation. **The alerts/screener release and its deployment remain a
separate workstream and must not be bundled here.**

Initial modes: **paper + dry_run** (`dry_run` on options = preview only). Supervisor: **dedicated
`strategy-runner`**, **no automatic restart/reattach**, **single-child concurrency default 1**.

---

## 1. Migrations and schemas (additive; names indicative)

| Table | Key columns / notes |
| --- | --- |
| `hosted_strategies` | `id` (`hs_<uuid>`), **`owner_id` (app owner)**, `name`, `template_id` (`hosted:<id>`), `description`, `default_execution_mode` (paper/dry_run), `default_job_kind` (continuous/finite), `default_account_scope`, `max_duration_s` (default configurable, e.g. 21600), `stale_exit_policy`, `progress_deadline_s`, `status`, timestamps |
| `hosted_strategy_versions` | `id`, `strategy_id`, `version`, `source` TEXT, `source_sha256`, **`parameters_schema` JSON**, **`capabilities_snapshot` JSON**, `created_by`, `created_at`; **immutable** |
| `hosted_strategy_schedules` | `id`, `strategy_id`, **`version_id`, `params_snapshot` JSON**, **`execution_mode`**, **`policy_snapshot` JSON**, **`capabilities_snapshot` JSON**, `job_kind`, `every`, **`weekday`**, `at`, `timezone`, `window_end`, `squareoff_at`, `enabled`, `manual_paused_at`, timestamps |
| `strategy_jobs` | `id`, `strategy_id`, `version_id`, `owner_id`, **`job_kind`** (continuous/finite), **`execution_mode`** (paper/dry_run), `desired_state`, `occurrence_key` UNIQUE, **`run_id` TEXT** (matches `algo_worker_runs.strategy_run_id`), `token_id`, `lease_owner`, `lease_epoch`, `lease_until`, `attempt`, `status` (queued/starting/running/fencing/**recovery_required**/stopped/failed/hung), `identity_json` (boot_id, container_id, pid, pgid, proc_start_time), `last_progress_at`, `exit_code`, `log_ref`, timestamps |
| `signal_events` extensions (H2) | add nullable `source_kind` (default `'workflow'`), **`owner_id` (hosted strategy app owner)**, **`run_id` TEXT** + index; **no FK change** |
| `option_run_states` | already exists (`backend/schema.sql:1212`) — no new migration |

All additive/nullable; alert/screener rows unaffected. **The supervisor holds no DB credentials**; it
uses the lifecycle API (below). The API's hosted-lifecycle endpoints authorize by the job's
`lease_owner`/`lease_epoch`/`attempt`.

---

## 2. H1 — Hosting core (paper + dry_run)

### 2.1 Backend (PROPOSED files)

1. **Strategy store API** (`backend/api/routers/strategies.py`, app-cookie): CRUD, versions, schedule,
   `POST /{id}/start|stop|cancel|flatten` (targets an **immutable job/run id**), logs, jobs.
2. **Hosted lifecycle API** (`backend/api/routers/hosted_lifecycle.py`, narrow service credential —
   *not* a worker token): `claim_job`, `job_state`, `create_run`, `mint_run_token`,
   `claim_session|heartbeat|release|fence`. Every mutation **authorizes against the persisted lease/
   attempt authority**; the control plane performs run-create/token-mint server-side.
3. **Service** (`backend/strategies/service.py`): token mint is idempotent on `(job_id, attempt)`;
   **ordering is create token → create run bound to that token → claim session**, recording
   `token_id`/`run_id` on the job so a retry reuses them (never a duplicate credential).
4. **Supervisor** (`backend/strategies/supervisor.py`): claims due jobs via the lifecycle API
   (lease/attempt CAS + UNIQUE `occurrence_key`); spawns one child (identity in §1 schema); heartbeats
   via the lifecycle API; **no reattach** — a `running` job found after restart becomes
   `recovery_required`. Cleanup by recorded identity (boot_id/container_id/pid/pgid/start_time), never
   by script path.
5. **SDK (PROPOSED, additive):** one attach signature
   `attach_run(run_id, *, session_nonce, config) -> ManagedRun` (no alternate contextmanager);
   `ManagedRun.progress(note=None)`; `ManagedRun.notify(text, channels, idempotency_key)`;
   `resolve_futures_contract(underlying, expiry)`.
6. **Options guard:** propagate the run's `execution_mode`; **paper only**; `live` rejected;
   **`dry_run` options mutation rejected (preview only)**; no synthetic fills ever surfaced.
7. **Stop/Cancel/Flatten** by immutable ids; Stop kills, releases, revokes; policy exits labelled.

### 2.2 Acceptance checks (H1)

| Scenario | Check |
| --- | --- |
| Happy paper run | Strategy runs in paper; appears as a worker run with P&L/journal; child env has **no** DB/broker/supervisor secrets |
| Attach-only + authority | Child **cannot** heartbeat (action absent, 403); supervisor heartbeats via the lifecycle API; a stale `lease_epoch` is refused |
| Hung child | Progress stalls past `progress_deadline_s` while the process lives → job **fenced to `recovery_required`**, no auto-restart; parent heartbeat does **not** hide it |
| Lease loss | Lease loss revokes old authority, marks `recovery_required`; **no run reuse, no automatic replay**; a new attempt requires a **new run and token** and cannot start until reconciliation |
| Ordering / no dup token | create-token → create-run-bound-to-token → claim; a retry with the same `(job, attempt)` reuses `token_id`/`run_id` and mints no second credential |
| Identity | Cleanup uses boot_id/container_id/pid/pgid/start_time; a recycled PID is not treated as the same process |
| Stop semantics | Stop kills + revokes, does **not** cancel/flatten; a policy exit is shown separately |
| Options guard | Paper options enter behaves as paper; **dry_run mutation rejected (preview only)**; `live` errors; never synthetic "filled" |
| Version/params | A running version is pinned; `parameters_schema` is part of the immutable version; a **new draft version** may be saved; invalid params fail before spawn |
| Concurrency | Two occurrences cannot run two children for one strategy (default 1) |

---

## 3. H2 — Run-scoped notifications

1. Migration extending `signal_events` (§1): nullable `source_kind`/`owner_id`/`run_id` TEXT; no FK change.
2. Repository `enqueue_run_notification(owner_id, run_id, channels, text, idempotency_key)`: event +
   deliveries in one transaction; unknown channel → **422**; same key different content → **409**.
   **Loader/rendering adapter** so workflow assumptions are not imposed.
3. Worker endpoint `POST /worker/runs/{id}/notify` (action `notifications:publish`); SDK
   `ManagedRun.notify(text, channels, idempotency_key)`.
4. Notify-only token: `[notifications:publish, runs:read]` — **no heartbeat, no order rights**.
5. Alert/screener delivery **unchanged** (regression-pinned).

### 3.1 Acceptance checks (H2)

| Scenario | Check |
| --- | --- |
| Notify-only child | Can attach/notify; heartbeat → 403 (action absent); intents → 403 |
| Atomic + explicit | Event + deliveries in one transaction; unknown channel → 422; enqueue failure explicit |
| Idempotency | Repeat key deduped; same key + different content → 409 |
| Owner binding | Event `owner_id` equals the **hosted strategy's app owner** (persisted), not account scope |
| Isolation | Provider outage/suppression affects no intent |
| Regression | Alert + screener delivery tests pass unchanged |

---

## 4. H3 — Scheduling and operations

1. Scheduler mirroring screener semantics (unique `occurrence_key`, lease, coalescing) with the
   **bounded** syntax: `every ∈ {1d,1w}`; **`weekday` required for `1w`**; `at ∈ {HH:MM, session_close}`;
   `timezone=Asia/Kolkata`; **NSE only (proposed v1 restriction)**. Reject month-end/MCX/currency/
   sub-daily. `session_close` only for finite data tasks that complete within the session.
2. A **missed catch-up that falls outside the permitted window expires** (no run outside the window).
3. Occurrences pin `version_id` + **params/capabilities/policy snapshots**.
4. Overlap skip (recorded); manual stop → schedule **paused** (resumable).
5. Ops: no-reattach reconciliation, orphan/janitor by recorded identity, token revocation sweep,
   bounded/redacted logs with retention, metrics.

### 4.1 Acceptance checks (H3)

| Scenario | Check |
| --- | --- |
| Scheduled run | Fires at the NSE window; holidays/weekends skipped |
| 1w weekday | `every=1w` without `weekday` is rejected; with it, fires on that weekday |
| Missed outside window | A catch-up outside the window expires; no run |
| session_close | Offered only for finite in-session data tasks; no trade-after-close promise |
| Rejected schedules | Month-end / MCX / sub-daily rejected at authoring with the reason |
| Restart | A `running` job after restart becomes `recovery_required` (no reattach, no duplicate) |

---

## 5. Frontend handoff (separate from the alerts release)

Pages under `frontend-next/app/(app)/strategies/` (PROPOSED): list (status/next-run), detail (source +
versions, params form from `parameters_schema`, schedule editor enforcing the bounded syntax,
start/stop/cancel/flatten **by immutable id** with explicit consequences, logs, notification history,
run/PnL/journal link). Before start, show **max-duration** and the **per-strategy stale-exit policy**.
Notification-only strategies hide trading controls. **Do not touch or bundle the alerts/screener
routes.** Acceptance: register→configure→paper start→inspect→stop; stop ≠ cancel ≠ flatten ≠ policy
exit; a hung/`recovery_required` job is visible.

---

## 6. Exact exclusions

- Live hosted execution; live multi-leg options submission.
- General backtesting/research; custom dependency environments; multi-file packages.
- Advanced signal/screener consumption.
- Activating or deleting `algo_runtime`; Go; MCP.
- Any change to alerts/screener evaluation/delivery, or to the external worker contract beyond the
  additive `attach_run`/`progress`/`notify` SDK additions.
- Bundling the alerts release or its deployment.

---

## 7. First bounded slice — schema + authorization foundation (NOT started)

**Purpose:** establish the durable schema and the **authorization gates** that must exist *before* any
process or execution is allowed. **No child process, no run creation, no order/notification path in
this slice.**

**Deliverables (exact):**
- **Migrations:** `hosted_strategies`, `hosted_strategy_versions`, `hosted_strategy_schedules`,
  `strategy_jobs` as defined in §1. Additive only.
- **Files:** `backend/api/routers/strategies.py` (store CRUD + version pinning, app-cookie);
  `backend/strategies/service.py` (validation + `parameters_schema` checks; **no** run/token/spawn);
  `backend/algo_runtime/account_scope.py` reuse for scope parsing.
- **Authorization gates (must be enforced, not merely declared):** (a) hosted-lifecycle mutations
  reject a stale `lease_epoch`/`attempt`; (b) a lease loss transitions a job to `recovery_required`
  and **blocks any replacement attempt until reconciliation** — tested at the service level without
  spawning; (c) a child run token **cannot** carry `heartbeat` (validated at mint, even though minting
  is deferred).
- **Tests:** migration up on Postgres; version immutability (source/parameters_schema); a
  `lease_epoch`/`attempt` CAS test proving stale authority is refused; a state-machine test proving
  `recovery_required` blocks replacement; a token-composition test proving `heartbeat` is rejected for
  child tokens; `parameters_schema` validation rejects bad params **before** any launch path.

**Explicitly not in this slice:** supervisor process, spawning, run/token creation, notifications,
scheduling loop, frontend. Those follow in H1/H2/H3 once these gates are green.

**Sequencing after review:** Slice 0 (this) → H1 → H2 → H3; frontend handoff tracks H1.

## 8. Coordinator review — implementation constraints

The first schema/authorization slice is the next implementation candidate; no implementation is authorized by this document alone.

- The `strategy_jobs` schema must also persist the params, capabilities and policy snapshots described in the design, plus effective max-duration and progress-deadline values. Do not reconstruct a queued job from mutable strategy defaults.
- Browser mutations must preserve the existing app's owner/account authorization and origin/CSRF controls, including cross-owner identifier handling. A single-operator deployment is not a reason to omit those checks.
- Hosted fencing must cover child-facing mutation endpoints as well as the internal lifecycle API. Removing `heartbeat` from a token is insufficient unless all claim/heartbeat/release paths enforce the hosted child restriction.
- Mint retries cannot recover plaintext credentials from a stored hash. The lifecycle protocol must specify one-time credential handoff and what happens when that response is lost; fail the attempt and reconcile rather than inventing recoverability or silently issuing a second credential.
- The proposed `session_close` schedule still has an ambiguous completion window. Defer that trigger from initial scheduling; implement explicit clock times and weekday with a validated allowed window first. NSE-only scheduling remains a proposed scope restriction, not an exchange restriction on manually launched strategies.
- Six hours is a proposed configurable maximum run duration, not a suitability guarantee for every F&O session. Require explicit stale-exit and progress-deadline configuration until defaults are agreed. Resource limits and log-retention defaults must be finalized before the supervisor slice.
- Operator Cancel/Flatten after the child token is revoked needs an app-authorized backend adapter to existing execution services, with run/account checks; it cannot reuse a revoked child credential.
