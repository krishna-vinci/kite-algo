# Hosted-strategy supervisor lifecycle — slice report (H1a)

**Base:** `c666343` (hosted-strategy schema + authorization foundation).
**Closed on:** lifecycle-closure fixes on `f58333b`, the process-supervision slice
on `5e3078e`, the supervision closure corrections on `d83e79c`, and the operator
reconciliation backend on `9b5bbad` + this commit (§1.10). This revision corrects
the earlier report's baseline evidence (§3).
**Scope:** supervisor lifecycle API, worker-run/session integration, child
credential handoff, hosted-attempt enforcement, SDK attach-only, the dedicated
supervisor process, and the operator reconciliation backend.

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

### 1.9 Supervision closure corrections (this slice)

- **Exception- and signal-safe shutdown.** Post-spawn work runs under
  ``try/except/finally``: any persistence/observation/API exception stops the
  child (bounded process-group termination) before the loop moves on, and the
  outcome is ``supervisor_exception`` → fail closed, never a false success.
  ``SIGTERM``/``SIGINT`` set a stop flag observed between supervision steps;
  ``run_forever`` exits, ``terminate_active()`` reaps any owned child, and the
  CLI returns non-zero when cleanup is unresolved. A spawn intent is persisted as
  phase ``spawning`` **before** the fork, so the spawn-to-identity window is
  visible after a crash and no process is signalled blind.
- **Real restart cleanup.** ``startup_recover()`` runs at startup: it loads
  persisted attempts, verifies recorded identity before touching any process,
  terminates surviving managed work, and explicitly resolves interrupted
  pre-spawn/post-spawn states (``spawning`` with no identity is fenced, not
  signalled). Conflicts/claim refusals are left untouched. It never reattaches or
  replays.
- **Process-group completion.** Leader exit is not proof the group emptied.
  ``finalize_group`` reaps surviving descendants attributed by **session id**
  (never a bare PGID, so a reused PGID is never signalled); when attribution is
  unavailable it reports ``group_unresolved`` instead of claiming success, and
  the record stays ``cleanup_required``.
- **Effective limits and authority.** ``max_duration_s`` is enforced with a
  monotonic wall-clock deadline; spawning is refused unless status, desired
  state, lease epoch/attempt and lease expiry are all positively confirmed.
  ``progress_observation_max_failures`` bounds how long progress may be
  unobservable while heartbeats continue. Configuration relationships are
  validated at startup (``heartbeat_interval_s ≤ lease_seconds/2``, positive
  intervals, log cap, identity fields). Required rlimits that cannot be installed
  surface as a spawn failure (Python raises the preexec exception to the parent)
  rather than being silently ignored.
- **Distinct-identity execution.** Workspace root ``0711``; ``attempts``/``logs``
  ``0700`` (child-inaccessible); source dirs ``0755`` with files ``0444``
  (readable, not writable); per-job scratch ``0700`` chowned to the child uid;
  supplementary groups cleared before the uid drop. ``Dockerfile.supervisor``
  creates a concrete unprivileged ``strategy-child`` user and the Compose default
  sets uid/gid ``10002`` with ``REQUIRE_IDENTITY_SEPARATION=true`` (startup fails
  closed if separation is required but absent). No claim of complete isolation.
- **Honest cleanup results.** Cleanup is resolved only on a confirmed 2xx or an
  authenticated state read proving a terminal condition; 403/409/5xx and
  transport errors preserve the ``cleanup_required`` record for retry. CLI exit
  codes: ``0`` success/idle, ``2`` unresolved cleanup or ``api_unreachable``.

### 1.10 Operator reconciliation backend (this slice)

Additive migration `20260915_000021` adds ``strategy_jobs.process_cleanup_state``
/ ``_at`` / ``_actor`` (supervisor-reported, attempt-bound) and the append-only
``strategy_job_reconciliations`` audit table. No existing table is altered
destructively.

- **Authenticated cleanup evidence.** New lifecycle route
  ``POST /hosted-supervisor/jobs/{id}/process-cleanup`` (supervisor credential
  only; a child run token is refused 401) records ``confirmed``/``unresolved``
  bound to the attempt. The supervisor reports it after every stop and during
  restart recovery. The child cannot forge it.
- **Operator surface (app-cookie, owner-scoped, origin-checked).**
  ``GET .../{strategy_id}/jobs``, ``GET .../jobs/{job_id}``,
  ``GET .../jobs/{job_id}/reconciliation`` (assessment + evidence + history),
  ``POST .../jobs/{job_id}/reconciliation`` (attempt-bound action). Cross-owner ⇒
  404; cross-account ⇒ 403; account-unauthorized jobs are omitted from lists.
- **No caller assertion.** The action accepts only ``attempt`` (and optionally
  ``lease_epoch``) for staleness — there is no ``flat=true``/``reconciled=true``
  input. The server classifies persisted evidence; a terminal job label alone
  never unblocks.
- **Reuses existing services, read-only.** The worker-run repo + the paper
  runtime's new **read-only settlement view**
  (`get_strategy_run_settlement_readonly`) — it does **not** call
  `ensure_account`, so reconciliation never creates account state. It runs an
  **authoritative, strategy-attributed order query**
  (`list_orders_for_strategy`, SQL-filtered on strategy identity) that reports a
  `coverage_complete` flag. Truncated results are treated as incomplete coverage
  (`unknown`, blocked) — never as settled. No second position or execution ledger.
- **Evidence axes** (examples and status meanings in §8): process cleanup,
  authority, work, exposure, protection, availability, quiescence. Any missing,
  malformed, unavailable or ambiguous source keeps the axis `unknown` and the
  assessment blocked — it is never read as flat. Confirmed-empty positions (an
  empty `positions` list) are distinguished from missing position data (no run
  state / missing `positions` / malformed quantities).
- **Work is authoritative, not the worker-run label.** `work_state` comes from
  attributed paper order settlement (pending/open/partially-filled ⇒
  outstanding) plus the options run state and protection activity — a
  `closed`/`failed` worker run does **not** by itself mean settled. Data-only
  attempts classify with no trading work regardless of trading-run status.
- **Atomic unblocking.** `reconcile_with_audit` does one transaction: locks the
  strategy row (serializing with `create_job` and cleanup-state updates),
  CAS-matches owner/attempt/lease-epoch/recovery state/process-cleanup/run,
  updates the job to `stopped`, and appends the audit row. If the audit insert
  fails the whole transaction rolls back, so the block is never cleared without a
  durable record.
- **Quiescence, not repeated reads.** There is no durable execution-settlement
  barrier/version in the platform today, so a **trading-capable** attempt cannot
  prove that already-admitted execution will not complete after assessment; it
  stays blocked with `EXECUTION_QUIESCENCE_UNVERIFIED`. Two matching evidence
  reads are **not** treated as proof of quiescence. Only `unlaunched` and
  `data_only_completed` attempts remain reconcilable; when a real barrier is
  later added, `quiescence_state="verified"` unlocks the trading case. If a re-read
  before commit disagrees with the assessment it is refused with
  `EVIDENCE_CHANGED`; the in-DB CAS still fences the final transition.
- **Cases.** (1) ``unlaunched`` — no handoff, no work; (2) ``data_only_completed``
  — no trading capability/work, cleanup established; (3) ``trading_settled_flat``
  — cleanup confirmed, work settled, exposure flat, authority revoked, **and**
  quiescence verified (not currently reachable without a barrier); (4) ``blocked``
  otherwise (open exposure, outstanding/unknown work, unknown/unresolved cleanup,
  active/uncertain authority, pending recovery, unavailable/truncated evidence,
  unverified quiescence, or an active job).
- **Races.** The action pins the attempt and the atomic `reconcile_with_audit`
  CAS-matches the in-DB evidence and locks the strategy row, so it serializes
  with `create_job` and cleanup-state updates (tested on PostgreSQL). A new
  attempt gets a new run/token; the old attempt is never revived. History is
  append-only and never overwritten.
- **Excluded:** Cancel/Flatten execution. Open exposure is reported as a blocking
  reason (``OPEN_EXPOSURE``) and the frontend shows why reconciliation is blocked.

### 1.11 Run-scoped notifications (this slice)

Additive migration `20260915_000022` extends `signal_events` with nullable
`source_kind` (default `workflow`), `owner_id` and `run_id` (TEXT) plus indexes —
no `deliveries` FK change; alert/screener rows are unaffected.

- **Reuses the durable event/outbox/delivery stack**: one `signal_events` row per
  notification and one `deliveries` row per channel, claimed/retried/fenced by the
  existing delivery worker. The `strategy_run` branch is additive; the
  alert/breadth/screener resolver paths are unchanged.
