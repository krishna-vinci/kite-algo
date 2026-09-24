# Phase A — recurring CNC portfolio and accounting: implementation report

Baseline: `eed55a1` (development), shared worktree `/home/krishna/kite-algo`.
Contract: `documents/hosted-recurring-portfolios-plan-2026-09-24.md` (Phase A).
Author: Flash implementation worker. Root Astra owns architecture, review and
integration; this report claims no acceptance.



## STATUS

**Accepted by root review** — correction round 4 delivered; the supervised
recurring acceptance passes with genuine manual authorization and a zero-free-cash
staged rebalance.

The evaluation-continuation mechanism, the exposure/financing corrections, the
exact-quantity basis validation and the **recurring momentum acceptance** are
implemented and verified. `momentum_recurring_sequence` (autonomous) and
`momentum_recurring_sequence_manual` drive ONE persistent strategy through four
fresh supervised child processes — entry → held completion → next evaluation
(a real no-op) → membership-driven sell/add rebalance → breadth exit — with the
bystander book untouched and no operator reconciliation. Two genuine
production-path defects the real run exposed were fixed (see round 3).

At worker completion nothing had been committed, pushed, deployed, or run against
a broker or production database. Root acceptance reviewed the final hashes,
evidence, migration, and focused tests before integration. Live staged financing
remains refused and is not claimed by this report.

## What shipped

### 1. Evaluation continuation (distinct from settlement; full `settled` unchanged)

- `backend/strategies/continuation.py` (new). A **separate** server-derived
  verdict for a FINITE evaluation that finished cleanly with an intentionally
  held book. It never reuses or relaxes the four-axis settlement rollup: the
  settlement rollup still calls an open book `unsettled`, and
  `trading_settled_flat` still requires flatness. An open book is reported as
  `held`, never flat/settled.
  - Eligibility (each a named refusal): finite job; normal completion
    (`exited` only); process cleanup `confirmed`; authority `revoked`; no
    in-flight work; no active recovery action; protection owner continuity
    (`active` in-flight protective exit blocks; `unknown` refuses); no
    outstanding discretionary approval/execution request; no unexplained
    reconciliation divergence; readable versioned barrier; fresh and complete
    published attributed book; exposure KNOWN.
  - `ContinuationService.attempt(...)` records the barrier proof (only when one is
    not already current) then clears the block through the existing CAS, pinned
    to owner, canonical/hosted strategy, account, environment, predecessor
    attempt/lease epoch/run, barrier version and projection version.
  - **Protection continuity:** a standing protection policy is refused by name
    (`CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED`) rather than silently
    transferred or disabled. The supported unprotected recurring path closes the
    predecessor run; an in-flight protective exit also blocks continuation.
- `backend/strategies/repository.py`: `get_blocking_job(...)` (the exact row
  `create_job` refuses on) and `report_completion(...)`; `reconcile_with_audit(...)`
  gained `outcome=` and `expected_projection_version=` (both defaulted, existing
  callers unchanged), so the unblock atomically validates the proof at the exact
  barrier version under the book lock AND pins the projection version.
- `backend/api/services/hosted_lifecycle.py`: the supervised `release` path now
  persists the runner's end-of-child report and, for a launched trade-capable
  attempt, attempts the automatic continuation **after** the fence (authority is
  already revoked). Only a clean exit is ever continued.
- `backend/strategies/supervisor.py` / `supervisor_api.py`: the runner reports
  `completion` (`exited` / `stop_requested` / `timeout`) with the release.
- Shared Run now / scheduled-job path: `backend/api/routers/strategies.py`
  (`_finish_predecessor_continuation`, called before `create_job`) and
  `backend/strategies/scheduling.py`
  (`finish_predecessor_continuation`, called before the pinned job insert) can
  finish the proof after a host restart, using the DURABLE
  `strategy_jobs.completion_state` report rather than re-deriving it.
- Audit storage is reused: the continuation is an
  `strategy_job_reconciliations` row with `outcome='continuation'`,
  `reason_code='CONTINUATION_ELIGIBLE'` and the pinned proof in
  `evidence_json.continuation_proof`. Refusals that look like a finished finite
  evaluation are recorded as `outcome='blocked'` with the named reason.

### 2. Additive migration (one head, no backfill)

- `backend/alembic/versions/20260924_000043_evaluation_continuation.py`:
  `strategy_jobs.completion_state` / `completion_at` (+ CHECK) and the widened
  `ck_strategy_job_reconciliations_outcome` (`'continuation'`). `backend/schema.sql`
  updated for parity; `StrategyJobReconciliation`'s model constraint widened.
  No destructive change; the downgrade narrows the CHECK, which the constraint
  itself refuses while continuation rows exist.

### 3. Exposure and financing

`backend/strategies/admission.py`:
- `plan_exposure(...)` derives post-plan quantities with the executor's own
  target/delta semantics (it calls `PaperPlanExecutor._opens_or_grows_exposure`,
  so there is one rule, not two). Unchanged held coordinates are included in the
  post-plan book, gross, per-instrument and open-instrument checks; a
  re-applied unchanged target is a zero-order, zero-additional-capital operation.
- Valuation is **per instrument** from the plan's pinned reference prices; the
  single-reference-price valuation of unrelated positions is gone. A required
  coordinate with no valid price is refused `POSITION_VALUATION_UNAVAILABLE`
  (new named refusal), never valued as zero.
- The enforced requirement is **incremental funding** (increasing legs only), so
  an unfilled sale funds nothing and cannot become headroom for a replacement
  buy.
- Every read is **environment-scoped** (`execution_environment`), so a paper plan
  can never see or be sized against the live book.
