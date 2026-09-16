# Alerts platform — Phase 3 parity matrix (screeners and attachments)

Status as of 2026-09-11, branch `development`. Phase 3 scope per
[alerts-platform-spec-v2 §9](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md):
F9 (scheduled screeners, result snapshots, attachments), E-17/E-18/E-19.

Publication hardening (2026-09-11): attachment evaluation no longer writes
anything before the run's fenced finalization, and — after a second pass —
no longer precomputes transitions either. The scheduler hands
`finalize_run` an `AttachmentPlan` carrying only the ranked results and the
attachment specs (NO baseline state); the per-attachment lock is taken first,
the baseline is read under it, and the entry/exit/top-N/rank-delta decisions
are derived from that locked baseline inside the same transaction. See
"Publication transaction boundary" below.

## Requirement parity

| Requirement | Status | Where | Evidence / notes |
| --- | --- | --- | --- |
| Screener block in canonical schema (reserved since Phase 1) | Closed | `models.py` `ScreenerSpec`/`ScheduleSpec`/`RankSpec`/`AttachmentSpec`; `parser.py` `_screener_spec`; `compiler.py` `_validate_screener` | Round-trip: parse → document dict → parse preserves the canonical hash (`test_screener_schema.py`) |
| Scheduled scans over STORED data (price/change/volume/liquidity) | Closed | `backend/screeners/runner.py` | Latest completed daily candle + `change_pct` + `turnover`; `screener_only` fields rejected in alert documents (no live path exists) |
| Fundamental filters from a real production source | Closed | `fundamentals_context.FundamentalsLoader` → `public.fundamentals_features` (Screener.in nightly sync); screener pipeline injects per-member context with acquisition metadata | Unit: `test_fundamentals_context.py`; runner tests; §5.8 honesty: only the latest snapshot is read, never presented as history |
| Completed-candle technical filters | Closed | runner: same `evaluate_stage`/feature engine semantics as alerts (3VL, layered chains, `stage:` refs) | `tests/screeners/test_runner.py` |
| Deterministic ranking; stable tie-break | Closed | `runner._rank`: sort by (directional score, `EXCHANGE:SYMBOL` asc); null scores never rank; top-N cut keeps the rank with `beyond_top_n` | `test_rank_desc_ties_break_by_instrument_identity`, `test_rank_asc_reverses_scores_but_not_ties`, `test_top_n_boundary_is_exact_and_deterministic` |
| Run persistence: revision identity, coverage, status, values, ranks, freshness | Closed | migration `20260910_000015` (`screener_run`, `screener_run_member`); `ScreenerRunRepository.finalize_run` writes status + members + freshness + attachment side effects in ONE fenced transaction | PG: `test_finalize_is_atomic_members_and_status_together`, `test_mid_publication_failure_rolls_back_entire_transaction`; schema mirrored in `backend/schema.sql` |
| Precise coverage/completeness; missing data ≠ negative match | Closed | runner coverage dict (`expected/evaluated/unavailable/unknown_conditions/rank_value_missing/qualifying/complete`); typed exclusion reasons | `test_missing_data_is_unavailable_not_a_failed_match`, `test_condition_failure_is_a_valid_negative_not_unavailable` |
| Coherent data cutoff, no future candles | Closed | runner filters every member's bars to `ts <= as_of` | `test_as_of_cutoff_never_consumes_future_candles` |
| Exchange-calendar scheduling; MCX/currency honesty | Closed | `compute_screener_bucket` (IST buckets, session-gated); parser/compiler reject any calendar except `nse_equity` with an actionable message (feed-driven eligibility is not a calendar) | `test_bucket_walks_back_over_inactive_sessions`, `test_mcx_schedule_calendar_rejected_with_actionable_error` |
| Durable occurrence ownership + idempotency (E-2) | Closed | unique `occurrence_key` = `workflow_id:bucket_epoch`; `claim_run` INSERT-or-CAS; 300 s leases | PG: `test_concurrent_claims_produce_exactly_one_logical_run` (6 concurrent claimants → 1 winner, 1 row) |
| Stale owners cannot finalize | Closed | `finalize_run` compare-and-swap on `lease_owner` + `status='running'` is the FIRST statement of the publication transaction; rowcount 0 aborts members, baselines, events and outbox together | PG: `test_takeover_during_evaluation_fences_stale_worker`, `test_attachment_events_idempotent_and_fenced_after_takeover` |
| Restart/crash recovery | Closed | crashed claim is taken over after lease expiry; the SAME logical run completes; replays are idempotent | PG: `test_crash_after_claim_recovers_via_lease_takeover` |
| Missed schedules coalesce (E-19) | Closed | scheduler evaluates only the LATEST due bucket; older buckets never replay; session gate walk-back is bounded (`max_walkback=40`) | `test_bucket_gate_never_active_returns_none`; scheduler double-pass test (second pass → 0 runs) |
| Attachments: entry/exit/top-N/rank-delta with explicit baseline | Closed | `scheduler.py::_build_attachment_plan` (baseline-free plan) → `ScreenerRunRepository.finalize_run(attachment_plan=...)`, which locks, reads the baseline and derives the transitions; state persisted per (owner, workflow, revision, attachment, instrument) | baseline-silent, `initial_match`, `exit_after`, rank-band hysteresis, rank-delta tests (SQLite) + PG idempotency and atomicity |
| Overlapping occurrences cannot invert baseline state | Closed | per-attachment PostgreSQL advisory transaction lock (`pg_advisory_xact_lock(hashtext('screener-attachment:' \|\| owner/workflow/revision/attachment))`) taken BEFORE the baseline read, with the transitions derived from that locked baseline — so a run can never publish transitions computed against a snapshot an earlier publication has superseded. Covers the empty-baseline first run, which has no rows for a row lock. The `scheduled_for` chronology gate remains as a second guard: a strictly superseded occurrence is suppressed whole (`attachment_events_stale_suppressed`) | PG: `test_two_runs_planned_on_empty_baseline_publish_one_entry_each`, `test_exit_after_counter_advances_from_locked_baseline`, `test_rank_delta_compares_against_locked_baseline`, `test_newer_occurrence_publishing_first_suppresses_stale_older_run`, `test_older_occurrence_publishing_first_applies_over_locked_baseline`, `test_concurrent_occurrences_never_interleave_baseline_state`, `test_concurrent_first_runs_never_invert_empty_baseline` |
| Precomputed-transition race (regression) | Closed | Before this fix a run planned its transitions from a baseline snapshot read outside the transaction: two runs planning on an empty baseline both emitted an entry for the same instrument. Verified failing at `3728f06` (`NSE:A` entered twice), passing after | PG: `test_two_runs_planned_on_empty_baseline_publish_one_entry_each` |
| Lease-expiry behavior is explicit | Closed | the fence is OWNERSHIP, not wall time: an expired-but-never-taken-over lease still finalizes (a completed run must not lose its notifications); once taken over, the original owner is permanently fenced by the CAS | PG: `test_expired_lease_without_takeover_still_publishes`, `test_takeover_during_evaluation_fences_stale_worker` |
| Crash before/inside publication leaves no side effects | Closed | everything a run publishes is one transaction; a failure injected before publication and one injected mid-publication (second attachment's delivery FK violation, after the first attachment's events, deliveries and baseline rows were written) both leave no run result, member, baseline row, event or delivery behind, and the retry publishes exactly once | PG: `test_failure_before_publication_leaves_zero_side_effects`, `test_mid_publication_failure_rolls_back_entire_transaction`, `test_retry_after_failure_publishes_exactly_once` |
| E-17 hysteresis survives restart, distinct from rank-delta | Closed | `entry_rank` < `exit_rank` validated; bands stored in `screener_attachment_state`; `rank_delta` compares previous COMPLETE ranks only | `test_top_n_hysteresis_buffers_boundary_oscillation`, `test_rank_delta_fires_on_threshold_cross` |
| E-18 partial runs: no exits, no baseline advance, no universe replacement | Closed | an attachment plan is built only for `status == 'complete'` runs; dependent-universe refresh only after complete runs; stale source → `UniverseSourceUnavailable` | scheduler code path; `test_stale_complete_run_expires_visibly`; PG: `test_partial_run_publishes_without_attachment_effects` |
| Dynamic universes from screener results | **Defective at release — repaired in Phase 4** | universe kind `screener` (`workflow`/`top_n`/`freshness_limit_s`); scheduler re-materializes dependents after each complete run | `tests/screeners/test_universe_screener_kind.py` covers the CODE path on SQLite only. The PostgreSQL CHECK constraint rejected `kind='screener'`, so creation failed on a real database — see "Acknowledged defects" below. Fixed by migration `20260911_000016` and covered by `tests/integration/test_universe_kind_postgres.py` |
| Cycle prevention + ownership on referenced resources | Closed | `_assert_no_screener_universe_cycle` bounded walk (depth 8, owner-scoped, origin-name aware) at authoring AND resolution | `test_dependency_cycle_rejected`, `test_cross_owner_workflow_reference_rejected` |
| API: run history, detail, events, manual runs, preview | Closed | `backend/api/routers/worker_screeners.py` under `/api/worker/screeners` (worker-token boundary; scopes reused — no new permission) | `tests/api/test_worker_screeners.py` (owner isolation 404, permission 403, idempotent manual trigger) |
| Preview purity (no persistent changes / notifications) | Closed | preview runs the production pipeline in-memory; asserts no run rows and no deliveries | `test_preview_is_pure_dry_run` |
| Reuse of the notification outbox | Closed | attachment signal events (subscription NULL, workflow-scoped) and their pending deliveries are written inside `finalize_run`'s publication transaction, occurrence-key deduplicated; delivery worker renders screener context from evidence (`build_screener_message`) | PG: `test_retry_after_failure_publishes_exactly_once` (3 events / 3 deliveries, no duplicates), FK-true fan-out in `test_attachment_events_idempotent_and_fenced_after_takeover` |
| Notification content explains screener/trigger/symbol/ranks/freshness | Closed | `build_screener_message`: screener name, trigger+action, symbol, rank/prev/delta, values, run as-of, event time, event id | unit-covered via delivery resolver tests; provider exactly-once explicitly NOT promised (E-22/E-23 carry over) |
| Existing Phase 1/2 behavior intact | Closed | 457 Python tests (screeners, workflows, alerts, notifications, fundamentals, worker screener API) + 24 real-PostgreSQL integration tests | commands and results in "Test evidence" below |

