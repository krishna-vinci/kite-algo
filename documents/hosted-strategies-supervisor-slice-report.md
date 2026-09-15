# Hosted-strategy supervisor lifecycle — slice report (H1a)

**Base:** `c666343` (hosted-strategy schema + authorization foundation).
**Closed on:** the reviewed lifecycle-closure fixes applied on top of `f58333b`
(see §1.6). This revision corrects the earlier report's baseline evidence (§3).
**Scope:** supervisor lifecycle API, worker-run/session integration, child
credential handoff, hosted-attempt enforcement, SDK attach-only.

This slice implements **preparation only**. It does **not** spawn or execute
uploaded strategy code, does not implement the runner container/process, sends
no orders or live notifications, and does not touch alerts/screeners, options
execution semantics, the Go runtime, MCP or the frontend. No migration was run
against a live database; the new migration is additive and exercised only on a
disposable database.

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
| POST | `/api/hosted-supervisor/jobs/{job_id}/claim` | CAS-claim a `queued` job |
| GET | `/api/hosted-supervisor/jobs/{job_id}` | Authoritative job state |
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
SDK version bumped `0.10.0 → 0.11.0` (additive). No new HTTP endpoint, so the
endpoint manifest is unchanged.

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
→ 529 passed, 1 skipped
```

Disposable PostgreSQL (real concurrency; own invocation):

```
HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q
→ 5 passed        # concurrent prepare ⇒ one credential/run; reservation CAS one winner;
                  # columns present; launched release blocks replacement; racing recover one fence

HOSTED_FOUNDATION_PG_URL='...' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_strategy_foundation_postgres.py -q
→ 9 passed        # chain still applies
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

**Unrelated pre-existing failures — corrected baseline evidence.** The earlier
report compared only `backend/api/routers/__init__.py`, which is *not* a complete
baseline for this commit (it also changes shared services and worker routes).
The corrected comparison stashes **all** tracked changes of this slice
(`backend`, `sdk`, `tests`) and re-runs:

| Scope | Baseline (slice stashed) | Current (slice applied) |
| --- | --- | --- |
| `tests/api tests/options` | 46 failed, 507 passed | 46 failed, 515 passed |

The failure **set and count are identical** (the 8 added passes are this slice's
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

## 5. Remaining gaps before process spawning

- The supervisor **process/container** and its restricted child environment,
  rlimits, identity tracking (`boot_id`/`container_id`/`pid`/`pgid`/start-time),
  log capture and cleanup are **not implemented** here.
- No progress ingestion endpoint exists yet (`ManagedRun.progress` remains
  PROPOSED); `last_progress_at` is stored but nothing writes it, so a
  progress-deadline fence is not yet wired.
- `prepare` returns the child config to the supervisor but nothing consumes it
  yet; the child SDK attach path is available but a hosted child is never
  launched in this slice.
- **Capability declaration is now required for trading.** A version saved without
  explicit `capabilities` is data-only, and any pre-existing marker-only snapshot
  fails closed at `prepare`. Existing foundation-era versions must be re-saved
  with explicit capabilities before they can trade.
- Options fail-closed mode propagation and the futures contract resolver remain
  out of scope (later H1/H2 work). The options boundary above binds hosted
  tokens to their worker run but does not add live options execution.
- Operator Cancel/Flatten adapters after child-token revocation remain to be
  built (an app-authorized backend adapter, not the revoked child credential).
  Reconciliation after `release`/`fence`/`recover` still has no operator API.
- The `handoff_at`-based protocol intentionally does not distinguish a lost
  response from a benign duplicate; both return 409 without re-issuing. The
  supervisor must call `fence` (live lease) or `recover` (expired lease) when it
  did not receive a credential.

### Handoff to the process-supervision slice

Implement `backend/strategies/supervisor.py` (or a runner entrypoint) that:
claims due jobs via `POST .../claim`; calls `prepare` exactly once and treats a
409 as "do not replay — `fence` a live lease / `recover` an expired one"; writes
the returned config into the child's restricted environment (never persisting
the token to logs); spawns one child with `start_new_session=True` and rlimits;
records identity; heartbeats via the lifecycle API on a schedule; and on lease
loss calls `recover` (expired) or `fence` (live), or on a stale progress deadline
calls `fence`, then requires operator reconciliation. Declare `trade`/`notify`
capabilities when saving versions. Add a child progress endpoint (and
`ManagedRun.progress`) so `last_progress_at` is written by the child, never by
the parent heartbeat.