- The allocation check is the strategy's own budget against that strategy's
  attributed book + pending commitments + incremental funding. `consumed`
  reservations no longer hold capacity (the published position already carries
  the exposure), which removes the double-charge; the consumed history stays in
  the ledger and in admission evidence (`consumed_history_inr`).
  Evidence now distinguishes current exposure, desired post-plan exposure,
  pending commitments, incremental funding and (unchanged) account
  availability/margin.

### 4. Exact-quantity basis validation

`backend/strategies/proposals.py` `_compile_payload`: an exact-quantity proposal
(`intent_bundle`) that STATES `capital_basis_inr` is validated against the owner's
recorded allocation — `CAPITAL_BASIS_MISMATCH` / `CAPITAL_BASIS_INVALID` by name,
no plan written. Omitting the field preserves the existing governed behaviour
byte-for-byte (weights keep their existing freeze semantics).

### 5. Frontend

- `frontend-next/features/strategies/hooks/use-hosted-strategies-queries.ts`:
  the strategy job list now polls (5s) while any visible attempt is moving, so a
  job that was `queued` at first paint cannot keep showing "Queued" after the
  server already reports its terminal state. This is the authenticated deployed
  finding root reported.
- `frontend-next/features/strategies/lib/modes.ts`: `MOVING_JOB_STATUSES` /
  `anyJobStillMoving(...)` (the polling decision, unit-tested).
- `frontend-next/features/strategies/lib/format.ts`: `blockOutcomeLabel(...)`
  labels `continuation` as "Continued automatically (book held, not flat)"; the
  `STRATEGY_BLOCKED` copy now says a healthy finite run clears itself.
- `frontend-next/features/strategies/components/hosted-job-detail-page.tsx`: the
  reconciliation history uses the labels and, when a continuation row exists,
  states that the book is **held, not flat**.

## Verification (exact commands and results)

Environment: this sandbox blocks raw sockets and thread-pool dispatch. PostgreSQL
suites therefore ran escalated against the **disposable test server on
127.0.0.1:15433 only** (production 15432 was never contacted). Suites were run
with a test-time `asyncio.to_thread` shim (`PYTHONPATH=/tmp -p patch_to_thread`);
without it, any suite whose code path calls `asyncio.to_thread` hangs inside the
sandbox's `ThreadPoolExecutor` dispatch, provably independent of this change set
(replacing `to_thread` with a direct call makes `test_account_truth` pass in
0.56s; the same test hangs unmodified). CI/normal hosts need no shim.