- **Ownership is the hosted strategy's app owner** (`strategy_jobs.owner_id`),
  persisted on the event. Account scope is **not** notification ownership: a
  channel owned by the account-scope string does not resolve (`channel_not_authorized`).
- **Child endpoint** `POST /worker/runs/{id}/notify` (action
  `notifications:publish`, hosted attempt + session nonce required; external runs
  refused `HOSTED_NOTIFY_UNSUPPORTED`). **SDK** `ManagedRun.notify(text,
  channels=..., idempotency_key=..., subject=...)` (SDK 0.13.0).
- **Atomic** event + deliveries in one transaction. Unknown (422), disabled (422)
  and unauthorized (403) channels are explicit errors raised **before** any write —
  no partial rows.
- **Idempotency** is the caller key (`hosted-run:{run_id}:{key}`): same key + same
  content deduplicates (`deduped`, writes nothing); same key + different content
  conflicts (409). There is no process-local sequence number.
- **Loader/rendering/history adapter**: `build_run_message` renders the caller
  text with the run id, and `list_run_notifications(owner_id, run_id)` provides
  run-scoped history. Provider acceptance is **not** confirmed receipt, and a
  notification outcome never authorizes trading.

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

Backend suites (SQLite), exact command and result of the pre-deployment pass:

```
.venv/bin/python -m pytest \
  tests/strategies \
  tests/api/test_strategies_api.py \
  tests/api/test_algo_worker_api.py \
  tests/api/test_algo_worker_route_mounts.py \
  tests/api/test_worker_run_discovery.py \
  tests/api/test_worker_notifications.py \
  tests/api/test_worker_workflows.py \
  tests/api/test_hosted_lifecycle_api.py \
  tests/api/test_hosted_child_authority.py \
  tests/api/test_operator_controls.py \
  tests/notifications \
  tests/sdk -q
→ 742 passed, 2 skipped
```

Disposable PostgreSQL (real concurrency; own invocation):

```
HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_supervisor_lifecycle_postgres.py \
    tests/integration/test_hosted_strategy_foundation_postgres.py \
    tests/integration/test_run_notifications_postgres.py -q
→ 26 passed       # + atomic reconcile-with-audit (one winner) serialized with
                  # create_job; changed-cleanup-evidence CAS refusal; append-only
                  # audit; concurrent same-key launch and stop-authority retention
```

Frontend suites (component tests, not browser tests) — run per command with
`NODE_ENV=test` because this environment exports `NODE_ENV=production`, which
removes `React.act` and breaks every `@testing-library/react` render (no global
setting is changed):

```
cd frontend-next
NODE_ENV=test npx vitest run features/strategies   → 14 passed
npx tsc --noEmit                                    → clean
npx eslint features/strategies lib/hosted-strategies "app/(app)/strategies" → clean
npm run build                                       → succeeds
```

`alembic heads` → single head `20260915_000024`. `git diff --check` clean.

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

Process-supervision tests (added this slice) — **real process tests** vs
**mocked orchestration tests** are distinguished:

- `tests/strategies/test_supervisor_process.py` — **real local children** (no
  mocks): completion, crash exit code, hang → process-group termination, leader
  exits while a **descendant survives** (group reaped), bounded log with overflow
  flag, PID-reuse rejection, and unattributable-group → `unresolved` without
  signalling. One **root-gated** test
  (`test_child_identity_reads_source_writes_scratch_cannot_alter_source`) runs a
  real cross-UID child and asserts it reads source, cannot write source, cannot
  read the supervisor attempt records, and can write scratch. This environment is
  non-root, so that case is **skipped** here (permission *bits* are still asserted
  non-root in the orchestrator tests).
- `tests/strategies/test_supervisor.py` — **mocked orchestration** (fake
  lifecycle API, harmless local child): happy path (prepare once, source
  verified, release → `recovery_required`), `409` conflict does not fence, lost
  prepare response fails closed without replay, authority loss stops+fences,
  API-unreachable cleanup visible/retryable, progress stall fences, progress
  observation-failure budget fails closed, max-duration deadline fences,
  exception immediately after spawn still stops the child, `request_stop` /
  signal handler stop the child, `startup_recover` terminates a surviving child
  and treats `spawning`-without-identity as unrecorded (no signal), expired lease
  refuses spawn, source-hash mismatch refuses spawn, config-relationship
  validation, cleanup 403/5xx stays retryable, workspace permission bits, and the
  child environment allowlist.
- `tests/api/test_hosted_child_authority.py` — child progress is
  `runs:progress` + session-bound, refused when fenced, and only accepted progress
  writes `last_progress_at`; options operation-permission tests.
- `tests/sdk/test_hosted_bootstrap.py` — `main(ctx)` loading and `ctx.progress`.
- `tests/strategies/test_repository.py` — `record_progress` only for live jobs.
- Reconciliation tests (this slice):
  - `tests/strategies/test_reconciliation.py` — **pure** assessment across the four
    cases and every evidence axis, plus the **real collector** driven with
    production-shaped read-only settlement responses: nested `strategy.positions`
    with nonzero exposure blocked; `None`/missing/malformed quantities stay
    unknown (never flat); confirmed-empty run state is flat; closed worker run
    with pending orders stays outstanding; unavailable paper service; attribution
    mismatch; active authority; data-only completion with an open trading run.
  - `tests/api/test_reconciliation_api.py` — allowed reconciliation unblocks a new
    attempt; open exposure stays blocked + audited; stale attempt refused;
    cross-owner 404; cross-account 403 (list omits); data-only completion records
    `not_applicable`; unavailable evidence blocked; **evidence changed between
    assessment and commit refused**; **audit-write failure rolls back unblocking**
    (job stays blocked, no audit row); owner list/detail; authentication required.
  - `tests/api/test_hosted_lifecycle_api.py` — process-cleanup evidence is
    attempt-bound, visible in state, refused for a stale attempt, and refused for
    a child worker token (401).
  - PostgreSQL — atomic reconcile-with-audit has exactly one winner and serializes
    with `create_job`; a changed cleanup-evidence CAS is refused; audit is
    append-only and ordered.
- Run-scoped notification tests (this slice):
  - `tests/notifications/test_run_notifications.py` — atomic event+deliveries,
    same-key dedupe, same-key/different-content conflict, unknown/disabled/
    unauthorized channels write nothing, account-scope is not ownership,
    run-scoped history, and the `strategy_run` resolver/rendering adapter.
  - `tests/api/test_hosted_child_authority.py` — child notify accepted then
    deduped, unknown channel 422, session nonce required, `notifications:publish`
    required, external run refused.
  - `tests/api/test_operator_controls.py` — Run now (idempotent retry, active/
    disabled/recovery blocks, cross-owner/account/origin), Stop (queued without
    launch; active preserves supervisor cleanup + replacement block;
    cleanup-unresolved without evidence), bounded/redacted logs with explicit
    unavailable/truncated states, and notification-history ownership. Plus a
    PostgreSQL concurrent same-key Run now (one job) and stop-authority retention.
  - `tests/integration/test_run_notifications_postgres.py` — columns present;
    concurrent same-key enqueue yields exactly one event + one delivery with one
    `accepted` and one `deduped`; conflicting content after commit is rejected.

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

- **Operator controls now exist (backend).** Run now (idempotent), Stop
  (queued/active, with distinct requested/stopping/confirmed/cleanup-unresolved
  states), bounded/redacted logs and run-notification history, plus the
  inspection/reconciliation from the previous slice. An authorized operator can
  inspect why a `recovery_required` attempt is blocked and clear the block only
  when server-side evidence supports it. **Restriction:** there is no
  durable execution-settlement barrier, so a **trading-capable** attempt remains
  blocked with `EXECUTION_QUIESCENCE_UNVERIFIED`; only `unlaunched` and
  `data_only_completed` attempts reconcile today. **Still missing:** the barrier
  itself, Cancel/Flatten execution (when exposure is open the operator has no
  backend action to settle it), and the frontend for the reconciliation surface
  (§8 is the API handoff).
- **Run-scoped notifications are backend-only.** The child endpoint and SDK
  exist; delivery happens asynchronously through the existing outbox worker and
  provider acceptance is not confirmed receipt. No frontend is added.
- **Scheduling is not implemented.** Discovery lists `queued` jobs; nothing
  creates them on a schedule. The supervisor polls (`run_forever`) or runs once.
- **Options fail-closed mode propagation and the futures contract resolver**
  remain out of scope (later H1/H2 work). The options boundary binds hosted
  tokens to their worker run and enforces operation permissions/paper-only, but
  does not add live options execution.
- **Delete/cleanup of workspace artifacts** (attempt records, source, logs) is
  not automated; retention is a deployment concern.
