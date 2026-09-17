# Hosted strategies — Project 4 parity matrix (admission, reservations, owner approval — G9+G10+G6)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 4](hosted-strategies-implementation-roadmap.md):
deterministic admission controls, the durable reservation lifecycle, and
account-owner-only approval records with structural validity. Plan:
`docs/superpowers/plans/2026-09-17-admission-reservations-approvals.md`
(R3 §6-D2/§8/§22 authored; design decisions D-1…D-9 binding).

> **Read this first.** **Closed** = implemented + covered by an executed test in
> this repo (database/concurrency claims proven on real disposable PostgreSQL).
> **NOT PROVEN** = requires separate authorization/infrastructure.

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Migration `20260917_000028` (single head, after `000027`; purely additive) | Closed — `strategy_admission_policies`, `strategy_reservations` (`UNIQUE plan_id`), `strategy_reservation_events` (insert-only trigger), `strategy_approvals` (partial unique `uq_approvals_plan_active WHERE status='active'`), `account_reconciliation_versions`; mirrored in `backend/schema.sql` |
| Deterministic admission (R3 §8, all eight controls) | Closed — ordered named refusals: `ADMISSION_POLICY_MISSING` (live policy row mandatory, `allocation_inr` required), `ALLOCATION_EXCEEDED` (fixed INR over attributed consumption + active reservations + plan), `INSTRUMENT_NOTIONAL_EXCEEDED`, `GROSS_NOTIONAL_EXCEEDED`, `MAX_OPEN_INSTRUMENTS_EXCEEDED`, `ORDER_RATE_EXCEEDED` (trailing window), `DAILY_LOSS_BUDGET_UNAVAILABLE` (honest fail-closed on live — no realized-loss source exists yet; paper enforces from paper fills), `CATALOG_INVALID` (reuses Phase 3 `plan_invalidation_state` — unrelated newer generation does NOT refuse), `SESSION_PRODUCT_INVALID`, `REFERENCE_PRICE_UNAVAILABLE`, `MARGIN_UNAVAILABLE`/`MARGIN_QUOTE_STALE` |
| Atomic capacity claim (invariant 1) | Closed — admission + reservation insert in one transaction under a per-account PG advisory lock; two plans, one capacity → exactly one reservation, loser `CAPACITY_EXCEEDED`; **negative test proves the lock is load-bearing** (lock no-op'd → two reservations) |
| Reservation lifecycle (R3 §8, verbatim) | Closed — `active → renewed → consumed | released | expired | action_required` with append-only event trail; unstarted expire with validity; renewal records events; terminal-unfilled releases; consumed NEVER releases on expiry (capital backing open positions stays); `action_required` disposition refuses `DISPOSITION_UNPROVEN` in V1; unstarted cancel releases atomically; no API can force-release active/consumed capacity (owner cannot steal capacity from active execution) |
| Owner approvals (R3 §6 D2) | Closed — actor = strategy's account owner only (worker tokens and foreign owners refused); binds plan_hash + account + exposure snapshot (`projection_version` + `content_sha256`; never-published = version 0/null) + `reconciliation_version` + catalog generation + active `reservation_id` + session/product snapshot + validity window; at most ONE active approval per plan (partial index; concurrent double-approval → one wins); re-approval supersedes; revoke is owner-only and terminal; paper/dry-run approve refuses `APPROVAL_NOT_REQUIRED` |
| Structural validity (D2) | Closed — `approval_structural_validity` lists **all** mismatched pins (`PLAN_HASH_MISMATCH`, `EXPOSURE_SNAPSHOT_CHANGED`, `RECONCILIATION_VERSION_CHANGED`, `CATALOG_RELEVANT_CHANGE`, `RESERVATION_NOT_ACTIVE`, `SESSION_PRODUCT_INVALID`, `APPROVAL_EXPIRED`); unrelated catalog change keeps validity; margin freshness re-checked inside validity |
| Margin freshness | Closed — `OrdersService.order_margins` evidence stored on the reservation with `as_of`; `ADMISSION_MARGIN_MAX_AGE_SECONDS` (default 60); stale/unavailable → `MARGIN_QUOTE_STALE`/`MARGIN_UNAVAILABLE` at admission and inside validity; a preview is not a reservation |
| Reconciliation version counter | Closed — bumped inside `reconcile_account` same-transaction, change-detected only; monotonic under concurrency (PG-proven); SAVEPOINT-safe upsert adopted after the race was found |
| Owner surfaces | Closed — 8 endpoints on `strategies.py`: policy PUT/GET, admission preview (not a reservation), reserve, approve, revoke, reservation/approval lists; owner-scoped incl. account authorization on plan-scoped writes |

Commits (unsigned, on `development`, not pushed): `9e27cb3` (schema), `f27eacc` (verdicts), `8c37f10` (reservations), `3594563` (approvals), `f74d58c` (owner surfaces), `8c87c61` (PG integration), `9296386` (verification fix).

## 1. Test evidence (executed 2026-09-17, this machine; counts independently re-run by the orchestrator)

| Suite | Result |
| --- | --- |
| `tests/strategies/test_admission.py` / `test_reservations.py` / `test_approvals.py` | 25 / 16 / 22 passed (red→green per task) |
| `tests/strategies` + binding/worker/hosted-router + `tests/sdk` | **777 passed, 2 skipped** |
| **PostgreSQL:** `ADMISSION_PG_URL=… pytest tests/integration/test_admission_approvals_postgres.py -q` | **14 passed** — head + upgrade-from-prior-head; **the capacity race incl. the lock-removal negative test**; lifecycle transitions under real PG; double-approval race; pin-mismatch matrix incl. unrelated-catalog validity; reconciliation-version monotonicity. Without a URL: **1 skipped — never fake-passed** |
| `tests/api` vs documented baseline | identical 20-failure set — no regressions |
| Head / `git diff --check` / leftovers | single head `20260917_000028`; clean; zero disposable databases left |

## 2. Defects and decisions (all reviewed and accepted by the orchestrator)

- **Task 7 scope-audit catch (real security gap):** plan-scoped writes lacked the account authorization check beyond owner+strategy — capacity could have been claimed on an unauthorized account. Fixed red-first and tested; this commit is what Task 7 existed for.
- **`REFERENCE_PRICE_UNAVAILABLE`:** Phase 3 compilers never emitted a reference price, so notional axes had unknown (not zero) requirements — contributing zero would have silently under-enforced configured limits. The compiler now carries `reference_price` into resolved legs and admission refuses when absent.
- **Two PostgreSQL-only defects found and fixed:** the Phase 3 plan-insert ordering, and `upsert_reconciliation`'s read-then-insert race under concurrent `reconcile_account` (now SAVEPOINT-safe, adopts the winner's row; a shared session re-raises instead of swallowing).
- **`APPROVAL_NOT_REQUIRED`** makes the paper/dry-run exemption explicit at the API rather than leaving it to callers.
- **`PUT/GET /admission-policy`** added — the plan omitted the surface that makes a mandatory live policy reachable.
- Endpoints take `execution_environment` defaulting to `live` (fail-closed; a live plan without a broker refuses `MARGIN_UNAVAILABLE`).

## 3. Known limitations and deferrals

- No execution path consumes verdicts yet (Project 6 wires paper execution) — pinned by the scope audit.
- Live attributed daily-loss enforcement honestly refuses (`DAILY_LOSS_BUDGET_UNAVAILABLE`) until a realized-loss source exists (Project 5+); the paper variant is enforced.
- "Once execution started" approval semantics are pinned as pure functions + tests; wiring is Project 6.
- `action_required` disposition refuses `DISPOSITION_UNPROVEN` until execution state exists.
- Auto-approval/delegation (Project 11), netted-margin redistribution, deterministic resizing — permanent V1 non-goals.

## 4. NOT PROVEN (requires separate authorization)

- Real broker margin quotes at scale; deployment/migration against any real database; no live-market contact this phase (no execution lane).

## 5. Gate summary

All roadmap Project 4 invariants and acceptance evidence are Closed with executed
tests, including the real-PostgreSQL capacity race with its load-bearing-lock
negative test. **Paper/live certification:** N/A this phase. Phase gate:
**PASSED**.
