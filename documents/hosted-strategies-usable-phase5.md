# Hosted strategies - Phase 5 (examples, guide, integration harness)

Date: 2026-09-24. Status: **ready_for_review**. Baseline `27c58b4` (development)
plus the accepted Phase 1/2/3-4 working tree and this correction pass. No commit,
stage, push, deploy, production migration, broker order, notification,
environment or allowlist change was made. The only PostgreSQL used is the
disposable, uniquely named database created on **127.0.0.1:15433** and dropped by
the harness/tests; production 15432 was never contacted.

This pass answers the six release-blocking findings in the root review. All six
reproduced on the real paths; two of them needed a slightly wider fix than the
finding described (weights aliases also covered `reference_prices` and a declared
`members` list; the options closure rule also needed the run's own status
vocabulary). Verification additionally found three further real defects
(section 3), including one the harness caught by itself.

Root review then accepted the production identity/alias/coverage/closure fixes and
required one financial blocker to be closed properly rather than documented: a
stated weights budget that differs from the owner's allocation is now REFUSED by
the server (`CAPITAL_BASIS_MISMATCH`/`CAPITAL_BASIS_INVALID`) before a plan,
reservation or order exists, with the matching and omitted cases unchanged
(section 3.2 and the `basis_mismatch` harness scenario).

## 1. The six findings, as actually fixed

### 1.1 Production never set the attribution session factory

`backend/api/routers/worker_auth.py::_run_strategy_attribution` read only
`app.state.strategies_session_factory`, which production never sets (tests do),
so every production run reported `unattributed` and a hosted strategy could not
discover its own identity. It now resolves the factory exactly like the other
worker routes (injected → else `backend.app.database.SessionLocal`), and it
validates the persisted binding against the run the caller is already
authorized on: a binding whose `account_id`/`execution_environment` disagrees
with the run is a mismatch reported as `unattributed`, never an attribution.
`_attach_worker_run_positions` passes the run's own account/environment down.

Tests: `tests/api/test_worker_run_attribution.py` (7) - normal route with no
injected factory, injected factory still wins, unknown run, empty run id,
another account, another environment, unreadable store.

### 1.2 The qualified-weight alias resolved to a zero target

`TargetWeightsCompiler` accepted a `NSE:SYMBOL` spelling but looked the weight up
with the bare persisted member, so `{"NSE:RELIANCE": 1}` sized RELIANCE at
**zero**. Weights, `reference_prices` and a declared `members` list are now
normalized onto the persisted (validated) member coordinate, preserving the
member's own exchange and the original `member_hash`:

* `NSE:RELIANCE` and `RELIANCE` fold to the same member in either direction;
* two spellings of one member with different weights raise
  `TARGET_WEIGHTS_ALIAS_CONFLICT` (identical values are the same instruction);
* an ambiguous bare symbol shared by two exchanges is dropped from the alias map
  and refused as out of scope instead of being silently bound;
* a weight or reference price outside the pinned scope is still
  `UNIVERSE_MEMBER_UNRESOLVED` with `outside_scope` named.

Tests: `tests/strategies/test_proposals.py` (37) - the two member-hash/member-list
guards that the previous pass had deleted are restored, plus new tests for the
qualified→bare direction, the bare→qualified direction, conflicting aliases and
an out-of-scope reference price.

### 1.3 A truncated option-run read was reported as complete coverage

`OwnedWorkSnapshotService._option_runs` now: keeps `coverage: "unknown"` and
`reason: "option_run_limit_truncated"` when more runs exist than the read limit
(the hidden runs may include the only open structure); reports the read failure
as the named reason `option_run_read_failed` **without** the driver's message
(which carried SQL text and bound parameters into a strategy's log); sets
`originating_plan_id`/`originating_phase` from the ENTRY edge rather than
whichever plan id sorts first lexically; and treats a run whose legs contradict
the frozen legs of its bound plan as `option_run_identity_mismatch` (unknown)
rather than as evidence about this strategy's book.

Tests: `tests/strategies/test_execution_snapshot_option_runs.py` (2, sanitized
reasons on both read paths) and
`tests/integration/test_owned_work_option_runs_postgres.py` (9 on disposable
PostgreSQL: scope/isolation, prior attempt, shared run, orphan edge, scope
mismatch, empty-known, entry-edge origin, >50-run truncation, leg contradiction,
symbol-only match is not a contradiction).

### 1.4 The options "adjustment" could exit without closing anything

In `examples/hosted_platform/options_index_setup_adjustment.py`:

* `_submit_close` waited only when the request was `awaiting_approval`, so an
  **autonomous** (queued/dispatching) close returned `None`, the child read that
  as a named refusal and exited. Every mode now waits for an authoritative
  outcome through the same helper, and only `executed` counts as a submitted
  close; a refusal, an unresolved dispatch or the deadline is `False`;
* `_pending_adjustment` whitelisted six states. ANY pending row now blocks a
  repeat adjustment, including a state the example does not recognise;
* the close was "confirmed" from the request alone. `_close_evidence` now needs a
  published book, the run's own durable status in the closed vocabulary (an
  unknown status is neither closed nor safe to re-enter), no pending/failed legs
  on that run, and no remaining non-zero attributed quantity for its symbols;