- **Reconciliation of a child that outlives the supervisor** is not attempted
  (no reattach by design). On restart the supervisor terminates surviving
  children it recorded and fences the attempt; a job found `running` without a
  local record is left for control-plane reconciliation.
- Normal script completion is **not** proof of flatness; a finite job may finish
  with open exposure, and the report does not claim otherwise.
- **The real cross-UID isolation test is root-gated** and is skipped when the
  test process is not root (as in this environment). Non-root tests assert the
  permission *bits* and the spawn mechanics; the Dockerfile/Compose defaults are
  configured for real separation but were not exercised as a container here.
- Signal handling is exercised through `request_stop`/`_handle_signal` in tests,
  not by delivering a real `SIGTERM` to the test process.

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
| `HOSTED_SUPERVISOR_CHILD_UID` / `_GID` | no | unset | Drop the child to a distinct OS identity (needs privilege; set together) |
| `HOSTED_SUPERVISOR_REQUIRE_IDENTITY_SEPARATION` | no | `false` | Fail startup unless child uid/gid are configured |
| `HOSTED_SUPERVISOR_PROGRESS_OBS_MAX_FAILURES` | no | `3` | Consecutive unobservable-progress reads before failing closed |
| `HOSTED_SUPERVISOR_API_TIMEOUT_S` | no | `10` | Lifecycle API timeout |

Validation at startup rejects unsafe relationships: ``heartbeat_interval_s ≤
lease_seconds/2``, positive intervals, ``max_log_bytes ≥ 1024``, uid/gid set
together, and (when required) identity separation present.

API side (unchanged from §2): `HOSTED_SUPERVISOR_CREDENTIAL(S)` and
`HOSTED_STRATEGY_ACCOUNT_SCOPES`.

CLI: `python -m backend.strategies.supervisor` (loop), `--once`,
`--job <job_id>`, `--retry-cleanup`. Exit codes: `0` success/idle, `2`
unresolved cleanup (or `api_unreachable`). Packaging: `Dockerfile.supervisor`
creates the unprivileged `strategy-child` user; `compose.supervisor.yml` adds a
`strategy-runner` service with **no** `env_file`/DB credentials, a dedicated
state volume, child uid/gid `10002` and `REQUIRE_IDENTITY_SEPARATION=true`. This
is configuration only — **not** deployed or started.

**Child environment allowlist (exact):** `PATH`, `HOME` (scratch),
`PYTHONUNBUFFERED`, `PYTHONPATH`, `KITE_ALGO_BASE_URL`,
`KITE_ALGO_WORKER_TOKEN`, `KITE_ALGO_RUN_ID`, `KITE_ALGO_SESSION_NONCE`,
`KITE_ALGO_TEMPLATE_ID`, `KITE_ALGO_ACCOUNT_SCOPE`, `KITE_ALGO_MODE`,
`KITE_ALGO_PARAMS`, `KITE_ALGO_SCRATCH`. `KITE_ALGO_ACCOUNT_SCOPE` is an
implementation-resolved detail: `attach_run` validates template/account/mode, so
the account scope (a non-secret config value) must reach the child.

## 7. Deployment readiness — what is and is not true

Four separate things, deliberately not conflated.

### 7.1 Implemented functionality

Supervisor lifecycle (claim → prepare → source verify → spawn → heartbeat →
progress fence → stop/release/fence/recover); process containment with a distinct
child OS identity; exception/signal-safe shutdown; restart cleanup; process-group
completion; limits/authority enforcement; honest cleanup results and CLI exit
codes; the child bootstrap with an attach-only `ManagedRun`; supervisor
credential auth; the operator reconciliation backend (inspection, action,
evidence classification, durable append-only audit); bounded reconciliation
safeguards; run-scoped notifications; the operator controls (Run now, Stop,
bounded/redacted logs, notification history); the **hosted-strategy frontend**
(list, versions, configure, run now, job detail, stop, logs, notifications,
reconciliation) and the `GET /api/strategies/options` authorization contract.

### 7.2 Executed verification (this pass — see §11 for commands and evidence)

- Focused backend suites: 742 passed, 2 skipped (SQLite).
- Disposable PostgreSQL concurrency suites: 26 passed.
- Frontend: `tsc --noEmit` clean, `eslint` clean, `next build` succeeds,
  component tests 14 passed.
- **Browser pass** (headless Chrome over CDP against an isolated deployment:
  real API + disposable PostgreSQL + dev server, never production accounts):
  list, configuration, version registration, Run now (queued/replay/new),
  Stop (queued and running), logs (pagination, truncation, post-termination
  availability), notification delivery/attempt history, data-only reconciliation,
  trading reconciliation blocked, and loading/empty/auth/authorization/server-error
  states. Screenshots in §11.3.
- **Runner packaging**: the supervisor image builds; the Compose configuration
  validates with no DB/broker environment in the runner service; a harmless
  data-only child ran inside the container as the configured distinct UID
  (10002); source readable-not-writable, per-job scratch writable, supervisor
  records unreadable to the child; a real `SIGTERM` to the supervisor while a
  child was alive produced bounded shutdown (2.1 s), no surviving child and an
  honest terminal/recovery state. Evidence in §11.4.

### 7.3 Unexecuted deployment checks (still required before rollout)

- Migration applied to the **real** deployment database (only the disposable
  database was migrated here; `alembic heads` = `20260915_000024`).
- The full application image/rollout (this pass built and ran only the
  supervisor image), plus a rollback rehearsal.
- Real-provider notification delivery/acceptance (fixtures only here).
- Cross-host networking/DNS for `HOSTED_SUPERVISOR_BASE_URL` in the target
  environment (verified here with host networking, not the Compose network).
- Restart behaviour under an orchestrator (restart policy, volume persistence
  across node replacement).

### 7.4 Deferred product capabilities (not blockers for the manual data-only release)

Scheduling loop, Cancel/Flatten execution, live trading, Go/MCP surfaces.

**Execution-settlement barrier:** the durable barrier is required for
**trading-capable reconciliation** (clearing a block on a launched, trade-capable
attempt). It is **not** required for the manual data-only release: data-only
attempts reconcile through the `unlaunched` / `data_only_completed` cases, and
trading-capable reconciliation stays blocked with
`EXECUTION_QUIESCENCE_UNVERIFIED` — a genuine, non-dismissible block.

### 7.5 Explicitly not claimed

Complete security isolation (v1 is a trusted single-operator setup; per-run
filesystem isolation is not promised), exactly-once execution, that a
stopped/revoked attempt is flat, that the child cannot be affected by host-level
compromise, or that provider acceptance equals receipt.

## 8. Frontend handoff — reconciliation API, examples and status meanings

All routes are app-cookie authenticated, origin-checked, owner-scoped and
account-authorized. Owner is server-derived; cross-owner ⇒ 404, cross-account ⇒
403.

**List jobs** `GET /api/strategies/{strategy_id}/jobs`

```json
{ "jobs": [ {
  "job_id": "hsj_ab12", "strategy_id": "hs_9", "owner_id": "app:admin",
  "attempt": 2, "status": "recovery_required", "desired_state": "started",
  "execution_mode": "paper", "account_scope": "kite:paper", "run_id": "run_77",
  "replacement_blocked": true, "recovery_required_at": "2026-09-15T10:00:00+00:00",
  "reconciled_at": null, "created_at": "...", "updated_at": "..."
} ] }
```

**Job detail** `GET /api/strategies/{strategy_id}/jobs/{job_id}` — the summary
plus `handoff_at`, `process_cleanup_state`, `process_cleanup_at`,
`process_cleanup_actor`, `last_progress_at`, `version_id`, `token_present`.

**Inspect** `GET /api/strategies/{strategy_id}/jobs/{job_id}/reconciliation`

```json
{
  "job_id": "hsj_ab12", "strategy_id": "hs_9", "attempt": 2,
  "replacement_blocked": true,
  "assessment": { "allowed": false, "case": "blocked",
                  "reason_code": "OPEN_EXPOSURE",
                  "blocking_reasons": ["OPEN_EXPOSURE"],
                  "notes": [] },
  "evidence": {
    "launched": true, "trade_capable": true, "job_status": "recovery_required",
    "process_cleanup_state": "confirmed", "authority_state": "revoked",
    "run_status": "closed", "work_state": "settled", "exposure_state": "open",
    "protection_state": "settled", "evidence_complete": true, "unavailable": []
  },
  "history": [ { "id": "hsr_1", "attempt": 2, "outcome": "blocked",
                 "reason_code": "OPEN_EXPOSURE", "actor_id": "app:admin",
                 "run_id": "run_77", "evidence": { }, "created_at": "..." } ]
}
```

