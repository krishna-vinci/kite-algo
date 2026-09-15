# Hosted-strategy foundation — slice 0 report

**Worktree:** `/tmp/kite-hosted-strategy-foundation` · **Branch:** `codex/hosted-strategy-foundation`
(base `03c749f`) · **Scope:** schema + authorization foundation only. This report
was revised after coordinator review to add real enforcement (not just helpers)
and to state the remaining limits accurately.

This slice implements **only** Slice 0 of
`documents/hosted-strategies-implementation-plan.md` (design in
`documents/hosted-strategies-design.md`). It creates no execution path.

---

## 1. What this slice delivers

- Additive migration + `schema.sql` parity for four tables: `hosted_strategies`,
  `hosted_strategy_versions`, `hosted_strategy_schedules`, `strategy_jobs`.
  **Never** the frozen baseline migration.
- Immutable versions (`source`, `source_sha256`, `parameters_schema`,
  `capabilities_snapshot`), numbered transactionally.
- **Store-built immutable snapshots**: jobs/schedules pin `account_scope`,
  `params_snapshot`, `capabilities_snapshot`, `policy_snapshot` and the effective
  `max_duration_s`/`progress_deadline_s`, deep-copied, so a queued job is never
  reconstructed from later defaults. `job_kind` is separate from `execution_mode`.
- **Composite identity FKs** so a job/schedule can only reference a version that
  belongs to its strategy and an owner that matches the strategy owner.
- Store API (app-cookie): create / list / get / **PATCH metadata+disable** /
  create+read versions. Owner server-derived; owner-scoped reads; same-origin on
  unsafe methods.
- **Server-side account-scope authorization** via `HOSTED_STRATEGY_ACCOUNT_SCOPES`
  (default-deny), separate from owner identity and from `ALERTS_OPERATOR_SCOPES`.
- Repository-enforced fencing and a durable `recovery_required` state: the
  replacement block is enforced **inside `create_job`'s transaction**, not only
  in a helper.
- Child run-token composition validation that **cannot** include `heartbeat`.

## 2. What it deliberately does NOT do

No runner, child process, worker-run creation, worker-token minting, notification
delivery, orders, frontend, deployment, or live-DB migration. No change to
alerts/screener or options execution behavior, and no change to existing
production routes except the additive strategy router. No public/unauthenticated
lifecycle route was added.

## 3. Changed files

- `backend/alembic/versions/20260915_000019_hosted_strategy_foundation.py` (new)
- `backend/schema.sql` (additive parity DDL; baseline migration untouched)
- `backend/requirements.txt` (`jsonschema` declared explicitly)
- `backend/strategies/__init__.py`, `models.py`, `service.py`, `repository.py` (new)
- `backend/api/services/hosted_strategy_authz.py` (new — account allowlist)
- `backend/api/schemas/strategies.py`, `backend/api/routers/strategies.py` (new)
- `backend/api/routers/__init__.py` (register the router under `/api`)
- `tests/strategies/__init__.py`, `test_service.py`, `test_repository.py` (new)
- `tests/api/test_strategies_api.py` (new)
- `tests/integration/test_hosted_strategy_foundation_postgres.py` (new)
- `documents/hosted-strategies-foundation-report.md` (new)

## 4. Authorization design

- **Cookie auth only.** `require_strategy_owner` calls `require_app_user`; the
  router is under `/api` (not an `auth_exempt_path` prefix) so middleware gates
  it too. A worker bearer token does not open it (tested).
- **Owner is server-derived:** `app:<username>`. `owner_id` is not a request
  field; a body `owner_id` is rejected (`extra="forbid"`).
- **Account scope is authorized separately (403).** Shape/mode is validated first
  (malformed ⇒ 422); a well-shaped scope outside
  `HOSTED_STRATEGY_ACCOUNT_SCOPES` ⇒ **403 and no row written**. Unset ⇒
  default-deny. This is not owner identity and is deliberately not the alerts
  owner allowlist.
