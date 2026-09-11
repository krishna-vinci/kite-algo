# Alerts platform — Phase 4 parity matrix (advanced conditions, external signals, SDK)

Status as of 2026-09-11, branch `development`. Phase 4 scope per
[alerts-platform-spec-v2 §9](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md):
F10 (advanced conditions and external signals) and the SDK half of F6.
**Phase 5 (MCP) is deferred; Phase 6 (editor, certification) is not started.**

Baseline: HEAD `ae98218` — 457 alerts-platform unit tests, 18 screener-PG,
6 phase-1.5-PG passing; live `kite-postgres` at `20260910_000015`;
`kite-alerts-worker` healthy at `3ce7303`.

## 1. Requirement parity

### F10 — advanced conditions

| Requirement | Status | Where | Evidence |
| --- | --- | --- | --- |
| N consecutive completed bars | Closed | `models.Stage.consecutive_bars`, `predicates._apply_consecutive_bars` | `test_consecutive_bars_counts_each_eligible_bar_once`, `..._restart_after_a_break`, `..._realert_after_a_new_streak`, `..._state_survives_restart` |
| Unknown/missing resets vs suspends, documented per machine | Closed | streak: unknown RESETS (unknown is not `true`, §5.3); sequence: unknown consumes the bound without invalidating | `test_unknown_bar_resets_the_streak`, `test_sequence_unknown_bar_consumes_the_bound_without_invalidating` |
| Bounded A-then-B sequences | Closed | `models.SequenceSpec`, `predicates._apply_sequence` | `test_sequence_cannot_satisfy_both_legs_on_one_bar`, `..._waits_for_the_pullback_rather_than_requiring_it_next_bar` |
| Sequence bounds: which clock, and independent | Closed | `within_bars` counts completed bars; `within` is ELAPSED EVENT TIME (no exchange calendar); both enforced when both are supplied | `test_sequence_bar_bound_expires_and_does_not_fire`, `test_sequence_time_bound_is_elapsed_time_not_bar_count`, `test_sequence_enforces_both_bounds_when_both_are_supplied` |
| Sequence rearm / expiry determinism | Closed | A arms on bar N, B completes strictly later; expiry disarms; completion disarms and re-arms on the next A | `test_sequence_rearms_after_completion`, PG `test_late_observation_is_recorded_without_minting_a_crossing` |
| Explicit condition hysteresis | Closed | `models.HysteresisSpec`, `predicates._apply_hysteresis` — constant thresholds only | `test_hysteresis_holds_through_a_boundary_oscillation`, `test_hysteresis_survives_restart`, `test_hysteresis_downward_direction` |
| Per-session notification caps | Closed | `advanced_repository.reserve_session_slot` + `service.py` cap branch | unit `tests/api/test_worker_signals.py`; PG `test_session_cap_is_shared_atomically_across_instruments` |
| Cap scope, reset boundary, counted events | Closed | one counter per (owner, workflow, revision, alert, session), shared across instruments; counts LOGICAL notifications; reset by session-id change; exchange-hours resets rejected | `test_cap_valid`, `test_cap_bad_reset`; docs `workflow-format.md` §Phase 4 |
| Durable cap suppression (inspectable) | Closed | `alert_suppression_counters`, reason `session_cap`, same transaction | PG `test_suppression_counters_accumulate_durably` |
| Advanced state survives restart and takeover | Closed | per-subscription checkpoint for streaks/sequences/hysteresis; workflow-level row for breadth | `test_sequence_progress_survives_restart`, `test_consecutive_bars_state_survives_restart`, PG `test_external_ingestion_survives_a_restart_before_evaluation` |
| State changes share the fenced publication boundary | Closed | all Phase 4 writes ride the caller's transaction | PG `test_failure_rolls_back_the_contribution_and_the_state` |
| Candle-close-only honesty | Closed | `ltp` + advanced conditions rejected with an actionable issue | `test_consecutive_bars_requires_candle_clock`, `test_sequence_requires_candle_clock`, `test_breadth_requires_the_candle_clock` |

### F10 — cross-symbol logic