**Reconcile** `POST /api/strategies/{strategy_id}/jobs/{job_id}/reconciliation`

```json
{ "attempt": 2, "lease_epoch": 3 }
```

- `200` `{ "status": "reconciled", "case": "trading_settled_flat", ... }` — the
  block is cleared; a new attempt may start (new run/token).
- `409` with `detail.rejection_reason` one of the reason codes below; the audit
  row is still written (`outcome: "blocked"`).
- `409 STALE_ATTEMPT` / `STALE_LEASE_EPOCH` — re-inspect and retry.
- `409 RECONCILE_RACE_LOST` — the attempt changed; re-inspect.

**Status meanings**

| Field / code | Meaning |
| --- | --- |
| `case = unlaunched` | No child credential was handed off; no work possible |
| `case = data_only_completed` | No trading capability/work; process cleanup established |
| `case = trading_settled_flat` | Cleanup confirmed, work settled, exposure flat, authority revoked |
| `case = blocked` | Evidence does not support unblocking (see codes) |
| `case = not_blocked` | Replacement is not blocked; nothing to reconcile |
| `process_cleanup_state` | `confirmed` (process group gone) / `unresolved` / `null` unknown |
| `authority_state` | `revoked` / `active` (credential still live) / `uncertain` |
| `work_state` | `none` / `settled` / `outstanding` / `unknown` |
| `exposure_state` | `not_applicable` (data-only) / `flat` / `open` / `unknown` |
| `PROCESS_CLEANUP_UNKNOWN`/`_UNRESOLVED` | Child cleanup not established |
| `AUTHORITY_ACTIVE`/`_UNCERTAIN` | Child credential not demonstrably revoked |
| `OUTSTANDING_WORK` / `WORK_UNKNOWN` | Accepted work not settled / could not be read |
| `OPEN_EXPOSURE` / `EXPOSURE_UNKNOWN` | Attributable exposure open / could not be read |
| `PROTECTION_UNKNOWN` / `RECOVERY_ACTION_PENDING` | Protection state unavailable / recovery action outstanding |
| `EVIDENCE_UNAVAILABLE` | A required evidence source could not be read |
| `EVIDENCE_CHANGED` | Execution evidence changed between assessment and commit (re-inspect) |
| `EXECUTION_QUIESCENCE_UNVERIFIED` | No durable barrier proves already-admitted execution cannot complete; trading-capable reconciliation blocked |
| `HOSTED_JOB_ACTIVE` | Attempt is live; stop it before reconciling |
| `HOSTED_JOB_NOT_BLOCKED` | Replacement is not blocked |

Open exposure is surfaced as `OPEN_EXPOSURE`; this slice deliberately does **not**
add Cancel/Flatten execution — the frontend shows the blocking reason and the
future operator action needed.

### 8.1 Run-scoped notification API (worker/SDK)

Child endpoint (hosted run, session nonce, `notifications:publish`):

```http
POST /api/algo-workers/worker/runs/{strategy_run_id}/notify
X-Worker-Session-Nonce: <nonce>
Authorization: Bearer <child token>

{ "text": "target hit", "channels": ["ops"], "idempotency_key": "abc12345", "subject": "optional" }
```

Responses:

```json
200 { "strategy_run_id": "run_77", "status": "accepted", "event_id": "...", "delivery_count": 1 }
200 { "strategy_run_id": "run_77", "status": "deduped",  "event_id": "...", "delivery_count": 1 }
409 { "detail": { "rejection_reason": "idempotency_conflict", "message": "idempotency key reused with different content" } }
422 { "detail": { "rejection_reason": "unknown_channel", "message": "unknown channel 'nope'" } }
403 { "detail": { "rejection_reason": "channel_not_authorized" } }
409 { "detail": { "rejection_reason": "WORKER_SESSION_REQUIRED" } }
403 { "detail": { "rejection_reason": "HOSTED_NOTIFY_UNSUPPORTED" } }
```

Status meanings: `accepted` = event + deliveries durably enqueued (provider send
happens later, asynchronously, and is not confirmed receipt); `deduped` = the same
key/content already enqueued, nothing written; `idempotency_conflict` = same key,
different content. Storage errors are surfaced explicitly to the caller; a
notification outcome never authorizes trading.

Operator/SDK surface for the future frontend: channel management stays on the
existing `/api/algo-workers/worker/notification-channels` routes; a run's
notification history is available via `list_run_notifications(owner_id, run_id)`
(run-scoped `signal_events` newest first). No frontend is added in this slice.

## 9. Frontend handoff — operator controls (end to end)

Additive migration `20260915_000023` adds `strategy_jobs.stop_requested_at/_by`
and the bounded, redacted `strategy_job_logs` table.

`GET /api/strategies/options` returns the **server-authorized** selection options
(`account_scopes` from the `HOSTED_STRATEGY_ACCOUNT_SCOPES` allowlist, plus
supported `execution_modes`, `job_kinds`, `stale_exit_policies`). The browser
must not invent account choices.

Endpoints (all app-cookie, origin-checked, owner-scoped, account-authorized;
cross-owner ⇒ 404, cross-account ⇒ 403):

| Step | Method / path |
| --- | --- |
| 0. Options | `GET /api/strategies/options` |
| 1. List | `GET /api/strategies` |
| 2. Register version | `POST /api/strategies/{id}/versions` |
| 3. Configure | `POST /api/strategies` / `PATCH /api/strategies/{id}` |
| 4. Run now | `POST /api/strategies/{id}/jobs` |
| 5. Inspect | `GET /api/strategies/{id}/jobs`, `.../jobs/{job_id}`, `.../reconciliation`, `.../logs`, `.../notifications` |
| 6. Stop | `POST /api/strategies/{id}/jobs/{job_id}/stop` |
| 7. Reconcile | `POST /api/strategies/{id}/jobs/{job_id}/reconciliation` |

**Run now** — `POST /api/strategies/{id}/jobs`

```json
{ "version_id": "hsv_4", "params": { "lots": 1 }, "execution_mode": "paper",
  "job_kind": "finite", "idempotency_key": "run-2026-09-15-1" }
→ 200 { "idempotent": false, "job": { "job_id": "hsj_…", "status": "queued",
        "attempt": 1, "replacement_blocked": true, "stop": { "state": "none" }, … } }
```

- The `idempotency_key` is **bound to the normalized launch request** (version,
  params, execution mode, job kind). An identical retry returns the same job
  with `"idempotent": true` (no duplicate, even concurrently); the same key with
  a different request returns `409 IDEMPOTENCY_CONFLICT`.
- `409 STRATEGY_BLOCKED` (active/unreconciled job), `409 STRATEGY_DISABLED`,
  `422` (unknown version or invalid params), `403` (account not authorized).
- The response returns **identity only**; it does not claim the process started.

**Inspect a job** — `GET /api/strategies/{id}/jobs/{job_id}` includes
`status`, `desired_state`, `attempt`, `run_id`, `process_cleanup_state`,
`stop` and `replacement_blocked`.

- `stop.state`: `none` | `requested` | `stopping` | `confirmed` |
  `cleanup_unresolved`. **Unknown process state is not "stopped"**: a launched
  job whose supervisor has not reported `process_cleanup_state="confirmed"`
  reports `cleanup_unresolved`, never `confirmed`.

**Stop** — `POST /api/strategies/{id}/jobs/{job_id}/stop`

```json
{ "attempt": 1, "lease_epoch": 3 }
→ 200 { "job_id": "hsj_…", "attempt": 1, "idempotent": false,
        "stop": { "requested": true, "state": "stopping",
                  "replacement_blocked": true,
                  "note": "… Stop does not cancel orders or flatten." } }
```

- Queued work is stopped **without launching** (`state: "confirmed"`,
  `replacement_blocked: false`).
- Active work gets a durable stop request the supervisor observes; it keeps its
  lease so it can stop the child and complete the authorized terminal
  transition. Launched work stays `recovery_required` until reconciled.
- **Stop does not cancel orders or flatten positions.** Cancel/Flatten are not
  implemented in this slice.

**Logs** — `GET /api/strategies/{id}/jobs/{job_id}/logs?after_seq=&limit=`

```json
{ "available": true, "truncated": false, "next_seq": 12,
  "entries": [ { "seq": 11, "content": "…[redacted]…", "created_at": "…" } ],
  "notice": "" }
```

- The API never reads the supervisor container's filesystem; the supervisor
  pushes bounded chunks to the lifecycle API, which **redacts known credentials**
  (request chunks are joined before redaction, so a credential split across
  transport chunk boundaries in one request is still masked) and caps the
  per-attempt size in exact UTF-8 bytes. `available:false` (with a `notice`) means
  logs were not collected; `truncated:true` reflects actual loss (a persisted
  `discarded` flag OR the cap), even when stored bytes are below the cap.
  `source: "post_termination"` distinguishes post-termination collection from
  live logs (live collection is not implemented). `after_seq` paginates.
  Redaction is best-effort: it cannot guarantee removal of an arbitrary secret,
  and a secret split across *separate* ingestion requests may not be masked.
  Log collection is independent of process-cleanup confirmation.

