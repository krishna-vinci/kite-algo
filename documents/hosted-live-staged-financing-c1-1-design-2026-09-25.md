# C1.1 design note: live staged CNC financing (fake broker only)

Date: 2026-09-25. Scope: live `target_weights` plans whose every leg is long-only CNC. No real order, account-scope expansion, deployment, push, or commit is authorized.

## Decision summary

Keep the existing durable multi-step protocol. Add one portfolio release rule, `staged_funding_gate`, and one per-buy reservation authorization step. A reduction is ordinary immediate work; every dependent buy remains `withheld` until all reductions are `filled`. After broker-confirmed fills and attribution publication, read authoritative account funds, authorize the exact buy under the account reservation lock, revalidate quote/funds/admission immediately before send, and only then dispatch. A projected or partial sale never supplies money.

The current live portfolio lane already orders reductions before increases and materializes each dependent increase as withheld work (`backend/strategies/live_sequence.py:449`, `backend/strategies/live_sequence.py:498`, `backend/strategies/live_sequence.py:517`, and `backend/strategies/live_sequence.py:556`). The missing production pieces are deferred live admission, authoritative post-fill funds evidence, and a durable per-buy authorization.

## Current behavior

### Paper staged financing

Admission recognizes a staged CNC rebalance only when the plan kind is `intent_bundle` or `target_weights`, it is not a roll, and every leg product is exactly `CNC`; it records the full incremental requirement as `staged_increase_inr` (`backend/strategies/admission.py:439`, `backend/strategies/admission.py:453`, and `backend/strategies/admission.py:482`). `staged_funding` computes the shortfall that must be funded by this plan's own confirmed releases, explicitly without crediting a projected sale (`backend/strategies/financing.py:524` and `backend/strategies/financing.py:536`).

The paper executor reorders untouched/reducing/increasing steps and maps every reduction as a dependency of every increase (`backend/strategies/execution.py:403`, `backend/strategies/execution.py:418`, and `backend/strategies/execution.py:434`). It refuses a dependent buy with `FINANCING_UNSECURED` unless each funding event is exactly `filled` or `no_op` (`backend/strategies/execution.py:510` and `backend/strategies/execution.py:523`). Even after that, it reads current account funds and calls `ReservationLedger.authorize_staged_increase`; failure becomes `ACCOUNT_FUNDS_UNSECURED` (`backend/strategies/execution.py:548`, `backend/strategies/execution.py:560`, and `backend/strategies/execution.py:829`).

The existing paper admission and execution tests already pin zero-free-cash staging and partial-sale refusal (`tests/strategies/test_admission.py:546`, `tests/strategies/test_admission.py:556`, `tests/strategies/test_execution.py:1349`, and `tests/strategies/test_execution.py:1388`).

### Live refusal and existing release machinery

`STAGED_LIVE_FINANCING_UNSUPPORTED` is raised only when live margin evidence says available funds are below the whole requirement and the plan is staged (`backend/strategies/admission.py:726` and `backend/strategies/admission.py:732`). The intent is fail-closed: the live adapter had no confirmed-release authorization path (`backend/strategies/admission.py:728`). C1.1 replaces this refusal; it does not weaken the refusal for lanes that cannot safely use the generic rule.

The live sequence has the needed shape: immutable `StepSpec` carries `depends_on` and `release_rule` (`backend/strategies/live_sequence.py:172` and `backend/strategies/live_sequence.py:200`); dependent or lane-gated steps are materialized `withheld` with barrier work (`backend/strategies/live_sequence.py:1099` and `backend/strategies/live_sequence.py:1137`); and only `filled` proves a prerequisite (`backend/strategies/live_sequence.py:916`). `release_step` takes the canonical book lock, rechecks prerequisites, fresh authority, approval, reservation, admission, and capacity, then CASes `withheld -> releasing`; dispatch happens only after that transaction commits (`backend/strategies/live_adapter.py:1591`, `backend/strategies/live_adapter.py:1660`, and `backend/strategies/live_adapter.py:1780`). The consumer publishes attribution before reservation effects and runs the sequence releaser after the outcome pass (`backend/strategies/live_ingestion.py:18`, `backend/strategies/live_ingestion.py:1049`, and `backend/strategies/live_ingestion.py:661`). This is the same dependency style used by roll close-after-acquire and option hedge gates (`backend/strategies/live_sequence.py:104` and `backend/strategies/live_sequence.py:108`).

