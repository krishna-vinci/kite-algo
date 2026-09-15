# Hosted-strategy supervisor lifecycle — slice report (H1a)

**Base:** `c666343` (hosted-strategy schema + authorization foundation).
**Closed on:** the reviewed lifecycle-closure fixes applied on top of `f58333b`
(§1.6) and the process-supervision slice applied on top of `5e3078e` (§1.7, §6).
This revision corrects the earlier report's baseline evidence (§3).
**Scope:** supervisor lifecycle API, worker-run/session integration, child
credential handoff, hosted-attempt enforcement, SDK attach-only, and the
dedicated supervisor process.

Slices 0–1 implement **preparation only**. This slice (process supervision) adds
a runner that *claims, prepares, spawns, supervises and stops* one child, but
still **does not** place orders or send notifications, does not touch
alerts/screeners or options execution semantics, and does not implement
scheduling, the frontend, Go/MCP or backtesting. No migration was run against a
live database.

---

## 1. Implemented behavior

### 1.1 Narrow supervisor credential (`backend/strategies/supervisor_auth.py`)

- A server-to-server credential, distinct from the app cookie and from every
  child worker token. Transport: `X-Hosted-Supervisor-Credential`. Compared with
  `hmac.compare_digest`; never returned to any caller, never logged.
- **Default-deny:** unset config ⇒ 401 even with a supplied header.
- **Rotation without downtime:** `HOSTED_SUPERVISOR_CREDENTIALS` is a
  comma-separated list (add the new value, roll the supervisor, remove the old).
  `HOSTED_SUPERVISOR_CREDENTIAL` (single) is also accepted.
- The router lives at `/api/hosted-supervisor` and is added to
  `auth_exempt_path`, so the cookie middleware does not gate it. A browser
  session cookie does not open it, and a worker bearer token does not open it.

### 1.2 Lifecycle API (`backend/api/routers/hosted_lifecycle.py`)

All routes require the supervisor credential **before any job detail is read**.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/hosted-supervisor/jobs` | Bounded discovery of jobs awaiting a supervisor |
| POST | `/api/hosted-supervisor/jobs/{job_id}/claim` | CAS-claim a `queued` job |
| GET | `/api/hosted-supervisor/jobs/{job_id}` | Authoritative job state (including `last_progress_at`) |
| GET | `/api/hosted-supervisor/jobs/{job_id}/source` | Deliver pinned source/version/hash (live lease) |
| POST | `/api/hosted-supervisor/jobs/{job_id}/prepare` | Prepare launch + one-time child handoff |
| POST | `/api/hosted-supervisor/jobs/{job_id}/heartbeat` | Runner-owned lease + session heartbeat |
| POST | `/api/hosted-supervisor/jobs/{job_id}/release` | Runner-owned stop (release session, revoke child, decide replacement safety) |
| POST | `/api/hosted-supervisor/jobs/{job_id}/fence` | Fence a **live** attempt to `recovery_required` + revoke child |
| POST | `/api/hosted-supervisor/jobs/{job_id}/recover` | Authenticated lease-loss recovery for an **expired** attempt |

Authority on every mutation is the **persisted job** `lease_owner` /
`lease_epoch` / `attempt` (plus a live, non-expired lease and a permitted
state). The caller never selects a run id: job identity and configuration
(account scope, mode, params, policy) are derived from the record, so a
supervisor cannot use an arbitrary run id to reach an unrelated run.

`prepare` performs exactly the required ordering, server-side:

```
reserve + mint child token  →  create worker run bound to that token
   →  claim the worker session  →  deliver the child config (one-time)