## Acceptance scenarios (assignment items 1–16)

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Stable rank ties, reproducible results | Pass — identity tie-break, deterministic sort |
| 2 | Unavailable data distinguished from failed condition | Pass — `no_data`/`fundamentals_unknown` vs `condition_filter` |
| 3 | First complete run silent unless initial-match | Pass — baseline test |
| 4 | Entry/exit hysteresis survives restart, suppresses oscillation | Pass — band + `exit_after` state persisted |
| 5 | Top-N/rank-delta deterministic boundaries | Pass — exact cut, delta threshold tests |
| 6 | Partial coverage: no false exits, no replacement universe | Pass — complete-only preparation; PG partial-run test asserts no events, deliveries or baseline advance |
| 7 | Last complete results expire visibly; recovery restores | Pass — freshness limit → source-unavailable; new run refreshes |
| 8 | Downtime coalesces missed schedules, no backlog storm | Pass — latest-due-only scheduling |
| 9 | Concurrent workers / stale-owner takeover → one logical run + event | Pass — real-PG concurrency + fencing tests (takeover during evaluation publishes nothing) |
| 10 | Crash injection → atomic publication of result/event/outbox | Pass — preparation-then-publication fault injection, mid-transaction rollback (first attachment fully written, second fails) and retry-exactly-once tests; events idempotent by occurrence key |
| 10b | Overlapping occurrences → one notification per transition | Pass — absence-baseline interleaving, exit_after counter and rank_delta deltas all derived from the locked baseline |
| 11 | Dynamic universe additions warm up; removals release | Pass — universe revisions flow through the Phase 2 warmup/pause machinery |
| 12 | Workflow/universe revisions historically attributable | Pass — run rows pin `workflow_revision_id` + `universe_revision` |
| 13 | Cross-owner references and cycles rejected | Pass — 404/typed errors, bounded cycle walk |
| 14 | Preview purity | Pass — zero persistence asserted |
| 15 | Production wiring scheduler → stored data → result → attachment → outbox | Pass — `test_scheduler_end_to_end_on_postgres` |
| 16 | Existing Phase 1/2 behavior covered | Pass — suites listed in test evidence |