## 1. Live lane and gate

Support staged live financing only for persisted `target_weights` plans. Requirements:

* every resolved leg product is `CNC`;
* every frozen target is `>= 0` and the reduction does not cross into a short;
* at least one reducing and one increasing delta exists;
* no roll or option structure metadata is attached.

Represent the rule as `RULE_STAGED_FUNDING_GATE = "staged_funding_gate"` in `live_sequence.RELEASE_RULES`. The portfolio builder uses it for every dependent increase instead of the generic `all_prerequisites_filled` label. Immediate reductions and an increase with no reductions remain `RULE_IMMEDIATE`.

### `staged_funding_gate`

Inputs:

```text
plan, parent, spec, all_specs, prerequisite_states,
confirmed_leg_outcomes, reservation, quote, funds_evidence,
admission_evidence, now
```

Outputs:

```text
allowed: bool
reason_code: str
detail: {
  confirmed_reduction_steps, unconfirmed_reduction_steps,
  confirmed_fill_quantities, buy_notional_inr,
  usable_funds_inr, funds_age_seconds, quote_age_seconds,
  authorization_key, competing_commitments_inr
}
```

The pure prerequisite stage fails with `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` unless every dependency is `filled`. The executable stage can return `LIVE_QUOTE_MISSING`, `LIVE_QUOTE_STALE`, `LIVE_FINANCING_PRICE_DRIFT`, `STAGED_FUNDING_EVIDENCE_UNAVAILABLE`, `STAGED_FUNDING_EVIDENCE_STALE`, `LIVE_CAPACITY_SHORTFALL`, or `ACCOUNT_FUNDS_UNSECURED`. Every non-allow result is recorded on the withheld claim by `LivePlanSequence.record_release_blocker` and places nothing (`backend/strategies/live_sequence.py:1505`).

The quote price used for the buy notional must be fresh. Refuse `LIVE_FINANCING_PRICE_DRIFT` when it exceeds the frozen reference price by more than `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` (default 0.005). C1.1 remains fake-broker-only; before C1.3, gated live CNC buys should become bounded LIMIT orders so the check bounds execution cost as well as estimated cost.

## 2. Funds/margin evidence after confirmation

Fix the existing production drift first: `LivePlanExecutor` calls `live_margin_evidence(..., session_factory=...)`, but the current function signature has no `session_factory` parameter; its broad `except` turns that TypeError into `None` (`backend/strategies/live_service.py:238` and `backend/strategies/plan_pipeline.py:42`). This is the observed baseline Phase 1 failure described below.

Then extend the live evidence reader into one CNC funding reader with two facts:

1. `required_inr`: retain broker order-margin evidence for the exact frozen leg, as today (`backend/strategies/plan_pipeline.py:54` and `backend/strategies/plan_pipeline.py:104`).
2. `usable`: authoritative account funds from the broker portfolio/funds boundary. Use `_load_live_kite_for_account` for session resolution (`backend/api/routers/worker_shared.py:469`) and `build_portfolio_snapshot`, whose funds component is a read-only `kite.margins()` call (`backend/broker_api/account/portfolio_snapshot.py:47` and `backend/broker_api/account/portfolio_snapshot.py:83`). For CNC use only `funds.equity.available.cash`; absent or non-numeric cash is unavailable, never zero.

