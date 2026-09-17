# Hosted strategies — Project 5 parity matrix (execution-quiescence barrier, settlement evidence — G7)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 5](hosted-strategies-implementation-roadmap.md):
the durable execution-quiescence barrier shared by every domain, and the four
settlement axes as evidence surfaces and release conditions. Plan:
`docs/superpowers/plans/2026-09-17-settlement-barrier.md` (R3 §16/§22 authored;
D-1…D-5 binding).

> **Closed** = implemented + covered by an executed test (database/concurrency
> claims proven on real disposable PostgreSQL). **NOT PROVEN** = requires separate
> authorization/infrastructure.

## 0. Delivered

| Item | State |
| --- | --- |
| Migration `20260917_000029` (single head; purely additive) | Closed — `strategy_execution_barriers` (PK = book triple, `barrier_version`, `quiet_since_version`, `last_proof_at`), `strategy_execution_barrier_events` (append-only, insert-only trigger), `strategy_settlement_assessments` (append-only, insert-only trigger) |
| Durable quiescence barrier (invariant 1) | Closed — work events bump the version transactionally (also under the book advisory lock); a proof = one transaction under the `barrier:<account>:<strategy>:<env>` lock with EMPTY in-flight enumeration, recorded at the current version (proofs never bump); any later work event invalidates all prior proofs via version mismatch — quiet windows and identical reads prove nothing |
| Fail-closed in-flight enumeration | Closed — non-terminal attributed orders, unresolved intents/links, non-aligned reconciliation coordinates, refreshing ingest state; unknown evidence fails the proof, never an empty-set proof on error |
| Four axes (R3 §16) | Closed — quiescence; attribution-scoped flatness (strategy book zero AND broker truth at its coordinates zero within `SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS`, stale ⇒ unknown); terminal domain state (runs/jobs/plans-via-reservation lifecycle; domain-adapter registry hook ships empty); no live evaluation authority (no active approval, jobs terminal, archived/closed). Rollup: failed ⇒ `unsettled`, else unknown ⇒ `unknown`; **unknown never releases** (invariant 2) |
| Strategy-scoped flatness (invariant 3) | Closed — another strategy holding the same broker line does not affect this strategy's axis; account flatness never substitutes |
| Assessments as snapshots (D-5) | Closed — append-only rows recording `barrier_version` + per-axis digests; staleness derived on read; late fill after a settled assessment bumps the barrier ⇒ detectably stale (invalidate, not impossibility) |
| Reconciliation integration (D-4) | Closed — `quiescence_state` becomes `verified` only on a valid barrier proof (via `barrier_quiescence_state()`); digest discipline and `settlement_watermark` unchanged; trading-capable attempts stay blocked without a valid proof |
| Owner surface | Closed — `GET /api/strategies/{id}/settlement?environment=` and `POST .../settlement/assess`, owner + account-authorized, worker-proof, `extra="forbid"` |

Commits (unsigned): `e88c3a8` schema, `ea408be` barrier, `3c5ee2f` four axes, `1da251a` reconciliation integration, `07748e2` owner surfaces, `95c85cd` PG integration, `ff4209e` Task-7 residual (PG-suite leftover cleanup), `7e64c82` monotonic head assertions across all PG suites (closure fix, orchestrator-authored).

## 1. Test evidence (independently re-run by the orchestrator)

- `tests/strategies/test_settlement.py`: 66 tests; settlement+reconciliation+API sweep 152 passed.
- **PostgreSQL:** `SETTLEMENT_PG_URL=…` → **16 passed** (head + upgrade-from-prior-head; both insert-only triggers; concurrent version bumps; fail-closed in-flight proof; partial cancellation; delayed-fill-after-terminal invalidation; concurrent proof+work serialization; stale-snapshot ⇒ unknown; late-work staleness; deterministic digests). Skip proven without URL.
- **Cross-phase proof after the head-pin fix:** all six prior PG suites re-run green on head `…000029` — ownership 5/5, attribution 29/29, truth 13/13, proposals 16/16, admission 14/14, settlement 16/16.
- `tests/api` failure set identical to the documented 20-failure baseline; `git diff --check` clean.

## 2. Decisions / deviations (accepted)

- Work events also take the book advisory lock (plan named it for proofs only) — makes the concurrency test literal; correctness rests on version arithmetic (unit-pinned).
- Reconciliation integration flows through `reconciliation_service.py` (the collector builds the evidence); `reconciliation.py` carries the mapping.
- Rollup precedence pinned: failed ⇒ `unsettled` before unknown ⇒ `unknown`.
- Plans' terminality read through their reservation (`UNIQUE plan_id`); `strategy_plans` has no status column.
- **Closure fix (orchestrator):** all PG suites now assert `alembic_version >= <suite's own head>` (the `test_universe_kind_postgres.py` precedent) instead of exact pins — ends the per-phase head-pin churn; all suites re-proven green.

## 3. Limitations / NOT PROVEN

- Domain adapters ship as an empty registry — CNC (P6), MIS (P8), futures (P9), option `settled` (P10) interpret the axes later; broker auto-square-off is never proof.
- Settlement is evidence, not yet wired to any release action (consumers arrive with execution phases).
- Real broker snapshots / deployment NOT PROVEN; no live-market contact.

## 4. Gate summary

All roadmap Project 5 invariants and acceptance evidence Closed with executed
tests on real PostgreSQL. **Paper/live certification:** N/A this phase. Phase
gate: **PASSED**.