## Publication transaction boundary

`ScreenerScheduler._run_pipeline` runs the ranked pipeline (the expensive
part) outside the transaction and passes `finalize_run` an `AttachmentPlan`:
the attachment specs plus the ranked results, carrying **no baseline state**.
The single transaction is ordered:

1. ownership compare-and-swap (`id` + `lease_owner` + `status='running'`) —
   rowcount 0 aborts before anything becomes visible;
2. member rows (delete + insert);
3. per attachment: advisory transaction lock keyed on
   (owner, workflow, revision, attachment) → **read the baseline under that
   lock** → chronology gate → derive the entry/exit/top-N/rank-delta events
   and baseline upserts from that baseline → insert signal events and pending
   deliveries (occurrence-key deduplicated) → upsert baseline rows;
4. fold the published counters into `coverage` (`attachment_events_published`,
   `attachment_events_suppressed`, `attachment_events_stale_suppressed`).

Why the baseline is read in step 3 rather than before the transaction: a
baseline snapshot taken outside it is invalidated the moment another
occurrence publishes. Two occurrences planned against an empty baseline both
compute "first run"; whichever publishes second would then replay an entry
for an instrument the first one already notified, and its absence counters
and rank deltas would be measured against a baseline that never existed.
Deriving the transitions under the lock makes the published transitions a
function of the baseline as it is at publication time.

