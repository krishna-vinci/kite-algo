# C1.2 design note: live options readiness (no real orders)

Date: 2026-09-25. Scope is the existing live option lane, fake broker only. No real order, account expansion,
deployment, commit, or push is authorized.

## Decision summary

C1.2 does not create a second option engine. It teaches the durable live step protocol the B2.2 adjust/roll
shapes, produces broker basket-margin evidence with the key B2.5 already reads, refuses stale option-chain or
Greeks evidence at freeze and immediately before send, and extends approval binding from the current plan/
exposure/catalog/reservation pins to strategy version, option run generation, and protection policy. Every
gated dependent leg is a bounded platform-side LIMIT with explicit cancel/terminal handling; an unfilled or
unknown leg never becomes a market order.

The existing live protocol already has the right seams: immutable `StepSpec` dependencies and release rules
(`backend/strategies/live_sequence.py:179`), withheld claims at materialization
(`backend/strategies/live_sequence.py:1065`), lane-owned release checks
(`backend/strategies/live_service.py:707`), and a confirmed-fill translation into the option run's own ledger
(`backend/strategies/live_lane_ledger.py:105`).

## Current live option lane

`build_option_steps` resolves the durable plan/run target, sizes each leg from the run's own fills, and refuses
`adjust` with `LIVE_OPTION_ADJUST_UNSUPPORTED` (`backend/strategies/live_sequence.py:735`,
`:769`). Entry hedges are immediate; a short entry is withheld behind every required hedge with
`RULE_HEDGE_FILL_GATE` (`backend/strategies/live_sequence.py:809`). In an exit, short closures run first and the
hedge half is withheld with `RULE_HEDGE_RELEASE_WITHHELD` (`backend/strategies/live_sequence.py:814`).

The release pass checks lane prerequisites before admission and dispatch. For entry, `hedge_fill_gate` releases
only when every dependent hedge is fully confirmed (`backend/strategies/live_service.py:850`,
`:893`). For exit, `build_structure_exit_orders` releases a hedge only for proven short closures
(`backend/strategies/live_service.py:897`, `:956`). A confirmed fill is recorded idempotently against the
durable option run leg and advances the run only from evidence
(`backend/strategies/live_lane_ledger.py:211`, `:266`).

Dispatch is committed as a CAS from `withheld` to `releasing` before the broker intent is sent; a refusal before
the handler can rewind, while transport uncertainty retains work/capacity
(`backend/strategies/live_adapter.py:1943`, `:1981`, `:2029`). `LiveDispatchFence` distinguishes no attempt,
known order, and unknown only after every attempted client ref is proven absent or present
(`backend/strategies/live_dispatch_fence.py:96`, `:144`). Repair is therefore evidence-driven, not a re-send.

B2.5 max-loss and notional checks already run in admission (`backend/strategies/admission.py:622`,
`:661`), but its margin gate compares `margin_evidence["required_margin_inr"]`
(`backend/strategies/admission.py:688`). The current reader returns `required_inr`
(`backend/strategies/plan_pipeline.py:151`, `:184`), so no live option caller satisfies the B2.5 field today.

## Reuse B2.2, but express timing as live release rules

Reuse the paper engine's derivations and state transitions; do not reuse its synchronous submit loop:

* derive deltas from the desired target minus the run's own confirmed opens
  (`backend/strategies/execution.py:1386`, `:1573`);
* keep the generation CAS and owner/precondition checks in `_begin_option_run`
  (`backend/strategies/execution.py:2032`, `:2200`);
* record orders/trades before state settlement and complete only from run evidence
  (`backend/strategies/execution.py:2239`, `:2327`, `:2371`);
* reuse the existing live option ledger for asynchronous fills
  (`backend/strategies/live_lane_ledger.py:211`);
* let `live_sequence` materialize immutable claims and let `live_service` release them.