Evidence shape: `{usable, required_inr, as_of, source, account_scope, legs}`. The existing admission freshness bound is 60 seconds, configurable through `ADMISSION_MARGIN_MAX_AGE_SECONDS` (`backend/strategies/admission.py:71` and `backend/strategies/admission.py:105`); use the same bound for funds. Quote freshness remains the adapter's five-second default (`backend/strategies/live_adapter.py:71` and `backend/strategies/live_adapter.py:928`).

Timing:

* initial admission may admit a staged live plan despite a whole-plan funds shortfall, exactly as paper does, while still refusing unavailable/stale evidence;
* the consumer must publish confirmed-fill attribution before release (`backend/strategies/live_ingestion.py:1065`);
* the release pass reads funds after the prerequisite is `filled`;
* unavailable, non-numeric, wrong-account, or stale evidence leaves the buy `withheld`, records `STAGED_FUNDING_EVIDENCE_UNAVAILABLE`/`STAGED_FUNDING_EVIDENCE_STALE`, and retries on the next bounded consumer pass (`backend/strategies/live_service.py:473` and `backend/strategies/live_service.py:560`).

No code adds sale proceeds to the broker figure. The only sale evidence accepted is a broker-confirmed, attributed, full reduction fill.

## 3. Reservation under the account lock

Keep one `StrategyReservation` row per plan. The claim already records the whole requirement but defers the staged portion from immediate account cash; it checks other commitments under `pg_advisory_xact_lock("admission:<account_id>")` (`backend/strategies/reservations.py:149`, `backend/strategies/reservations.py:173`, and `backend/strategies/reservations.py:391`).

Make per-buy authorization safe and auditable:

* add `authorization_key = plan_id:step_no`;
* record a distinct `staged_increase_authorized` event (not generic execution progress) with the incremental amount, cumulative amount, quote, funds evidence digest, and key;
* scan authorizations by key, so two equal-sized buys cannot treat one authorization as covering both;
* compute `needed = current_buy_notional - amount_already_authorized_for_key`;
* subtract only other plans' unfilled commitments from `usable`; this parent's reservation is not its own competitor (`backend/strategies/reservations.py:526`);
* commit the authorization and `withheld -> releasing` CAS in the same release transaction after taking both the canonical book lock and account lock.

Use a new event kind because `release()` currently refuses any reservation with a generic `advanced` event (`backend/strategies/reservations.py:707`). A staged authorization is not a fill: if the bounded disposition later proves the buy was never attempted, the parent can still release unused capacity. If any real fill exists, the parent remains consumed or retained under the existing parent rule (`backend/strategies/live_sequence.py:1250` and `backend/strategies/live_sequence.py:1431`).

Concurrent plans on the account are serialized by the reservation account lock; work and proofs on the same strategy book are serialized by the barrier book lock (`backend/strategies/settlement.py:939` and `backend/strategies/settlement.py:956`). Lock order is fixed: canonical book lock, then hosted strategy row lock if applicable, then reservation account lock.

## 4. Immediate pre-send revalidation

`release_step` repeats, inside the transaction:

1. persisted binding/authority and governed-request authority (`backend/strategies/live_adapter.py:1672`);
2. owner approval, tolerating only an exposure-snapshot move proved to be this parent's own fills (`backend/strategies/live_adapter.py:1682` and `backend/strategies/live_adapter.py:847`);
3. live reservation status/environment/validity (`backend/strategies/live_adapter.py:904`);
4. admission with current catalog and evidence (`backend/strategies/live_adapter.py:1727`);
5. parent-wide capacity coverage (`backend/strategies/live_adapter.py:1731`);
6. `staged_funding_gate` with fresh quote/funds and the per-buy authorization.

A refusal before the broker handler is called rewinds `releasing -> withheld` only while no broker order reference exists (`backend/strategies/live_adapter.py:1795` and `backend/strategies/live_adapter.py:1824`). After commit, `dispatch_step` validates the just-read quote again before handler invocation (`backend/strategies/live_adapter.py:1409`). A handler exception, malformed result, or missing order id becomes `uncertain`, retains work/capacity, and is never retransmitted (`backend/strategies/live_adapter.py:1510` and `backend/strategies/live_adapter.py:1559`).

