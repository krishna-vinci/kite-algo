# Hosted-strategy supervisor lifecycle — slice report (H1a)

**Base:** `c666343` (hosted-strategy schema + authorization foundation)
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
| POST | `/api/hosted-supervisor/jobs/{job_id}/release` | Runner-owned stop (release session, revoke child, mark stopped) |
| POST | `/api/hosted-supervisor/jobs/{job_id}/fence` | Fence to `recovery_required` + revoke child |

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
- The child token is minted with `runs:read`, `runs:log` and the paper order
  actions and **never** `heartbeat`. Hosted v1 is paper/dry_run by
  construction (job CHECK).
- **External workers are unchanged:** the guard is a no-op for any non-`hosted:`
  template; a regression test pins external claim-session behavior.

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
→ 505 passed, 1 skipped
```

Disposable PostgreSQL (real concurrency; own invocation):

```
HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q
→ 3 passed        # concurrent prepare ⇒ one credential/run; reservation CAS one winner; columns present

HOSTED_FOUNDATION_PG_URL='...' \
  .venv/bin/python -m pytest \
    tests/integration/test_hosted_strategy_foundation_postgres.py -q
→ 9 passed        # chain still applies with 000020
```

`alembic heads` → single head `20260915_000020`. `git diff --check` clean.

**New tests:** supervisor auth (default-deny, wrong/absent credential, rotation);
lifecycle state machine (happy path, repeat, partial, wrong owner/epoch/attempt,
expired lease, failures between token/run/session steps, heartbeat ≠ progress,
release, fence); HTTP boundary (auth before job detail, claim/state, unrelated
authority 403, one-time handoff, repeat fails closed); child authority (session
lifecycle forbidden, fenced/expired/mismatched mutations, external compat);
SDK attach-only; PostgreSQL concurrency.

**Unrelated pre-existing failures observed** (not caused by this slice, and not
touched by it): `tests/api/test_auth_*`, `test_control_plane_api`,
`test_public_runtime_config` fail on `ModuleNotFoundError:
backend.app.runtime_public_config`; a subset of `tests/options` fails on
`OptionRunCreateRequest` model drift. Both reproduce with the slice's
`backend/api/routers/__init__.py` reverted.

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
- **Heartbeat is not progress and not health.** `heartbeat` renews the job lease
  and records the worker-session heartbeat; it never writes `last_progress_at`.
  Runner liveness and child progress are separate signals.
- **Stop/revoke ≠ cancel/flatten.** `release` stops launching, releases the
  session, revokes the child and marks the job `stopped`; it does not claim
  cancellation or flatness, and any exposure remains to be reconciled.

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
- Options fail-closed mode propagation and the futures contract resolver remain
  out of scope (later H1/H2 work).
- Operator Cancel/Flatten adapters after child-token revocation remain to be
  built (an app-authorized backend adapter, not the revoked child credential).
- The `handoff_at`-based protocol intentionally does not distinguish a lost
  response from a benign duplicate; both return 409 without re-issuing. The
  supervisor must call `fence`/`release` when it did not receive a credential.

### Handoff to the process-supervision slice

Implement `backend/strategies/supervisor.py` (or a runner entrypoint) that:
claims due jobs via `POST .../claim`; calls `prepare` exactly once and treats a
409 as "do not replay — fence/release"; writes the returned config into the
child's restricted environment (never persisting the token to logs); spawns one
child with `start_new_session=True` and rlimits; records identity; heartbeats via
the lifecycle API on a schedule; and on lease loss or a stale progress deadline
calls `fence` and requires operator reconciliation. Add a child progress
endpoint (and `ManagedRun.progress`) so `last_progress_at` is written by the
child, never by the parent heartbeat.
