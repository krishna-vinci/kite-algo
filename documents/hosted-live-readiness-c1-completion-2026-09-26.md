# C1 completion report: live readiness (no real orders)

Date: 2026-09-26. Branch `codex/c1-report`. Companion: `documents/runbooks/README.md`
and the four lane runbooks. Status: C1.1 and C1.2 are landed and reviewed against
the fake broker only. **No real order was placed by any slice of C1.** No live
account is allowlisted yet; C1.3 (the owner's own account) is not started.

Commit range: `4af40b2..HEAD` (`git log --oneline 4af40b2..HEAD`).

## 1. What C1 delivers

### C1.1 live staged CNC financing

A live `target_weights` plan that is short of whole-plan cash is admitted as
staged instead of refused. Reductions are immediate; every dependent buy is
`withheld` behind `staged_funding_gate` and released only against the plan's own
confirmed reduction fills (`backend/strategies/live_sequence.py:115`; builder at
`backend/strategies/live_sequence.py:590`).

| Slice | What landed | Commits |
| --- | --- | --- |
| C1.1 S1+S2 | One funds/margin reader (broker order margin plus authoritative CNC available cash; absent or non-numeric cash is unavailable, never zero) whose programming errors surface; staged admission replaces the removed `STAGED_LIVE_FINANCING_UNSUPPORTED`; CNC shorts refuse `STAGED_CNC_SHORT_UNSUPPORTED`; keyed `staged_increase_authorized` reservation event (`plan_id:step_no`) committed with the release CAS under the book and account locks | `61f9c46`, `be0a94e` |
| C1.1 S3 | Bounded operator repair for dead staged buys (`abandon_staged_dependent`, disposition `staged_dependent_abandoned`) and the funds read moved INSIDE the reservation account lock | `7cf54c4` |

Reader: `live_margin_evidence` (`backend/strategies/plan_pipeline.py:160-206`),
`required_inr` and `required_margin_inr` both supplied so B2.5's margin gate is
enforced live. Gate: `backend/strategies/live_adapter.py:1258` (`_staged_funding_gate`),
split into a money-free quote stage (`:1290`) and a funds stage that must run under
the lock (`:1361`). The lock is taken at
`backend/strategies/live_adapter.py:2916` (`self.ledger._lock_account`), and the keyed
authorization is `ReservationLedger.authorize_staged_increase`
(`backend/strategies/reservations.py:472`). Race suite:
`tests/integration/test_live_staged_financing_race_postgres.py`.

### C1.2 live options readiness (S1-S5 + hardening)

| Slice | What landed | Commit |
| --- | --- | --- |
| S1 | Broker basket-margin evidence (`option_live_margin_evidence`, basket for the frozen deltas with `consider_positions`, roll overlap peak, `required_margin_inr` supplied, zero required for reduce-only with usable funds still mandatory) and option-chain/Greeks freeze evidence plus a pre-send freshness recheck | `9dbba29` |
| S2 | `build_option_steps` accepts live adjust/roll plans and expands them into the release-ruled step classes; new `RULE_OPTION_ROLL_RELEASE_GATE`; live adjust completes only from the run's own ledger (generation bump and protection freeze in the same CAS write) | `92d932c` |
| S3 | Approvals pin strategy version (id, number, source hash, policy hash) and, for option plans, the option run, its based-on generation, the protection policy version, and the generation the approval reserves to move (migration `20260926_000051`); a partial unique index lets one active approval own a run generation | `cd452a5` |
| S4 | Every gated dependent live leg is a bounded platform-side LIMIT; a working gated LIMIT that times out has explicit cancel/terminal handling with no repricing or replacement | `d983530` |
| S5 | Protection-owner continuity proven end to end on the live lane (one owner row, superseded callers refused, new policy frozen on completion, restart duplicates no protective stage) | `173310c`, `807884f` |
| Hardening | Four dispatch gaps from independent review closed (see section 5) | `399c8f0` |

Code entry points: bounded pricing `backend/strategies/live_limit_orders.py`
(`derive_bounded_limit` at `:269`); roll release gate
`backend/strategies/live_service.py:1060` (`_option_roll_release_rule`); live adjust
completion `backend/strategies/live_lane_ledger.py:273` (`_advance_option_adjust`);
approval binding `backend/strategies/live_adapter.py:921` (`_check_approval_binding`).

## 2. The C1 exit requirement

> Gated dependent live CNC buys use bounded LIMIT orders before any real account is
> enabled (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:128-129`).

**Met in `d983530` (S4), widened to every gated leg.** One shared helper derives the
price and refuses rather than widening the bound or falling back to MARKET
(`backend/strategies/live_limit_orders.py:269-421`):

* BUY limit is `min(ask, reference * (1 + drift))`; SELL limit is
  `max(bid, reference * (1 - drift))` (`:324-327`).
* When the required book side is absent, the fresh LTP is the reference and the
  same band applies (`:328-371`); a one-sided book whose LTP is already outside the
  band refuses `LIVE_LIMIT_PRICE_BOUND_EXCEEDED` (`:345-368`).
* The price is rounded toward the passive side to the broker tick (BUY floors, SELL
  ceils) so rounding can never cross the band (`:218-238`).
* The frozen reference is the step's frozen sizing (`:199-209`); a release step
  built from the run's own legs has no frozen plan price, so the fresh quote LTP
  stands in and the source is recorded as `reference_price_source` (`:1916-1948`,
  `:1953-1961`).
* Refusals: `LIVE_LIMIT_PRICE_UNAVAILABLE`, `LIVE_LIMIT_PRICE_BOUND_EXCEEDED`,
  `LIVE_LIMIT_TICK_UNKNOWN`, and `LIVE_REFERENCE_PRICE_UNAVAILABLE`
  (`backend/strategies/live_limit_orders.py:34,37,40`;
  `backend/strategies/live_adapter.py:1943`).

Gated legs are classified structurally, so a new option gate cannot silently fall
back to MARKET: an option-lane step is gated LIMIT whenever its rule is not
`immediate`, and every other lane uses the registered gated-rule list
(`backend/strategies/live_limit_orders.py:69-111`). The chosen price and its evidence
(frozen reference, observed quote, band, tick source, `submitted_at`) are persisted
with the step before the broker call (`backend/strategies/live_adapter.py:2089-2148`).
The price and order type are built into the broker intent payload
(`backend/strategies/live_adapter.py:2104-2116`), and the timeout's cancel goes
through the same handler through the new `cancel_order` intent
(`backend/algo_runtime/intent_bridge.py:64-81`).

Timeout and outcomes (`LIVE_GATED_LIMIT_TIMEOUT_SECONDS`, default 10 s,
`backend/strategies/live_limit_orders.py:150-161`): the sweep runs at the head of the
shared sequence pass (`backend/strategies/live_service.py:501-540`), driven by the
live outcome consumer (`backend/app/bootstrap.py:106`). It cancels only an order id
already known through the durable claim, records the attempt BEFORE the broker call,
and never re-cancels or reprices
(`backend/strategies/live_adapter.py:2224-2590`). A zero-filled cancel is terminal
`rejected`, or `repair_required` when the leg was a risk reduction; a partial fill
retains its remainder; a cancel that raced a complete fill is left to ingestion; an
uncertain cancel stays unresolved and is never repeated.

## 3. Refusal vocabulary added in C1

Grep-verified against the current code. Every code below is raised by name; none is
generic.

| Code | Meaning | Where enforced |
| --- | --- | --- |
| `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` | A dependent buy's funding reduction is not confirmed `filled` | `backend/strategies/live_adapter.py:1313`; `backend/strategies/live_service.py:653`; `backend/strategies/live_repair.py:793` |
| `STAGED_FUNDING_EVIDENCE_UNAVAILABLE` | Authoritative funds read is missing or unreadable, or the account scope mismatches | `backend/strategies/live_adapter.py:1390,1398` |
| `STAGED_FUNDING_EVIDENCE_STALE` | Funds evidence older than `ADMISSION_MARGIN_MAX_AGE_SECONDS` | `backend/strategies/live_adapter.py:1410` |
| `LIVE_FINANCING_PRICE_DRIFT` | Buy quote drifted beyond `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` from the frozen reference | `backend/strategies/live_adapter.py:1349` |
| `STAGED_CNC_SHORT_UNSUPPORTED` | A live CNC leg targets a short | `backend/strategies/admission.py:1322` |
| `ACCOUNT_FUNDS_UNSECURED` | Reservation authorization cannot cover the leg | `backend/strategies/live_adapter.py:2956` |
| `LIVE_CAPACITY_SHORTFALL` | The reservation no longer covers every outstanding leg | `backend/strategies/live_adapter.py:1733`, `:2918`, `:2973` |
| `LIVE_OPTION_MARGIN_EVIDENCE_UNAVAILABLE` | Option basket margin or usable funds cannot be read | `backend/strategies/admission.py:84,709,1336` |
| `LIVE_OPTION_MARGIN_EVIDENCE_STALE` | Option margin evidence older than the admission bound | `backend/strategies/admission.py:85,1343,1351` |
| `LIVE_OPTION_MARGIN_EVIDENCE_SCOPE_MISMATCH` | The basket read answered for a different account scope | `backend/strategies/plan_pipeline.py:302` |
| `LIVE_OPTION_ROLL_PEAK_UNAVAILABLE` | A roll's overlap basket cannot be read; the peak is never treated as the final | `backend/strategies/plan_pipeline.py:324,330` |
| `MARGIN_INSUFFICIENT` | `required_margin_inr` exceeds the version's `margin_limit_inr` | `backend/strategies/admission.py:725` |
| `OPTION_CHAIN_SNAPSHOT_UNAVAILABLE` / `_STALE` | Frozen chain snapshot missing, leg missing, non-finite LTP, or older than the bound | `backend/options/market/freshness.py:93-119,215-249` |
| `OPTION_GREEKS_UNAVAILABLE` / `_STALE` | Greek packet missing or stale, or required `iv`/`delta` absent | `backend/options/market/freshness.py:129,165-189,240-266` |
| `LIVE_OPTION_RUN_UNAVAILABLE` | The durable plan/run binding or the engine's own step derivation is absent | `backend/strategies/live_sequence.py:790` |
| `option_roll_not_proven` (blocker) | Not every roll acquisition is `filled` | `backend/strategies/live_service.py:1092-1098` |
| `LIVE_OPTION_RUN_LEDGER_INCONSISTENT` | Roll steps do not name one run, the ledger is unreadable, or it does not hold the exact replacement generation | `backend/strategies/live_service.py:1106,1115,1148` |
| `LIVE_OPTION_RUN_LEG_UNRESOLVED` | A frozen option step has no durable run leg; the parent stays in flight | `backend/strategies/live_lane_ledger.py:220` |
| `LIVE_LIMIT_PRICE_UNAVAILABLE` | No usable book side, fresh LTP, or positive reference | `backend/strategies/live_limit_orders.py:34` |
| `LIVE_LIMIT_PRICE_BOUND_EXCEEDED` | The only price evidence lies outside the frozen band | `backend/strategies/live_limit_orders.py:37` |
| `LIVE_LIMIT_TICK_UNKNOWN` | The broker tick is unknown, so no price can be placed on the grid | `backend/strategies/live_limit_orders.py:40`; `backend/strategies/live_adapter.py:1855,1864,1877` |
| `LIVE_REFERENCE_PRICE_UNAVAILABLE` | A gated leg has neither a frozen reference nor a usable quote LTP | `backend/strategies/live_adapter.py:1341,1543,1943` |
| `LIVE_APPROVAL_VERSION_CHANGED` | The approval's pinned strategy version, source hash, or policy hash moved | `backend/strategies/live_adapter.py:957` |
| `LIVE_OPTION_CATALOG_GENERATION_CHANGED` | The option catalog generation moved under a pinned approval | `backend/strategies/live_adapter.py:992` |
| `OPTION_PROTECTION_POLICY_CHANGED` | The owner row's protection policy version moved | `backend/strategies/live_adapter.py:1069` |
| `APPROVAL_OPTION_GENERATION_OWNED` | Another active approval already owns this option run generation | `backend/strategies/approvals.py:145,401` |
| `LIVE_RESERVATION_REQUIRED` / `_MISMATCH` / `_EXPIRED` | The plan's reservation is absent/inactive, not live, or past `valid_until`; never renewed during release | `backend/strategies/live_adapter.py:1169,1177,1183` |
| `OPTION_ADJUSTMENT_STALE_BASIS` | The frozen option target no longer names the approval's run or generation | `backend/strategies/live_adapter.py:978,1011,1023` |
| `OPTION_PROTECTION_OWNER_CONFLICT` / `OPTION_PROTECTION_OWNER_UNKNOWN` | The run's protection ownership is unreadable, moved, or unknown while exposure grows | `backend/strategies/live_adapter.py:1042,1054,1060`; `backend/options/execution/plan_binding.py:552,571,1236-1245` |

Retired in C1: `STAGED_LIVE_FINANCING_UNSUPPORTED` (removed in `61f9c46`) and
`LIVE_OPTION_ADJUST_UNSUPPORTED` (the live lane now executes adjust/roll, `92d932c`).

## 4. Test evidence (fake broker only)

Every suite drives the fake `intent_handler`; no test opens a network or a real
broker session, and the plan/no-real-order boundary holds
(`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:12-14`;
`documents/runbooks/README.md:26-29`). The suites were run by the implementing agent
at commit time; this docs-only task did not re-run them.

C1.1:

| File | Proves |
| --- | --- |
| `tests/strategies/test_admission.py` | staged admission twins, zero-free-cash staging, short refusal |
| `tests/strategies/test_live_submission_store.py` | withheld dependent buys, gate release once, drift/stale/unavailable twins |
| `tests/strategies/test_reservations.py` | keyed staged authorization |
| `tests/strategies/test_execution.py` | paper staged authorization keyed by step |
| `tests/integration/test_hosted_live_phase1_postgres.py` | live entry/exit smoke after the reader fix |
| `tests/integration/test_hosted_live_phase2a_routes_postgres.py` | live lane routes and no-repeat skeleton |
| `tests/integration/test_hosted_execution_governed_release_postgres.py` | governed release path |
| `tests/integration/test_live_staged_financing_race_postgres.py` | two staged plans competing for one account balance; funds read under the account lock |

C1.2:

| File | Proves |
| --- | --- |
| `tests/strategies/test_live_limit_orders.py` | band derivation, passive rounding, LTP fallback, refusals, timeout sweep, uncertain cancel fenced |
| `tests/strategies/test_live_submission_store.py` | adjust/roll step model and approval-binding release twins |
| `tests/strategies/test_admission.py` | option margin/chain admission twins |
| `tests/strategies/test_approvals.py` | version/generation binding and `APPROVAL_OPTION_GENERATION_OWNED` |
| `tests/integration/test_live_options_c1_2_postgres.py` | 16 live-fake scenarios: resize, roll, protection continuity, owner unknown/superseded, bounded LIMIT and timeout, released reservation |
| `tests/integration/test_live_limit_races_postgres.py` | two writers record one option fill once; a later ingested fill is recovered from an uncertain claim |
| `tests/integration/test_admission_approvals_postgres.py` | concurrent adjust approvals cannot both own one generation |
| `tests/integration/test_hosted_live_phase2a_routes_postgres.py`, `..._phase2b_...` | option margin/chain routes |

Representative scenario names: `test_live_options_resize_roll_and_protective_exit_end_to_end`
(`:575`), `test_live_roll_partial_acquisition_holds_old_generation_then_releases_once`
(`:1716`), `test_gated_option_legs_are_bounded_limits_and_a_timeout_cancels_once`
(`:2176`), `test_a_released_reservation_blocks_the_gated_release_by_name` (`:2301`).

## 5. Reviews

**Independent reviews.**

* C1.1 + B2.4: C1.1 clean; three B2.4 protection-ownership findings, fixed in
  `724e1c6` (recorded in the plan progress table).
* C1.2: four findings, all fixed in `399c8f0`:
  1. option roll-release legs (`RULE_OPTION_ROLL_RELEASE_GATE`) were not bounded
     LIMITs, and classification was by rule name only; it is now structural so a new
     option gate cannot fall back to MARKET
     (`backend/strategies/live_limit_orders.py:69-111`);
  2. the timeout sweep now takes the same token-guarded lease as the outcome
     consumer and never moves a leased or finalizing claim
     (`backend/strategies/live_adapter.py:2304-2344`);
  3. a timed-out cancel becomes terminal only on authoritative broker order state
     read at the cancel boundary; missing or ambiguous evidence leaves the claim
     uncertain, and uncertain claims stay in the consumer's scan
     (`backend/strategies/live_adapter.py:2590-2630`);
  4. live option fills are written with `record_trades_once` keyed by plan, step and
     cumulative quantity so overlapping consumers cannot double-count
     (`backend/strategies/live_lane_ledger.py:231-262`;
     `backend/options/execution/durable_store.py:567-600`).

**Defects caught in orchestrator review.**

* The live margin/funds reader TypeError: `LivePlanExecutor` called
  `live_margin_evidence(..., session_factory=...)` against a signature that did not
  accept it, and a broad `except` turned the `TypeError` into "no evidence". One
  reader now supplies `required_inr` and `required_margin_inr`, and programming
  errors propagate (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:67`;
  `backend/strategies/plan_pipeline.py:160-206`; fixed in `61f9c46`).
* The C1.1 staged keying/evidence hazard: paper staged financing passed no
  `step_no`, so the keyed authorization defaulted to step 0 and could match the wrong
  buy in a sell-then-buy plan, and the live release read `staged_detail` before
  assigning it on non-staged and MIS releases (fixed in `be0a94e`).
* The C1.1 sell-then-stall hazard: a confirmed sale whose funding leg is terminal
  without a complete fill left the dependent buy `withheld` with no way out; the
  bounded operator disposition `staged_dependent_abandoned` closes it
  (`backend/strategies/live_repair.py:13-16,624-717`; `7cf54c4`).
* Funds were read before the account lock, so a competitor's confirmed fill could
  move cash out of the account between the read and the authorization. The read now
  happens inside the reservation account lock and inside the same release
  transaction (`backend/strategies/live_adapter.py:2905-2936`; `7cf54c4`).
* The option margin/chain checks are gated strictly on `option_structure` plans;
  CNC, MIS and futures never touch the option market source or the option margin
  reader (`backend/strategies/live_adapter.py:1208-1228`; `9dbba29`).

## 6. What is NOT done (owner action)

* **C1.3 (owner go-ahead required).** Add the owner's own broker account to
  `HOSTED_STRATEGY_ACCOUNT_SCOPES` and verify the selectors, readiness and account
  identity read-only (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:127`).
  No order is placed. Requires explicit owner authorization.
* **C2 rollout (owner approval per deploy).** Lane order CNC, MIS, futures,
  options; each step needs owner approval
  (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:132-140`).
  Production is at migration head `20260924_000043`
  (`documents/hosted-recurring-portfolios-deployment-2026-09-24.md:20`); the code head
  is `20260926_000051` (`backend/alembic/versions/20260926_000051_live_approval_binding.py`),
  so `20260925_000044` through `20260926_000051` must land before any lane rollout.

## 7. Honest limitations

* **Immediate legs and non-gated protective exits remain MARKET by design.** Only
  gated dependent legs are bounded LIMITs (`backend/strategies/live_limit_orders.py:69-111`).
  In the option lane an immediate short-closing buy-back is MARKET; the hedge half of
  an exit is the gated LIMIT (`backend/strategies/live_sequence.py:884-892`). The
  platform protection runtime's own staged exit submits MARKET
  (`backend/api/services/protection_runtime.py:727`). MIS square-offs keep their
  current order type (`backend/strategies/live_adapter.py:2041`).
* **Live non-option flatten refuses** `FLATTEN_LIVE_NONOPTION_UNSUPPORTED`
  (`backend/api/services/owner_actions.py:117`), so a live CNC/MIS/futures book must be
  reduced through its own governed lane, not flatten.
* **Rolled runs: resolved.** Owner exit, repair and flatten now act on a rolled run (`c53dd58`). The shared
  `known_run_legs` rule in `backend/options/protection/staged_exit.py` covers the held legs plus the recorded
  `structure_generation_history`. A released leg that nets to zero drops out; one that nets non-zero is closed
  short-first. A trade on an unknown leg, or unreadable history, is still refused as ambiguous.
* **Live residual option closes have no governed submission path**: an operator
  repair is refused `OPTION_RUN_REPAIR_LIVE_UNSUPPORTED`
  (`backend/api/routers/strategies.py:1404`).

## Related documents

* Readiness plan: `documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md`
* C1.1 design: `documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md`
* C1.2 design: `documents/hosted-live-options-c1-2-design-2026-09-25.md`
* Runbooks: `documents/runbooks/README.md`,
  `documents/runbooks/hosted-live-{cnc,mis,futures,options}.md`