**Notification history** — `GET /api/strategies/{id}/jobs/{job_id}/notifications`

```json
{ "run_id": "run_77", "events": [ {
  "event_id": "…", "fired_at": "…", "text": "target hit",
  "delivery_status_counts": { "pending": 1 },
  "deliveries": [ { "channel_name": "ops", "status": "pending", "attempts": 0,
                    "attempt_history": [] } ] } ] }
```

- Delivery `status`: `pending|delivering|delivered|retrying|failed|expired`.
  **Provider acceptance is not confirmed receipt** — `delivered` means the
  provider accepted the send, not that a human received it.

**Reconciliation** — see §8. `EXECUTION_QUIESCENCE_UNVERIFIED` is a **genuine
block**, not a dismissible warning: without a durable execution-settlement
barrier, a trading-capable attempt cannot prove already-admitted execution will
not complete later, so the block cannot be cleared. Only `unlaunched` and
`data_only_completed` attempts reconcile today.

**Data-only example**: register a version with `{"capabilities": {"data": true}}`
and `POST .../jobs`; on completion the job reconciles via the
`data_only_completed` case (no trading work). **Notification-capable example**:
register `{"capabilities": {"data": true, "notify": true}}`; the child uses
`ManagedRun.notify(...)` (§8.1). **Paper trading limitations**: hosted v1 is
paper/dry_run only; options mutation is paper-only and `dry_run` is
preview-only; no Cancel/Flatten, no live trading; trading-capable reconciliation
is blocked (above).

### 9.1 Remaining release prerequisites

- **Execution-settlement barrier** (quiescence) — required before trading-capable
  reconciliation can clear a block.
- **Cancel/Flatten adapters** — settling open exposure after a stop.
- **Frontend implementation** — delivered in §10 (list, versions, configure,
  run now, job inspection, stop, logs, notifications, reconciliation).
- **Scheduling loop**, **notification delivery in a real environment**, and a
  **deployment pass** (container build, real cross-UID run, delivered-signal
  shutdown) — none executed here.

## 10. Frontend implementation (hosted strategies)

Implemented in `frontend-next` using the existing conventions (App Router under
`app/(app)/strategies`, `apiFetch`, TanStack Query, server-derived auth via the
session cookie and `require_strategy_owner`, existing UI primitives).

### 10.1 Files

| Concern | Path |
| --- | --- |
| Types | `lib/hosted-strategies/types.ts` |
| API wrappers | `lib/hosted-strategies/api.ts` |
| Query keys / hooks | `features/strategies/hooks/keys.ts`, `features/strategies/hooks/use-hosted-strategies-queries.ts` |
| List | `features/strategies/components/hosted-strategies-list-page.tsx` |
| Detail (versions + configure + run now + jobs) | `features/strategies/components/hosted-strategy-detail-page.tsx` |
| Job (state, stop, logs, notifications, reconciliation) | `features/strategies/components/hosted-job-detail-page.tsx` |
| Routes | `app/(app)/strategies/page.tsx`, `app/(app)/strategies/[strategyId]/page.tsx`, `app/(app)/strategies/[strategyId]/jobs/[jobId]/page.tsx` |
| Helpers | `features/strategies/lib/format.ts`, `features/strategies/lib/params.ts` |

### 10.2 User flows

- **List / enable / disable** — `GET /api/strategies`; `PATCH` toggles
  `status`. Disabling stops *new* attempts only.
- **Register version** — Python source + JSON-Schema parameters + `data|trade|notify`
  capabilities → `POST …/versions`; versions are immutable and shown with their
  pinned SHA-256.
- **Configure** — account scope, mode, job kind and stale-exit policy are chosen
  from `GET /api/strategies/options` only; no hardcoded account list.
- **Run now** — an **idempotency key is generated per launch** (`New key` for a
  genuinely new launch; retries reuse it). The success toast/alert says
  **"Queued — the process has not started yet"**; a replay is labelled as a
  replay. Params are entered as JSON and validated server-side.
- **Job detail** — status, desired state, attempt, run id, replacement block,
  process-cleanup state and stop state. The job query polls while the attempt is
  `queued|starting|running|fencing`.
- **Stop** — targets the current attempt identity; the UI shows the API's
  `requested` / `stopping` / `confirmed` / `cleanup_unresolved` distinction and
  states plainly that **Stop does not cancel orders or flatten**.
- **Logs** — paginated by `after_seq`, with an explicit **unavailable** notice
  (not "no output") and a **truncation** alert that reflects persisted discard;
  `source: post_termination` is shown.
- **Notifications** — event/delivery status with per-attempt history; the copy
  states provider acceptance ≠ confirmed receipt.
- **Reconciliation** — inspection (case, reason, blocking reasons, raw evidence,
  history) and the existing action. An `EXECUTION_QUIESCENCE_UNVERIFIED` block is
  rendered as **not dismissible** and disables the action; only `unlaunched` /
  `data_only_completed` attempts can be reconciled.

### 10.3 Product semantics encoded in the UI

- Run-now success = **queued**, not started.
- Stop ≠ cancel orders / flatten.
- Unknown cleanup = **not** confirmed stopped.
- `EXECUTION_QUIESCENCE_UNVERIFIED` is a genuine block.
- Data-only reconciliation is supported; trading reconciliation stays blocked.
- Provider acceptance ≠ notification receipt.
- Paper / dry-run limitations are shown; live trading is not advertised.

### 10.4 Verification

These are **component tests** (`@testing-library/react` + jsdom), not browser
tests. The real-browser pass is §11.3.

- `npm run typecheck` (tsc `--noEmit`) → clean.
- `npx eslint features/strategies lib/hosted-strategies "app/(app)/strategies"` → clean.
- `npm run build` → succeeds; routes `/strategies`, `/strategies/[strategyId]`,
  `/strategies/[strategyId]/jobs/[jobId]` are emitted.
- `NODE_ENV=test npx vitest run features/strategies` → **14 passed** (list scoping
  + toggle, queued-attempt stop affordance, job cleanup-unresolved + truncation +
  non-dismissible quiescence, format and params helpers).
- Note: this environment exports `NODE_ENV=production`, which removes `React.act`
  and makes every `@testing-library/react` render fail (`React.act is not a
  function`) — pre-existing for other suites too. Component tests therefore run
  with `NODE_ENV=test` **per command**; no global or repo setting was changed.

### 10.5 Remaining restrictions vs. deployment prerequisites

Still **restrictions** (v1 product scope): no scheduling UI, no Cancel/Flatten,
no live trading, trading-capable reconciliation blocked, real-provider
notification delivery not exercised.

Still **deployment prerequisites**: applying the migration to the real database,
the full application rollout, real-provider notification delivery, and
cross-host networking validation. (The runner image build, the distinct-UID
child run and delivered-signal shutdown were executed in this pass — §11.4.)

## 11. Pre-deployment verification pass — manual data-only release

Scope: verify the **manual data-only release** (operator registers a version with
data-only capabilities, runs it, watches it, stops it, reconciles it) before any
deployment. No production system was touched: no orders, no real notifications,
no deployment, no live migration, no scheduling, no Cancel/Flatten.

### 11.1 Isolated environment actually used

| Piece | What ran |
| --- | --- |
| Control plane | The real routers (`auth`, `strategies`, `hosted_lifecycle`, `worker_auth`, `worker_execution`) served by uvicorn on `127.0.0.1:8181` from a harness module in the session scratch dir — no production container, no production credentials |
| Database | **Disposable** PostgreSQL 16 in `kite-test-postgres` (`127.0.0.1:15433`), database `hosted_verify`, migrated from `alembic upgrade head` |
| Data | Disposable fixtures seeded through the real HTTP APIs (strategies, versions, jobs) plus synthetic notification rows |
| Frontend | `next dev` on `127.0.0.1:3300` with `BACKEND_INTERNAL_URL=http://127.0.0.1:8181` |
| Browser | Headless Chrome 146 driven over CDP (`Page.captureScreenshot`, real DOM events) |
| Supervisor | `kite-strategy-runner:verify` built from `Dockerfile.supervisor`, run with host networking against the same disposable control plane |

The account scope allowlist was `kite:paper` (the seeded fixture scope) except in
the authorization-state check, where it was deliberately changed to prove the
refusal path. No production account, token, order or notification was involved.