## 5. Bad reductions and repair

Let `R` be a reduction and `B` a dependent buy.

* `R` pending/partial/finalizing: `B` stays `withheld`; no blocker spam while waiting.
* `R` explicitly rejected/cancelled with no fill: `B` records `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` and remains in flight.
* `R` terminal cancel with residual: ordinary ingestion makes it `repair_required`; the owner repairs `R` through `LiveRepairService` (`backend/strategies/live_repair.py:1` and `backend/strategies/live_repair.py:47`). After `R` becomes `residual_abandoned`, `B` is still never auto-released.
* `R` transport-unknown: retain it. Use the pre-send fence; no rows proves not attempted, a broker order is adopted, and an attempted/inconclusive send remains unknown (`backend/strategies/live_dispatch_fence.py:1`, `backend/strategies/live_dispatch_fence.py:18`, and `backend/strategies/live_dispatch_fence.py:24`).
* `B` itself uncertain/repaired: never repeat. The repair disposition applies only to the bounded releasing/repair states and refuses unknown authority (`backend/strategies/live_repair.py:47`, `backend/strategies/live_repair.py:162`, and `backend/strategies/live_repair.py:453`).

Extend `live_repair` with one bounded disposition for a dependent buy whose state is `withheld`, it has no order, every funding leg is terminal-but-not-filled, and plan authority is provably gone. State: `residual_abandoned`, detail disposition `staged_dependent_abandoned`, plus the funding-leg evidence. Record barrier `work_resolved` once. If any leg filled, parent capacity is consumed; if no leg filled, it is released. Unknown authority remains `LIVE_REPAIR_AUTHORITY_UNKNOWN`.

Until that disposition, an unresolved dependent keeps the parent and settlement barrier in flight, so the plan cannot look quiet.

## 6. What stays refused

* Live `intent_bundle` remains `LIVE_PLAN_KIND_UNSUPPORTED` (`backend/strategies/live_adapter.py:64` and `backend/strategies/live_sequence.py:889`).
* Mixed CNC/MIS/NRML products do not enter the staged lane; generic financing must not adopt domain margining (`backend/strategies/admission.py:453` and `backend/strategies/execution.py:403`).
* Futures rolls and option structures keep their acquire-first and hedge-first release rules (`backend/strategies/live_sequence.py:579` and `backend/strategies/live_sequence.py:711`).
* CNC shorts and reductions that cross flat are refused `STAGED_CNC_SHORT_UNSUPPORTED`.
* MIS staged plans are out of scope; MIS has its own square-off rule (`backend/strategies/live_sequence.py:395`).
* Missing or stale funds/margin never becomes zero headroom (`backend/strategies/plan_pipeline.py:42`).

Remove `STAGED_LIVE_FINANCING_UNSUPPORTED` from `ADMISSION_REFUSALS` and the live staged branch. Keep its focused paper/live refusal test as a mutation twin for the new admitted branch.

## 7. Slices and tests

All broker/order tests inject the fake at `intent_handler`; no test opens a network or real broker session (`backend/strategies/live_adapter.py:18`). Extend existing files before adding a focused integration file.

### S1: evidence repair and live staged admission

* Fix `live_margin_evidence` signature/call and add the CNC funds reader.
* Allow a live staged CNC plan when whole-plan cash is short, preserving all other admission refusals.
* Remove the unsupported refusal and add lane/product/short guards.

Tests:

1. live zero-free-cash sell-A/buy-B is admitted with `staged_increase_inr`;
2. missing funds refuses `MARGIN_UNAVAILABLE`; the passing twin has funds present and is admitted as staged;
3. stale funds twin: fresh admits, stale refuses `MARGIN_QUOTE_STALE`;
4. mixed-product and short-CNC twins refuse the new lane guards;
5. run `tests/strategies/test_admission.py` and `tests/integration/test_live_adapter_preparation_postgres.py`.

### S2: gate and authorized release

