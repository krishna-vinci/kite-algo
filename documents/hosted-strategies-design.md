# Platform-hosted Python strategies — design (revised)

**Status:** IMPLEMENTED AND DEPLOYED (2026-09-15). The design below was
implemented in reviewable slices; the running state, the live data-only
acceptance evidence and the deliberate limits are recorded in
`documents/hosted-strategies-supervisor-slice-report.md` §12 and the release
manifest §9. This document is kept as the design record — read it together with
that report, which is authoritative for what is deployed.
**Supersedes:** `documents/hosted-strategies-proposal-draft.md` (deliberately
untracked: retained locally as a lead, superseded by this document).
**Scope guard:** alerts/screeners evaluation untouched; external workers unchanged; no Go rewrite,
no MCP, no second order pipeline, no universal engine. Initial execution modes: **paper + dry_run
only.** Runner placement: **dedicated `strategy-runner` supervisor, one child per run.**
Run-scoped notifications: **in the minimum hosting release.** Single-child concurrency default **1**.

> Where a draft or earlier report conflicts with current source, current source wins.
> **PROPOSED** marks anything not yet in the SDK/backend.

---

## 0. Source-backed findings that shape the design

| # | Finding | Evidence |
| --- | --- | --- |
| F1 | `client.run(config)` **claims a session and heartbeats on enter by default**, and `release_on_exit` releases it | `sdk/python/kite_algo_worker/client.py:160-198` |
| F2 | `RunConfig.strategy_run_id` already makes the SDK **attach** to an existing run instead of creating one | `client.py:1285-1305`; `run_config.py:15` |
| F3 | Session ownership is a **nonce CAS** on the run row; intents/exit require the request nonce to match when a session is active | `backend/api/repositories/algo_worker_repo.py:769-860`; `backend/api/routers/worker_shared.py:172-176` |
| F4 | Token revocation/expiry is **backend-enforced**: non-active/expired → 401 | `worker_shared.py:221-230` |
| F5 | Options **run state is durable** (`option_run_states`) | `backend/options/execution/durable_store.py:13-318`; `backend/schema.sql:1212` |
| F6 | Options `enter`/`exit` use the **paper runtime only in paper mode**; otherwise injected results or a **deterministic synthetic "filled" stub** | `backend/options/api/execution_router.py:299-373,396-452`; `runtime_instance.py:10-76` |
| F7 | The worker options wrapper does **not propagate the run's `execution_mode`**; mode comes only from the action payload | `worker_options_router.py:149-204`; `execution_router.py:126-151` |
| F8 | `deliveries.event_id` is a **NOT NULL FK to `signal_events.id`**; `signal_events` has nullable `subscription_id`/`workflow_id`, unique `occurrence_key`, JSON `evidence` | `notifications/repository.py:89`; `workflows/repository.py:196-208` |
| F9 | The idle `algo_runtime` kernel has an **empty registry** and its `IntentBridge` has **no notification handler** | `backend/app/bootstrap.py:388-429`; `algo_runtime/intent_bridge.py:139-143` |
| F10 | `exit_on_worker_stale` **auto-closes** stale paper runs and defers live stale runs to protection when enabled | `backend/api/services/runtime_recovery.py:205-255` |
| F11 | Scheduler pattern to mirror: unique `occurrence_key` + lease + missed-run coalescing | `backend/screeners/scheduler.py:1-30,69` |
| F12 | No in-platform executor/store/scheduler exists (`subprocess`/`runpy`/`importlib` absent from `backend/`) | repo grep |
| F13 | `algo_worker_runs.strategy_run_id` is **TEXT** (PK); `execution_mode` CHECK ∈ {paper,dry_run,live}; `WorkerToken` has **no owner field** (id/name/account_scope/modes/actions/templates/status/expiry only) | `backend/schema.sql:953-971`; `algo_worker_repo.py:142-150` |

---

## 1. Architecture and ownership boundaries

