# Alerts platform — Phase 3 parity matrix (screeners and attachments)

Status as of 2026-09-10, branch `development`. Phase 3 scope per
[alerts-platform-spec-v2 §9](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md):
F9 (scheduled screeners, result snapshots, attachments), E-17/E-18/E-19.

## Requirement parity

| Requirement | Status | Where | Evidence / notes |
| --- | --- | --- | --- |
| Screener block in canonical schema (reserved since Phase 1) | Closed | `models.py` `ScreenerSpec`/`ScheduleSpec`/`RankSpec`/`AttachmentSpec`; `parser.py` `_screener_spec`; `compiler.py` `_validate_screener` | Round-trip: parse → document dict → parse preserves the canonical hash (`test_screener_schema.py`) |
| Scheduled scans over STORED data (price/change/volume/liquidity) | Closed | `backend/screeners/runner.py` | Latest completed daily candle + `change_pct` + `turnover`; `screener_only` fields rejected in alert documents (no live path exists) |
| Fundamental filters from a real production source | Closed | `fundamentals_context.FundamentalsLoader` → `public.fundamentals_features` (Screener.in nightly sync); screener pipeline injects per-member context with acquisition metadata | Unit: `test_fundamentals_context.py`; runner tests; §5.8 honesty: only the latest snapshot is read, never presented as history |
| Completed-candle technical filters | Closed | runner: same `evaluate_stage`/feature engine semantics as alerts (3VL, layered chains, `stage:` refs) | `tests/screeners/test_runner.py` |
| Deterministic ranking; stable tie-break | Closed | `runner._rank`: sort by (directional score, `EXCHANGE:SYMBOL` asc); null scores never rank; top-N cut keeps the rank with `beyond_top_n` | `test_rank_desc_ties_break_by_instrument_identity`, `test_rank_asc_reverses_scores_but_not_ties`, `test_top_n_boundary_is_exact_and_deterministic` |
| Run persistence: revision identity, coverage, status, values, ranks, freshness | Closed | migration `20260910_000015` (`screener_run`, `screener_run_member`); `ScreenerRunRepository.finalize_run` writes status+members+freshness in ONE transaction | `test_finalize_is_atomic_members_and_status_together` (PG); schema mirrored in `backend/schema.sql` |
| Precise coverage/completeness; missing data ≠ negative match | Closed | runner coverage dict (`expected/evaluated/unavailable/unknown_conditions/rank_value_missing/qualifying/complete`); typed exclusion reasons | `test_missing_data_is_unavailable_not_a_failed_match`, `test_condition_failure_is_a_valid_negative_not_unavailable` |
| Coherent data cutoff, no future candles | Closed | runner filters every member's bars to `ts <= as_of` | `test_as_of_cutoff_never_consumes_future_candles` |
| Exchange-calendar scheduling; MCX/currency honesty | Closed | `compute_screener_bucket` (IST buckets, session-gated); parser/compiler reject any calendar except `nse_equity` with an actionable message (feed-driven eligibility is not a calendar) | `test_bucket_walks_back_over_inactive_sessions`, `test_mcx_schedule_calendar_rejected_with_actionable_error` |
| Durable occurrence ownership + idempotency (E-2) | Closed | unique `occurrence_key` = `workflow_id:bucket_epoch`; `claim_run` INSERT-or-CAS; 300 s leases | PG: `test_concurrent_claims_produce_exactly_one_logical_run` (6 concurrent claimants → 1 winner, 1 row) |
| Stale owners cannot finalize | Closed | `finalize_run` compare-and-swap on `lease_owner` + `status='running'`; rowcount 0 aborts the whole transaction | PG: takeover test (old owner's publish rejected wholesale, members never written) |
| Restart/crash recovery | Closed | crashed claim is taken over after lease expiry; the SAME logical run completes; replays are idempotent | PG: `test_crash_after_claim_recovers_via_lease_takeover` |
| Missed schedules coalesce (E-19) | Closed | scheduler evaluates only the LATEST due bucket; older buckets never replay; session gate walk-back is bounded (`max_walkback=40`) | `test_bucket_gate_never_active_returns_none`; scheduler double-pass test (second pass → 0 runs) |
| Attachments: entry/exit/top-N/rank-delta with explicit baseline | Closed | `screeners/scheduler.py::evaluate_attachments`; state persisted per (owner, workflow, revision, attachment, instrument) | baseline-silent, `initial_match`, `exit_after`, rank-band hysteresis, rank-delta tests (SQLite) + PG idempotency |
| E-17 hysteresis survives restart, distinct from rank-delta | Closed | `entry_rank` < `exit_rank` validated; bands stored in `screener_attachment_state`; `rank_delta` compares previous COMPLETE ranks only | `test_top_n_hysteresis_buffers_boundary_oscillation`, `test_rank_delta_fires_on_threshold_cross` |
| E-18 partial runs: no exits, no baseline advance, no universe replacement | Closed | attachments evaluate only on `status == 'complete'`; dependent-universe refresh only after complete runs; stale source → `UniverseSourceUnavailable` | scheduler code path; `test_stale_complete_run_expires_visibly` |
| Dynamic universes from screener results | Closed | universe kind `screener` (`workflow`/`top_n`/`freshness_limit_s`); scheduler re-materializes dependents after each complete run | `tests/screeners/test_universe_screener_kind.py` |
| Cycle prevention + ownership on referenced resources | Closed | `_assert_no_screener_universe_cycle` bounded walk (depth 8, owner-scoped, origin-name aware) at authoring AND resolution | `test_dependency_cycle_rejected`, `test_cross_owner_workflow_reference_rejected` |
| API: run history, detail, events, manual runs, preview | Closed | `backend/api/routers/worker_screeners.py` under `/api/worker/screeners` (worker-token boundary; scopes reused — no new permission) | `tests/api/test_worker_screeners.py` (owner isolation 404, permission 403, idempotent manual trigger) |
| Preview purity (no persistent changes / notifications) | Closed | preview runs the production pipeline in-memory; asserts no run rows and no deliveries | `test_preview_is_pure_dry_run` |
| Reuse of the notification outbox | Closed | `record_attachment_event` writes signal_event (subscription NULL, workflow-scoped) + pending deliveries; delivery worker renders screener context from evidence (`build_screener_message`) | PG FK-true delivery fan-out in the attachment idempotency test |
| Notification content explains screener/trigger/symbol/ranks/freshness | Closed | `build_screener_message`: screener name, trigger+action, symbol, rank/prev/delta, values, run as-of, event time, event id | unit-covered via delivery resolver tests; provider exactly-once explicitly NOT promised (E-22/E-23 carry over) |
| Existing Phase 1/2 behavior intact | Closed | 447 Python tests incl. full workflows/alerts/notifications/API suites; 11 real-PG integration tests | commands below |

## Acceptance scenarios (assignment items 1–16)

| # | Scenario | Result |
| --- | --- | --- |
| 1 | Stable rank ties, reproducible results | Pass — identity tie-break, deterministic sort |
| 2 | Unavailable data distinguished from failed condition | Pass — `no_data`/`fundamentals_unknown` vs `condition_filter` |
| 3 | First complete run silent unless initial-match | Pass — baseline test |
| 4 | Entry/exit hysteresis survives restart, suppresses oscillation | Pass — band + `exit_after` state persisted |
| 5 | Top-N/rank-delta deterministic boundaries | Pass — exact cut, delta threshold tests |
| 6 | Partial coverage: no false exits, no replacement universe | Pass — complete-only attachment path |
| 7 | Last complete results expire visibly; recovery restores | Pass — freshness limit → source-unavailable; new run refreshes |
| 8 | Downtime coalesces missed schedules, no backlog storm | Pass — latest-due-only scheduling |
| 9 | Concurrent workers / stale-owner takeover → one logical run + event | Pass — real-PG concurrency + fencing tests |
| 10 | Crash injection → atomic publication of result/event/outbox | Pass — finalize CAS rollback test; events idempotent by occurrence key |
| 11 | Dynamic universe additions warm up; removals release | Pass — universe revisions flow through the Phase 2 warmup/pause machinery |
| 12 | Workflow/universe revisions historically attributable | Pass — run rows pin `workflow_revision_id` + `universe_revision` |
| 13 | Cross-owner references and cycles rejected | Pass — 404/typed errors, bounded cycle walk |
| 14 | Preview purity | Pass — zero persistence asserted |
| 15 | Production wiring scheduler → stored data → result → attachment → outbox | Pass — `test_scheduler_end_to_end_on_postgres` |
| 16 | Existing Phase 1/2 behavior covered | Pass — suites listed in test evidence |

## Measured workload (no certification claim)

Representative scan measured on the isolated test PostgreSQL (SQLite unit
harness measures the pipeline core): 500 members × 120 daily bars, single
rank pass — see `test_screener_postgres.py` suite output for the per-run
wall time on this machine. This is a workload OBSERVATION at modest scale,
NOT the Phase 6 500-symbol/5,000-rule certification.

## Out of scope (per assignment)

- Phase 4 advanced conditions (F10), SDK authoring, external signals.
- Phase 5 MCP; Phase 6 visual editor + certification.
- Screeners/attachments placing orders — alerts NEVER trade.

## Live deployment status

Recorded after the final rebuild + verification in this assignment's
closing report (migration `20260910_000015` on live `kite-postgres`,
container health, API reachability under `/api/worker/screeners`).