### 11.2 Browser flows exercised (results)

| Flow | Result |
| --- | --- |
| List + enable/disable, authorized scopes only | Renders seeded strategies; the scope selector offers only `kite:paper`; toggling issues `PATCH` |
| Register strategy and version, configure parameters | Created from the form (Radix selects included); version registered and shown with its pinned SHA-256; empty jobs table showed the empty state |
| Run now → queued is honest | Toast: “Queued. The process has not started yet.” |
| Retry the same launch after an uncertain response | Second click with the same key: “Queued — this retry replayed the original launch (no new job).”; job count unchanged |
| Change inputs and start a genuinely new request | New key + changed params → blocked while an attempt was active (operator copy + `STRATEGY_BLOCKED`), then created a second job once the queued attempt was stopped |
| Stop: requested vs confirmed cleanup | Running attempt → “Stopping — Stop requested…”; after supervisor cleanup + release → “Stopped and process cleanup confirmed”; queued attempt → “Stopped before launch; no child process was ever started.” |
| Logs: pagination, post-termination availability, truncation | 200 chunks → “Load more” → 226 chunks accumulated; `post_termination` source notice; a job past the 256 KB cap showed the truncation banner |
| Notification delivery/attempt history (synthetic) | Two events, three deliveries (`delivered` with provider id, `failed` with last error, `pending`), per-attempt history rendered |
| Supported data-only reconciliation | `Ready to reconcile / data_only_completed` → action cleared the block (`reconciled_at` set) |
| Trading reconciliation blocked | `Blocked / EXECUTION_QUIESCENCE_UNVERIFIED`, action disabled, copy says the block is not dismissible |
| Unknown process state | Fenced attempt shows “Cleanup unresolved — … Unknown is not 'stopped'.” |
| Loading / empty / auth / authorization / server-error | Skeletons under throttling; “No attempts yet.”; unauthenticated visit redirects to `/login`; unauthorized scope refused explicitly; injected 500 shows “Could not load job …” |

### 11.3 Screenshots

Committed (captured against the isolated deployment above — never production):
`documents/verification/hosted-strategies-2026-09-15/`

| File | Shows |
| --- | --- |
| `01-strategies-list.png` | Strategy list + create form, authorized scope only |
| `02-strategy-detail.png` | Configuration, Run now, immutable versions, jobs |
| `03-job-detail.png` | Job state, process cleanup, stop, logs, notifications |
| `06-reconciled.png` | Data-only reconciliation cleared the block |
| `07-trading-blocked.png` | `EXECUTION_QUIESCENCE_UNVERIFIED` blocked and non-dismissible |
| `11-cleanup-unresolved.png` | Cleanup unresolved ≠ confirmed stopped |
| `17-queued-stop-before-launch.png` | Queued stop confirmed before launch |
| `19-loading-state.png` | Loading skeletons |
| `20-auth-redirect.png` | Unauthenticated redirect to login |
| `21-server-error.png` | Server-error state |

Additional session-only captures (not committed): stop-requested, recovery with
confirmed cleanup, queued stop with the older wording, log pagination before/after
“Load more”, truncated logs, run-now replay/new-request, blocked-run copy,
unauthorized-scope and injected-failure states.

### 11.4 Runner packaging evidence (executed)

```
docker build -f Dockerfile.supervisor -t kite-strategy-runner:verify .        → success
HOSTED_SUPERVISOR_CREDENTIAL=… docker compose -f compose.yml \
  -f compose.supervisor.yml config                                            → valid
  strategy-runner env: 0 entries matching DB_/DATABASE_URL/REDIS/KITE_API/env_file
```

Harmless data-only child (short-lived) inside the container:

```
docker run … kite-strategy-runner:verify python -m backend.strategies.supervisor \
  --once --job hsj_a771c3af9bb545d5ad9a50b45b4ece4e
→ job: recovery_required, process_cleanup_state=confirmed, last_progress_at set
→ logs (post_termination): "short child: hello from the supervised container"
```

Distinct identity, confirmed at runtime with `docker top`:

```
root    python -m backend.strategies.supervisor --once --job hsj_…
10002   python -m kite_algo_worker.hosted /var/lib/kite-supervisor/source/<job>/<version>.py
```

Filesystem separation, verified as the child uid (`--user 10002:10002`):

```
supervisor attempts dir: PERMISSION DENIED (expected)
supervisor logs dir:     PERMISSION DENIED (expected)
source files visible: 5 | readable: True | writable: False | append refused: PermissionError
per-job scratch writable by child: True (wrote a probe file)
```

Delivered signal (not a handler call) — `docker stop -t 30` while the child was
alive and reporting progress:

```
before: status=running, last_progress_at set, cleanup=null
docker stop → shutdown took 2.1 s, container exit code 0
supervisor log: WARNING hosted_supervisor_signal → INFO hosted_supervisor_once
after:  status=recovery_required, process_cleanup_state=confirmed,
        replacement_blocked=true, no child process owned by uid 10002 survives
```

### 11.5 Defects found by this pass and fixed

Backend / packaging:

1. **`heartbeat` returned a `datetime` in a `str` response field** → HTTP 500 on
   every heartbeat once a run session existed. The fake worker repo returned an
   ISO string, so unit tests could not catch it; the container run did. Fixed and
   covered by `test_heartbeat_serializes_session_heartbeat_timestamp`.
2. **The supervisor image could not build** — it installed the platform's whole
   `backend/requirements.txt` (psycopg2 has no wheel on slim images and needs
   `libpq-dev`). Fixed: install only the child SDK's runtime dependencies, and
   document that a DB driver is deliberately absent.