```
 Browser ── /api/strategies/* (app-cookie) ── FastAPI control plane
   │                                            • strategy store + versions (NEW)
   │                                            • schedule config (NEW)
   │                                            • hosted lifecycle API (NEW; authorizes by lease/attempt)
   │                                            • EXISTING worker run/execution/options/notify routes
   │
   │  supervisor  ⇄  lifecycle API over service DNS (narrow authenticated credential)
   ▼                (the supervisor holds NO database credentials)
 strategy-runner supervisor (dedicated container; own OS identity)
   • claims due jobs via the lifecycle API (lease/attempt CAS, occurrence_key)
   • asks the API to create a token then a run bound to that token, then claim the session
   • spawns ONE child/run (concurrency default 1): restricted env, rlimits, scratch
   • owns heartbeat + release + fence; records boot/container/process identity
   • captures logs (redacted); cleans up by recorded identity — no script-path matching
   │  HTTP over service DNS (http://finance-app:8777), nonce header, run token in env
   ▼
 strategy child (user .py; separate OS identity; NO DB creds, NO broker secrets, NO heartbeat right)
   • kite_algo_worker SDK (attach-only; never claims/heartbeats/releases)
   │
   ▼  /api/algo-workers/worker/*  +  /worker/options/*
 EXISTING execution authority: run model • token scope • paper/dry_run routing •
 idempotency • protection • recovery • journal • notifications outbox
   ▲
   │  Go market-runtime + Redis: ingestion only; never loads user code
 External worker (UNCHANGED): same SDK + HTTP; user hosts the process.
```

**Boundaries.** The supervisor owns process lifecycle, env, limits, logs and cleanup. The **control
plane** owns authorization, account scope, mode routing, idempotency, protection, PnL, journal and
notifications — including the **hosted lifecycle authority** the supervisor acts through. The child
owns only strategy logic. Arbitrary code never runs in the API or market-ingestion processes.

**Supervisor authority is API-mediated, not DB-mediated (resolves finding 3).** The supervisor does
**not** hold database credentials and does **not** get "DB grants to a path". It calls a **narrow
authenticated internal lifecycle API** (service-to-service credential, distinct from any worker
token) whose endpoints — job claim/state, run-create, token-mint, session claim/heartbeat/release,
fence — **authorize against the persisted hosted lease/attempt authority** (the job's `lease_owner`,
`lease_epoch`, `attempt`). This is why a run can be heartbeated without giving the child heartbeat
rights: heartbeats go through the lifecycle API, not the child's run token. HTTP between supervisor
and app is **service DNS inside the compose network, not loopback.**

---

## 2. Capability table (current source)

| Capability | Exists | Evidence | Gap |
| --- | --- | --- | --- |
| Worker token model (modes/actions/templates/expiry) | Yes | `backend/api/schemas/worker.py:9-31` | `template_id` free string; **no owner field** (F13) |
| App-cookie token minting; server-side mint via repo | Yes | `worker_auth.py:23-37`; `algo_worker_repo.py:156` | — |
| Run create + live contract | Yes | `worker_auth.py:169-186`; `worker_shared.py:108-111` | `strategy_run_id` TEXT (F13) |
| Session claim/heartbeat/release (nonce CAS) + 60 s freshness | Yes | `worker_auth.py:80-167`; `worker_shared.py:112`; repo `:769-860` | — |
| Token↔run binding (run stores `token_id`) | Yes | `worker_shared.py:454-466` | drives the launched ordering (§5) |
| SDK attach to existing run | Yes | `client.py:1285-1305` | **claims/heartbeats by default** (F1) |
| Options market/strategy/run state machine + durable store | Yes | `worker_options_router.py:39-255`; `durable_store.py` | **live submit not wired** (F6/F7) |
| Futures contract helper | **No** | no FUT helpers in `sdk/**` | adapter needed |
| Intent routing (paper/live/dry_run) + idempotency | Yes | `worker_execution.py:800-886` | — |
| Exit run (paper/live/dry) | Yes | `worker_execution.py:934-959` | — |
| Protection / recovery / stale policy | Yes | `protection_runtime.py:29`; `runtime_recovery.py:205-255` | active behavior (F10) |
| Journal v2 + timeline | Yes | `worker_auth.py:216-241`; `broker_api/timeline/worker_timeline.py:12` | — |
| Durable notification outbox | Yes | `notifications/repository.py:85-120` | FK to `signal_events` (F8) |
| Run-scoped notify | **No** | `intent_bridge.py:139-143` | build (F8/F9) |
| In-platform executor / store / scheduler | **No** | grep | build |

---

## 3. A. Strategy contract

**v1 = a single Python file.** Package format deferred (see the implementation plan §6 exclusions).

**Attach-only API — ONE exact signature (resolves F1).** Add to the SDK:
```python
client.attach_run(run_id: str, *, session_nonce: str, config: RunConfig) -> ManagedRun
```
It GETs the run, constructs a `ManagedRun` **without claiming, heartbeating or releasing**, and
carries the nonce so the child can set the nonce header on intents/exit. The earlier alternate
(`client.run(config, create=False, …)`) is **removed**: there is no no-nonce context manager and no
second attach path.