The `scheduled_for` chronology gate is kept as a second, independent guard:
when the locked baseline belongs to a strictly newer occurrence, the older
run's attachment publication is suppressed whole and counted as stale — its
results still publish, its comparison state never overwrites newer state.

Consequences: a crash at any point, a stale-owner rejection, or a failure
inside step 3 rolls back the WHOLE batch — a takeover re-runs the occurrence
to produce exactly one logical publication. Attachment work is NOT deferred
after `finalize_run`, so there is no window in which a completed run's
notifications can be lost.

## Test evidence (executed 2026-09-11)

| Suite | Command | Result |
| --- | --- | --- |
| Alerts-platform unit suites | `.venv/bin/python -m pytest tests/screeners tests/workflows tests/alerts tests/notifications tests/fundamentals tests/api/test_worker_screeners.py -q` | `457 passed` |
| Screener PostgreSQL fault injection (18 tests) | `ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' .venv/bin/python -m pytest tests/integration/test_screener_postgres.py -q` | `18 passed`, stable over 8 consecutive runs |
| Phase 1.5 PostgreSQL hardening (6 tests) | `DATABASE_URL='postgresql+psycopg2://postgres:testonly@127.0.0.1:15433/kite_test' ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' .venv/bin/python -m pytest tests/integration/test_alerts_postgres_hardening.py -q` | `6 passed` |
| Pre-fix regression probe | same race scenario executed against commit `3728f06` in a detached worktree | `FAILED — NSE:A entered 2 times: ['NSE:A', 'NSE:B', 'NSE:A', 'NSE:C']`; the same scenario passes after the fix |

These counts replace the stale `447` / `11` figures of the previous revision.
The PostgreSQL suites run against the isolated disposable database only
(`kite-test-postgres`, port 15433, migrations at `20260910_000015`); without
`ALERTS_TEST_DATABASE_URL` the module skips cleanly. Fault-injection suites
must not share the database with a live worker, and test workflows must be
named `pg-*` (the `clean_runs` fixture only reclaims that namespace).

## Measured workload (no certification claim)