| Requirement | Status | Where | Evidence |
| --- | --- | --- | --- |
| Distinct-symbol aggregation within a window | Closed | `breadth.py` + `alert_breadth_state`/`alert_breadth_triggers` | PG `test_first_crossing_notifies_because_satisfied_starts_false` |
| Windowed participation ≠ simultaneous breadth | Closed | labeled windowed; `mode: simultaneous` reserved and REJECTED as not implemented | `test_simultaneous_breadth_is_rejected_as_not_implemented`, capabilities `breadth_modes` |
| One instrument counts once per window | Closed | one contribution row per instrument, latest trigger wins | PG `test_older_observation_never_rewrites_a_newer_contribution`, `test_concurrent_stages_cannot_interleave_the_windows` |
| Event-time ordering / arrival-order independence | Closed | monotonic contribution upsert + aggregation watermark; evaluated at the watermark | PG `test_reversed_arrival_order_converges_to_the_same_crossing` |
| Window expiry and rearm | Closed | rearm requires an observed `count < K`; time alone never rearms | PG `test_same_timestamp_crossings_get_distinct_identities` |
| Crossing identity (no same-timestamp collision) | Closed | monotonic `crossing_seq`, never a timestamp or bucket | PG `test_same_timestamp_crossings_get_distinct_identities` |
| Membership revision handling, no window clearing | Closed | current members counted; retained rows never counted | PG `test_membership_change_midwindow_filters_without_clearing` |
| Membership re-entry does not restore a contribution | Closed | `evict_breadth_contribution` on re-admission | PG `test_readmitted_instrument_does_not_regain_its_contribution` |
| Membership freshness bound | Closed | `membership_stale` past `ALERTS_BREADTH_MEMBERSHIP_MAX_AGE_S` | PG `test_stale_membership_and_capacity_report_unknown` |
| Bounded state; no pruning of valid contributions | Closed | one row per member; `breadth_capacity_exceeded` instead of a partial count | same test |
| Relative strength / pair ratios, precise formulas | Closed | `registry.PAIR_COMPUTATIONS` (advertised verbatim), `pairs.py` | `test_pair_ratio_uses_the_same_completed_bar`, `test_relative_strength_compares_the_same_period` |
| Alignment, skew, freshness, missing, zero denominators | Closed | head alignment (+`max_skew_bars`), lookback ENDPOINT alignment, per-leg freshness, named unknown reasons | `test_pair_ratio_reports_misaligned_heads`, `..._tolerates_the_configured_skew`, `..._zero_denominator_is_unknown`, `..._stale_head_is_unknown` |
| Never combine arbitrarily fresh and stale values | Closed | freshness bound on the head; no fallback for externals | `test_pair_stale_head_is_unknown`, external `test_lookup_does_not_fall_back_to_an_older_valid_value` |
| Basis compatibility stated honestly | Closed | store carries no adjustment column, documented as structural rather than promised | `workflow-format.md` §Phase 4 |
| No future bars consumed | Closed | `as_of` cutoff in pair indexing | `test_pair_never_consumes_a_future_bar` |

### F10 — external signals

