# Journal V2 Production Validation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove Journal V2 is production-ready with migration checks, integration tests, paper end-to-end validation, live read-only validation, and optional explicitly approved tiny live trade validation.

**Architecture:** Keep Journal V2 code as-is unless validation exposes defects. Add repeatable validation scripts and tests that exercise real Postgres behavior, environment scoping, projection idempotency, backfill/recompute scripts, API contracts, and frontend V2-only surfaces. Live broker validation is staged: read-only first, then optional tiny live drill only after explicit user confirmation.

**Tech Stack:** FastAPI, SQLAlchemy text queries, Postgres, pytest, Next/Vitest/TypeScript, existing Kite session/runtime services, existing paper runtime and algo-worker APIs.

---

## Production-ready definition

Journal V2 is **not 100% production-ready** until all gates below pass:

1. Schema/migration applies cleanly to a real Postgres database twice, proving idempotency.
2. Real DB integration tests pass for environment isolation, projection idempotency, note revision durability, metric snapshots, unresolved queue, and V1/V2 replay preservation.
3. Paper end-to-end drill creates a full V2 trail: environment → context → episode → intent → fact → timeline → metrics → API/frontend read.
4. Live read-only drill confirms live environment/account discovery, auth, API boundaries, and no mixed paper/live analytics.
5. Optional tiny live drill, only with explicit user approval, creates and exits a minimal trade and confirms V2 projection correctness without corrupting V1 paths.
6. Frontend typecheck either passes or the unrelated missing-page `.next/types` issue is cleaned before release.
7. All validation results are written into `docs/journal-v2-production-validation-report.md` and `documents/kite-backend-progress.md`.

## Execution status — 2026-05-01

- Validation infrastructure has been implemented:
  - `tests/journaling/test_v2_db_integration.py`
  - expanded `tests/test_journal_v2_router.py`
  - `scripts/validate_journal_v2_production.py`
  - `docs/journal-v2-production-validation-report.md`