The hardening commit `a279059` adds the live-safe lessons: a step is closed only when its order is terminal, a
partial remainder remains in flight, run trades and plan fills are compared by order identity, and a generation
stale at CAS becomes `OPTION_ADJUSTMENT_STALE_BASIS` (`backend/options/execution/plan_binding.py:638`,
`:819`; `backend/options/execution/durable_store.py:420`). In live, the paper-order lookup is replaced by the
`live_order_intents` fence and broker order/trade ingestion; the same “terminal order plus matching run trade”
invariant remains.

### Step model

Keep phases `entry`, `exit`, and `adjust` in the live lane. A live adjust expands to explicit step classes and
release rules:

1. `option_reduce_short` / `option_remove_short`: immediate reductions. Multiple shorts may proceed, but a
   hedge reduction remains behind the short closures it defends with the existing
   `RULE_HEDGE_RELEASE_WITHHELD`.
2. `option_reduce_hedge`: gated by `RULE_HEDGE_RELEASE_WITHHELD` behind proven short reductions.
3. `option_increase_hedge`: after reductions, when both exist. If reductions and increases coexist, hedge
   increases depend on all reduction steps through `RULE_ALL_PREREQUISITES_FILLED`; otherwise it is immediate.
4. `option_increase_short`: depends on every required hedge increase and keeps `RULE_HEDGE_FILL_GATE`. Only a
   full confirmed hedge releases the full short; a partial hedge releases nothing.
5. `option_roll_acquire`: the new generation uses the same hedge-before-short graph. Name the parent detail
   `roll={stage:"acquire"}`.
6. `option_roll_release`: every old-generation step depends on every acquire step through a new
   `RULE_OPTION_ROLL_RELEASE_GATE`. Inside the release, shorts are immediate and hedges retain
   `RULE_HEDGE_RELEASE_WITHHELD`. The gate proves all acquire parent steps are `filled` and the run ledger holds
   the exact new-generation quantities; it refuses `OPTION_ADJUSTMENT_ROLL_INCOMPLETE` otherwise.

`RULE_OPTION_ROLL_RELEASE_GATE` and blocker `option_roll_not_proven` are new. The existing
`hedge_fill_gate`, `build_structure_exit_orders`, `StagedStructureExit`, and the generation CAS are reused;
`RollStateMachine` storage is not imported into options. The builder may know release step numbers before the
first send because the desired legs and old generation are frozen, but quantities are re-derived only during
resolution; once materialized, parent `StepSpec`s remain immutable and evidence updates only claim outcomes.

For async consistency: the option-run status/generation CAS is taken before the first live submission. Each
parent leg outcome remains terminal only on broker terminal evidence plus a matching run trade. If they diverge,
record `LIVE_OPTION_RUN_LEDGER_INCONSISTENT`, keep the parent in flight, and route to repair; never derive a new
generation from an incomplete ledger. Generation bump and protection freeze happen only after the release
generation is proven flat and all acquisition legs are proven filled.

## Broker margin evidence

Add `option_live_margin_evidence(account_scope, plan, *, session_factory=None)` beside the C1.1 reader. It uses
`OrdersService.basket_margins`, which posts the broker basket endpoint with `consider_positions`
(`backend/broker_api/orders/service.py:611`), not a sum of independent order margins that would lose structure
offsets.

Evidence shape:

```json
{
  "usable": 123456.0,
  "required_margin_inr": 23456.0,
  "margin_basis": "basket_final|roll_peak",
  "as_of": "2026-09-25T10:00:00Z",
  "source": "broker_basket_margin",
  "account_scope": "<account>",
  "legs": ["..."],
  "breakdown": {"final_required_inr": 23456.0, "peak_required_inr": 34567.0}
}
```

Rules:

* entry/resize: one basket containing every non-zero frozen delta with `consider_positions=true`; use the final
  basket requirement.
* roll: compute final and overlap baskets, one including both generations, and carry the maximum as
  `required_margin_inr`. A missing peak is unavailable, never treated as final.
* exit/reduction-only: required option margin is zero, but authoritative usable funds are still mandatory and
  wrong account or non-numeric funds are unavailable.
* use the broker's account margin/funds read for `usable`; do not reuse the CNC-only equity cash projection.
* freshness remains the existing 60-second configurable admission bound
  (`backend/strategies/admission.py:1329`). Add `LIVE_OPTION_MARGIN_MAX_AGE_SECONDS` only if options need a
  different operational bound; do not introduce a second default silently.