| Requirement | Status | Where | Evidence |
| --- | --- | --- | --- |
| Registered producers with typed, expiring, idempotent values | Closed | `external_signals.py`, tables `external_signal_producers`/`_credentials`/`_values` | `test_accepted_value_is_durably_stored`, `test_duplicate_idempotency_key_with_same_content_deduplicates` |
| No code execution in payloads | Closed | typed scalars (number/string/boolean) only, schema-validated | `test_payload_rejects_executable_shapes`, `test_value_schema_rejects_non_scalar_types` |
| Least-privilege authorization, both directions | Closed | worker token `signals:admin`/`signals:read` (grantable, NOT default) administers; a producer credential submits; neither can do the other | `test_producer_administration_requires_signals_admin`, `test_a_worker_token_cannot_submit_values_as_a_producer`, `test_a_producer_credential_cannot_administer`, `test_signals_actions_are_grantable_but_not_default` |
| Credential issue/rotate/revoke | Closed | one-time secret, hash-only storage, per-credential and whole-producer revocation | `test_secret_is_returned_once_and_never_again`, `test_rotation_keeps_the_old_credential_valid_until_revoked`, `test_revoking_the_producer_stops_all_submissions` |
| Durable acceptance precedes the response | Closed | endpoint commits before 2xx | `test_submit_returns_after_durable_storage`, PG `test_external_ingestion_survives_a_restart_before_evaluation` |
| Duplicate ingestion idempotent; conflicting payload explicit | Closed | same key+content dedups; different content 409s; concurrent races converge | `test_duplicate_idempotency_key_with_different_content_conflicts`, PG `test_concurrent_duplicate_ingestion_produces_one_row` |
| Expiry / lateness / future skew | Closed | rejects future beyond bound; stores too-late as unusable `late`; rejects past expiry | `test_future_skew_beyond_the_bound_is_rejected`, `test_late_value_is_stored_but_unusable`, `test_expiry_in_the_past_is_rejected` |
| Expired/revoked values visibly unknown | Closed | `external_expired`/`_revoked`/`_future`/`_late`, exposed in health and evidence | PG `test_expired_and_revoked_values_are_unusable`; `test_lookup_excludes_future_values` |
| Bounded payload/ingestion; capacity rejects rather than evicts | Closed | 8 KiB / 32 fields / 512 chars; 429 at the row cap | `test_payload_bounds_are_enforced`, `test_capacity_is_rejected_rather_than_evicting_live_values` |
| Retention and purge of unusable values only | Closed | purge only past expiry + grace | `test_purge_only_removes_long_expired_values` |
| Evaluation clock explicit (sampled, not pushed) | Closed | consumed at the stage's `candle_close` clock; no queue, no wake-up | `external_context.py`; disclosed in `signals_health()` `note` and in the operations guide |
| Lookup cutoff / expiry clock / no fallback | Closed | `event_time <= T`; expiry evaluated at `T` not `now()`; no older-value fallback | `test_lookup_selects_the_newest_value_at_or_before_the_cutoff`, `test_lookup_expiry_is_evaluated_at_the_cutoff_not_now` |
| Cross-owner isolation | Closed | owner-scoped queries; cross-owner reads are absent, API returns 404 | `test_cross_owner_lookup_is_isolated`, `test_cross_owner_producer_access_is_404` |

### F6 — SDK authoring

| Requirement | Status | Where | Evidence |
| --- | --- | --- | --- |
| SDK methods for the settled API | Closed | `sdk/python/kite_algo_worker/{client,async_client}.py` (sync + async), version `0.10.0` | `tests/sdk/test_worker_sdk_platform.py` |
| Capability discovery from the registry used by validation | Closed | `capabilities_payload()` is a pure function of `registry`; SDK exposes it | `test_capabilities_endpoint_matches_the_registry`, `test_every_curated_operation_is_actually_mounted` |
| Validate + read-only preview | Closed | both transports; preview documented as non-persisting | `test_platform_and_worker_prefixes_do_not_collide`, `test_document_payload_rejects_ambiguous_input` |
| CRUD + lifecycle + YAML import/export | Closed | curated operation list with `mutates` classification | `test_mutating_operations_are_classified` |
| Expected-revision conflicts preserved | Closed | `update_workflow(expected_revision=...)` surfaces `REVISION_CONFLICT` | server-side `tests/api/test_worker_workflows.py`; SDK passes it through |
| Idempotency keys respected; no implicit retry of non-idempotent mutations | Closed | create/import/manual-run take explicit keys; **no automatic retry anywhere in the SDK** | `test_mutating_operations_are_classified`; SDK has no retry logic |
| Pagination and error details preserved | Closed | `page_params` enforces bounds; typed exceptions parse `rejection_reason` | `test_pagination_bounds_are_enforced_client_side` |
| No second workflow language / evaluator | Closed | the SDK shapes payloads only; the server validates | `capabilities_payload` + `test_every_curated_operation_is_actually_mounted` |
| SDK methods for Phase 4 examples | Closed | universe/screener/producer/value operations are in the curated list | `sdk/python/examples/phase4_authoring.py` |