* a non-empty Greeks payload was treated as fresh. Freshness now comes from the
  platform's own signals (`updated_at` inside `quote_max_age_seconds`, no
  `resource_error`), so a stale or unstamped read is a named refusal;
* the close fell back to the leg's STORED price when no live premium was
  available, and defaulted a missing lot to the leg quantity. Both are now named
  refusals: a missing live premium or a missing lot size refuses instead of
  submitting a guessed price or size;
* the entry request is waited on in all modes too, and its outcome decides
  whether a close is attempted at all.

Tests: `tests/strategies/test_hosted_option_example.py` (22) - autonomous
queued→executed close, rejected close, awaited-approval timeout, any-state
pending blocking, stale/unstamped/resource-error chain, missing lot, missing live
premium, unknown run status, open leg work, unpublished book, residual quantity,
closed run, and the repeat observation that submits nothing.

### 1.5 The harness's `settled` flag and its premature stop

`examples/hosted_platform/_assertions.py` was rewritten. A green scenario now
requires all of:

* the child exited **by itself** with the expected exit code and its own final
  marker in its log (`outcome == "exited"`; a stopped/killed child fails);
* exactly the expected number of execution requests, every status inside the
  allowed set, and the expected status sequence;
* exactly the expected paper orders, matched on symbol, side and quantity, with
  the attributed book agreeing with the net those orders imply (a book that no
  order explains is a failure);
* for a holding scenario: the exact expected open exposure per symbol and an
  `attribution_scoped_flatness` settlement axis of `failed` (open exposure is not
  settlement);
* for the closed option structure: every bound option run closed, exactly one
  entry edge and exactly one close edge, and the real four-axis assessment
  satisfied;
* for a manual request: `orders_before_approval == 0` (the request was *observed*
  waiting before any order existed);
* for a deferral/refusal scenario: zero requests AND zero orders, plus the child
  naming the reason it received;
* an ordered notional ceiling where a budget is declared.

`settled` is no longer `bool(positions) and not dispatching`; the four-axis
assessment (`SettlementService`, via `POST /api/strategies/{id}/settlement/assess`)
is collected and asserted. The scenario loop no longer posts `stop` when the
request count goes terminal: it waits for the supervised child to finish, then
performs the production operator reconciliation
(`POST /jobs/{id}/reconciliation`, which records the durable quiescence proof,
closes the linked worker run and moves the attempt terminal in one transaction)
before assessing settlement. On timeout it stops its own attempt, records the
failure and does not claim the scenario.

An attempt that still holds exposure is refused by the platform with
`OPEN_EXPOSURE`; for the three holding scenarios that refusal is expected and is
recorded as evidence (`reconciliation.status = refused`), not as a harness
error.

Tests: `tests/strategies/test_hosted_harness_assertions.py` (15) - stopped child,
non-zero exit, missing marker, missing assessment, wrong open exposure, book that
disagrees with the orders, order before the owner decision, manual request never
observed waiting, unpublished book, closed-options pass, duplicated close, open
option run, missing axes, deferral with and without a request.

### 1.6 The guide's incorrect claims

`examples/hosted_platform/USER-GUIDE.md` now states, from the code the platform
actually runs:

* `main(ctx)` must be **synchronous** (`run_child` calls it directly and uses the
  return value; an `async def main` returns an un-awaited coroutine and the child
  exits without trading);
* the runner profile is `hosted-python-dataframe-indicators` with its exact
  packages, and readiness comes from `POST /api/strategies/readiness`
  (`kite_algo_worker.readiness.SourceReadiness`) - there is no `ctx.readiness`;
* identity comes from `ctx.run.attribution()` (`strategy_id`, `account_id`,
  `execution_environment`) - not `ctx.run.run["strategy_id"]`;
* `ctx.scratch` is a per-job scratch directory, not "the only writable place";
* run logs are shipped at the attempt's terminal transition (bounded, redacted):
  post-attempt history, not live streaming (`ctx.progress` is the live signal);
* `EXCHANGE:SYMBOL` coordinates depend on the catalog's own key (this catalog
  stores `NSE:NIFTY50`); resolve the instrument instead of blanket-stripping;