3. **The child could not import the SDK** in the image (copied to `/app`, which is
   not the child's working directory or `PYTHONPATH`) — every child died with
   `ModuleNotFoundError`. Fixed by installing the SDK into the image.
4. **`compose.supervisor.yml` base URL was missing `/api`** → every lifecycle call
   would 404. Fixed in the Compose environment.
5. **The child was handed the lifecycle base URL**, but the SDK appends
   `/api/algo-workers` itself (doubled prefix → child could not attach). Fixed by
   deriving a host-root child base URL, with `HOSTED_SUPERVISOR_CHILD_BASE_URL` as
   an explicit override, plus a regression test.

Frontend / contract:

6. **Queued attempts had no Stop affordance** in the UI even though the API
   supports stopping before launch. Fixed (Stop is offered for
   `queued|starting|running`), covered by a component test.
7. **Stop wording was wrong**: a queued job reported “Running; no stop requested”,
   and a stopped-but-never-launched job claimed “Stopped and process cleanup
   confirmed”. Both now state the truth (“Queued; …”, “Stopped before launch; no
   child process was ever started.”).
8. **“Load more” replaced the loaded log page** instead of appending it. Fixed with
   per-page queries, so output accumulates (verified 200 → 226 chunks).
9. **Raw machine codes surfaced to operators** (e.g. `STRATEGY_BLOCKED`). Added
   operator copy while keeping the code visible; unknown codes still show the
   server's blocking reasons.
10. **Run now defaulted to the oldest registered version** rather than the newest,
    so a newly registered version was not the one launched. Fixed.

### 11.6 Cleanup

All verification processes and artifacts were removed after the pass: the
frontend dev server, the harness API, the headless Chrome instance, the
verification container, its volume and image, and the disposable
`hosted_verify` database. Unrelated work (including the alerts/market-day
fixtures) was untouched; no production container, database or account was
modified.

### 11.7 Verdict

**Manual data-only release ready for deployment.** The data-only happy path —
register → configure → Run now → inspect → Stop → reconcile — was executed end to
end in a real browser against a real API on a disposable database, and the runner
image was built, started under a distinct child identity, and shut down by a real
`SIGTERM` with honest terminal state.

Remaining prerequisites are **deployment steps**, not product gaps (§7.3):
apply the migration to the real database, perform the full application rollout
with a rollback rehearsal, validate cross-host networking for
`HOSTED_SUPERVISOR_BASE_URL`, and authorize real-provider notification delivery
separately. The execution-settlement barrier remains required only for
trading-capable reconciliation, which stays blocked by design.

---

## 12. Deployment and live acceptance — manual data-only release (2026-09-15)

Deployed from branch `development` at `f6d1740` into the existing application
environment, then extended by the fixes listed in §12.5. **Secrets are not
recorded here** (the supervisor credential is a generated ≥32-byte value; only
its presence and a SHA-256 fingerprint prefix are known to the operator).

Status vocabulary used below: **deployed** (running in the environment),
**live verified** (observed against live data/providers), **locally verified**
(isolated tests), **not tested**, **deferred**.

### 12.1 What was deployed

| Item | Value |
| --- | --- |
| Branch / revision | `development`, deployed at `f6d1740` + forward-fix commits `a7ce3c3`, `3d348ab`, `3a48346`, `a60639c`, `3e79fb5` |
| Migration | `20260912_000018` → `20260915_000024` (the app ran `alembic upgrade head`) |
| Images rebuilt | `finance-app` (`kite-app`), `alerts-worker`, `frontend-next`, `strategy-runner` (new service); `market-runtime` unchanged (no source change since its image) |
| Services recreated | `finance-app`, `alerts-worker`, `frontend-next`, `strategy-runner` — no `compose down`, unrelated volumes preserved |
| New configuration (names only) | `HOSTED_SUPERVISOR_CREDENTIAL` (API + runner), `HOSTED_STRATEGY_ACCOUNT_SCOPES=kite:paper-a` |
| Runner credential surface | `HOSTED_SUPERVISOR_BASE_URL=http://finance-app:8777/api`; no `DB_*`/`DATABASE_URL`/`KITE_*`/`TELEGRAM_*`/`APP_JWT_*` in the runner service; child base URL derived (host root) |

**Deployed-code check:** SHA-256 of `backend/api/routers/hosted_lifecycle.py`,
`backend/api/routers/strategies.py`, `backend/api/services/hosted_lifecycle.py`,
`backend/strategies/supervisor.py` and the `000024` migration inside the running
container match the tree at the deployed revision; the runner image matches for
`supervisor.py` and `supervisor_api.py`.

### 12.2 Supervisor credential — verified behaviour

| Check | Result |
| --- | --- |
| Missing credential on the lifecycle API | 401, uniform body `{"detail":"Supervisor authentication required"}` |
| Wrong credential | 401 with a byte-identical body |
| Correct credential, discovery | 200 `{"jobs":[]}` then claimable work |
| Credential in the runner's environment | Present (required) — and nothing else: no DB, broker, Telegram or app-JWT keys |
| Credential in child environment / stored logs / browser responses | Absent (child env is a fixed allowlist; grep of runner logs = 0 matches; the API never returns it) |

### 12.3 Live data-only acceptance (executed)

Browser, production build of the deployed frontend; the strategy was created,
versioned, configured, run, stopped and reconciled **through the UI**.

| Step | Observed |
| --- | --- |
| Register immutable version | `v1`, capabilities shown as `data` only (`trade: false`, `notify: false`), SHA-256 pinned |
| Configure + Run now | Parameters validated server-side; toast “Queued. The process has not started yet.”; job row `Queued · paper · Blocked` — queued is not shown as started |
| Supervisor claim → prepare → spawn | Job reached `running`; `handoff_at` set; child alive inside `kite-strategy-runner` |
| Child identity | `docker top` shows supervisor as `root` and the child as **uid 10002**: `python -m kite_algo_worker.hosted /var/lib/kite-supervisor/source/<job>/<version>.py` |
| Source / scratch / records as the child | Source `r--r--r--` root-owned, readable but **not writable**; per-job scratch owned by 10002 and writable; `attempts/` and `logs/` **permission denied**; append to source refused |
| Progress + status in the browser | `last_progress_at` advanced from the child's own `ctx.progress`; job state visible |
| Logs | Labelled “Collected after the child terminated (live streaming is not implemented)” with the child's lines (including the harmless SDK read `ctx.client.get_run`) |
| Stop (running attempt) | UI showed `Stopping — Stop requested…`; the supervisor terminated the child, reported cleanup, released → terminal `recovery_required` with `stop.state=confirmed`, `process_cleanup_state=confirmed`; **no child process remained** |
| Stop (queued attempt) | Confirmed before launch, no process ever existed |
| Reconciliation (data-only) | `Ready to reconcile / data_only_completed / DATA_ONLY_COMPLETED`; the action cleared the replacement block; `reconciled_at` recorded |
| Idempotency | Same key + identical launch request → `idempotent: true`, the original job, job count unchanged; same key + different content → `409 IDEMPOTENCY_CONFLICT` |
| Second attempt | New key → new run/token, stop mid-run, reconcile cleanly |

Both attempts ended `stopped`; no attempt is active or replacement-blocked.

### 12.4 Order / position invariance

Counted before deployment and after cleanup (live database):

```
paper_orders 16 (pending 0) · paper_positions 6 rows / 4 open lots · paper_trades 16
live_order_intents 24 · order_projection_rows 22 · option_runs 4 · protection 0
```

**Unchanged.** `signal_events` 38 → 39 and `deliveries` 2 → 3 (the single MCX
test notification, §12.6) plus `channel_references` 1 → 2 (the operator channel
used for that test) are the only deltas, and all three are explained by the
authorized test. No order, position, paper fill or protection row was created,
modified or cancelled.

### 12.5 Defects found by deploying and running it, and fixed forward

Deploying to a live database and exercising the real paths exposed five defects
that the isolated suites could not: four are the same class (SQLite tolerates
what PostgreSQL does not, or tests disable `expire_on_commit` while the app does
not), one is a missing column write.

| Commit | Defect |
| --- | --- |
| `a7ce3c3` | `GET /api/alerts/capabilities` delegated to the worker handler, which requires a worker bearer token → an authenticated operator got 401 and the entire alert/screener authoring form rendered “Could not load capabilities”. Now uses the worker route's pure builder. |
| `3d348ab` | `func.max(EvaluationCheckpoint.state)` → `max(jsonb)` does not exist in PostgreSQL, so the workflow health route returned 500 for every workflow with subscriptions (SQLite hid it). Replaced with “newest checkpoint per subscription”. |
| `3d348ab` | `PgCandleHistory` required both `get()` and `snapshot()`, so the API's lazy catalog token map fell into the dict branch and raised `AttributeError: '_CatalogTokenMap' object has no attribute 'items'` — every manual screener run and preview 500'd. |
| `3a48346` | `ScreenerRunRepository.claim_run` returned a committed, expired ORM instance after `session.close()` → `DetachedInstanceError` on every manual screener run (tests pass `expire_on_commit=False`). |
| `a60639c` | `_set_state` (pause/resume) read `active.revision` after the session closed → 500 on Pause. |
| `3e79fb5` | Fired signal events were written with a NULL `workflow_id`, so an alert that fired and delivered showed “No signal events recorded” / “No deliveries recorded” in its own Events and Deliveries tabs. The one pre-fix event was backfilled from its occurrence key so the operator can audit the delivered notification. |

Each fix has a focused regression test that fails without it (three of them
drive production-shaped `expire_on_commit=True` sessions). Rebuilds were limited
to the affected services (`finance-app` for the API-only fixes; `finance-app` +
`alerts-worker` for the shared runtime/repository ones).

### 12.6 MCX alert acceptance — result

- **Instrument:** `MCX:SILVER10026SEPFUT` — canonical identity from the catalog,
  catalog generation `48d56789-9ee2-4ca6-af83-b536368d6fb1` (published
  2026-09-09), broker token `147154951` (kite/MCX), segment `MCX-FUT`,
  lifecycle `active`; `MCX:CRUDEOIL26DECFUT` was probed first and is far-month
  and thinly traded.
- **Attempt 1 (crossing, ₹8740 on CRUDEOIL DEC)** and **attempt 2 (crossing,
  ₹2305 on SILVER SEP)** ran in bounded 15-minute windows: **live evaluation
  observed, crossing not observed.** The worker evaluated continuously
  (`last_evaluated_at` within ~1 s of poll) and the runtime delivered live ticks
  (exchange timestamps advancing), but the price did not cross either level
  inside its window; the far-month crude contract did not move at all during its
  window.
- **Attempt 3 (level trigger, `ltp > 2200`, `notify_if_already_true: true`)**
  completed the delivery path with a genuine live tick: exchange event time
  `14:37:37Z` → event `8a529e4d-a37b-46c4-9cc6-e443ffafbd45` →
  one outbox delivery `f44410c7…` → **provider accepted** Telegram message id
  `6` at `14:37:44Z`, exactly **one** event and **one** notification (no
  duplicates). The workflow's Events tab shows the event (with its instrument
  binding: broker kite, public key, broker token, catalog generation) and the
  Deliveries tab shows `PROVIDER ACCEPTED`, attempt 1, provider id 6.
- **Freshness guard observed honestly:** the worker counts rejected ticks
  (`stale_tick`) and the subscription's `tick_age_s` grows without ticks; the UI
  reports `stale`/`stale_reason` rather than treating silence as success. In this
  environment the MCX feed sends intermittent snapshots for these contracts, and
  the guard correctly refused the frozen re-publishes (263 counted).