* pass the object into `_check_admission`; B2.5 then enforces `required_margin_inr` against
  `margin_limit_inr` at `backend/strategies/admission.py:688`.

Refusals: `LIVE_OPTION_MARGIN_EVIDENCE_UNAVAILABLE`, `LIVE_OPTION_MARGIN_EVIDENCE_STALE`,
`LIVE_OPTION_MARGIN_SCOPE_MISMATCH`, and `LIVE_OPTION_ROLL_PEAK_UNAVAILABLE`. B2.5 keeps
`MARGIN_INSUFFICIENT` for the limit breach. Missing evidence blocks release and records a blocker; it is never
zero.

## Chain and Greeks freshness

The canonical snapshot is produced by `OptionsSession` on a configurable cadence and carries one underlying
`updated_at` (`backend/broker_api/options/options_sessions.py:247`, `:262`; manager read at
`backend/broker_api/options/options_sessions.py:705`). `OptionsMarketService` exposes chain and Greeks derived
from that same snapshot (`backend/options/market/service.py:44`, `:98`), and the Greek packet carries the row
`updated_at` (`backend/options/market/greeks.py:6`).

Freeze evidence on every option plan:

```json
{
  "underlying": "NIFTY",
  "expiry": "2026-10-01",
  "snapshot_updated_at": "...",
  "snapshot_digest": "sha256...",
  "legs": {
    "<instrument_id>": {
      "tradingsymbol": "...", "ltp": 1.0, "iv": 0.1,
      "delta": -0.2, "greek_updated_at": "..."
    }
  }
}
```

The freshness key is `(account/broker source, underlying, expiry, instrument_id, field timestamp)`. Every frozen
leg must be present with finite `ltp`; require finite `iv` and `delta` when the plan or protection rule uses
delta/IV. A missing leg or an expired snapshot is unknown, not “no protection needed”.

Bounds: at proposal freeze, use `OPTION_CHAIN_MAX_AGE_SECONDS=10` (two default cadence cycles); immediately
before live send, use `LIVE_OPTION_CHAIN_MAX_AGE_SECONDS=5`. Refusals are `OPTION_CHAIN_SNAPSHOT_UNAVAILABLE`,
`OPTION_CHAIN_SNAPSHOT_STALE`, `OPTION_GREEKS_UNAVAILABLE`, and `OPTION_GREEKS_STALE`; pre-send variants use the
same names. Check at plan compilation/approval and again in `release_step` alongside quote validation. A closed
market makes the plan not executable; it does not invalidate the immutable approval.

## Approval binding and invalidation

Today approval pins plan hash, exposure snapshot version/hash, reconciliation version, catalog generation, and an
active reservation (`backend/strategies/approvals.py:287`, `:390`). The release pass rechecks these pins and
tolerates an exposure move only when it is exactly this parent's own fills
(`backend/strategies/live_adapter.py:1816`, `:1852`).

C1.2 extends the approval/request binding:

* `strategy_version_id`, `version_number`, `source_sha256`, and `policy_hash` from
  `HostedExecutionRequest` (`backend/strategies/models.py:536`, `:549`);
* `option_run_id` and `based_on_generation` for option plans, matching the frozen target and
  `_begin_option_run` CAS (`backend/strategies/execution.py:2200`);
* `protection_policy_version` from the owner row/policy snapshot, as required by B2.4 live section
  (`documents/hosted-options-b2-4-protection-ownership-design-2026-09-25.md:229`);
* explicit `pinned_catalog_generation` and `reserved_option_generation` where the latter means “this approval
  owns the right to move the run from that generation”. Persist them on `StrategyApproval` or in a typed,
  non-editable binding JSON plus the audit evidence; columns are preferable for indexed checks.

Invalidation rules:

1. plan hash differs: invalid; the changed plan needs a new plan id and approval. Version content changes the
   compiled logical/resolved artifact and therefore naturally produces a new plan; the approval also refuses if
   the request/binding version no longer equals its pinned version.