```

Run creation reuses the **shared** worker run-creation path
(`worker_shared.create_worker_run_for_token`, extracted from the worker route),
so journal-v2 attribution, backend-protection normalization and all
token/account/template/mode/safety validation are preserved — it is not a bare
INSERT.

### 1.3 Child authorization (`backend/api/services/hosted_attempt.py`)

- A **hosted run** is one whose `template_id` is `hosted:<strategy_id>`; the
  authoritative decision comes from the `strategy_jobs` record (`run_id` +
  `token_id`), not from an SDK convention or a naming rule alone.
- Enforced on the hosted child mutation routes: `intents`, `exit`, bracket
  create/cancel, order cancel/modify, options `enter`/`exit`/`protection`, and
  run risk/protection patches. A stale, expired, fenced or mismatched attempt
  is refused with 403/409.
- A hosted child token **cannot** claim, heartbeat or release a session: those
  routes reject a `hosted:` run outright (`HOSTED_CHILD_LIFECYCLE_FORBIDDEN`),
  independent of token composition. Lifecycle authority is the supervisor's,
  via the lifecycle API.
- **Options surface is bound to the worker run.** The options routes do not
  require a worker run, so a hosted token must additionally be bound: a hosted
  credential that selects an options id with no corresponding worker run, or a
  run owned by another token, is refused (`HOSTED_CHILD_RUN_REQUIRED`) rather
  than silently skipping the guard. External tokens are unchanged. Unsupported
  hosted options modes stay closed; this does not add live options execution.
- **Child token actions come from the pinned capability snapshot** (see 1.6).
- **External workers are unchanged:** the guard is a no-op for any non-`hosted:`
  template and the token pre-filter never touches the strategies store for an
  ordinary external token; a regression test pins external claim-session
  behavior.

### 1.4 SDK attach-only (`sdk/python/kite_algo_worker/client.py`)

```python
client.attach_run(run_id, *, session_nonce, config) -> ManagedRun
```

Fetches and validates the existing run (template/account/mode must match the
config), returns a `ManagedRun` carrying the **caller-supplied** nonce, and
performs **no** create/claim/heartbeat/release. `client.run(...)` is unchanged.
SDK version `0.10.0 → 0.12.0` (additive).

This slice adds `ManagedRun.progress(note=None)` / `client.run_progress(...)`
(child-authenticated `POST /worker/runs/{id}/progress`, in the endpoint manifest)
and the hosted child bootstrap `kite_algo_worker.hosted` (`ChildContext`,
`main(ctx)` loader).

### 1.5 Schema (additive)

`strategy_jobs.handoff_at` and `strategy_jobs.last_error` (migration
`20260915_000020`, `schema.sql` parity, ORM parity). `handoff_at` is the durable
evidence that a credential was handed off; `last_error` is a short, non-secret
diagnostic. Neither stores a credential.

### 1.6 Lifecycle closure (review fixes)

- **Lease-loss recovery (`/recover`).** The ordinary `fence` requires a live
  lease, so an expired attempt could not be fenced through the API. A distinct,
  credential-authenticated path now authorizes by full identity (id +
  `lease_owner` + `lease_epoch` + `attempt`) and requires the lease to have
  **expired**; it durably fences to `recovery_required` and revokes the child
  token. It never renews a lease or restores execution authority, a still-live
  lease is refused (`HOSTED_LEASE_STILL_LIVE`, use `fence`), and a stale or
  unrelated identity is refused (403). Repository primitive:
  `expire_to_recovery_authorized`.
- **Release vs replacement.** Stopping code/session authority is not exposure
  reconciliation. The server decides: an **unlaunched** attempt (no credential
  ever handed off) is marked `stopped` and replacement is allowed; a
  **launched** attempt may have accepted work, so it is fenced to
  `recovery_required` and replacement stays blocked until explicit
  reconciliation. There is no caller-supplied `flat=true` shortcut. The
  response carries `replacement_blocked`.
- **Effective configuration.** Child token actions are derived from the pinned
  capability snapshot, not hardcoded. `capabilities_snapshot` has an explicit
  schema (`schema_version: 2`, `{data, trade, notify}`); `trade` grants the
  paper order actions, `notify` grants `notifications:publish`, `data` the
  baseline read/log. A missing/legacy-marker-only/ambiguous snapshot **fails
  closed** (`HOSTED_CAPABILITIES_AMBIGUOUS`) before anything is reserved or
  minted. The store accepts explicit `capabilities` on version creation
  (default data-only; unknown/non-boolean → 422). The pinned stale-exit policy
  is mapped through the validated run-creation path into the run's
  `backend_protection` config (`exit_on_worker_stale` with a pinned stale
  threshold); `none` installs no protection.
- **Preparation failure handling.** Every durable step — `record_child_run`,
  `claim_run_session`, `mark_running_and_handoff` — is wrapped; failures fence
  the job and revoke using the **reserved** token id (not the stale job object).
  Cleanup is best-effort and retryable, and the error honestly reports
  `fencing: confirmed | unconfirmed` rather than claiming a failed write
  guarantees fencing.
- **Terminal observability.** Terminal transitions retain `lease_owner` /
  `lease_epoch` / `attempt` as attribution (only `lease_until` is cleared), so an
  authorized `GET /jobs/{id}` still works after `release`/`fence`/`recover`
  without restoring mutation authority (heartbeat/prepare remain refused).

### 1.7 Gate-1 boundary corrections (this slice)

- **Terminal reads after release.** `require_job_authority(..., require_started=False)`
  is used only by the state read, so a released-*unlaunched* job (whose
  `desired_state` became `stopped`) can still be read. Reads do **not** re-enable
  heartbeat/prepare/mutation, which remain gated on live status + a live lease.
- **Operation-specific options permissions.** Hosted options mutations now check
  the action the operation needs — `intents:submit` for enter/exit and options-run
  creation, `risk:update` for protection — *independently of* token/run identity.
  A data-only token cannot trade or alter protection. Execution mode is derived
  from the persisted worker run: **paper only**; a `dry_run` mutation is rejected
  (`HOSTED_OPTIONS_MUTATION_PAPER_ONLY`) while previews remain allowed (they do
  not mutate). Caller-supplied `order_results`/`trade_results` are refused on the
  hosted path (`HOSTED_EXECUTION_INJECTION_FORBIDDEN`); external behavior is
  unchanged.

### 1.8 Process supervision (this slice)

Modules: `backend/strategies/supervisor_api.py` (stdlib HTTP client, no DB),
`backend/strategies/supervisor_process.py` (containment), and
`backend/strategies/supervisor.py` (orchestration + CLI). Child bootstrap:
`sdk/python/kite_algo_worker/hosted.py`.

- **Launch path.** `GET /hosted-supervisor/jobs` (bounded discovery) selects a
  queued job; `POST …/claim` CAS-claims it; `POST …/prepare` returns the one-time
  child credential; `GET …/jobs/{id}/source` delivers the pinned
  `source`/`version`/`source_sha256` to the **authorized** supervisor. The
  supervisor writes the source to supervisor-owned storage, **verifies the
  SHA-256 before spawning**, and never imports it into the API or supervisor
  process. `def main(ctx)` receives `ctx.params`, `ctx.client` and an
  attach-only `ctx.run` (`ManagedRun`) via the SDK's existing `attach_run`.
- **One-time preparation.** Prepare is called exactly once. A lost/timed-out
  response is **not** replayed: the supervisor fails its own attempt closed
  (fence while live, recover once expired). A `409` conflict does **not** fence —
  another caller's valid attempt is not destroyed. A local nonsecret attempt
  record (job/attempt/lease/run/SHA/identity, never credentials) is persisted
  before it is needed. Spawning is refused if authority cannot be shown live.
- **Containment.** Separate child OS identity when configured (`child_uid`/
  `child_gid`), explicit environment allowlist (no supervisor/DB/broker secrets,
  nothing inherited), supervisor-owned read-only source and attempt records,
  child-owned scratch, own process group/session, rlimits, wall-clock observe
  bound, byte-capped logs, and recorded boot/container/PID/PGID/start-time
  identity. PID reuse is rejected before any signal. No Docker socket; **no claim
  of complete security isolation**.
- **Progress.** New child-authenticated `POST /worker/runs/{id}/progress`
  (`runs:progress`, session-bound) is the **only** writer of `last_progress_at`;
  the supervisor heartbeat never writes it. Stale/fenced/expired attempts are
  refused. The supervisor fences when progress is stale past
  `progress_deadline_s + startup_grace_s`.
- **Stop/failure/restart.** SIGTERM → bounded grace → SIGKILL on the process
  group; the local child is stopped within a bounded deadline even if the API is
  unreachable. Fence while authorized/live, recover after expiry. Unconfirmed
  cleanup is recorded as `cleanup_required` and retryable (`--retry-cleanup`); no
  false “stopped”. No automatic replay/reattach after restart; a launched attempt
  stays `recovery_required` with the replacement block. Normal completion is not
  proof of flatness (it triggers a runner-owned release).

---

## 2. Authentication / configuration

- `HOSTED_SUPERVISOR_CREDENTIAL` (or `HOSTED_SUPERVISOR_CREDENTIALS` for
  rotation) must be set and delivered to the supervisor out of band. Unset ⇒
  default-deny.
- No secret is written to source, tests, logs or this report. Tests set a
  throwaway value in `monkeypatch.setenv`.
- The supervisor credential is never included in a lifecycle response; the
  only secret in a `prepare` response is the **child** token, shown once.

---

## 3. Verification (exact results)

Targeted suites (SQLite):

```
.venv/bin/python -m pytest \
  tests/strategies \
  tests/api/test_strategies_api.py \
  tests/api/test_algo_worker_api.py \
  tests/api/test_algo_worker_route_mounts.py \
  tests/api/test_worker_run_discovery.py \
  tests/api/test_worker_notifications.py \
  tests/api/test_hosted_lifecycle_api.py \
  tests/api/test_hosted_child_authority.py \
  tests/sdk -q