## 2. Canonical semantics and material design decisions

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | Breadth state is **one durable row per (owner, workflow, revision, stage)**, never a per-subscription checkpoint | A checkpoint is keyed per instrument; N instruments would each hold a private copy of `satisfied`, so a workflow-level event would be governed by whichever copy happened to evaluate |
| D2 | A per-session cap **suppresses the notification but advances state**, recorded durably | A capped bar must still count toward a streak or an armed sequence; the skip must be inspectable |
| D3 | The migration repairs the `universes.kind` CHECK and the ORM declares it too | SQLite never enforced it, so the drift was invisible until a real database rejected the insert |
| D4 | Advanced conditions are `candle_close` only | Ticks carry no bar identity; an `ltp` stage would silently never fire |
| D5 | A and B cannot be satisfied on the same observation | Otherwise A-then-B degenerates into A∧B |
| D6 | Unknown **resets a streak** but **suspends a sequence** | Unknown is not `true`, so a gap must not extend a run; but one unknown bar should not discard an armed sequence |
| D7 | Sequence bounds are independent: `within_bars` counts bars, `within` is elapsed time | Bar-count-equivalence would mean inventing market hours for feed-driven segments |
| D8 | Session-cap scope is (workflow, alert), counted per logical notification, reset on session-id change | A cap that counted channel deliveries would leak with fan-out; the row is keyed by session so no reset job exists |
| D9 | Pairs: single timeframe, head alignment (bounded skew), **lookback endpoint alignment**, per-leg freshness, same session | Matching timestamps alone do not prove freshness or period equivalence |
| D10 | Dedicated producer credentials, one-time secret, hash-only storage | A worker token plus a body field would let any token spoof any producer |
| D11 | Breadth rearm is a durable threshold state machine with a monotonic crossing number | Timestamp or bucket identity collides on same-timestamp transitions and imposes notification granularity |
| D12 | SDK coverage is a curated operation list | Bounded, reviewable obligation instead of wrapping every mounted route |
| D13 | External values are **sampled, never pushed** | No new clock, stream or service; the disclosed cost is that a value can expire between evaluations |
| D14 | Hysteresis requires a constant threshold | The release bound must be comparable at compile time; dynamic release is deferred, not half-implemented |
| D15 | Windowed participation only; `simultaneous` rejected | "K symbols on the same bar" has its own alignment and partial-bar semantics |
| D16 | Breadth membership has a freshness bound | "Keep the last valid membership" alone would let an indefinitely stale set support a signal |
| D17 | Breadth capacity reports unknown rather than pruning | A partial count is indistinguishable from genuine absence of breadth |

### Defects found and fixed while implementing Phase 4

| Where | Defect | Fix |
| --- | --- | --- |
| `advanced_repository.count_breadth_contributions` | The member filter ran in Python AFTER the SQL `LIMIT`, so retired members consumed the row budget and could evict valid contributions — nondeterministically, since contributions share timestamps | Filter is now a `WHERE` clause; the limit is bounded by the member count (PG `test_membership_change_midwindow_filters_without_clearing`) |
| `breadth.evaluate_breadth` | A late observation returned before recording its contribution, dropping it entirely | The contribution is written first and unconditionally; the monotonic guard makes any order safe (PG `test_older_observation_never_rewrites_a_newer_contribution`) |
| `breadth.evaluate_breadth` | The aggregate was evaluated at the observation's own time, so with out-of-order arrival a crossing could be **missed entirely** — the newest-timestamped contribution was counted before its peers committed, and later observations looked "stale" so nothing re-evaluated | The aggregate is evaluated at the **watermark** (`max(observed, stored)`), never backwards; the crossing is minted at the watermark, which is the current logical time, so it is not a retroactive notification (PG `test_concurrent_stages_cannot_interleave_the_windows`) |
| `parser._conditions` | **The documented `conditions: {all, any, not}` form validated `any`/`not` and then silently ignored them**, turning an OR/NOT rule into AND-only with no error | Inline groups are parsed and merged with the top-level aliases (`test_groups_inside_conditions_are_preserved_not_silently_dropped`) |
| `compiler` | `MAX_ARITHMETIC_DEPTH` was declared and advertised in `/capabilities` but **never enforced** | Enforced during operand validation (`test_arithmetic_depth_is_enforced_and_advertised`) |
| `worker_workflows.workflow_capabilities` | `stage_types` and limits were hard-coded literals that could drift from the registry | Extracted to a pure `capabilities_payload()` derived from the registry (`test_capabilities_endpoint_matches_the_registry`) |
| `external_signals.ingest_value` | Losing an idempotency race rolled back the caller's whole transaction and reported a conflict even for identical content | Uses a savepoint and converges on the winner when the content matches (PG `test_concurrent_duplicate_ingestion_produces_one_row`) |

### Open defect (not Phase 4, not repaired)