* a weights plan is sized against the owner's admission allocation: a stated
  `capital_basis_inr` that differs from it is refused by name
  (`CAPITAL_BASIS_MISMATCH`, either direction) and a non-finite/non-positive one
  is `CAPITAL_BASIS_INVALID`; omitting it uses the recorded allocation. No
  warning-only wording remains in the guide.

## 2. Verification performed

| Check | Command | Exit | Result |
| --- | --- | --- | --- |
| Full harness, 5 scenarios + recovery | `.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py --timeout 200` | 0 | **ok: true, errors: []** |
| Weights allocation guard (harness, focused) | `.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py --only universe_equal_weight,basis_mismatch --timeout 150` | 0 | **ok: true**; matching scenario 1 request / 4 orders (59/66/65/25); mismatch scenario 0 requests / 0 orders / 0 plans with `CAPITAL_BASIS_MISMATCH` named by the child |
| Corrected unit suites | `pytest tests/strategies/test_proposals.py tests/strategies/test_weights_compiler.py tests/strategies/test_execution_snapshot_option_runs.py tests/strategies/test_hosted_option_example.py tests/strategies/test_hosted_harness_assertions.py tests/api/test_worker_run_attribution.py tests/strategies/test_option_structure_compiler.py -q` | 0 | **124 passed**, 5 subtests |
| Capital-basis guard (store + API) | `pytest tests/strategies/test_proposals.py tests/api/test_hosted_execution_requests.py -q -k "stated_basis or capital_basis"` | 0 | **passed**; mismatch refuses both directions with no plan/reservation and the executor never called |
| Governed request pipeline (targeted) | `pytest tests/api/test_hosted_execution_requests.py -q -k "weights or stated_basis or proposal or universe or recovery or no_operation or pre_send or option or owned or settlement or snapshot"` | 0 | **16 passed**, 33 deselected |
| Run-binding trust (unit) | `pytest tests/api/test_strategy_owner_and_binding.py::TrustedRunBindingTests -q` | 0 | **12 passed** |
| Worker run read route (GET run) | `pytest "tests/api/test_algo_worker_api.py::AlgoWorkerRepositoryMappingTests::test_worker_run_read_surface_includes_health_fields" -q` | 0 | **1 passed** |
| Owned-work option runs (disposable PG) | `HOSTED_EXECUTION_PG_URL=postgresql://…@127.0.0.1:15433/kite_test pytest tests/integration/test_owned_work_option_runs_postgres.py -q` | 0 | **9 passed** (39.8s) |

Final harness evidence: `examples/hosted_platform/evidence/phase5-20260923T200234Z.json`
(`ok: true`, `errors: []`; the focused weights run above is
`evidence/phase5-20260923T195614Z.json`). Recorded axes:

| Scenario | Requests (statuses) | Paper orders | Attributed positions | Settlement | Child |
| --- | --- | --- | --- | --- | --- |
| `index_indicator` (manual) | 1 (`executed`) | 1 × RELIANCE BUY 5 | RELIANCE 5 | `unsettled`; flatness `failed` (open exposure); quiescence `satisfied`; `orders_before_approval = 0` | exited 0 + marker |
| `options_adjustment` (manual) | 2 (`executed, executed`) | 4 (entry 2 × 100, governed close 2 × 100) | - (own lane) | `settled`; all four axes `satisfied`; option run CLOSED; 1 entry edge + 1 close edge | exited 0 + marker |
| `universe_equal_weight` (manual) | 1 (`executed`) | 4 (HDFCBANK 59, INFY 66, RELIANCE 65, TCS 25) | the same four quantities | `unsettled`; flatness `failed`; ordered notional 389500 ≤ budget 400000 | exited 0 + marker |
| `autonomous` (grant) | 1 (`executed`) | 1 × INFY BUY 3 | INFY 3 | `unsettled`; flatness `failed` | exited 0 + marker |
| `basis_mismatch` (deferral) | 0 | 0 (0 plans, 0 reservations) | - | `settled` (nothing was opened) | exited 0, names `CAPITAL_BASIS_MISMATCH` (stated 400000 vs allocation 500000) |
| `recovery` | - | - | - | `{scanned 4, proved_submitted 1, proved_rejected 1, unresolved 2, stale 0}`, `still_dispatching 0` | - |

## 3. Real defects found while verifying (not in the six findings)

1. **The SDK/schema rejected the new snapshot key.** `originating_phase` was not
   a field of `OwnedOptionRunRow`, so the options child crashed on its first
   `owned_work()` read (`Extra inputs are not permitted`) and exited 1. The field
   is now part of the response model; the harness caught this because the child
   exit code is an acceptance axis.