2. exposure snapshot differs beyond own confirmed fills: keep today's
   `LIVE_SEQUENCE_BOOK_MOVED_BEYOND_OWN_FILLS` refusal.
3. catalog generation differs: for live options refuse `LIVE_OPTION_CATALOG_GENERATION_CHANGED`. This is stricter
   than the general unrelated-listing rule because every option leg is a pinned derivative contract.
4. reservation inactive/expired: refuse `LIVE_RESERVATION_REQUIRED` or `LIVE_RESERVATION_EXPIRED`; never renew it
   during release.
5. option run generation or protection policy/version differs: refuse
   `OPTION_ADJUSTMENT_STALE_BASIS`, `OPTION_PROTECTION_OWNER_CONFLICT`, or
   `OPTION_PROTECTION_POLICY_CHANGED`; a replacement plan must observe the new generation and be approved again.

## Bounded LIMIT orders

Every gated dependent live leg is LIMIT: C1.1 dependent CNC buys and all option hedge/short/roll release legs.
Immediate option reductions may also use LIMIT because an unbounded risk exit is not worth a silent cost/liquidity
failure; if a reduction cannot be executed within bounds it remains action-required. The current adapter hard-codes
`order_type: "MARKET"` (`backend/strategies/live_adapter.py:1618`, `:1628`); replace this with a
release-time `execution_order` block on the step.

Price derivation:

* prefer exchange bid/ask. BUY limit is `min(ask, reference * (1 + max_drift))`; SELL limit is
  `max(bid, reference * (1 - max_drift))`. If the required side of the book is absent, use fresh LTP as reference
  and apply the same bound; never send MARKET as a fallback.
* round toward the passive side to the broker tick. Refuse `LIVE_LIMIT_PRICE_UNAVAILABLE` or
  `LIVE_LIMIT_PRICE_BOUND_EXCEEDED` rather than widening the bound.
* `LIVE_OPTION_LIMIT_MAX_DRIFT_PCT` defaults to `0.005`; C1.1 may keep its existing name/default but should use
  the same helper. Persist frozen reference price, observed quote, bid/ask, chosen price, bound, and tick source in
  the step detail and pre-send evidence.

Timeout and outcomes:

* platform timeout `LIVE_GATED_LIMIT_TIMEOUT_SECONDS=10` (configurable). Broker day/TTL validity is not sufficient
  for an intraday gate.
* on timeout, cancel only an order id known through ingestion/intents. Zero-filled cancel becomes terminal
  `cancelled`; a partial becomes `partially_filled` and retains the remainder.
* a cancelled/rejected/timed-out hedge releases no short; a partially closed short releases no protected hedge; a
  partial roll acquisition proves no old-generation release.
* an uncertain cancel is unknown work, handled by the existing fence/repair boundary. There is no automatic
  repricing or silent replacement order. Continued execution requires a new governed attempt/plan and fresh
  approval, except that an explicitly authorized platform risk-reduction pass may submit a new bounded leg from
  current owned positions.

## Protection-owner continuity

Live entry creates the option run and owner atomically as paper does; the owner is transferred at successor
hosted-run creation, not first evaluation (`documents/hosted-options-b2-4-protection-ownership-design-2026-09-25.md:138`).
The live adjust builder must call the same owner check before any touching leg: unknown/superseded ownership blocks
increases and touching adjust work, while reduce-only/exit work remains available
(`backend/strategies/execution.py:1767`).

The live basket boundary is the durable option run, not the strategy aggregate. `StagedStructureExit` derives
stages from the run's own confirmed trades and net stages before submission
(`backend/options/protection/staged_exit.py:1`); live protection uses the existing structure submitter and the
same pre-send stage claims. A hedge is offered only after every held short is proven closed, and a dead child or
restart cannot create a second stage. On a successful adjust/roll, the new `policy_version` and frozen protection
facts are written in the same generation completion that marks the structure held.

## Slices and tests

All broker work uses the fake broker. Every gate has one refusal and one admitted twin. PG integration tests use a
scratch database created from `postgresql://postgres:testonly@127.0.0.1:15433`.