→ 554 passed, 1 skipped
```

Disposable PostgreSQL (real concurrency; own invocation):

```
HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_supervisor_lifecycle_postgres.py \
    tests/integration/test_hosted_strategy_foundation_postgres.py -q
→ 14 passed       # concurrent prepare ⇒ one credential/run; reservation CAS one winner;
                  # launched release blocks replacement; racing recover one fence; chain applies
```

`alembic heads` → single head `20260915_000020`. `git diff --check` clean.

**New tests:** supervisor auth (default-deny, wrong/absent credential, rotation);
lifecycle state machine (happy path, repeat, partial, wrong owner/epoch/attempt,
expired lease, failures between token/run/session/handoff steps, incomplete
cleanup reported honestly, heartbeat ≠ progress); lease-loss recovery (expired
lease fences, live lease refused, stale identity refused, no re-gain of
authority); release vs replacement (launched blocks, unlaunched allows, actual
create_job blocked/allowed); capability composition (trade/data-only/notify-only
and legacy marker-only fail-closed); pinned protection policy mapping; terminal
state reads; HTTP boundary (auth before job detail, claim/state, unrelated
authority 403, one-time handoff, repeat no second credential, recover);
child authority (session lifecycle forbidden, fenced/expired/mismatched
mutations, options run-binding boundary, external compat); SDK attach-only; and
the PostgreSQL concurrency checks above.

Process-supervision tests (added this slice):

- `tests/strategies/test_supervisor_process.py` — real harmless children:
  completion, crash exit code, **hang → process-group termination** (grandchild
  also dies), **bounded log** capped with overflow flag, and **PID-reuse**
  rejection (`terminate_identity` refuses to signal an unproven identity).
- `tests/strategies/test_supervisor.py` — fake lifecycle API: happy path
  (prepare once, source verified, release → `recovery_required`, block preserved),
  `409` conflict does **not** fence, lost prepare response fails closed without
  replay, authority loss stops the child and fences, **API-unreachable cleanup is
  visible and retryable**, progress stall fences, source-hash mismatch refuses
  spawn, authority-not-live refuses spawn, and the child **environment allowlist**
  contains no supervisor/DB/broker secrets.
- `tests/api/test_hosted_child_authority.py` — child progress is
  `runs:progress` + session-bound, refused when fenced, and only accepted progress
  writes `last_progress_at`; options operation-permission tests (data-only token
  cannot enter/exit/protection; dry-run mutation rejected while preview allowed;
  execution-result injection refused).
- `tests/sdk/test_hosted_bootstrap.py` — `main(ctx)` loading, `ctx.progress`
  delegation, and missing-`main` refusal.
- `tests/strategies/test_repository.py` — `record_progress` only for live jobs.

**Unrelated pre-existing failures — corrected baseline evidence.** The earlier
report compared only `backend/api/routers/__init__.py`, which is *not* a complete
baseline for a commit that also changes shared services and worker routes. The
corrected comparison stashes **all** tracked changes of this slice (`backend`,
`sdk`, `tests`) — i.e. baseline = `5e3078e` — and re-runs:

| Scope | Baseline (`5e3078e`) | Current (this slice) |
| --- | --- | --- |
| `tests/api tests/options` | 46 failed, 515 passed | 46 failed, 521 passed |

The failure **set and count are identical** (the added passes are this slice's
new tests), so no regression is attributable to this slice. Causes of those
pre-existing failures: `tests/api/test_auth_*`, `test_control_plane_api`,
`test_public_runtime_config` fail on `ModuleNotFoundError:
backend.app.runtime_public_config`; a subset of `tests/options` fails on
`OptionRunCreateRequest`/route-test drift. A smaller pre-existing set also fails
when only `tests/options` is collected first, due to an import cycle in
`worker_options_router` → `worker_protection` → `routers/__init__`; it is
independent of this slice (reproduces with the slice stashed).

---

## 4. Failure / retry semantics (explicit)

- **One-time credential.** The child token plaintext exists only in the
  `prepare` response. It is stored as a hash and is **never** recoverable; the
  protocol never pretends otherwise.
- **`token_id` is reserved durably before minting.** A crash between reserve and
  mint leaves a visible, revocable marker rather than an invisible credential.
- **A repeat `prepare` for an attempt that already has `token_id` (or
  `handoff_at`) returns 409 and mints nothing.** It does not silently issue a
  second credential. It also does **not** fence a healthy attempt it does not
  own: recovery is explicit via `fence`/`release` (or `expire_to_recovery`).
- **A failure inside a `prepare` request** (token mint, run create, run-record,
  session claim, handoff mark) fences the job to `recovery_required`, revokes any
  minted token, and returns 503/409. The fence commits in its own transaction.
- **Concurrent `prepare`** produces at most one authorized attempt: the
  `token_id` reservation is a single-row CAS, so one caller wins and the other
  receives 409.
- **No exactly-once.** Preflight authority checks are not atomic with effects
  already admitted upstream (an accepted intent, a broker-side order). A lease
  loss revokes authority and marks `recovery_required`; it does not replay and
  does not reuse the old run with a replacement token. A new attempt needs a new
  run and token and cannot start until reconciliation.
- **Lease loss has two entry points.** `fence` handles a live lease; `recover`
  handles an already-expired lease under the same full identity. Neither renews
  a lease nor restores execution authority, and neither resurrects an expired
  attempt.
- **Heartbeat is not progress and not health.** `heartbeat` renews the job lease
  and records the worker-session heartbeat; it never writes `last_progress_at`.
  Runner liveness and child progress are separate signals.
- **Stop/revoke ≠ cancel/flatten.** `release` withdraws session and token
  authority and then decides replacement safety from persisted evidence: an
  unlaunched attempt is `stopped` (replacement allowed); a launched attempt is
  `recovery_required` (replacement blocked). Open exposure is never asserted to
  be flat, and there is no caller-supplied flatness flag.
- **Cleanup honesty.** A preparation failure records `fencing: confirmed` only
  when the durable fence was actually written; otherwise it reports
  `unconfirmed` and the attempt is left retryable (the job remains in a
  pre-terminal state so the fence can be re-driven).

---

## 5. Remaining gaps (after process supervision)

- **Operator reconciliation has no API.** `release`/`fence`/`recover` leave a
  launched attempt in `recovery_required`, which is correct, but there is still
  no operator-facing reconciliation workflow to inspect exposure and clear the
  replacement block. Until it exists, `recovery_required` is terminal for the
  operator surface. This is the single most important missing piece.
- **Scheduling is not implemented.** Discovery lists `queued` jobs; nothing
  creates them on a schedule. The supervisor polls (`run_forever`) or runs once.
- **Options fail-closed mode propagation and the futures contract resolver**
  remain out of scope (later H1/H2 work). The options boundary binds hosted
  tokens to their worker run and enforces operation permissions/paper-only, but
  does not add live options execution.
- **Delete/cleanup of workspace artifacts** (attempt records, source, logs) is
  not automated; retention is a deployment concern.
- **Reconciliation of a child that outlives the supervisor** is not attempted
  (no reattach by design). A restart leaves the job `recovery_required`.
- Normal script completion is **not** proof of flatness; a finite job may finish
  with open exposure, and the report does not claim otherwise.

## 6. Operational configuration

Supervisor process (no DB credentials; service network only):

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `HOSTED_SUPERVISOR_BASE_URL` | yes | — | Lifecycle API base, e.g. `http://finance-app:8777` |
| `HOSTED_SUPERVISOR_CREDENTIAL` | yes | — | Narrow supervisor credential (matches the API env) |
| `HOSTED_SUPERVISOR_LEASE_OWNER` | no | `host:pid` | Lease holder identity (unique per supervisor) |
| `HOSTED_SUPERVISOR_WORKSPACE` | no | `supervisor-workspace` | Supervisor-owned state (attempts/source/logs/scratch) |
| `HOSTED_SUPERVISOR_LEASE_SECONDS` | no | `120` | Lease TTL requested on claim/renew |
| `HOSTED_SUPERVISOR_HEARTBEAT_INTERVAL_S` | no | `30` | Heartbeat cadence |
| `HOSTED_SUPERVISOR_STARTUP_GRACE_S` | no | `30` | Grace before progress staleness applies |
| `HOSTED_SUPERVISOR_PROGRESS_POLL_S` | no | `5` | Progress poll cadence |
| `HOSTED_SUPERVISOR_TERM_GRACE_S` | no | `10` | SIGTERM→SIGKILL grace for the child group |
| `HOSTED_SUPERVISOR_MAX_LOG_BYTES` | no | `5242880` | Hard cap per child log |
| `HOSTED_SUPERVISOR_CHILD_PYTHON` | no | `sys.executable` | Interpreter for the child |
| `HOSTED_SUPERVISOR_CHILD_PYTHONPATH` | no | repo `sdk/python` | Where the child imports the SDK |
| `HOSTED_SUPERVISOR_CHILD_UID` / `_GID` | no | unset | Drop the child to a distinct OS identity (needs CAP_SETUID/SETGID) |
| `HOSTED_SUPERVISOR_API_TIMEOUT_S` | no | `10` | Lifecycle API timeout |