* Add the rule, gate output, funds read after confirmation, and keyed reservation authorization.
* Run the release pass from a confirmed sell fill to a dependent buy submission through the fake handler.

Tests:

1. reductions dispatch immediately; every dependent buy stays withheld with no order;
2. full confirmed reduction releases the buy exactly once; duplicate consumer/restart sends no second order;
3. one refusal/admit twin for each gate: non-filled reduction, stale funds, unavailable funds, stale quote, price drift, reservation shortfall;
4. equal-sized buys require two distinct authorization events;
5. parent reservation remains retained until every leg terminal.

### S3: repair and account serialization

* Add bounded withheld-dependent disposition and preserve no-repeat behavior.
* Add the PG account-lock serialization scenario.

Tests:

1. partial/rejected/unknown reduction refuses every increase; fake broker call count stays at the reduction count;
2. repair-required reduction is dispositioned; dependent remains withheld, then bounded disposition resolves it only with proven-dead authority;
3. uncertain buy is never retransmitted and fence repair adopts an order without re-sending (`tests/integration/test_hosted_live_phase2a_routes_postgres.py:1052` already pins the no-repeat skeleton);
4. PG test starts two staged plans competing for one account cash balance; exactly the first releasable authorization wins, the other records `ACCOUNT_FUNDS_UNSECURED`, and only one fake order is sent;
5. focused suites: phase2a routes, modified admission/execution tests, `tests/strategies/test_live_submission_store.py`, and the new C1.1 PG file.

## Observed baseline

Current checkout, disposable PostgreSQL 15433:

* `tests/integration/test_live_adapter_preparation_postgres.py`: 20 passed.
* `tests/strategies/test_admission.py`: 38 passed.
* `tests/integration/test_hosted_live_phase1_postgres.py`: 18 passed, 1 failed. `test_single_instrument_live_entry_ingestion_exit_and_settlement` fails `LIVE_ADMISSION_REFUSED` on the exit leg. The production reader call signature mismatch above explains the reader returning `None`; S1 must fix it.
* `POSITION_VALUATION_UNAVAILABLE` did not reproduce as a current failure; the two refusal assertions pass (`tests/strategies/test_admission.py:513` and `tests/strategies/test_admission.py:523`). Keep them as C1.1 guardrails; do not special-case valuation for staged CNC.

The Phase 1 failure blocks the existing end-to-end smoke but not S1 design work; it must be fixed and green before S2 claims a live staged path.

## Open questions

1. **Buy order type.** Recommendation: use the existing market dispatch for fake-broker C1.1, but do not enable real accounts until dependent CNC buys are bounded LIMIT orders. The pre-send drift check alone cannot bound market-order slippage.
2. **Drift limit.** Recommend `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT=0.005`, operator-visible and configurable. A tighter default may strand ordinary rebalances.
3. **Stale reservation.** Recommendation: do not auto-renew an expired reservation during dependent release. Require the existing governed reservation/approval flow so the owner authorizes continued exposure.
4. **Dependent abandonment UX.** Recommendation: expose `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` and the blocked funding steps together, so the operator sees why a seemingly successful reduction did not fund the replacement.

## Decisions (orchestrator, 2026-09-25)

Design accepted as written.

1. **Buy order type:** C1.1 keeps market dispatch against the fake broker only. Before C1.3 enables any real
   account, gated dependent CNC buys must become bounded LIMIT orders. This is recorded as a C1 exit requirement
   in the readiness plan.
2. **Drift limit:** `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` defaults to 0.005, is configurable, and is shown to the
   operator.
3. **Stale reservation:** it is never auto-renewed during dependent release. Continuing requires the governed
   reservation and approval flow.
4. **UX:** the owner view shows `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` together with the blocked funding steps
   (B2.6b/C1 UI).
5. **Order of work:** the `live_margin_evidence` signature bug (§2) is fixed first, in S1. It is a live-path
   correctness bug, and the Phase 1 live smoke test must pass before S2.