**Server-enforced lifecycle separation (resolves finding 3).** The child's run token **excludes the
`heartbeat` action**. Heartbeat/claim/release are performed only by the supervisor through the
**lifecycle API**, authorized by the hosted lease/attempt authority (§1). A child that attempts a
heartbeat is refused by action enforcement, not merely by contract.

**Three distinct liveness signals (resolves finding 1).**
- **Supervisor alive** = supervisor process + job lease valid.
- **Child alive** = a process matching the recorded **boot id + container id + pid + pgid + process
  start time** (§4), not "the supervisor says so".
- **Progress** = the child emits a marker each loop via **PROPOSED `ManagedRun.progress(note=None)`**;
  the supervisor never fabricates it.

**What `hung` does (resolves finding 3).** If progress is stale beyond a **configurable
`progress_deadline_s`** (long values are allowed for legitimate waiting, e.g. a candle or expiry),
the supervisor **fences**: it revokes the child's authority, marks the job **`recovery_required`**, and
does **not** auto-restart. Any resulting position action is **policy-governed** (`exit_on_worker_stale`
/ squareoff / reconciliation), shown distinctly from operator actions. The parent heartbeat keeps the
*session* alive; it never asserts *progress*.

**Lifecycle authority:** the **supervisor** creates the token and run, claims the session, heartbeats,
fences and releases. The script only attaches.

**Parameters and versions:** each save writes an immutable version that includes `source`,
`source_sha256`, **`parameters_schema`**, and a **capabilities snapshot**. A run pins `version_id`;
the job/schedule stores a **params snapshot**, the **execution_mode**, the **capabilities snapshot**
and a **policy snapshot** (not a params hash alone).

**Job kind vs execution mode (resolves finding 2):** `job_kind ∈ {continuous, finite}` is separate
from `execution_mode ∈ {paper, dry_run}`. Both are stored independently.

**Minimal hosted↔external delta:** same SDK and HTTP. Only the supervisor mints the token, creates the
run, heartbeats, and supplies source.

---

## 4. B. Hosting boundary

**Placement and isolation (resolves finding 4).** v1 is a **trusted single-operator** setup: one
**shared supervisor container** with **minimal mounts**, a **separate supervisor vs child OS
identity**, and **actual permission enforcement** (child cannot write supervisor-owned files).
**Per-run filesystem isolation is NOT promised** — a shared container cannot give a child per-run bind
mounts without namespaces, so v1 does not claim it. Stronger isolation is deferred to untrusted/
multi-tenant authors.

- **Mounts:** the strategy **source** is mounted read-only and **owned by the supervisor**; the scratch
  dir is **child-owned** (mode 0700) and is the cwd. The supervisor's own state (including the
  authoritative process marker) lives in a **supervisor-owned** directory the child cannot write.
- **No authoritative process marker in child-writable storage.**
- **Identity (no path/PGID alone):** a running child is identified by
  `boot_id` (`/proc/sys/kernel/random/boot_id`) + `container_id` + `pid` + `pgid` + **process start
  time** (`/proc/<pid>/stat`), so PID reuse cannot alias a different process.
- **No reattach in v1 (resolves finding 1).** On supervisor restart, a job that was `running` becomes
  **`recovery_required`**; the supervisor reconciles exposure **through the backend** and a human must
  start a **new attempt with a new run and token**. There is no automatic re-attachment or replay.
- **Least privilege to spawn the child identity:** only what is needed to change UID/GID (e.g.
  `CAP_SETUID`/`CAP_SETGID` or a minimal `setpriv`/sudoers rule for the child user). **No docker
  socket**, no broad host capabilities.

**Child environment (restricted, exact):** only `PATH`, `PYTHONPATH`, `KITE_ALGO_BASE_URL`,
`KITE_ALGO_WORKER_TOKEN`, `KITE_ALGO_RUN_ID`, `KITE_ALGO_SESSION_NONCE`, `KITE_ALGO_TEMPLATE_ID`,
`KITE_ALGO_MODE`, `KITE_ALGO_PARAMS`, `KITE_ALGO_SCRATCH`. **Never** DB credentials, broker secrets,
supervisor credentials, or unrelated env.

**Resource limits:** rlimits (AS, CPU seconds, NOFILE, NPROC, FSIZE), `start_new_session=True`,
wall-clock/max-duration cap, per-run scratch.

---

## 5. C. Execution and F&O