API side (unchanged from §2): `HOSTED_SUPERVISOR_CREDENTIAL(S)` and
`HOSTED_STRATEGY_ACCOUNT_SCOPES`.

CLI: `python -m backend.strategies.supervisor` (loop), `--once`,
`--job <job_id>`, `--retry-cleanup`. Packaging: `compose.supervisor.yml` adds a
`strategy-runner` service with **no** `env_file`/DB credentials and a dedicated
state volume. This is configuration only — **not** deployed or started.

**Child environment allowlist (exact):** `PATH`, `HOME` (scratch),
`PYTHONUNBUFFERED`, `PYTHONPATH`, `KITE_ALGO_BASE_URL`,
`KITE_ALGO_WORKER_TOKEN`, `KITE_ALGO_RUN_ID`, `KITE_ALGO_SESSION_NONCE`,
`KITE_ALGO_TEMPLATE_ID`, `KITE_ALGO_ACCOUNT_SCOPE`, `KITE_ALGO_MODE`,
`KITE_ALGO_PARAMS`, `KITE_ALGO_SCRATCH`. `KITE_ALGO_ACCOUNT_SCOPE` is an
implementation-resolved detail: `attach_run` validates template/account/mode, so
the account scope (a non-secret config value) must reach the child.

## 7. Deployment readiness — what is and is not true

- **Implemented and tested:** the supervisor process (claim → prepare → source
  verify → spawn → heartbeat → progress fence → stop/release/fence/recover), the
  child bootstrap and attach-only context, process containment and identity
  handling, the two lifecycle corrections, and the lifecycle API additions.
- **Not deployment-ready / not done:** the operator reconciliation workflow
  (still missing), scheduling, frontend, notification delivery, cancel/flatten
  adapters, live trading, and any production rollout. `compose.supervisor.yml`
  was added but **not** deployed, and no live database migration was run.
- **Explicitly not claimed:** complete security isolation (v1 is a trusted
  single-operator setup; per-run filesystem isolation is not promised), exactly-once
  execution, or that a stopped/revoked attempt is flat.