- **Screener check:** explicit MCX universe `mcx-acceptance-universe`
  (5 members, 0 rejected, generation `48d56789…`); a manual run executed
  end-to-end and the scheduler also claimed the day's due `session_close` bucket
  once. Coverage recorded `expected=5, evaluated=0, unavailable=5`,
  `candle_max_ts=null`, status **failed** — **no stored daily candles exist for
  MCX futures in this deployment**, so no member could be ranked. This is a data
  availability result, not a pipeline error. No attachment notifications were
  configured or sent.

### 12.7 Verdicts (independent)

- **Alerts/screeners live acceptance: PARTIAL.** The live UI → API → worker →
  event → outbox → provider-accepted path is proven end-to-end once (including
  the UI's event and delivery views), and the screener path runs with honest
  coverage reporting. Not proven: a *crossing* alert firing on natural price
  movement, and any screener producing ranked members (no MCX daily candles).
- **Hosted manual data-only deployment: PASS.** The full data-only journey ran
  through the deployed frontend with a distinct-identity child, honest queued/
  stop/cleanup/reconciliation states, working idempotency, and unchanged order
  and position state.
- **Hosted real-order readiness: NOT READY by design.** Trading-capable
  reconciliation stays blocked on `EXECUTION_QUIESCENCE_UNVERIFIED`; there is no
  Cancel/Flatten, no scheduling and no live-trading activation.

### 12.8 Not tested / deferred (unchanged by this deployment)

- Real-provider notification acceptance beyond the single authorized Telegram
  test message; ntfy delivery; delivery retry/backoff under failure.
- Scheduling beyond the single coalesced screener bucket; no long-running
  schedule observation.
- Currency (CDS/BCD) live validation; capacity campaign; restart fault injection.
- Rollback was **not** prepared or rehearsed by instruction (forward-fix only);
  the pre-migration `pg_dump` was taken as operational protection only.
- MCX daily-candle backfill (the reason a screener over MCX cannot rank yet).

---

## 13. Release cleanup, MCX candle screening and runner healthcheck (2026-09-15, second pass)

Deployed forward from `880bb9e` (branch `development`, pushed to
`origin/development` as a fast-forward after the working tree was repaired).
Services rebuilt: `finance-app`, `frontend-next`, `strategy-runner`.

### 13.1 Repository state

A foreign stash apply (`stash@{0}`, "opencode: stash local development before
sync", 2026-05-02) had left four unmerged index entries and a staged test file.
Resolved after investigating the index stages, the stash contents and each path's
deletion history: three files stay deleted (they were removed deliberately —
open-source prep and a leaked-file cleanup — and the stash's versions were
byte-identical resurrects), and `sdk/python/README.md` kept upstream's text plus
the one still-valid sentence the stash added. The same stash carried a revived
order-runtime schema test, which was kept after repairing the module paths it
patched (the file had 10 pre-existing failures for the same reason). Both foreign
stash entries were left intact. Local and remote `development` are now the same
commit (`880bb9e` at push time), and `origin/development` is an ancestor of local
(all later work is additive).

### 13.2 MCX candle-backed screening

**Diagnosis (live, not assumed):** the catalog resolved every member of the
acceptance universe to a current broker token and the broker returned 58–77 daily
bars per contract, while `historical_candles` held **zero** rows for all of them.
Nothing acquired daily history for a screener universe: the post-close finalizer
targets NSE indexes, and the worker history API only ingests when asked for one
symbol.

**Fix:** `backend/screeners/candle_warming.py` — one bounded, idempotent
operation, wired into both the scheduled and the manual run paths, plus two
operator endpoints (`GET /screeners/{id}/data-status`,
`POST /screeners/{id}/warm-candles`). Only the universe's own members are
considered, only those missing history are fetched, the fetch goes through the
existing authenticated adapter (chunked, 0.35s rate limiter, `ON CONFLICT`
upsert) over a bounded lookback, and every member reports its catalog-resolved
token, generation and lifecycle. Members that already hold enough final daily
candles are skipped, so restarts and repeats converge instead of refetching.

**Finality:** MCX and currency schedules are anchored to the NSE calendar, so a
bucket can fire while those exchanges are still trading. The pipeline drops a
daily bar whose session has not closed (+15 min) for feed-driven sessions and
records `forming_candles_excluded`. NSE equity is unchanged (its buckets fire at
its own close).

**Live evidence:** 5/5 MCX futures warmed in 2.8s; a second warm made zero broker
calls; the screener then ran `complete` with `expected=5, evaluated=5,
qualifying=5, unavailable=0` and a real ranking by `change_pct` (NATURALGAS 0.92
> CRUDEOIL 0.86 > ZINC -0.60 > SILVER -1.03 > COPPER -1.18). A mixed universe
(one contract with history, one recently listed) produced an honest **partial**
run: "Scanned 1 of 2 symbols; 1 could not be scored (missing or insufficient
data)". Tests: 21 unit/pipeline + 5 PostgreSQL (persistence, repeat no-op,
forming-bar handling, retryable failure, concurrent backfill converging to one
row set).

**One history window:** the API's manual-run scheduler defaulted `window_bars` to
30 while the worker's used 120, so the same definition was `partial` from the
schedule and `complete` from Run now. Both now take the pipeline's single default.

### 13.3 Strategy-runner healthcheck

The runner had no healthcheck at all, so Docker could not report its state. The
loop now publishes a nonsecret snapshot after every cycle (`state/health.json`,
dir 0700 / file 0600 so the child identity cannot read it) and
`python -m backend.strategies.supervisor_health` turns it into a verdict:
healthy, starting (inside the grace), loop_stalled, auth_rejected,
control_plane_unavailable, or `health_file_absent/invalid`. It makes no API call,
claims no job, renews no lease, mutates no lifecycle state, needs no database or
broker credential and never reads the supervisor credential. A transient outage
stays healthy (no restart thrashing); an authentication rejection stops being
tolerated as soon as the grace passes; a failed **child** is the job's outcome and
does not mark the supervisor dead.

**Live evidence:** Docker reports `healthy` with failing streak 0 (`healthy: ok`
once past the grace, `healthy: starting (grace 60s)` during it), the runner
container was recreated with the healthcheck wired through
`compose.supervisor.yml` (interval 30s, timeout 5s, retries 3, start_period 45s),
and uid 10002 gets "Permission denied" reading the snapshot. 20 focused tests.

### 13.4 Frontend creation defect and usability

**Root cause of "cannot create an alert" (reproduced in a real browser against
the deployed API):** the operator reaches the app over plain HTTP on a LAN
address, where `window.isSecureContext` is false and `crypto.randomUUID` does not
exist (`{'secure': false, 'hasRandomUUID': 'undefined'}`). Saving called it
directly, so the browser threw and the form showed
"Could not save — crypto.randomUUID is not a function". A second, independent
blocker appeared when that was fixed: the API's CSRF allowlist
(`APP_ALLOWED_ORIGINS`) did not contain the operator's LAN origin, so every
cookie-authenticated mutation was refused after Next forwarded the browser's
`Origin`. Both are fixed — ids come from one tested helper
(`crypto.randomUUID` → `getRandomValues`-built UUIDv4 → timestamp+counter, never
`Math.random()`), the create key is generated once per attempt and reused on
retry, and the deployment lists its origins.

**Redesign:** `/alerts/new` is now a single screen (instrument, condition, level,
destination, generated-but-editable name) with "Create and activate" primary and
"Save draft" secondary; the session is inferred from the instrument's exchange,
the clock is LTP for price rules, a timeframe appears only for percentage rules,
and the crossing/silence contract is stated where it matters. A create that
succeeds while activation fails is reported as "Saved as a draft — activation
failed" with the reason and a link, never as success. `/alerts/screeners/new` is
the same shape for screeners (scan / qualification / rank / schedule / optional
entry notification), with the session inferred from the universe's instruments.
The seven-step wizard and the screener step editor remain at `?mode=advanced`,
so every backend capability (groups, sequences, breadth, producers, arithmetic
operands, raw preview observations, custom messages) stays reachable and
unmodeled documents still route to the lossless YAML/JSON editor.

**Honesty fixes found in the same pass:** the lifecycle badge and action row
showed ACTIVE/Pause for a paused workflow (pause acts on subscriptions while the
revision stays active — the API now reports `lifecycle_state`); an unresolved
universe was reported as candle data "ready" (zero members is not readiness); the
Definition panel rendered instruments as "[object Object]" and the session as the
raw code; a failed run printed its reason twice.

**Verified in the deployed browser** (desktop 1600×1113 and narrow 390×844, no
horizontal overflow): quick create → activated, retry with the same key returns
the same workflow (workflow count grew by exactly one), edit, pause/resume,
archive, advanced editor reachable, screener warming → ready → run with coverage,
and a partial run with a data reason. Screenshots: `documents/verification/…`.