**D-2 — from-zero installation is blocked.** `alembic upgrade head` runs
`20260330_000001_baseline_schema`, which executes `backend/schema.sql`. That
file ALTERs `signal_events` (Phase 3 section) but the table is created by
migration `20260908_000011` — later in the same chain. The chain aborts at the
first statement, so no from-zero install is possible today. Present at
`ae98218`; pinning test: `test_fresh_database_lifecycle_is_recorded_not_assumed`
(skips with this reason, so a repair flips it to a real assertion).

## 3. Executed evidence

| Suite | Command | Result |
| --- | --- | --- |
| Alerts-platform unit suites | `.venv/bin/python -m pytest tests/alerts tests/workflows tests/screeners tests/notifications tests/fundamentals tests/api/test_worker_signals.py tests/api/test_worker_screeners.py -q` | `572 passed` |
| SDK | `.venv/bin/python -m pytest tests/sdk -q` | `255 passed, 1 skipped` |
| SDK version guard | `.venv/bin/python scripts/check_worker_sdk_version_refs.py` | `All worker SDK version references match 0.10.0` |
| Phase 4 PostgreSQL | `ALERTS_TEST_DATABASE_URL=... pytest tests/integration/test_alerts_phase4_postgres.py -q` | `16 passed`, stable over 3 consecutive runs |
| Universe-kind lifecycles | `ALERTS_TEST_DATABASE_URL=... ALERTS_FRESH_DATABASE_URL=... ALERTS_SCHEMA_DATABASE_URL=... pytest tests/integration/test_universe_kind_postgres.py -q -rs` | `2 passed, 1 skipped` (skip = D-2, reason recorded) |
| Phase 3 screener PG regression | `ALERTS_TEST_DATABASE_URL=... pytest tests/integration/test_screener_postgres.py -q` | `18 passed` |
| Phase 1.5 PG regression | `DATABASE_URL=... ALERTS_TEST_DATABASE_URL=... pytest tests/integration/test_alerts_postgres_hardening.py -q` | `6 passed` |

### Pre-existing failures (separately identified, NOT caused by Phase 4)

Running the whole unit tree (`pytest tests/ --ignore=tests/integration --ignore=tests/live
--ignore=tests/mcp -q`) yields **84 failed, 1492 passed, 11 skipped**. The
failure list is **byte-identical with and without the Phase 4 changes**, verified
by stashing the changes and re-running: `algo_runtime` (24: paper executor,
market engine, snapshot builder, intent bridge), `options` (26), `broker_api`
(11), `api` (20: control-plane, auth policy, auth service release, public
runtime config), `journaling` (2), `paper_runtime` (1),
`execution_accounting` (1). These are unrelated subsystems with environment and
module-resolution failures that predate this work.

## 4. Real-PostgreSQL concurrency and rollback evidence

| Property | Test | Result |
| --- | --- | --- |
| Five concurrent contributors serialize on the advisory lock; every row lands and exactly one crossing is minted | `test_concurrent_stages_cannot_interleave_the_windows` | last_count 5, crossing_seq 1, 5 rows |
| Reversed arrival order converges to the same single crossing | `test_reversed_arrival_order_converges_to_the_same_crossing` | both orders → `[1]` |
| A late observation neither rewrites a newer contribution nor mints a crossing | `test_older_observation_never_rewrites_a_newer_contribution`, `test_late_observation_is_recorded_without_minting_a_crossing` | pass |
| An injected failure rolls back the contribution AND the state together | `test_failure_rolls_back_the_contribution_and_the_state` | count and rows unchanged |
| The session cap is shared atomically: 12 concurrent claims grant exactly 3 | `test_session_cap_is_shared_atomically_across_instruments` | 3 granted, counter 3 |
| Concurrent duplicate ingestion produces one row and converges | `test_concurrent_duplicate_ingestion_produces_one_row` | 1 row, 1 insert |

## 5. SDK parity and external-producer authorization evidence

- Every curated platform operation is asserted to exist on **both** transports
  and to correspond to a **mounted** route (`test_every_curated_operation_is_actually_mounted`,
  `test_every_curated_operation_has_sync_and_async_methods`), so a manifest
  entry cannot be a phantom and the clients cannot drift.
- `mutates` is asserted for representative read-only and mutating operations
  (`test_mutating_operations_are_classified`); the SDK performs no automatic
  retry, so nothing non-idempotent is retried implicitly.