**Launch ordering (resolves finding 2):** **create token → create run bound to that token ("run.token_id") →
claim session.** Durable ids/states are recorded on the job (`run_id`, `token_id`, `attempt`); a retry
**reuses the recorded token_id/run_id** for the same attempt rather than minting a duplicate. Token
minting is idempotent on `(job_id, attempt)` so a retry cannot silently create a second live credential.

**No automatic restart/reattach after lease loss (resolves finding 1).** When a lease is lost: the
supervisor **revokes the old authority**, marks the job **`recovery_required`**, reconciles exposure
via the backend, and requires an **explicit new attempt with a NEW run and token**. There is **no
automatic replay and no takeover that reuses the old run**. **In-flight caveat:** preflight
nonce/revocation checks are **not atomic** with effects already admitted upstream (an intent already
accepted, an order already at the broker). We therefore make **no exactly-once guarantee** across a
lease loss. The backend must **validate hosted lease/attempt authority on mutations**, and **no
replacement attempt is allowed until reconciliation completes**. Single-child concurrency default **1**.

**Mode handling (resolves F6/F7, finding 6).** Hosted v1 **fails closed**:
- The run's `execution_mode` is propagated into the options action; the wrapper no longer leaves it to
  the payload.
- Options execution is **paper-only**. `live` is rejected. **`dry_run` options mutation is rejected:
  `dry_run` on options is preview-only**, never a synthetic fill.
- **No synthetic default result is ever surfaced as genuine execution** — the deterministic injection
  seam stays a test fixture.
- **Persisted `option_run_states` does not prove recovery of an in-flight paper submission**: the row
  is durable state, not a submission journal. Reconciliation is by explicit re-read, not assumption.

**Exact-contract resolution:** options via `client.options` (`resolvers.py`); futures via a **PROPOSED
`resolve_futures_contract(underlying, expiry)`** on the instrument catalog (lot/tick/expiry).

**Partial fills / leg failures:** reuse the options state machine (`PARTIAL_ENTRY`, `PARTIAL_EXIT`,
`CLEANUP_REQUIRED`; per-leg completed/failed/pending). **Do not claim multi-leg submission is atomic.**

**Stop vs Cancel vs Flatten (resolves finding 6, 7):** three distinct operator actions targeting
**immutable job/run ids** (never "latest strategy"):
1. **Stop strategy** — stop launching; SIGTERM→grace→SIGKILL the child; release session; revoke token.
2. **Cancel orders** — existing worker order-cancel routes, against the run id.
3. **Flatten / Exit** — explicit, separately confirmed; existing `exit_run` / options `exit`.
**Explicitly not promised:** Stop does not itself flatten, but **policy can**. `exit_on_worker_stale`
auto-closes stale **paper** runs and defers stale **live** runs to protection when enabled (F10). The
UI labels **policy-governed exits** distinctly. A finite job may finish with **open exposure**; that is
surfaced, not hidden. The **stale-exit policy is chosen per strategy and shown before start** — there is
no universal forced default that silently closes open positions.

**Recovery ownership after credential expiry:** recovery/protection loops use **backend authority, not
the worker token**, so they can still act when the child's token has expired; the supervisor cannot.

**Scheduling (resolves finding 5, 8).** v1 supports a **bounded** schedule syntax:
`{ every: "1d"|"1w", weekday?: <explicit, required when every="1w">, at: "HH:MM"|"session_close",
timezone: "Asia/Kolkata" }`. **NSE-only is a PROPOSED v1 scheduling restriction** — it is **not**
evidence that MCX/currency instruments are unsupported for manual runs. **Rejected in v1:** month-end,
MCX/currency schedules, sub-daily, other calendars. **Window end** and **squareoff** are separate
fields. `session_close` is offered only for **finite data tasks that complete within the session**; it
**does not promise trades after close**. A **missed catch-up must expire** if it falls outside the
permitted window (no run outside the window). Scheduled occurrences pin **`version_id` + params
snapshot + capabilities + policy snapshot** (not "latest", not a hash alone). Overlap: skip while a run
is active (recorded). A manual stop **pauses** the schedule (visible, resumable).

---

## 6. D. Shared capabilities

**Run-scoped notifications (in v1) — ONE backward-compatible design (resolves finding 6).**
Reuse `signal_events`; extend it additively so the `deliveries` FK is unchanged:
- Migration (additive): `signal_events.source_kind` (String, nullable, default `'workflow'`),
  `signal_events.owner_id` (String(255), nullable), `signal_events.run_id` (**TEXT — matches
  `algo_worker_runs.strategy_run_id` TEXT**, F13), index on `(run_id)`. Existing rows keep
  NULL/defaults.