2. **A stated weights budget could be silently replaced by the allocation.**
   `ProposalStore._compile_payload` overwrites `capital_basis_inr` with
   `StrategyAdmissionPolicy.allocation_inr` ("an approved size must not be
   narrated into existence"), so a strategy that declared a 400000 budget against
   a 500000 allocation had 486875 notional executed - spending past the budget it
   declared. That is now a NAMED REFUSAL rather than a documented hazard:
   `ProposalStore._compile_payload` compares a stated basis with the recorded
   allocation and refuses `CAPITAL_BASIS_MISMATCH` (either direction) or
   `CAPITAL_BASIS_INVALID` (non-numeric, non-finite, zero or negative) **before**
   an envelope, plan, reservation or order exists. Omitting the field keeps the
   existing policy basis; a matching value freezes the POLICY value. The
   admission-policy drift checks at execution time are unchanged.
   Evidence: `tests/strategies/test_proposals.py` (store level, both directions,
   no plan written), `tests/api/test_hosted_execution_requests.py`
   (`test_a_stated_basis_below_or_above_the_allocation_refuses_before_any_work`
   and `test_a_non_finite_stated_basis_is_invalid_not_a_sizing_input`: zero plans,
   zero reservations, the order boundary never called) and the harness
   `basis_mismatch` scenario (zero requests, zero paper orders, the child naming
   `CAPITAL_BASIS_MISMATCH` with both amounts and exiting 0).
3. **The example expected a fill it could not predict to the share.** The
   executor floors `weight × basis × (1-buffer) / price` after its own
   arithmetic, so rounding the weight client-side produced a 66-vs-67 share
   disagreement on INFY. The example's wait now allows one pinned lot per member
   (the platform's rule) while the harness asserts the exact executed quantities
   from the plan, and the child reports the settled notional.

## 4. Phase map

| Phase | Evidence |
| --- | --- |
| 1 data/dependency | consumed as accepted; the harness exercises catalog, quotes, candles (real `ts`), indicator and option-chain reads through the production routes |
| 2 authorization | consumed as accepted; real grants, approval, dispatch and the recovery matrix on disposable PostgreSQL |
| 3 first-run UI | consumed as accepted; no frontend file changed in this pass |
| 4 schedule/approval/recovery | consumed as accepted; the harness now performs the production reconciliation/terminal transition and reads the four-axis assessment |
| 5 examples and guide | three examples + schemas + corrected guide + harness, all asserted on real child exit codes, exact counts/quantities and settlement axes |

## 5. Remaining constraints

* Simulated execution is not live certification. The paper executor, the
  governed pipeline, the dispatcher, the barrier and the settlement axes are
  production code; the market/option-chain source is a synthetic boundary.
* Three `tests/api/` suites that build the full app with its background
  services hang in this sandbox after a few tests
  (`test_hosted_child_authority.py` at 4 dots, `test_hosted_proposal_authority.py`
  at 2, `test_strategy_owner_and_binding.py::OwnerStrategyApiTests` at 12). This
  is pre-existing: `test_hosted_child_authority.py::test_hosted_mutation_refused_when_fenced`
  hangs identically on a clean worktree at `27c58b4`. They are not used as
  evidence; the targeted selections above are.
* `tests/api/test_hosted_execution_requests.py` full-file hang (41 dots) is
  unchanged and not reported as evidence.
* A weights caller that states a `capital_basis_inr` different from the owner's
  allocation is now refused. Nothing else about sizing changed: an omitted basis
  still resolves to the recorded allocation, the frozen plan value is the
  allocation, and the execution-time drift check still refuses a plan whose
  recorded allocation has since fallen below its frozen basis.
* The options adjustment is a governed CLOSE. A fresh re-entry is deliberately
  not performed; it would only be defensible after a proven close, and the
  example stops at the closed, flat state.
* Named option-domain boundary (not blocking, recorded for the reviewer): the
  paper harness's assessment carries the four CORE axes because
  `register_option_settlement_adapter()` is wired by the live service
  (`backend/strategies/live_service.py`), which this paper app does not build.
  The governed close leaves the run `exited` (flat) with no
  `option_settlement_evidence` row - correct for an exit, and exactly why the
  live adapter reports `domain:option_settlement = unsettled` until cash/physical
  settlement evidence exists. "Closed by exit" is not "settled cash/physical",
  and the example/guide do not claim it.
* The holding scenarios are asserted as **unsettled with open exposure**. That is
  the honest reading of "the strategy still holds a position", not a failed
  settlement.
* The accepted mobile-shell overflow is unchanged and unclaimed.
* The live-account allowlist change rejected earlier remains unapplied. No live
  order, notification or production resource was touched.