1. **S1: option margin and chain/Greeks evidence.** Add the option basket-margin reader, `required_margin_inr`,
   roll peak, chain/Greek freeze evidence, and pre-send checks.
   Tests: admission admits/rejects `margin_limit_inr`; unavailable and stale evidence twins; roll peak exceeds
   limit; every leg present/stale chain twins; fresh/stale Greeks twins. One live-fake entry E2E reaches dispatch
   only with fresh evidence and blocked claims otherwise.
2. **S2: live adjust/roll step model.** Enable `adjust` in `build_option_steps`, map the step classes above, add
   `RULE_OPTION_ROLL_RELEASE_GATE`, and reuse run CAS/ledger translation.
   Tests: resize reduction-first and hedge-before-short; full/partial/rejected hedge release twins; roll partial
   acquisition does not release old generation; full acquisition releases old shorts then hedges exactly once;
   stale generation twin. One live-fake adjust and one roll E2E.
3. **S3: approval and version/generation binding.** Add migration/binding fields, request joins, approval checks,
   and option-generation reservation.
   Tests: approval admits matching plan/version/catalog/generation/reservation; separate twins invalidate on
   version change, relevant catalog change, stale generation, inactive reservation, and exposure drift beyond own
   fills. PG test proves concurrent CAS/adjust approvals cannot both own the same generation.
4. **S4: bounded LIMIT dispatch lifecycle.** Replace gated-leg market dispatch, derive bounded prices, add timeout
   and explicit cancel/terminal outcomes.
   Tests: price unavailable/bound-exceeded twins; fresh LIMIT accepted E2E; timeout + zero-fill cancel; partial
   fill releases nothing or retains remainder; uncertain cancel is fenced and never repeated; no second broker
   call after restart.
5. **S5: protection continuity and end-to-end live readiness.** Wire owner checks, transfer, policy pinning, and
   staged exits across live adjust/restart.
   Tests: owner absent/superseded increase twins; reduce-only remains admitted; protective exit unresolved blocks
   adjust; restart duplicates no stage; one full live-fake resize/roll/protective-exit scenario with no real
   account.

Suggested commands: focused `tests/strategies/test_live_*`, `tests/options/*`, and one new
`tests/integration/test_live_options_c1_2_postgres.py`; use `.venv/bin/python -m pytest ... -q`. No full-suite run.

## Open questions and recommendations

1. **Limit timeout policy.** Recommendation: no automatic retry within a plan. A new governed attempt is safer
   than a background repricer, while platform-initiated risk reduction remains a narrowly authorized exception.
2. **Option margin freshness bound.** Recommendation: retain 60 seconds initially and revisit after fake-broker
   latency measurements; do not split margin and funds clocks until evidence shows they need it.
3. **Unfilled risk reduction.** Recommendation: expose a named action-required state and allow a new bounded
   platform exit plan with explicit approval policy; do not relax the limit bound merely because the leg is
   protective.
4. **Catalog strictness.** Recommendation: strict generation equality for C1.2 options. A later optimization may
   prove an unrelated catalog generation safe, but it should be explicit, tested, and live-options specific.
5. **Roll rollback.** Recommendation: no automatic unwind of a partially acquired new generation. Record
   `cleanup_required`, preserve owner/protection continuity, and make the remediation a new observed-state plan.

## Decisions (orchestrator, 2026-09-25)

Design accepted with all five recommendations:

- no automatic retry or repricing inside a plan;
- the 60-second margin freshness bound stays;
- an unfilled risk reduction becomes a named action-required state, not a relaxed bound;
- catalog generation must match exactly for live options;
- a partial roll never unwinds itself automatically (`cleanup_required`, then a new plan).

Also fixed in S1: B2.5's margin gate reads `required_margin_inr`, but the live reader only ever produced
`required_inr`, so `margin_limit_inr` was never enforced live. The live readers must provide `required_margin_inr`.

Order: S1 → S2 → S3 → S4 → S5. S4 also moves C1.1's dependent CNC buys onto the shared bounded-LIMIT helper, which
closes the C1 exit requirement.