Representative scan measured on this machine (2026-09-11) with an ad-hoc
harness mirroring `tests/screeners/test_runner.py::_Bars`: 500 members × 120
daily bars, one filter stage (`close > 90`), rank by `change_pct` descending
with `top_n: 50`, five warm runs — `status: complete`, `evaluated: 500`,
`qualifying: 50`, wall time 263–272 ms per run (median 266 ms). This is a
workload OBSERVATION at modest scale, NOT the Phase 6 500-symbol/5,000-rule
certification. The 15-test PostgreSQL screener suite (including publication
and concurrency fault injection) completes in ~6 s.

## Out of scope (per assignment)

- Phase 4 advanced conditions (F10), SDK authoring, external signals.
- Phase 5 MCP; Phase 6 visual editor + certification.
- Screeners/attachments placing orders — alerts NEVER trade.

## Live deployment status

Deployed 2026-09-11 on this host:

| Item | Value |
| --- | --- |
| Deployed commit | `3ce7303` — "fix(alerts): derive screener attachment transitions from the locked baseline" |
| Image | `kite-algo-alerts-worker`, built from `compose.yml` + `compose.worker.yml` |
| Command | `docker compose -f compose.yml -f compose.worker.yml up -d --build --no-deps alerts-worker` |
| Container | `kite-alerts-worker` — `Up (healthy)` after restart |
| Code verification | `sha256sum` of `/app/backend/screeners/scheduler.py` and `/app/backend/workflows/screener_repository.py` in the container match the checked-out `3ce7303` tree exactly |
| Schema | live `kite-postgres` at migration `20260910_000015`; `screener_run`, `screener_run_member`, `screener_attachment_state` present — no schema change in this pass |
| Runtime | worker booted with no errors; health file written; 0 active screener workflows on the live database, so the scheduler is idle rather than exercised |

Only the `alerts-worker` service was rebuilt/recreated (`--no-deps`); the
database, Redis, market-runtime and API containers were left untouched. The
end-to-end verification of this fix was executed against the isolated
disposable PostgreSQL (`kite-test-postgres`, port 15433), not against live
data.

## Acknowledged defects (identified after release)

### D-1 — `universes.kind` rejected `screener` on PostgreSQL

**Defect.** The dynamic-universe code path has supported `kind='screener'`
since Phase 3 (`universes.py::SUPPORTED_UNIVERSE_KINDS`), but migration
`20260909_000014` declared
`CHECK (kind IN ('explicit','index','portfolio'))` and `backend/schema.sql`
mirrored it. The SQLAlchemy model declared no CHECK at all, so
`Base.metadata.create_all` — and therefore every SQLite unit test — never
enforced or noticed it.

**Blast radius.** Creating or resolving a screener-backed universe failed on
real PostgreSQL. The feature was unusable in production while its parity row
read "Closed", because the evidence behind that row was SQLite-only. The
PostgreSQL suite did not cover it either: `test_screener_postgres.py` exercises
the screener RUN tables, not the `universes` constraint.

**Repair (Phase 4).** Migration `20260911_000016` alters the constraint to admit
`screener` — an `ALTER`, not a table rebuild, so an existing database is fixed
in place; `backend/schema.sql` is corrected for fresh installs; and the ORM
model now declares the same `CheckConstraint`, so SQLite tests enforce the rule
and the drift cannot silently return.

**Evidence.** `tests/integration/test_universe_kind_postgres.py` asserts, on
real PostgreSQL, that `pg_get_constraintdef` admits `'screener'` and that a
screener-backed universe can be created and resolved. It covers the upgraded
database and the `schema.sql` path separately; the from-zero Alembic path is
blocked by defect D-2 below and is recorded as such.

## Acknowledged defects (open)

### D-2 — from-zero installation is blocked

`alembic upgrade head` runs `20260330_000001_baseline_schema`, which executes
`backend/schema.sql`. That file ALTERs `signal_events` (the Phase 3 screener
section) but the table is only created by migration `20260908_000011` — later
in the same chain — and the file likewise assumes an alerts-platform baseline
it never creates. The chain therefore aborts at the first statement and no
from-zero install is possible.

This predates Phase 4 (present at `ae98218`) and is **not** repaired here:
the fix is a restructure of `schema.sql` so every statement is ordered after
its dependencies. The condition is pinned by a test that skips with this reason
rather than passing silently, so repairing it flips that test to a real
assertion.