| Command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/strategies/test_continuation.py tests/strategies/test_reconciliation.py tests/strategies/test_settlement.py tests/strategies/test_admission.py tests/strategies/test_reservations.py tests/strategies/test_proposals.py tests/strategies/test_repository.py tests/strategies/test_scheduling.py -q` | 0 | `300 passed, 14 subtests passed` |
| `.venv/bin/python -m pytest tests/strategies -q` (7 heavy files excluded, then the rest with the shim) | 0 | `236 passed` + `597 passed, 1 skipped` + `7 passed` (dispatcher, no shim) |
| `npx vitest run features/strategies` (frontend-next) | 0 | `11 files, 83 tests passed` |
| `npx tsc --noEmit` (frontend-next) | 0 | clean |
| `ALERTS_TEST_DATABASE_URL=…15433/kite_test pytest tests/integration/test_admission_approvals_postgres.py -q` | 0 | `14 passed` (also proves `alembic upgrade head` applies the new revision) |
| `… pytest tests/integration/test_hosted_strategy_foundation_postgres.py tests/integration/test_settlement_barrier_postgres.py tests/integration/test_reconciliation_barrier_toctou_postgres.py -q` | 1 | `28 passed, 2 failed` — both failures pre-existing and unrelated (see below) |
| `… CONTINUATION_PG_URL=…15433/kite_test pytest tests/integration/test_evaluation_continuation_postgres.py -q` | 0 | `8 passed` (new) |

### New continuation integration coverage (`tests/integration/test_evaluation_continuation_postgres.py`)

Real repository + real `ExecutionBarrier` + real `hosted_lifecycle.release` on a
disposable, migration-built PostgreSQL database:

- a clean finite exit with a held book clears its own block, writes a
  `continuation` audit row whose proof pins attempt/epoch/run/barrier
  version/projection version and `held: true`, and the next `create_job`
  succeeds with no operator reconciliation;
- `stop_requested`, `timeout` and an unreported exit never clear the block and
  `create_job` still raises `StrategyFenceError`;
- unknown process cleanup never clears the block;
- an unexplained reconciliation divergence never clears the block;
- an outstanding execution request never clears the block;
- the shared Run now path finishes the proof after a host restart using the
  durable `completion_state` marker.

### Pre-existing failures (not caused by this bundle)

`tests/integration/test_hosted_strategy_foundation_postgres.py`:

- `test_migration_constraints_reject_invalid_rows` asserts that
  `strategy_jobs.execution_mode='live'` raises. It cannot: the unmodified,
  already-committed migration `20260922_000039_hosted_live_mode.py` widens
  `ck_strategy_jobs_execution_mode` to include `'live'`. The test is stale at
  `eed55a1`; this bundle never touches `execution_mode`.
- `test_migration_downgrade_is_clean` fails for the same reason: the downgrade
  path re-narrows that constraint (via `000039`'s downgrade) while a `'live'` row
  exists in the module-scoped temporary database.

Both are reported rather than "fixed", because changing them would alter an
unrelated migration's asserted semantics.

## Source hashes (sha256) of the changed/added backend files

Current as of the end of correction round 2.

```
8516cb2fc70a2846135a96a932bf42de072c0f933b33ca1318fcb82d39ae52be  backend/strategies/continuation.py
fa0aa2650e64c3983b510afd090f3f1955e73646dffffe58db12714e21c5b471  backend/strategies/financing.py
f7fab391df7521714f8b16b01714922c404d48b478d8532f201628b7c5cedf8d  backend/strategies/admission.py
b7371e7dd6f9ae0488ebee602eb408e0d58a5d8a60543cafdc1448810b7cdcd0  backend/strategies/reservations.py
601520b49da0d4524485e5e3aef5d43894734d72a690ca6c078e3e8d9e87efd4  backend/strategies/plan_pipeline.py
a9927e41eeedab051f9566f1db8412fe86e461946f7317dfe4b175da3da40a17  backend/strategies/execution.py
c33a42d42fab23574083fb323555afbaeb098ed9d945a06cddae140d628749eb  backend/strategies/repository.py
430a87f90dca478091ff21cbc2c314566ee1c718e9b824e8c7818f27c1c26530  backend/strategies/models.py
42c1b580a8b86d8263201513402c911eb79463c8d29e585f5e77dc6e6f1bd2a9  backend/strategies/proposals.py
0c6bb63b9f2c0ed1b909ccf9dc9edc57596e03a88374f8978ecde9b4da77212f  backend/strategies/scheduling.py
bf8e67965679e67388cf3bce261d6efe3a5b4918016525486c04e6ff4d4899a1  backend/strategies/supervisor.py
c0bfb15a267302244b1b0afbd7c0dbdbea1c2f718321fc31f12cee638513be60  backend/strategies/supervisor_api.py
01e4e80fc6ff90e224adce07b7c84d985cc4b47c978746ec2df8fa94e5e89ec7  backend/api/services/hosted_lifecycle.py
b0800482a838227ffc6395ee46f25b0b9e1d42847a1ebd1e3836ddaf4f793631  backend/api/routers/hosted_lifecycle.py
10c47ee06220c1e986dbaea606b2f8c49644fb957a3ce613746eb4f19f413695  backend/api/routers/strategies.py
4f8faddf84021cd165c1710e27022d97cf65a0aa1bd9928c2a924a154de8ae96  backend/api/schemas/hosted_lifecycle.py
0644ef0ee68ab950cc7786c8ff3116ca0da70599eee881e269938ffaaf575d2f  backend/alembic/versions/20260924_000043_evaluation_continuation.py
452e4c7c46955a0a66270d7794afa98c3b45ffd990f0e98812884f8e1add5ddf  backend/schema.sql
```

Frontend and test files changed: `frontend-next/features/strategies/{hooks/use-hosted-strategies-queries.ts,
lib/modes.ts, lib/modes.test.ts, lib/format.ts, components/hosted-job-detail-page.tsx,
components/hosted-strategy-detail-page.test.tsx}`, `tests/strategies/{test_continuation.py (new),
test_admission.py, test_proposals.py}`, `tests/integration/test_evaluation_continuation_postgres.py (new)`.

## Unrelated work preserved

Every pre-existing modification and untracked file in this shared worktree was
left untouched, including `documents/hosted-strategies-architecture-r1.md`, the
`.commandcode/` and `documents/*r3*` drafts, and the accumulated
`examples/hosted_platform/evidence/*.json`. The production job root flagged
(and its data) was never read or written; no production database, credential or
environment was touched.

## Superseded first-round remaining scope

The items below were the first review's open scope. Correction rounds 2–4
delivered the recurring supervised acceptance, corrected the protection boundary,
and re-ran the relevant suites; they remain only as the review trail.

1. **Momentum example + harness recurring evidence** (the largest remaining
   item): `examples/hosted_platform/nifty500_momentum.py` must re-adopt the
   durable book and emit a full-snapshot `intent_bundle` whose removals are zero
   targets and whose unchanged names are no-ops; `run_phase5_acceptance.py` needs
   one persistent strategy across entry → held completion → next job → no-op →
   sell/add rebalance → breadth exit, plus manual + autonomous, a restart between
   evaluations, a bystander book check, and the race matrix (duplicate Run
   now/scheduler, stale authority, late fill, unknown cleanup/book, partial sale,
   concurrent capacity claims). No harness-only seeding of the book.
2. **Live-adapter protection-continuity tests** with broker fakes (in-flight
   protective exit blocks continuation; the run is left open when protection is
   installed).
3. Re-run the settlement/reconciliation PostgreSQL suites once more after the
   harness work, and the full `tests/strategies` + `tests/integration` set in an
   environment without the sandbox's thread dispatch restriction.
4. Phase C (authenticated deployed UI pass) stays with root; the polling/label
   change here is the frontend half and is verified by unit tests only, not by a
   signed-in browser.

## Limits and risks

- No broker order of any kind was placed; every order studied is paper inside a
  disposable database. Live behaviour is argued from code and fakes only.
- The continuation verdict is new; it is exercised end to end on PostgreSQL but
  the acceptance path through a supervised child is not yet run.
- Admission semantics changed deliberately (incremental funding, per-instrument
  valuation, environment scoping, consumed no longer holding capacity). Existing
  admission unit tests were updated to the new contract, and no test was
  weakened to pass: each changed expectation states the new invariant.
- `strategy_jobs.completion_state` is a new column; a job fenced by `fence`,
  `expire` or `recover` has `NULL` there and is therefore never auto-continued.

---

## Correction round 2 (after root reviewed the first patch)

Every item below was implemented and re-verified. Test runs are now made
**escalated, without the `asyncio.to_thread` shim**, so real thread and socket
behaviour is exercised rather than simulated.

### 1. Supervisor completion truth (root item 1)

- `backend/strategies/supervisor.py` now sends the runner-observed `exit_code`
  with the release, and records `clean_exit = (outcome == "exited" and exit_code == 0)`
  locally. A non-zero or signalled exit is reported truthfully instead of being
  flattened into `exited`.
- `repository.report_completion(...)` is now fenced by the FULL attempt authority
  (`id + lease_owner + lease_epoch + attempt`, via `_authority_clause`) and is
  **write-once**: a later report may only fill in a MISSING exit code for the
  SAME completion value. An unsafe outcome therefore can never be laundered into
  `exited`/`0`, and a stale runner cannot report at all.
- `reconcile_with_audit(..., require_clean_completion=True)` revalidates
  `completion_state == 'exited' AND exit_code == 0 AND desired_state == 'started'`
  INSIDE the unblock transaction, so an operator stop that lands after the
  runner's report, or a completion row that moved under the assessment, fails the
  CAS instead of clearing the block.
- `ReleaseRequest`/`ActionResponse`/`SupervisorApiClient.release` carry the new
  field; `ContinuationEvidence` gained `exit_code` and `desired_state` (both in
  the digest and the pinned proof).

### 2. Financing contract (root item 2)

- New `backend/strategies/financing.py` holds the ONE capacity rule used by both
  gates: strategy-and-environment-scoped held capacity (unfilled commitments plus
  `consumed` reservations whose exposure is NOT yet visible in the published
  book), a separate account-wide helper, `free_headroom_inr` and
  `staged_funding(...)`.
- `reservations.claim` no longer sums account-wide `consumed`+held against one
  strategy's allocation. It enforces (a) the strategy's own budget in its own
  environment and (b) the ACCOUNT's actual funds across strategies when the
  caller supplies authoritative evidence, both under the existing account
  advisory lock. `plan_pipeline.reserve` passes the account figure from the
  admission evidence, so there is one evidence source.
- Admission's budget test is now on the **desired post-plan book** (`ALLOCATION_EXCEEDED`
  uses post-plan exposure + pending commitments), with the pre-plan number kept
  only as evidence. A fully-allocated sell-A/buy-B rebalance is therefore
  ADMITTED, and the part of its funding not covered by free headroom is reported
  as `funding_shortfall_inr` / `requires_staged_financing` — a requirement, never
  a credit for a projected sale.
- `execution.PaperPlanExecutor` implements the staged release on the non-option
  lane: reductions are ordered first and any dependent increase is refused by
  name (`FINANCING_UNSECURED`, naming the unresolved funding legs and their
  recorded events) unless every reduction it depends on recorded `filled`/`no_op`.
  A partial, failed, rejected or unobserved sale cannot fund the replacement.
- Test: `tests/strategies/test_execution.py::ExecutorStagedFinancingTests`.

### 3. `plan_exposure` correctness (root item 3)

- `per_instrument` now covers EVERY post-plan coordinate, not just the plan's
  targets, so unchanged held names are measured by the per-instrument and gross
  limits.
- Sizing mirrors the executor: signed quantities are used as the instruction and
  the DELTA is floored to the pinned lot; weight legs are sized with the
  executor's own `weight x basis x (1 - buffer) / price` arithmetic and floored
  to the pinned lot; weight legs are long-only for the delta.
  A contract test asserts admission's `order_quantity` EQUALS
  `PaperPlanExecutor._plan_steps` for a lot-floored weight leg.
- Raw (unattributed) projection facts are no longer counted and dropped: they are
  reported with reason `unresolved_projection_fact` and refuse the plan
  (`POSITION_VALUATION_UNAVAILABLE`).
- The `consumed` exclusion is now publication-evidence based (see item 2), not an
  assumption: `consumed_unpublished_inr` / `consumed_published_inr` are separate
  evidence fields and only the latter stops holding capacity.

### 4. Continuation book state (root item 4)

- `_book_state` requires the platform's OWN publication marker
  (`strategy_projection_state.last_rebuild_at`) — an unpublished book is not flat.
- A published book containing an `identity_kind='raw'` fact is `incomplete` and
  refuses with the new `CONTINUATION_BOOK_INCOMPLETE`; for `live` the account
  ingest cycle must be idle with a completion stamp, mirroring the platform's
  account-truth policy.
- `unresolved_identity` is now consumed (it was computed and dropped before).

### 5. Protection ownership (root item 5)

- Demonstrated with the PRODUCTION reader: `_list_protection_enabled_runs`
  selects every run whose own status is `open`/`exiting` AND whose
  `runtime_state.backend_protection.enabled` is true, and the protection reader's
  gate requires `run_status == "open"`. Ownership is therefore **run-scoped**,
  and leaving a predecessor run open while a successor exists is two owners, not
  continuity.
- Consequently continuation **refuses by name** when a standing protection policy
  is installed (`CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED`), and the
  predecessor run is now CLOSED on the supported path (no protection installed)
  instead of being left open. This clearly separates the supported recurring
  `keep_positions` case (`stale_exit_policy = 'none'`) from the unsupported
  protected case. Active protection is never disabled to make a continuation pass.
- Tests: `test_a_standing_protection_policy_refuses_by_name` (unit) and
  `test_protection_ownership_is_run_scoped_so_two_runs_are_two_owners` (PostgreSQL,
  two protection-enabled runs plus a disabled control).

### 6. Approval semantics (root item 6)

- The blanket "any active `StrategyApproval` blocks" check is gone. The authority
  is the durable EXECUTION REQUEST for the predecessor run: a request whose
  status is not `executed`/`refused`/`rejected` is unfinished work, and
  `dispatch_unresolved` is deliberately INCLUDED as blocking because its broker
  outcome is unknown.
- Tests: `test_a_standing_active_approval_does_not_block_a_finished_evaluation`
  and `test_a_dispatch_unresolved_request_blocks_even_though_it_is_a_final_state`
  (PostgreSQL).

### 7. Stale live-mode assertion (root item 9)

`tests/integration/test_hosted_strategy_foundation_postgres.py` asserted that
`execution_mode = 'live'` is rejected. Migration `20260922_000039_hosted_live_mode`
(committed, unmodified) widened that constraint to include `live`, so the
assertion was stale and its stray `live` row also broke the module-scoped
downgrade test. Corrected to an actually-invalid mode (`continuous`) with an
explicit comment; migration semantics were NOT changed. Both tests pass now.

### Correction-round verification (exact)

| Command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/strategies/{test_continuation,test_admission,test_execution,test_reservations,test_settlement,test_reconciliation,test_proposals,test_repository,test_scheduling,test_service,test_approvals}.py -q` (in sandbox, no shim needed) | 0 | `410 passed, 14 subtests passed` |
| **escalated, no shim** `.venv/bin/python -m pytest tests/strategies/test_supervisor.py tests/strategies/test_lifecycle_prepare.py -q` | 0 | `61 passed` (real threads) |
| **escalated, no shim** `CONTINUATION_PG_URL=…15433/kite_test pytest tests/integration/test_evaluation_continuation_postgres.py -q` | 0 | `17 passed` (real threads + real sockets) |
| **escalated, no shim** `… pytest tests/integration/test_settlement_barrier_postgres.py tests/integration/test_reconciliation_barrier_toctou_postgres.py tests/integration/test_hosted_strategy_foundation_postgres.py -q` | 0 | `30 passed` (includes the corrected live-mode assertion) |
| **escalated, no shim** `ADMISSION_PG_URL=…15433/kite_test pytest tests/integration/test_admission_approvals_postgres.py -q` | 0 | `14 passed` |

The continuation PostgreSQL suite now covers: the healthy handover end to end
(clean exit 0 + held book clears its own block and the next job starts);
`stop_requested`, `timeout`, unreported exit, and `exit_code` in
`{1, 2, -15, None}` all keeping the block; a stop race with a clean exit code
keeping the block; a stale/foreign completion report being refused (write-once);
unknown process cleanup; unexplained divergence; an outstanding request;
`dispatch_unresolved` blocking; a standing `active` approval NOT blocking; and
run-scoped protection ownership.

### Remaining (unchanged from the first report)

The **momentum adapter + real supervised-child persistent-sequence acceptance**
(entry -> held completion -> next evaluation -> no-op -> sell/add rebalance ->
breadth exit, manual and autonomous, restart, bystander book untouched, plus the
targeted race tests) is still NOT delivered. The focused PostgreSQL fixtures in
this report are not a substitute for it. That is the next checkpoint.

---

## Correction round 3 (root's follow-up financing findings + the supervised acceptance)

Every item below was implemented and re-verified. The acceptance deliverable that
rounds 1–2 left open is now **delivered and green** on a disposable PostgreSQL
server (port 15433 only; production 15432 was never contacted).

### A. Financing: consumption evidence (root A)

`backend/strategies/financing.py` already keys a `consumed` reservation's release
on an **immutable `consumed` EVENT timestamp** (`_consumption_times`) plus a
**complete publication at-or-after that instant** (`_publication`), never on the
reservation row's `created_at` — a rebuild that lands between creation and the
fill therefore proves nothing and the reservation keeps holding budget. This is
pinned by `tests/strategies/test_admission.py::{test_consumed_capacity_is_history_not_a_second_charge,
test_a_consumed_reservation_without_a_consumption_event_still_holds}`.

`account_capacity_held_inr` is deliberately narrower (unfilled commitments only)
and **the omission of `consumed` is the correct behaviour, not a gap**: the
account figure it is tested against is the broker's *currently available* money,
from which the spent cash has already left. Adding consumed rows here would
subtract the same cash twice. The per-strategy budget (which IS spent by a fill
whether or not the book has caught up) uses `capacity_held(...)["committed_inr"]`
instead. Both numbers are returned as explicit evidence fields.

### B. Reservation claim revalidates under the lock (root B)

`backend/strategies/reservations.py::claim` now enforces TWO independent gates,
both under the account advisory lock and both from freshly-read rows:

1. `held + requirement <= allocation` (the ledger's own necessary hold), where
   `held` is the strategy-and-environment-scoped unfilled commitments plus any
   consumed-not-yet-published reservation;
2. the **POST-PLAN book** revalued from the PERSISTED `strategy_plans` row and the
   current attributed book, plus the OTHER unfilled commitments.

The scope of gate 2 is the CNC lane (`execution.CNC_REBALANCE_PLAN_KINDS`), the
same scope the executor uses for staged financing, so a futures roll that
deliberately carries both contracts at claim time and an option structure that is
measured by its own run are not charged generic portfolio arithmetic (root D).

New regression: `tests/strategies/test_reservations.py::CapacityClaimTests::test_two_individually_admitted_cnc_plans_cannot_exceed_the_budget`
(current 8000 against a 10000 budget; two plans that each buy 1500 are
individually admissible, and the ledger refuses the second under the lock with
`post_plan_inr=9500`, `other_unfilled_inr=1500`).

### C. Staged financing is genuine (root C)

- `backend/strategies/admission.py`: a CNC rebalance that both REDUCES and
  INCREASES exposure is no longer refused because the account's cash is short of
  the whole incremental requirement before the reductions execute. The shortfall
  is recorded (`staged_financing_shortfall_inr`) and the plan is admitted; it is
  never credited with a projected sale. A buy-only plan with no free cash still
  refuses `MARGIN_UNAVAILABLE`.
- `backend/strategies/execution.py`: the generic sell-before-buy ordering stays
  restricted to CNC shapes and is disabled whenever a domain ordering exists
  (`roll_ref`), so the futures acquire-first roll and the options hedge-first
  order are untouched. A dependent buy is released only against a CONFIRMED
  reduction event.
- The **actual money** is revalidated per order under the paper runtime's own
  lock (`backend/paper_runtime/service.py` rejects an order whose
  `required_cash > available_funds`), so a dependent buy that a reduction did not
  actually fund is refused by the runtime rather than passing on an event name.
- `backend/strategies/financing.py::plan_exposure` now iterates the FULL
  coordinate set, so a coordinate CLOSED to zero is visible as a negative order
  quantity (it is what funds a staged rebalance), and a coordinate whose
  POST-plan quantity is zero no longer needs a price.

New regressions: `tests/strategies/test_admission.py::{test_a_fully_allocated_rebalance_with_no_free_cash_is_staged_not_refused,
test_a_buy_only_plan_with_no_free_cash_is_still_refused}`.

### D. Two production defects the supervised run exposed (both fixed)

1. **A durable JSON write could not carry a `datetime`.** Admission's evidence
   carried a raw `projection_published_at`, so on PostgreSQL the dispatch's
   `UPDATE hosted_execution_requests … execution_detail::JSON` raised
   `(builtins.TypeError) Object of type datetime is not JSON serializable`, the
   request fell to `dispatch_unresolved` and the child exited 2. Fixed at the
   source (`admission.py` emits ISO) and guarded at the durable boundary
   (`execution_requests._json_safe` coerces datetimes/dates/Decimals, so an
   UNKNOWN broker outcome can never go unrecorded).
2. **The momentum adapter submitted delta-only bundles.** Root's plan requires a
   full-snapshot `intent_bundle`; `nifty500_momentum._legs_for_targets` now emits
   EVERY coordinate (unchanged names as no-op legs, removals as zero targets),
   while `changes` remains the smaller truth that decides whether there is work.
   Without this the platform could not value the post-plan book and refused the
   rebalance `POSITION_VALUATION_UNAVAILABLE`.

### E. The supervised recurring acceptance (the delivered deliverable)

`examples/hosted_platform/run_phase5_acceptance.py` gained two scenarios and one
runner (`run_momentum_recurring_scenario`), plus `MOMENTUM04` (a name that is NOT
a member at entry and becomes one later, so the rebalance has a genuine ADD
beside its REMOVAL) and a `members=` override on `MomentumFixture`. Each
evaluation is a **fresh supervised child process** on the SAME durable strategy
and frozen version, and the book each one reads is the one the previous one
actually filled through the production rebuild — the book is never seeded.

| Command (escalated, no shim) | Exit | Result |
| --- | --- | --- |
| `python examples/hosted_platform/run_phase5_acceptance.py --only momentum_recurring_sequence --timeout 200` | 0 | `ok: true` — 4 evaluations, 10 orders, bystander untouched |
| `… --only momentum_recurring_sequence_manual --timeout 200` | 0 | `ok: true` — 4 evaluations, 10 orders, 4 live owner approvals |
| `… --only momentum_manual_entry,momentum_autonomous_entry,momentum_breadth_exit,momentum_mid_month_deferral` | 0 | `ok: true` — the four pre-existing momentum scenarios unregressed |

Observed sequence (from the evidence file):

- **entry** — 4 BUYs (51 MOMENTUM00/01/02/03); book = the membership; child exit 0.
- **noop** — ZERO orders, ZERO execution requests, book byte-identical; the child
  logged `no action: the book already matches the momentum target`; exit 0.
- **rebalance** — SELL 51 MOMENTUM00 + BUY 51 MOMENTUM04; the book moves to
  `{01,02,03,04}` and nothing else trades; exit 0.
- **exit** — breadth 0/4 → SELL 51 each of 01/02/03/04; the strategy's own book
  ends EMPTY; exit 0.
- **bystander** — `[BYSTANDER 7]` before and after, byte-identical.
- **no operator reconciliation**: each job's reconcile POST returned
  `HOSTED_JOB_NOT_BLOCKED`, and four `CONTINUATION_ELIGIBLE` audit rows
  (`audit_id hsr_…`, `held: true` for the three held handovers and `held: false`
  for the final flat one) carry the pinned `barrier_version` / `projection_version`.
  The runner FAILS the scenario if a reconciliation is ever required, so this is
  asserted rather than observed.

### F. Rounds 1–2 corrections re-verified in this round

| Command (escalated, no shim) | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/strategies -q` | 0 | `859 passed, 1 skipped, 38 subtests passed` |
| `.venv/bin/python -m pytest tests/strategies/test_nifty500_momentum_source.py tests/strategies/test_hosted_starter_source.py tests/strategies/test_hosted_option_example.py -q` | 0 | `80 passed` |
| `CONTINUATION_PG_URL=…15433/kite_test pytest tests/integration/test_evaluation_continuation_postgres.py tests/integration/test_admission_approvals_postgres.py -q` | 0 | `31 passed` |
| `… pytest tests/integration/test_settlement_barrier_postgres.py tests/integration/test_reconciliation_barrier_toctou_postgres.py tests/integration/test_hosted_strategy_foundation_postgres.py -q` | 0 | `30 passed` |
| `npx vitest run features/strategies` (frontend-next) | 0 | `11 files, 83 tests passed` |
| `npx tsc --noEmit` (frontend-next) | 0 | clean |

### G. Known, pre-existing and unrelated

`tests/strategies/test_execution.py` leaves the deprecated
`asyncio.get_event_loop()` without a loop for `tests/strategies/test_execution_dispatcher.py`
when the two run in the same pytest session; the dispatcher file passes on its own
(`7 passed`) and alongside every other file. Any `IsolatedAsyncioTestCase` in
`test_execution.py` triggers it (the legacy `ExecutorPreconditionTests` does too),
so this is a test-harness ordering artifact, not a regression from this bundle.

### H. Limits and risks (unchanged in kind)

- No broker order of any kind was placed; every order is paper inside a
  disposable database. Live behaviour is argued from code and fakes only. **The
  live adapter is unchanged, so unsupported staged LIVE financing still refuses
  by name and live is not claimed ready.**
- The child log line `thread pool unavailable (RuntimeError); reading
  sequentially` is the momentum example's own degrade path inside this sandbox;
  the sandbox's thread dispatch restriction is why the suites above were run
  escalated.
- Protection continuity is the bounded limit root accepted:
  `CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED` refuses by name when a standing
  protection policy is installed, and the supported recurring shape used here is
  `stale_exit_policy = 'none'`. No active protection is ever disabled.
- Phase C (authenticated deployed UI pass) stays with root; the polling/label
  change here is verified by unit tests only.

### I. Source hashes (sha256) of the files this round changed

```
34e66e8abec74d071bbfafc003f76da425fc9887e721567f573a7c856358fcff  backend/strategies/financing.py
fed018857b26480eaf6fb4d2f8ee1aa85e7f5e00205eb352b005c3cc0414ae8c  backend/strategies/reservations.py
ccb812a812f368f04dbdbc3cf5a39ddcd7a4ccd941dd51cbf011dd12e6bf8bbd  backend/strategies/admission.py
ad1288e5084bbd93bc99e562c0d58bf05f5bddd70355aae525e7b63322bda1de  backend/strategies/execution.py
00df909d96b079b51eb546c64675e7fc08e9139d9f8abbc45cca4b738fa76c08  backend/strategies/execution_requests.py
55b143dd5fc1394758da56c839dd409093686cee12d2ef503fad46a1bd92c130  examples/hosted_platform/nifty500_momentum.py
33cdc31e4d9b34743d361a79460502e68201772779fc336cf2488a45ba255d00  examples/hosted_platform/run_phase5_acceptance.py
```

Also changed this round: `tests/strategies/test_reservations.py` (fixture repaired
after the interrupted edit, plus the CNC over-budget regression),
`tests/strategies/test_admission.py` (two staged-financing regressions).

---

## Correction round 4 (root's two precise acceptance blockers)

Both blockers are fixed and re-verified. The earlier claim that
`phase5-20260924T102902Z.json` was a MANUAL run is **superseded and withdrawn**:
that artifact's requests were `authorization_mode=autonomous`,
`decision_kind=automatic` (the runner installed an autonomous grant regardless of
the scenario). The corrected manual artifact is
`phase5-20260924T110301Z.json`.

### 1. The manual scenario is genuinely manual (blocker 1)

`examples/hosted_platform/run_phase5_acceptance.py` now branches the setup on the
scenario (`if spec["autonomous"]`): a manual scenario installs NO autonomous mode
and NO grant. The runner additionally FAILS a manual scenario unless, for every
trading evaluation:

- at least one owner approval reached the platform **while the attempt that
  waited for it was still alive**;
- no NEW paper order existed at that moment (the baseline is the count at the
  start of that evaluation, not zero — earlier evaluations in the same persistent
  strategy have their own fills);
- every trading request is `authorization_mode='approval_based'` AND
  `decision_kind='manual'`.

Corrected evidence (`momentum_recurring_sequence_manual`, `ok: true`):

| phase | approvals | requests (mode / decision) | orders |
| --- | --- | --- | --- |
| entry | 1 | `executed` / `approval_based` / `manual` | 4 BUYs |
| noop | 0 | none at all | none |
| rebalance | 1 | `executed` / `approval_based` / `manual` | SELL 51 M00 + BUY 51 M04 |
| exit | 1 | `executed` / `approval_based` / `manual` | 4 SELLs, own book flat |

### 2. Zero-free-cash rebalance is a real, reserved, phase-scoped flow (blocker 2)

The reservation contract is now genuinely PHASE-SCOPED instead of refusing a
staged rebalance up front:

- `ClaimRequest.staged_increase_inr` (set by `plan_pipeline.reserve` from the
  admission evidence) marks the part of a staged CNC plan's requirement that its
  OWN reductions fund. `ReservationLedger.claim` compares only the IMMEDIATE cash
  (`requirement - staged_increase_inr`) against `account_capacity_inr`, so a plan
  that spends nothing before it sells can be claimed **without dropping the
  account cap** for anything else. The deferral is recorded durably on the
  `created` event.
- `ReservationLedger.authorize_staged_increase` is the second half: immediately
  before an increase is submitted, the executor supplies the account's CURRENT
  authoritative available funds and the ledger re-derives, under the same account
  advisory lock, whether the money is really there — counting every OTHER
  unfilled reservation on the account/environment and excluding only this plan's
  own. It refuses `CAPACITY_EXCEEDED` (`scope=account_funds_increase`) otherwise,
  is idempotent by amount, and records the authorization on the existing
  `advanced` event (`staged_increase_authorized: true`, with the money it used) —
  no new event vocabulary, **no migration**.
- `PaperPlanExecutor._authorize_staged_increase` reads the paper runtime's own
  summary and refuses the dependent buy `ACCOUNT_FUNDS_UNSECURED` when the figure
  is unknown, unreadable, missing a reference price, or short. Nothing projected
  is ever credited.

New real-account acceptance: `momentum_recurring_zero_cash_rebalance`
(`ok: true`, `phase5-20260924T110544Z.json`). Entry at budget 1,485,000 buys 150
shares of each of four members (~900,000 of the account's 1,000,000). The next
evaluation must SELL the dropped member and BUY the added one with
`paper_available_before_inr = 99730.0` against an increase that actually cost
`increase_notional_inr = 225000.0`. The runner reads the paper account's own
`available_funds` row and FAILS unless free cash is strictly below the increase,
and it prices the increase from the **actual fill** (`paper_orders.average_price`)
rather than the plan's frozen reference price. 6 orders, bystander untouched.

Targeted regressions:

- `tests/strategies/test_reservations.py::CapacityClaimTests::test_a_staged_claim_defers_its_increase_and_authorizes_it_later`
  (claim with `account_capacity_inr=0` succeeds and records the deferral; the
  increase is then REFUSED at 0 money and authorized exactly once at 2,000) and
  `::test_an_unstaged_claim_still_enforces_the_account_cap` (the control: the same
  zero-cash claim without staging is still refused).
- `tests/strategies/test_execution.py::ExecutorStagedFinancingTests::test_a_partial_sale_cannot_fund_the_dependent_buy`
  (a half-filled removal funds nothing and the buy never reaches the runtime),
  `::test_an_increase_is_refused_when_the_account_cannot_carry_it`
  (`ACCOUNT_FUNDS_UNSECURED`, `scope=account_funds_increase`), and
  `::test_an_increase_proceeds_only_against_confirmed_account_money` (the durable
  authorization carries `account_capacity_inr`).

### 3. Lane scope and the live boundary

- The staged lane now requires **every** leg's product to be `CNC`, in both
  admission and the executor. An `intent_bundle` carrying MIS/NRML legs is NOT
  classified as a staged CNC rebalance and is not reordered
  (`tests/strategies/test_admission.py::OptionalAxisTests::test_a_non_cnc_intent_bundle_is_not_staged`).
- **Live staged financing is explicitly unsupported**: a live rebalance whose
  increases are not covered by available margin now refuses by name
  (`STAGED_LIVE_FINANCING_UNSUPPORTED`), rather than borrowing the paper path
  (`::test_live_staged_rebalance_refuses_by_name`). Live is therefore NOT claimed
  ready.

### 4. Verification (escalated, no `asyncio.to_thread` shim)

| Command | Exit | Result |
| --- | --- | --- |
| `run_phase5_acceptance.py --only <4 one-off + 3 recurring momentum scenarios> --timeout 220` | 0 | `ok: true`; evidence `phase5-20260924T110544Z.json` |
| `.venv/bin/python -m pytest tests/strategies -q` | 0 | `866 passed, 1 skipped, 38 subtests` (only the pre-existing `test_execution_dispatcher.py` `get_event_loop()` ordering artifact fails, and only when `test_execution.py` runs first) |
| `CONTINUATION_PG_URL=…15433 pytest tests/integration/test_evaluation_continuation_postgres.py tests/integration/test_admission_approvals_postgres.py tests/integration/test_settlement_barrier_postgres.py tests/integration/test_reconciliation_barrier_toctou_postgres.py -q` | 0 | `52 passed` |

### 5. Harness property worth knowing

This harness's paper runtime quotes the synthetic market price (~1,500 for the
momentum names) while the momentum example's daily-history fixture closes at
~659.5, so the adapter's frozen `reference_price` and the paper FILL price differ
in this fixture only (in production both are the live market price). The
zero-free-cash assertions therefore use the ACTUAL fill price, and the entry
budget is calibrated so the account ends with ~100,000 free — comfortably enough
for the reductions to run and genuinely insufficient for the increase.

### 6. Hashes (sha256) after round 4

```
40b438fe7991151c7316e5df9fc12be07a9d9a27d27c9fec29e5b0cd436fbdc2  backend/strategies/reservations.py
7d182ea1161fc490b63b202f3a313d9df3944a27a9b590aa3dd292de2f5876e2  backend/strategies/admission.py
cd2d78e8e339b6fe6b2a1d45224d0e61467ce599fe182a6ca7c0aacb3934e210  backend/strategies/execution.py
77300964d978a39e12bf9f3e1595be2e203e969ef13c836fe988222067ba79a0  backend/strategies/plan_pipeline.py
55b143dd5fc1394758da56c839dd409093686cee12d2ef503fad46a1bd92c130  examples/hosted_platform/nifty500_momentum.py
eecf83eea8ac14e35c9acb4b9579c5f567f7ade5e31ca6e17c957f16da82d423  examples/hosted_platform/run_phase5_acceptance.py
ea91f53dae6e6330257a580cb03bc45916eab412741b6f0aaaebc03424cf75b2  tests/strategies/test_reservations.py
cf6ac971470668dc3933b60b119d370618d20450101127e6dccd54009230929b  tests/strategies/test_admission.py
2cf28af415b6fab5b2d37106d0fabb246b14e67a1e263bba14d06140c411e6b1  tests/strategies/test_execution.py
```

### 7. Limits unchanged

No broker order of any kind was placed; every order is paper in a disposable
database. Live staged financing refuses by name. Protection continuity remains the
root-accepted bounded refusal. Phase C (authenticated deployed UI) stays with root.