- **Ownership (resolves finding 6):** `owner_id` = the **hosted strategy's app owner** (`owner_id` on
  `hosted_strategies`), **persisted as an explicit binding**. It is **not** a worker-token owner —
  `WorkerToken` has no owner field (F13) — and it is **not** assumed to equal account scope.
- A run-scoped event is a `signal_events` row with `source_kind='strategy_run'`, `owner_id` as above,
  `occurrence_key` derived from the **caller-supplied `idempotency_key`** (unique → idempotent), and
  the message/evidence in `evidence`.
- **SDK `ManagedRun.notify(text, channels=..., idempotency_key=...)` takes the caller idempotency
  key** — there is **no process-local sequence number** and no claim that a local retry is
  deduplicated. A same-key/different-content request is an **idempotency conflict (409)**, distinct
  from a repeat (deduped).
- **Atomic enqueue:** event + `deliveries` in one transaction; unknown channel → explicit **422**.
- **Loader/rendering adapter:** the existing delivery loader may assume a workflow-shaped event;
  enqueue/load goes through a **hosted adapter** so alert/screener assumptions are not imposed.
- **Backwards compatibility:** the external worker contract is unchanged; run-scoped notify is an
  additive endpoint; alert/screener delivery is pinned by regression tests. **Retention** follows the
  existing `signal_events`/delivery retention.
- **Isolation:** an enqueue failure is returned to the caller. We do **not** promise arbitrary Python
  cannot couple an exception to a decision — that is the author's code.
- **Optional:** wiring the idle `NotifyAction` here is later work, not v1.

**Notifications independent of trading.** A notifications-only run token carries
`notifications:publish` + `runs:read` and **no** `heartbeat` (supervisor-only), **no**
`intents:submit`, **no** `runs:exit`. Each route enforces its action.

**Optional screener/alert consumption:** deferred (implementation plan §6).

**Numerical consistency.** The idle kernel EMA seeds differently from alerts/pandas
(`algo_runtime/indicators.py:59` vs `alerts/features.py:174`); do not silently unify. Hosted
strategies use **SDK indicators**. The kernel stays **idle** — not activated, not deleted (F9).

---

## 7. E. User experience

```
Register code → Configure → Execution settings → Start/Schedule → Inspect → Stop
  new version     params +      (paper|dry_run),     run now /       logs, state,  (Stop ≠
  (pinned)        capabilities   stale-exit policy    window (bounded) PnL, notify  Cancel ≠ Flatten)
```
- Before start, the operator sees the **stale-exit policy** and the **max-duration**; both are per
  strategy.
- Notification-only strategies show **no** trading controls.
- Stop/Cancel/Flatten each target the **immutable job/run id** and state their exact consequences,
  including policy-governed exits.
- F&O: two-leg options show leg state / cleanup-required; futures show contract/lot/tick.

---

## 8. Decisions, alternatives, blockers

| # | Decision | Alternatives | Chosen |
| --- | --- | --- | --- |
| H-1 | Supervisor placement | in-API / in-worker | **Dedicated service** |
| H-2 | Lifecycle authority | child-owned / supervisor-owned | **Supervisor-owned via lifecycle API; attach-only child** |
| H-3 | Lease loss | takeover-reuse / no-reuse | **No automatic restart/reattach; recovery_required + explicit new attempt** |
| H-4 | v1 source format | file / package | **Single file** |
| H-5 | v1 modes | paper+dry_run / +live | **paper + dry_run** (`dry_run` options = preview only) |
| H-6 | Notify schema | extend `signal_events` / new table + FK change | **Extend `signal_events`** (backward-compatible) |
| H-7 | Stop semantics | one button / three actions | **Three actions on immutable ids** + policy exits labelled |
| H-8 | Signal consumption | v1 / deferred | **Deferred** |
| H-9 | Idle kernel | activate / delete / idle | **Idle** |
| H-10 | Schedule syntax | reuse screener fully / bounded subset | **Bounded NSE daily/weekly subset** |

**Remaining genuine blockers / product choices:**
1. **Live multi-leg options submission is not wired** (F6/F7). H1 is paper/dry_run; blocks only a later
   live milestone.
2. **Stale-exit policy for hosted jobs** (F10): chosen **per strategy and shown before start**; the
   open product choice is the recommended default value, not whether the setting exists.
3. **Progress deadline default** for legitimate long waits is configurable; the recommended default is
   a product choice.

See `documents/hosted-strategies-implementation-plan.md` for the bounded plan, migrations, acceptance
checks and exclusions.