- Local verification completed:
  - `PYTHONPATH=. uv run pytest tests/test_journal_v2_router.py tests/journaling/test_v2_db_integration.py -q` → `19 passed, 5 skipped, 1 warning`
  - Docker validation DB `kite_algo_validation`: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_db_integration.py -q` → `5 passed, 1 warning`
  - Docker validation DB runner: `--schema-check --api-check --paper-drill --frontend-typecheck` → passed
  - `cd frontend-next && npm run typecheck` → passed
- Live broker runtime read-only status is connected, daily token gate ready, and websocket `CONNECTED`.
- Live Journal V2 read-only validation is blocked because the main DB has no existing live Journal V2 environment rows for `kite:XJJ446`; a V2-attributed live run/fill is needed to prove the live projection path.
- Optional tiny live drill remains skipped and still requires explicit user approval immediately before any live order action.

## Safety rules

- Do not place live orders without explicit user confirmation immediately before the live drill.
- Live drill quantity must be the smallest safe quantity for the selected instrument and product.
- Prefer read-only live checks first.
- Never combine paper/live metrics in assertions.
- Use a fresh paper account epoch or dedicated paper account scope for validation.
- Keep validation logs free of access tokens or secrets.

## File map

### Create

- `tests/journaling/test_v2_db_integration.py` — real DB integration tests for Journal V2 schema/repository/service behavior.
- `scripts/validate_journal_v2_production.py` — operator validation runner for schema, API, paper, live-read-only, optional-live-trade checks.
- `docs/journal-v2-production-validation-report.md` — generated/manual validation report.

### Modify if failures require fixes

- `journaling/repository.py`
- `journaling/service.py`
- `journaling/live_projector.py`
- `paper_runtime/service.py`
- `api/routers/journal.py`
- `frontend-next/app/(app)/journal/**`
- `frontend-next/lib/journal/**`
- `schema.sql`
- `documents/kite-backend-progress.md`

---

## Batch 1 — Real DB migration and repository validation

### Task 1: Add DB-backed Journal V2 integration tests

**Files:**
- Create: `tests/journaling/test_v2_db_integration.py`
- Modify only if failing: `schema.sql`, `journaling/repository.py`, `journaling/service.py`

- [ ] Add tests that apply `schema.sql` to a real test Postgres database twice.

Run:

```bash
PYTHONPATH=. uv run pytest tests/journaling/test_v2_db_integration.py -q
```

Expected:

```text
all tests pass; second schema apply has no DDL failure
```

- [ ] Add DB test: live and paper environments with same external run id create different contexts and episodes.

Expected assertion:

```python
assert live_environment_id != paper_environment_id
assert live_context_id != paper_context_id
assert live_episode_id != paper_episode_id
```

- [ ] Add DB test: replaying the same V2 fill source key does not mutate `net_quantity_by_instrument` twice.

Expected assertion:

```python
assert first["episode_id"] == replay["episode_id"]
assert replay["duplicate"] is True
assert episode.metadata["net_quantity_by_instrument"] == {"111:MIS": 1}
```

- [ ] Add DB test: V1 fact replay does not erase existing V2 fields.

Expected assertion:

```python
assert fact.environment_id == original_environment_id
assert fact.episode_id == original_episode_id
assert fact.intent_id == original_intent_id
```

- [ ] Add DB test: concurrent note updates serialize revisions.

Expected assertion:

```python
assert [rev.revision_no for rev in revisions] == [1, 2]
```

## Batch 2 — Backend API and script validation

### Task 2: Validate V2 APIs against real app/test database

**Files:**
- Modify: `tests/test_journal_v2_router.py`
- Modify only if failing: `api/routers/journal.py`, `journaling/service.py`

- [ ] Add integration-style router tests for all ID-only reads requiring `environment_id`.

Expected:

```text
missing environment_id returns 422
wrong environment_id returns 400 or 404
correct environment_id returns 200
```

- [ ] Add tests for analytics summary/strategies requiring explicit `environment_id`.

Expected:

```text
no summary/strategy analytics endpoint returns mixed live+paper totals
```

- [ ] Add paper-vs-live comparison test with two paper environments and one live environment.

Expected assertion:

```python
assert body["paper_environment_id"] == selected_paper_environment_id
assert body["live_environment_id"] == selected_live_environment_id
assert body["combined"] is None
```

### Task 3: Add production validation runner

**Files:**
- Create: `scripts/validate_journal_v2_production.py`
- Create/update: `docs/journal-v2-production-validation-report.md`

- [ ] Implement CLI modes:

```text
--schema-check
--api-check
--paper-drill
--live-read-only
--live-tiny-drill
--account-scope VALUE
--paper-account-scope VALUE
--template-id VALUE
--json
```

- [ ] Make `--live-tiny-drill` require `--i-understand-this-places-live-orders`.

Expected:

```text
without confirmation flag, live tiny drill exits with non-zero status and places no order
```

- [ ] Report counts:

```json
{
  "schema_check": "passed",
  "api_check": "passed",
  "paper_drill": "passed",
  "live_read_only": "passed",
  "live_tiny_drill": "not_run",
  "failures": []
}
```

## Batch 3 — Paper and live validation drills

### Task 4: Paper end-to-end validation

**Files:**
- Use: `scripts/validate_journal_v2_production.py`
- Modify only if failing: `paper_runtime/service.py`, `journaling/service.py`, `api/routers/journal.py`

- [ ] Run paper drill with dedicated paper account scope.

Run:

```bash
PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py \
  --paper-drill \
  --paper-account-scope kite:paper-journal-v2-validation \
  --json
```

Expected:

```text
one paper entry and exit are recorded, one closed V2 episode exists, metrics show one closed episode, notes/timeline APIs return environment-scoped data
```

### Task 5: Live read-only validation

**Files:**
- Use: `scripts/validate_journal_v2_production.py`
- Modify only if failing: auth/session/journal routing files

- [ ] Run live read-only check.

Run:

```bash
PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py \
  --live-read-only \
  --account-scope kite:<LIVE_BROKER_USER_ID> \
  --json
```

Expected:

```text
live environment can be resolved, journal V2 live reads require explicit environment, no paper data appears in live analytics
```

### Task 6: Optional tiny live trade validation

**Files:**
- Use: `scripts/validate_journal_v2_production.py`
- Modify only if failing: `journaling/live_projector.py`, `journaling/service.py`, order projection paths

- [ ] Ask user for explicit confirmation before running this task.

Required confirmation text:

```text
I approve a tiny live Journal V2 validation trade with the smallest safe quantity.
```

- [ ] Run tiny live drill only after confirmation.

Run:

```bash
PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py \
  --live-tiny-drill \
  --i-understand-this-places-live-orders \
  --account-scope kite:<LIVE_BROKER_USER_ID> \
  --json
```

Expected:

```text
entry and exit/projected fill facts land in V2 live environment only; one closed live episode exists; paper environment remains unchanged
```

## Batch 4 — Frontend production gate and handoff

### Task 7: Resolve frontend typecheck blocker or isolate it from Journal V2 release gate

**Files:**
- Inspect: `frontend-next/.next/types/validator.ts`
- Likely modify/create missing page shells if those routes are intended:
  - `frontend-next/app/(app)/alerts/page.tsx`
  - `frontend-next/app/(app)/algos/page.tsx`
  - `frontend-next/app/(app)/charts/page.tsx`
  - `frontend-next/app/(app)/custom-display/page.tsx`
  - `frontend-next/app/(app)/quick-trade/page.tsx`
  - `frontend-next/app/(app)/screeners/page.tsx`

- [ ] Run frontend typecheck.

Run:

```bash
cd frontend-next && npm run typecheck
```

Expected:

```text
typecheck passes, or validation report explicitly records this as unrelated and accepted by user
```

### Task 8: Final production validation report

**Files:**
- Update: `docs/journal-v2-production-validation-report.md`
- Update: `documents/kite-backend-progress.md`

- [ ] Record exact commands, environment IDs, account scopes, and results.
- [ ] Record whether optional tiny live drill was run or skipped.
- [ ] Record all failures and fixes.
- [ ] Mark Journal V2 production-ready only if all required gates pass.

Final report status values:

```text
production_ready: yes | no
live_tiny_drill: passed | skipped_by_user | failed
frontend_typecheck: passed | failed_unrelated_accepted | failed_blocking
```
