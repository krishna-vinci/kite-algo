# Recurring hosted portfolios deployment — 2026-09-24

Released revision: `8f9438c` (`feat(strategies): continue recurring paper
portfolios`) on `development`, pushed to the authorized origin. The release
commits the accepted Phase A recurring CNC portfolio work, the authenticated UI
evidence, and the final combined momentum acceptance artifact.

## Rollout

The four application images were built before any container was replaced:

| Service | Image ID | Container started (UTC) |
| --- | --- | --- |
| `finance-app` | `1790a7b436bf` | 2026-09-24 11:58:18 |
| `alerts-worker` | `67c76b1f9134` | 2026-09-24 11:59:08 |
| `strategy-runner` | `6b2cf3613f16` | 2026-09-24 11:59:08 |
| `frontend-next` | `588be3ac08a5` | 2026-09-24 11:59:08 |

The API was recreated first. Its entrypoint applied the additive migration
`20260923_000042 -> 20260924_000043`, logged `Application startup complete`,
and became healthy before the worker, runner, and frontend were recreated.
PostgreSQL, Redis, market-runtime, and their volumes were not recreated.

## Verification

- Production migration head: `20260924_000043`.
- `strategy_jobs.completion_state` and `completion_at` are present.
- `strategy_job_reconciliations.outcome` admits `continuation` in addition to
  the existing values.
- SHA-256 hashes inside `finance-app` match the worktree for the new
  continuation/financing modules, changed admission/execution/reservation code,
  lifecycle service, repository, and migration.
- The compiled frontend bundle contains the new wording “Continued automatically
  (book held, not flat)”.
- All four recreated application containers report healthy.
- Unauthenticated API `/api/strategies` returns `401`; published frontend
  `/strategies` returns `307` to authentication.
- Startup logs show the API migration and normal startup; the runner is cycling
  normally; the frontend is ready.

Focused release verification completed before rollout: 288 backend tests plus 14
subtests passed for the changed strategy suites, all 83 frontend component tests
passed, TypeScript compilation passed, `git diff --check` passed, and Alembic
reported exactly one head. The implementation report also records the worker's
866-test strategy sweep, 52 PostgreSQL tests, and supervised momentum scenarios
against disposable PostgreSQL.

## Production state and boundaries

Deployment created no hosted work:

- `strategy_jobs`: one pre-existing `recovery_required` row and three stopped
  rows, unchanged by the rollout;
- enabled hosted schedules: 0;
- governed execution requests: 0;
- continuation audit rows: 0.

No real broker order was placed, no notification was sent, and the hosted live
account allowlist was not widened. Staged live financing remains explicitly
unsupported and refuses by `STAGED_LIVE_FINANCING_UNSUPPORTED`. This deployment
makes recurring **paper CNC portfolios** operational; it is not live-market or
real-order certification. Phase B options continuity also remains open.