- The two mounts are proven distinct (`test_platform_and_worker_prefixes_do_not_collide`):
  `/api/algo-workers/worker/health` vs `/api/worker/workflows`.
- Authorization is proven in both directions and for the default-grant rule
  (`test_a_worker_token_cannot_submit_values_as_a_producer`,
  `test_a_producer_credential_cannot_administer`,
  `test_signals_actions_are_grantable_but_not_default`).
- Producer credentials are proven one-time-reveal and secret-free elsewhere
  (`test_secret_is_returned_once_and_never_again`, PG
  `test_credential_hash_round_trip_on_postgres`).

## 6. Modest workload measurements (no certification claim)

Measured on the isolated PostgreSQL (2026-09-11), single-threaded:

| Measurement | Value |
| --- | --- |
| Sequence checkpoint state, 50-bar bound | **303 bytes** per subscription |
| Breadth contributions, 500 members | **500 rows**, ~120 bytes/row |
| Breadth evaluation (advisory lock + count + state write), median / p95 | **9.0 ms / 11.5 ms** at 500 members |
| External ingestion (validate + dedup check + insert), median / p95 | **3.0 ms / 3.7 ms** over 200 values |
| External lookup (cutoff query + selection), median | **1.77 ms** over 200 stored values |
| External storage, 200 values | **180 KB** total relation size |

Enforced limits: `ALERTS_BREADTH_MAX_INSTRUMENTS` 1000,
`ALERTS_EXTERNAL_MAX_ROWS_PER_PRODUCER` 100000, payload 8 KiB / 32 fields /
512 chars, breadth window ≤ 24h, sequence bound ≤ 500 bars, `consecutive_bars`
≤ 50, `max_per_session` ≤ 1000.

This is a workload OBSERVATION at modest scale — **NOT** the Phase 6
500-symbol/5,000-rule certification, and no extrapolation is claimed.

## 7. Deployment status

**Local and isolated only. NOT deployed.**

| Item | Value |
| --- | --- |
| Branch | `development` |
| Phase 4 commits | `05cbfc5`, `aa2c1ad`, `5ba407c`, `a1359d9`, `087c1f0`, `af7eeee`, `22db69d`, `d295216`, `cfb49db` |
| Migration head | `20260911_000016_alerts_phase4` (applied to the ISOLATED test database only) |
| Unit tests run | 572 alerts-platform + 255 SDK |
| Isolated PostgreSQL | upgrade path, concurrency, fencing, rollback, ingestion durability |
| Live `kite-postgres` | still at `20260910_000015` — **the Phase 4 migration has NOT been applied** |
| Live `kite-alerts-worker` | still at `3ce7303` — **not rebuilt** |

No live Telegram/ntfy notification was sent: destination authorization for a
Phase 4 smoke test has not been given.

### Outstanding live procedure (for the operator)

1. `docker compose -f compose.yml -f compose.worker.yml up -d --build alerts-worker`
   (the migration runs in the `finance-app` container at API start).
2. Confirm the head: `docker exec kite-postgres psql -U postgres -d postgres -tAc
   "select version_num from alembic_version"` → `20260911_000016`.
3. Confirm the repair: `... -tAc "select pg_get_constraintdef(oid) from
   pg_constraint where conrelid='universes'::relname ..."` — or simply create a
   `screener`-kind universe through `/api/worker/universes`.
4. Register a test producer, issue a credential, submit one value, and confirm
   `GET /api/worker/signals/health` counts it.
5. Author a Phase 4 workflow via `sdk/python/examples/phase4_authoring.py`,
   activate it, and verify health shows evaluations.
6. **Only after explicit destination authorization**, point one alert at a test
   channel and confirm a single delivery.

## 8. Out of scope / deferred

- **Phase 5 (MCP)**: deferred, requires a separately agreed scope.
- **Phase 6**: the visual/canvas editor and the 500-symbol/5,000-rule
  certification.
- `mode: simultaneous` breadth: reserved in the schema, rejected at validation,
  not implemented.
- Dynamic (indicator-derived) hysteresis release operands: rejected with an
  actionable issue, deferred.
- Per-leg pair timeframes and adjustment-basis mixing: unsupported; the store
  has no adjustment column.
- Orders: alerts and screeners never place orders.
