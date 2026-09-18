# Hosted strategies — Project 6 parity matrix (paper single-instrument end-to-end, Foundation B)

Status 2026-09-17, branch `development`. Plan: `docs/superpowers/plans/2026-09-17-paper-execution.md` (D-1…D-7 binding). Executed in two delegations (interrupted once mid-Task-2; resumed from the exact commit/test state).

## Delivered
- Migration `20260917_000030` (single head; additive): `strategy_plan_execution_events` (insert-only trigger; per-step state derived from the trail).
- `PaperPlanExecutor` (backend/strategies/execution.py): fail-closed precondition chain with named refusals (`PLAN_KIND_UNSUPPORTED`, `PLAN_NOT_VALIDATED`, `RESERVATION_REQUIRED`, `PAPER_ONLY_EXECUTION`, `RESERVATION_EXPIRED`, `STRATEGY_RUN_BINDING_MISSING`, `ACCOUNT_SCOPE_MISMATCH`, `PLAN_ALREADY_EXECUTED`); zero-delta `no_op`; submission through the existing paper runtime with bound-run attribution + plan/reservation/step refs (G1 paper fold attributes fills to the strategy); lot flooring from the pinned catalog; exactly-one-submission per plan (threading + PG advisory lock).
- Reservation lifecycle closes the Phase 4 boundary on paper: fills `consume` capacity (with plan/order refs in the event), rejections `release` terminal_unfilled; consumed capacity cannot double-spend.
- `IntentBundleCompiler`: per-leg single-instrument resolution; `LEG_KIND_UNSUPPORTED` fail-closed; per-leg admission/events, no all-or-nothing.
- Owner surfaces: `POST .../plans/{plan_id}/execute`, `GET .../plans/{plan_id}/executions` (owner + account authorization preserved, `extra="forbid"`).
- Barrier/settlement observers: work events on submit/resolve; assessment recordable; staleness derived.

## Test evidence (independently re-run by the orchestrator)
- `tests/strategies/test_execution.py`: 30 tests; `tests/strategies` + owner-API suites: **453 passed, 1 skipped**.
- **PG:** `PAPER_EXEC_PG_URL=…` → **12 passed**: head + upgrade-from-prior-head; insert-only trigger; **the end-to-end acceptance — proposal → validated plan → paper admission + reservation → execute → paper fills → G1 attribution projection shows the strategy book → reservation consumed → settlement assessment recorded**; refusals by name; concurrency → exactly one submission/order/consumed event; skip proven without URL.
- `tests/api` failure set identical to the documented 20-failure baseline; single head; `git diff --check` clean.

## Defects/deviations (accepted)
Inherited Task-2 test arithmetic corrected (signed_quantity 100→10; the literal 100 made fills impossible); executor fixture registered the tables it exercises; schema-impossible scope-mismatch test replaced (composite FK makes the drift impossible) with the live-binding branch still covered; `PLAN_ALREADY_EXECUTED` keyed on a committed `submitted` event so refusal-only trails never block a corrected retry (both semantics pinned). No new migration needed; alembic/schema.sql untouched after Task 1.

## Limitations / NOT PROVEN
Paper-only (`PAPER_ONLY_EXECUTION`); no target_weights execution (P7); no partial fills (P7); execution is owner-triggered (scheduler P7); live enablement requires separate authorization. Real broker behavior/deployment NOT PROVEN.

## Gate summary
Roadmap Project 6 acceptance evidence Closed (end-to-end + named refusals). **Paper certification of the single-instrument lane: demonstrated by the PG end-to-end.** Phase gate: **PASSED**.