- **Cross-owner ids are 404**, never 403-with-existence-leak (tested).
- **Unsafe methods** (POST/PATCH) run `enforce_same_origin` from
  `backend.api.services.csrf`.

## 5. Fencing, snapshots and recovery

- Fencing matches **id + lease_owner + lease_epoch + attempt (+ state)**.
  `claim_job` advances `lease_epoch`, rejects a blank holder / non-future
  deadline, and only claims `queued` jobs (no reattach/resurrection of an expired
  starting/running lease).
- `mark_recovery_required` requires the exact authority and commits in its **own
  transaction**; `expire_to_recovery` is the trusted-reconciler path for an
  expired starting/running job (it does not pretend the worker is authorized).
- `create_job` locks the strategy row, rejects a **disabled** strategy, validates
  the **pinned account scope against the requested mode**, validates version/owner
  identity, then refuses while any job of that strategy is
  `queued`/`starting`/`running` or an unreconciled `recovery_required`.
  `claim_job` locks the **same parent row first**, so it serialises with
  disable/update: a queued job cannot be claimed after the strategy is disabled.
  Recovery/reconciliation use the same lock order.
- Disable is metadata only: it stops **new** attempts and claims; it does not
  stop an already-running job (there is no runner in this slice to stop it).
- PostgreSQL-verified: two racing version saves ⇒ versions {1,2}; two racing
  claims ⇒ one winner; after reconciliation, two racing `create_job` calls ⇒ one
  winner (the other is `StrategyFenceError`); composite FKs reject a mismatched
  version or owner; disable-then-claim is refused; a concurrent disable+claim
  completes without deadlock and ends disabled.

## 6. Checks executed

| Check | Result |
| --- | --- |
| `tests/strategies` + `tests/api/test_strategies_api.py` (SQLite) | **65 passed** |
| `tests/integration/test_hosted_strategy_foundation_postgres.py` (disposable temp DB; alembic up/down; constraints/FKs; version/claim/replacement concurrency; disable/claim serialisation) | **9 passed** |
| Combined with `tests/api/test_alerts_operator.py` (router registration) | 103 passed, 1 skipped |
| `alembic heads` | single head `20260915_000019`, down `20260912_000018` |
| `py_compile` of changed modules | ok |
| `git diff --check` | clean |

The PostgreSQL suite creates a uniquely named disposable database, runs
`alembic upgrade head`, asserts tables/constraints/FKs and real concurrency, then
runs `downgrade` and drops the database. It never touches existing data.

## 7. Limits and genuine blockers (no false claims)

- **No HTTP lifecycle fencing is implemented or claimed.** The internal lifecycle
  API and child-facing mutation routes **do not exist** in this slice, so fencing
  is enforced only at the repository boundary. The coordinator constraint that
  fencing cover those routes applies to the runner slice (H1).
- **No token minting** and therefore no one-time credential handoff; only
  composition validation exists. The "lost response ⇒ fail and reconcile, never
  a second credential" protocol is a documented **H1** requirement.
- **Schedules are stored but not exposed**; there is no schedule API/trigger.
  `session_close` is rejected (explicit clock time + weekday only).
- **Account policy is a coerce-to-string allowlist** (`HOSTED_STRATEGY_ACCOUNT_SCOPES`,
  exact trimmed match). There is no per-account resource/limit model.
- **`jsonschema`** was present only transitively; it is now declared in
  `backend/requirements.txt`. Remote `$ref`/`$dynamicRef`/`$id` and other
  resolution-changing constructs are rejected, and a no-network registry refuses
  retrieval; a self-recursive schema fails boundedly.
- **PostgreSQL suite must run in its own pytest invocation** (pre-existing repo
  fragility: a browser/API suite stubs `psycopg2`). It skips with a clear reason
  in combined runs.

## 8. Remaining H1 work (not started)

Supervisor lifecycle API + child-capability enforcement on real routes; run/token
creation with one-time handoff; child process spawn with restricted env, rlimits
and identity tracking; heartbeat via the lifecycle API; options fail-closed
adapter; futures contract resolver; scheduling loop; frontend.
