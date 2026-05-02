# Journal V2 Production Validation Report

Generated at: 2026-05-01T12:35:00+00:00

## Status

- production_ready: no
- schema_check: passed
- api_check: passed
- paper_drill: passed
- live_read_only: failed_no_existing_v2_live_environment
- live_tiny_drill: skipped_by_user
- frontend_typecheck: passed

Journal V2 now has the production-validation tests and runner needed for the final gate. Docker dev Postgres was available through `.env`, so a dedicated validation database (`kite_algo_validation`) was created and used for schema/API/paper/frontend validation. Live broker runtime is connected, but the main application DB has no existing Journal V2 live environment rows yet, so live Journal V2 read-only validation cannot pass until a V2-attributed live run/fill exists.

## Implemented validation assets

- Added `tests/journaling/test_v2_db_integration.py` for real Postgres validation:
  - schema applies twice
  - live/paper same external run ID stays isolated
  - replayed V2 fill does not mutate episode state twice
  - V1 fact replay preserves existing V2 fields
  - concurrent note updates serialize note revisions
- Hardened `tests/test_journal_v2_router.py` with additional V2 route scoping checks:
  - ID-only episode/timeline/note reads require `environment_id`
  - wrong environment returns `400`/`404`
  - strategies/unresolved resolve `mode + account_scope`
  - analytics endpoints require explicit `environment_id`
  - paper/live comparison keeps `combined: null`
- Added `scripts/validate_journal_v2_production.py` with modes:
  - `--schema-check`
  - `--api-check`
  - `--paper-drill`
  - `--live-read-only`
  - `--live-tiny-drill`
  - `--frontend-typecheck`
- Resolved frontend typecheck blocker by adding route shells for:
  - `/alerts`
  - `/algos`
  - `/charts`
  - `/custom-display`
  - `/quick-trade`
  - `/screeners`

## Safety decisions

- `--live-tiny-drill` refuses to run unless `--i-understand-this-places-live-orders` is supplied.
- Automated tiny live order placement is intentionally not implemented in the safety runner; it must remain an explicit operator-approved/manual drill.
- Write validations refuse to use `DATABASE_URL` unless explicitly opted in with `--use-database-url-for-validation` or `JOURNAL_V2_ALLOW_DATABASE_URL_WRITES=1`.
- `--live-read-only` uses read paths only and does not recompute/persist metric snapshots.
- `--api-check` no longer creates synthetic live environments; it only uses existing live environments if available.

## Commands run

```text
PYTHONPATH=. uv run pytest tests/journaling/test_v2_db_integration.py -q
PYTHONPATH=. uv run pytest tests/test_journal_v2_router.py -q
PYTHONPATH=. uv run pytest tests/test_journal_v2_router.py tests/journaling/test_v2_db_integration.py -q
PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py --live-tiny-drill --json
TEST_DATABASE_URL=postgresql://<masked>@127.0.0.1:15432/kite_algo_validation DATABASE_URL=postgresql://<masked>@127.0.0.1:15432/kite_algo_validation PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py --schema-check --api-check --paper-drill --frontend-typecheck --paper-account-scope kite:paper-journal-v2-validation --json
TEST_DATABASE_URL=postgresql://<masked>@127.0.0.1:15432/kite_algo_validation DATABASE_URL=postgresql://<masked>@127.0.0.1:15432/kite_algo_validation PYTHONPATH=. uv run pytest tests/journaling/test_v2_db_integration.py -q
DATABASE_URL=postgresql://<masked>@127.0.0.1:15432/postgres PYTHONPATH=. uv run python scripts/validate_journal_v2_production.py --live-read-only --account-scope kite:XJJ446 --json
cd frontend-next && npm run typecheck
```

## Results

```text
tests/test_journal_v2_router.py tests/journaling/test_v2_db_integration.py without TEST_DATABASE_URL:
19 passed, 5 skipped, 1 warning

tests/journaling/test_v2_db_integration.py against Docker validation DB:
5 passed, 1 warning

schema/API/paper/frontend runner against Docker validation DB:
schema_check: passed
api_check: passed
paper_drill: passed
frontend_typecheck: passed

frontend-next typecheck:
passed

live broker runtime read-only status:
broker connected: true
daily token gate ready: true
websocket status: CONNECTED

live-read-only Journal V2 runner against main Docker DB:
failed because there is no existing live Journal V2 environment for account_scope=kite:XJJ446

live tiny drill:
not run; explicit user approval still required before any live order drill
```

## Account scopes

```json
{
  "paper_drill_target": "kite:paper-journal-v2-validation",
  "live_read_only_target": "kite:XJJ446"
}
```

## Environment IDs

```json
{
  "validation_db_paper_environment": "58d79b5f-9aa6-45e0-8f6a-c444b5984184"
}
```

## Remaining live gate

The validation DB gate has passed. The remaining live Journal V2 gate requires creating a V2-attributed live environment/fill. The backend broker runtime is already connected, but no `journal_execution_environments` live rows exist in the main DB yet.

To create the missing live V2 environment and prove projection end-to-end, optionally ask for explicit live-trade approval:

```text
I approve a tiny live Journal V2 validation trade with the smallest safe quantity.
```

## Final gate

Journal V2 can be marked `production_ready: yes` only after:

1. real Postgres schema/API/paper drills pass — **passed on Docker validation DB**,
2. live read-only drill passes for the real broker account scope — **blocked until a V2 live environment exists**,
3. frontend typecheck remains passing — **passed**,
4. optional tiny live drill is either explicitly skipped by user or run successfully with approval.
