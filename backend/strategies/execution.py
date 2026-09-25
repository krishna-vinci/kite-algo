"""The paper plan executor: the first consumer of a frozen plan (Project 6, D-2).

The chain — proposal → validated plan → paper admission + reservation →
**execution** → attributed paper fills → settlement — is exercised end to end
for the first time here, on paper accounts only (D-1). The executor:

* fails closed through a precondition chain where every refusal is NAMED and
  lands in the append-only event trail (``PAPER_ONLY_EXECUTION`` for anything
  that is not a paper book — live is a separate authorization that does not
  exist yet);
* derives each step from the plan's resolved representation (target − current
  attributed book, floored to the pinned catalog's lot size; a zero delta is a
  real ``no_op`` outcome, not an absence);
* submits through the EXISTING paper runtime
  (``PaperTradingService.place_order``) with attribution carrying the BOUND
  run's ``strategy_run_id`` plus the plan/reservation/step refs, so G1's paper
  fold attributes the fills and the whole audit chain is recoverable from the
  order metadata alone;
* consumes the reservation on fills and releases it ``terminal_unfilled`` on
  rejections (D-4) — Phase 4's deliberately-open lifecycle closes here;
* records work events on the settlement barrier (created on submission,
  resolved on a terminal outcome) and leaves settlement assessment to its own
  surface — an observer, never a new settlement semantics (D-5).

Execution events are append-only facts (D-3): current step state is derived
from the trail, never stored, so history cannot be rewritten into something
that did not happen. Unknown execution state (a runtime error, an accepted
order that never reached a terminal outcome) records ``failed`` and HOLDS the
reservation — capacity is never released on a guess.

Concurrency: one plan executes at most once. The ``submitted`` event and the
already-executing check commit inside ONE transaction that holds the plan's
advisory lock on PostgreSQL, so two concurrent executes produce exactly one
submission; the loser refuses ``PLAN_ALREADY_EXECUTED`` from the committed
trail. In-process threads are additionally serialized on a per-plan lock.
"""

from __future__ import annotations

import math
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select, text

from backend.strategies.attribution_models import (
    EXECUTABLE_PLAN_KINDS,
    StrategyAdmissionPolicy,
    StrategyPositionProjection,
    StrategyProposal,
    StrategyRunBinding,
)
from backend.strategies.reservations import (
    CapacityExceeded,
    HOLDING_STATUSES,
    ReservationLedger,
    _as_datetime,
)
from backend.strategies.settlement import ExecutionBarrier

#: The paper book this executor executes. Anything else (live, dry_run) is a
#: different authorization and a different book: refused by name.
PAPER_ENVIRONMENT = "paper"

#: Refusal vocabulary. Every refusal is an event with its reason (D-3) and a
#: catchable exception with the same name — an operator never has to guess.
REFUSAL_REASONS = (
    "PLAN_KIND_UNSUPPORTED",
    "PLAN_NOT_VALIDATED",
    "RESERVATION_REQUIRED",
    "PAPER_ONLY_EXECUTION",
    "RESERVATION_EXPIRED",
    "STRATEGY_RUN_BINDING_MISSING",
    "ACCOUNT_SCOPE_MISMATCH",
    "PLAN_ALREADY_EXECUTED",
    "PAPER_ORDER_REJECTED",
    "PAPER_EXECUTION_FAILED",
    "PLAN_UNITS_UNPINNED",
    "PLAN_SIZING_UNAVAILABLE",
    "PLAN_REFERENCE_PRICE_UNAVAILABLE",
    "PLAN_ALLOCATION_UNAVAILABLE",
    "PLAN_CAPITAL_BASIS_UNPINNED",
    "PLAN_CAPITAL_BASIS_DRIFT",
    "OPTION_HEDGE_NOT_FILLED",
    "OPTION_HEDGE_RELEASE_WITHHELD",
    "ROLL_UNKNOWN",
    "ROLL_PLAN_INVALID",
    "ROLL_PLAN_MISMATCH",
    "ROLL_CLOSE_NOT_RELEASED",
    "ROLL_CLOSE_REQUIRES_BINDING",
    "ROLL_FILL_RECORD_FAILED",
)

#: Reservation statuses from which this plan may still begin executing.
EXECUTABLE_RESERVATION_STATUSES = ("active", "renewed")

#: Plan kinds whose steps are a CNC portfolio rebalance, and are therefore
#: eligible for the generic staged sell-before-buy financing rule. Futures rolls
#: (acquire-first) and option structures (hedge-first) own their own ordering and
#: are deliberately excluded.
CNC_REBALANCE_PLAN_KINDS = ("intent_bundle", "target_weights")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    """A finite float, or ``None`` when the caller's evidence is not a number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hedge_outcome(events: Sequence[str]) -> str:
    """The hedge gate's outcome word, from the hedge legs' recorded events.

    ``failed`` is the trail's word for "the runtime did not do what was asked";
    the gate has one word for a hedge that never arrived, so it maps to
    ``rejected``. The raw events always travel in the refusal detail, so nothing
    is lost by the mapping.
    """
    if any(event in ("rejected", "failed") for event in events):
        return "rejected"
    if "partially_filled" in events:
        return "partially_filled"
    if events and all(event == "filled" for event in events):
        return "filled"
    return "pending"


def _event_for_step(outcomes: Sequence[Mapping[str, Any]], step_no: int) -> str:
    """The recorded event for one step, or ``""`` when it never produced one.

    An unrecorded (or unrecognised) event is deliberately NOT treated as a
    confirmation: staged financing releases a dependent buy only against
    ``filled``/``no_op``.
    """
    for outcome in outcomes:
        if int(outcome.get("step_no") or 0) == int(step_no):
            return str(outcome.get("event") or "")
    return ""


class ExecutionRefusal(Exception):
    """A named, fail-closed refusal from the executor (D-2)."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


class PaperPlanExecutor:
    """Executes one validated, admitted paper plan through the paper runtime."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        paper_service: Any = None,
        *,
        barrier: Optional[ExecutionBarrier] = None,
        ledger: Optional[ReservationLedger] = None,
        clock: Optional[Callable[[], datetime]] = None,
        paper_service_factory: Optional[Callable[[], Any]] = None,
        fill_progress_store: Any = None,
        option_run_store: Any = None,
        plan_binding_store: Any = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        if paper_service is None and paper_service_factory is not None:
            paper_service = paper_service_factory()
        self._paper_service = paper_service
        self.paper_service_factory = paper_service_factory
        self.barrier = barrier or ExecutionBarrier(session_factory=session_factory)
        self.ledger = ledger or ReservationLedger(session_factory=session_factory)
        if fill_progress_store is None:
            from backend.paper_runtime.partial_fills import PaperFillProgressStore

            fill_progress_store = PaperFillProgressStore(session_factory=session_factory)
        self.fill_progress = fill_progress_store
        # The options lane executes through its own durable engine; these two
        # collaborators are injectable so a test can drive the real classes
        # without a real database, and the production path uses the real stores.
        self._option_run_store = option_run_store
        self._plan_binding_store = plan_binding_store
        self._clock = clock or _utcnow
        self._plan_locks: Dict[str, threading.Lock] = {}
        self._plan_locks_guard = threading.Lock()

    # ------------------------------------------------------------------ entry

    async def execute(self, plan: Mapping[str, Any], *, actor: str) -> Dict[str, Any]:
        """Execute one plan on paper, or refuse by name with a trail event."""
        plan_id = str(plan.get("plan_id") or "")
        plan_kind = str(plan.get("plan_kind") or "")
        reservation = self.ledger.for_plan(plan_id)

        # A plan executes once, ever. A committed ``submitted`` event is the
        # proof of a prior execution attempt; this guard raises WITHOUT
        # appending a trail event (the trail already tells that story, and a
        # refusal row here would muddy its derivation). Refusal-only trails do
        # NOT block a corrected retry — a refusal is evidence, not execution.
        # The concurrent double-execute case is decided per step inside the
        # locked submission transaction below.
        if self._has_submission(plan_id):
            raise ExecutionRefusal(
                "PLAN_ALREADY_EXECUTED",
                {
                    "plan_id": plan_id,
                    "message": "This plan already has an execution submission; a plan executes once",
                },
            )

        steps_spec = None
        try:
            envelope = self._envelope(plan)
            self._plan_preconditions(plan, envelope)
            binding = self._binding(envelope, plan)
            roll_ref = self._roll_binding(plan)
            if roll_ref:
                roll = self._roll_preconditions(plan, roll_ref)
            else:
                # No declared role: the plan still may not close an open roll's
                # old leg. There is no "optional" roll bypass.
                self._refuse_ungated_roll_close(
                    plan,
                    strategy_id=str(plan.get("strategy_id") or ""),
                    account_id=str(plan.get("account_id") or ""),
                )
                roll = None
            # The options lane resolves to the DURABLE run it executes against
            # (creating it for an entry plan, validating the reference for an
            # exit) FIRST, and derives its steps from that run's OWN confirmed
            # executions. The aggregate strategy projection mixes structures that
            # share a contract, so it must not size an option step (an exit that
            # read it could close another structure or open a new exposure).
            option_target = None
            if plan_kind == "option_structure":
                option_target = self._resolve_option_target(plan, binding)
                steps_spec = self._option_run_steps(plan, option_target)
            else:
                steps_spec = self._plan_steps(plan, binding)
            # Capacity is reserved only when exposure increases (D-6): a bundle
            # whose every leg reduces risk needs no reservation, while any
            # increasing leg demands the full active+paper+valid chain. Exposure
            # is measured per leg as "grows the book" OR "crosses flat", so
            # opening a SHORT (target -50 from flat) and reversing (+10 -> -5, a
            # sell of 15) both demand admission - using the sign of the quantity
            # alone would let every new short and every reversal past admission.
            increasing = any(
                leg.get("_increases_exposure") for _, leg, _quantity, _side in steps_spec
            )
            if increasing or reservation is not None:
                self._reservation_preconditions(reservation)
        except ExecutionRefusal as exc:
            self._record_event(
                plan_id,
                step_no=1,
                event="rejected",
                refusal_reason=exc.reason_code,
                actor_id=actor,
                detail=exc.detail,
            )
            raise
        base = self._clock()


        # Option structures enter hedge-first, and a short leg is released only
        # against a CONFIRMED hedge fill: R3's full-required-fill rule, which the
        # options lane reuses as the hedge gate. ``hedge_fill_gate`` is the
        # existing pure rule; this loop is its production caller. A structure with
        # no BUY leg at all is an admitted naked shape (Project 10), so the gate
        # is not applied to it - there is nothing to release against.
        step_order = list(steps_spec)
        gating = plan_kind == "option_structure"
        hedge_required = 0
        hedge_filled = 0
        hedge_events: List[str] = []
        short_exit_symbols: Dict[str, int] = {}
        closed_short_quantities: Dict[str, int] = {}
        if gating and option_target is not None:
            # The run's OWN evidence seeds the hedge-release rule: a short leg
            # this run already holds FLAT (closed by an earlier plan) is proven
            # closed, so a later plan may release the hedge it defended.
            run_state = option_target.get("run")
            open_now = self._option_run_open_by_leg(run_state)
            for run_leg in getattr(run_state, "legs", []) or []:
                if str((run_leg or {}).get("transaction_type") or "").upper() != "SELL":
                    continue
                if int(open_now.get(str((run_leg or {}).get("leg_id") or ""), 0)) == 0:
                    closed_short_quantities[str((run_leg or {}).get("tradingsymbol") or "")] = int(
                        (run_leg or {}).get("quantity") or 0
                    )
        if gating:
            # Role, not side, decides the order. An OPENING buy leg is the hedge
            # defending an OPENING sell leg (the short); a leg that REDUCES the
            # book is a CLOSE - a buy-to-close short is not a hedge, and a
            # sell-to-close hedge is not a short awaiting release. Entry uses the
            # existing option engine's buy-first planner; exits use its exit
            # builder's rule (close short liabilities first). One engine, reused.
            from backend.options.execution.planner import build_entry_order_plan
            from backend.options.protection.exit_builder import build_structure_exit_orders

            entry_steps = [step for step in steps_spec if step[1].get("_increases_exposure")]
            exit_steps = [step for step in steps_spec if not step[1].get("_increases_exposure")]
            entry_steps = self._ordered_entry_steps(entry_steps, build_entry_order_plan)
            exit_steps = self._ordered_exit_steps(exit_steps, build_structure_exit_orders)
            if str((option_target or {}).get("phase") or "") == "adjust":
                # An ADJUST reduces first and increases second: a reduction frees
                # the run's own hedge only against its PROVEN short closure, while
                # every increase is released only against a confirmed hedge fill.
                # The two orderings are the entry and exit builders' own; nothing
                # is re-derived here.
                step_order = exit_steps + entry_steps
            else:
                step_order = entry_steps + exit_steps
            hedge_required = sum(
                abs(int(quantity)) for _, _, quantity, side in entry_steps if side == "BUY"
            )
            # Symbols the plan closes a SHORT on: their proven closure is what a
            # hedge release must be measured against (the exit builder decides it).
            short_exit_symbols = {
                str(leg.get("tradingsymbol") or ""): abs(int(leg.get("_current_quantity") or 0))
                for _, leg, _quantity, _side in exit_steps
                if int(leg.get("_current_quantity") or 0) < 0
            }

        # Entering/exiting is persisted BEFORE the first submission: the durable
        # run must never read as "created" while orders are already in flight. A
        # plan with NOTHING to do (every step is a zero delta - a repeated exit
        # against an already-flat run) changes no run state, so it must not try.
        #
        # STAGED FINANCING (non-option lane): a plan that both reduces and
        # increases exposure places its reductions FIRST and releases its
        # dependent increases ONLY against those reductions' CONFIRMED outcomes.
        # A partial, rejected, failed or unobserved sale cannot fund a
        # replacement, so the dependent buy refuses by name instead of trading on
        # money the platform has not seen.
        # SCOPE: only the CNC portfolio-rebalance shapes, and only when the plan
        # is not already governed by a domain ordering that owns its sequence. A
        # futures ROLL deliberately acquires the replacement BEFORE releasing the
        # old contract (peak-margin semantics), and the options lane has its own
        # hedge/exit ordering; neither may be reordered by this generic rule.
        staged_by_release: Dict[int, List[int]] = {}
        # The product must be the CNC cash segment on EVERY leg: an
        # ``intent_bundle`` also carries NRML/MIS shapes whose sequencing and
        # margining are the domain's own, so the generic portfolio rule must not
        # silently adopt them.
        plan_products = {
            str(leg.get("product") or "").upper()
            for _index, leg, _quantity, _side in step_order
        }
        staging_applies = (
            not gating
            and roll_ref is None
            and str(plan.get("plan_kind") or "") in CNC_REBALANCE_PLAN_KINDS
            and bool(plan_products)
            and plan_products == {"CNC"}
        )
        if staging_applies:
            reducing_steps = [
                step
                for step in step_order
                if int(step[2] or 0) != 0 and not step[1].get("_increases_exposure")
            ]
            increasing_steps = [
                step
                for step in step_order
                if int(step[2] or 0) != 0 and step[1].get("_increases_exposure")
            ]
            if reducing_steps and increasing_steps:
                untouched = [
                    step for step in step_order if step not in reducing_steps and step not in increasing_steps
                ]
                step_order = untouched + reducing_steps + increasing_steps
                reducing_numbers = [int(step[0]) for step in reducing_steps]
                for step in increasing_steps:
                    staged_by_release[int(step[0])] = list(reducing_numbers)

        option_touches_run = bool(option_target) and any(
            int(step[2] or 0) != 0 for step in step_order
        )
        if option_target is not None and option_touches_run:
            option_target = self._begin_option_run(option_target, plan_id=plan_id, actor=actor)

        outcomes: List[Dict[str, Any]] = []
        order_ids: List[str] = []
        filled_ids: List[str] = []
        rejected = False
        failed = False
        filled = False
        submitted_any = False
        partial_order_ids: List[str] = []
        counter = 0

        def _stamp() -> datetime:
            nonlocal counter
            # Two ticks per event that has an outcome: ``_submit_step`` stamps the
            # outcome at ``at + 1us``, so a one-tick step would let the NEXT step's
            # submission share the previous outcome's ``created_at`` - and
            # ``created_at`` is the trail's only ordering.
            counter += 2
            return base + timedelta(microseconds=counter)

        for step_no, leg, quantity, side in step_order:
            dependent_on = staged_by_release.get(int(step_no))
            if dependent_on:
                unresolved_sales = [
                    number
                    for number in dependent_on
                    if _event_for_step(outcomes, number) not in ("filled", "no_op")
                ]
                if unresolved_sales:
                    outcomes.append(
                        self._record_event(
                            plan_id,
                            step_no=step_no,
                            event="rejected",
                            refusal_reason="FINANCING_UNSECURED",
                            actor_id=actor,
                            detail={
                                "instrument_id": leg.get("instrument_id"),
                                "tradingsymbol": leg.get("tradingsymbol"),
                                "side": side,
                                "quantity": int(quantity),
                                "funding_legs": list(dependent_on),
                                "unresolved_funding_legs": unresolved_sales,
                                "confirmed_funding": {
                                    str(number): _event_for_step(outcomes, number)
                                    for number in dependent_on
                                },
                                "message": (
                                    "This leg increases exposure and depends on this plan's own "
                                    "reductions. Only CONFIRMED sale outcomes may fund it; a "
                                    "partial, rejected, failed or unobserved reduction leaves the "
                                    "financing unsecured."
                                ),
                            },
                            at=_stamp(),
                        )
                    )
                    rejected = True
                    continue
                # The reductions are CONFIRMED, but confirmation is not money.
                # Re-derive, under the reservation's own account lock, whether
                # the account's CURRENT authoritative funds can carry this
                # increase. Nothing projected is credited: the figure comes from
                # the paper runtime, which has already applied every fill.
                authorization = await self._authorize_staged_increase(
                    plan,
                    reservation,
                    leg=leg,
                    quantity=int(quantity),
                    actor=actor,
                )
                if not authorization.get("authorized"):
                    outcomes.append(
                        self._record_event(
                            plan_id,
                            step_no=step_no,
                            event="rejected",
                            refusal_reason=str(
                                authorization.get("reason_code") or "ACCOUNT_FUNDS_UNSECURED"
                            ),
                            actor_id=actor,
                            detail={
                                "instrument_id": leg.get("instrument_id"),
                                "tradingsymbol": leg.get("tradingsymbol"),
                                "side": side,
                                "quantity": int(quantity),
                                "funding_legs": list(dependent_on),
                                **dict(authorization.get("detail") or {}),
                                "message": (
                                    "This increase is released only against CONFIRMED funding, "
                                    "and the account's authoritative funds at submission time "
                                    "cannot carry it alongside its other commitments."
                                ),
                            },
                            at=_stamp(),
                        )
                    )
                    rejected = True
                    continue
            if quantity == 0:
                outcomes.append(
                    self._record_event(
                        plan_id,
                        step_no=step_no,
                        event="no_op",
                        actor_id=actor,
                        detail={
                            "instrument_id": leg.get("instrument_id"),
                            "target_quantity": leg.get("signed_quantity"),
                            "current_quantity": leg.get("_current_quantity"),
                            "message": "The attributed book already sits at the plan's target",
                        },
                        at=_stamp(),
                    )
                )
                continue

            if (
                gating
                and hedge_required > 0
                and quantity < 0
                and leg.get("_increases_exposure")
            ):
                from backend.options.protection.hedge_gate import hedge_fill_gate

                decision = hedge_fill_gate(
                    required_hedge_quantity=hedge_required,
                    confirmed_filled_quantity=hedge_filled,
                    dependent_short_quantity=abs(int(quantity)),
                    outcome=_hedge_outcome(hedge_events),
                )
                if int(decision.released_quantity) < abs(int(quantity)):
                    # The hedge has not proven enough to carry the short: record
                    # the refusal with the gate's own evidence and do NOT submit.
                    outcomes.append(
                        self._record_event(
                            plan_id,
                            step_no=step_no,
                            event="rejected",
                            refusal_reason="OPTION_HEDGE_NOT_FILLED",
                            actor_id=actor,
                            detail={
                                **decision.as_dict(),
                                "instrument_id": leg.get("instrument_id"),
                                "tradingsymbol": leg.get("tradingsymbol"),
                                "quantity": quantity,
                                "required_hedge_quantity": hedge_required,
                                "confirmed_hedge_quantity": hedge_filled,
                                "hedge_outcomes": list(hedge_events),
                            },
                            at=_stamp(),
                        )
                    )
                    rejected = True
                    continue

            if gating and quantity < 0 and not leg.get("_increases_exposure"):
                # A closing SELL that releases a hedge is released only against the
                # short closure this plan has PROVEN. The existing exit builder
                # states that rule (shorts precede hedges, a hedge is withheld
                # until its short is proven closed), so the decision is delegated
                # to it rather than re-derived here.
                from backend.options.protection.exit_builder import (
                    build_structure_exit_orders as _build_exits,
                )

                # A short this RUN already holds flat still answers for its
                # hedge: the run's own evidence proves the closure, so the builder
                # can attribute the release instead of withholding forever.
                proven_flat_shorts = [
                    {
                        "tradingsymbol": str(step[1].get("tradingsymbol") or ""),
                        "side": "SELL",
                        "quantity": int(
                            step[1].get("quantity")
                            or abs(int(step[1].get("signed_quantity") or 0))
                            or 0
                        ),
                    }
                    for step in exit_steps
                    if int(step[1].get("_current_quantity") or 0) == 0
                    and str(step[1].get("tradingsymbol") or "") in closed_short_quantities
                ]
                ordered, meta = _build_exits(
                    [
                        {
                            "tradingsymbol": str(step[1].get("tradingsymbol") or ""),
                            "side": "SELL",
                            "quantity": abs(int(step[1].get("_current_quantity") or 0)),
                        }
                        for step in exit_steps
                        if int(step[1].get("_current_quantity") or 0) < 0
                    ]
                    + [
                        {
                            "tradingsymbol": str(step[1].get("tradingsymbol") or ""),
                            "side": "BUY",
                            "quantity": abs(int(step[1].get("_current_quantity") or 0)),
                        }
                        for step in exit_steps
                        if int(step[1].get("_current_quantity") or 0) > 0
                    ]
                    + proven_flat_shorts,
                    closed_short_quantities=dict(closed_short_quantities),
                )
                released = any(
                    str(order.get("tradingsymbol") or "")
                    == str(leg.get("tradingsymbol") or "")
                    for order in ordered
                )
                if not released:
                    withheld = [
                        row
                        for row in (meta.get("withheld_hedges") or [])
                        if str(row.get("tradingsymbol") or "")
                        == str(leg.get("tradingsymbol") or "")
                    ]
                    outcomes.append(
                        self._record_event(
                            plan_id,
                            step_no=step_no,
                            event="rejected",
                            refusal_reason="OPTION_HEDGE_RELEASE_WITHHELD",
                            actor_id=actor,
                            detail={
                                "tradingsymbol": leg.get("tradingsymbol"),
                                "quantity": quantity,
                                "proven_short_closures": dict(closed_short_quantities),
                                "plan_short_symbols": sorted(short_exit_symbols),
                                "withheld": withheld,
                                "reason": "short_not_proven_closed",
                            },
                            at=_stamp(),
                        )
                    )
                    rejected = True
                    continue

            submission = await self._submit_step(
                plan, reservation, actor, step_no=step_no, leg=leg, quantity=quantity,
                side=side, binding=binding, at=_stamp(),
            )
            outcomes.append(submission["outcome"])
            submitted_any = True
            if submission["order_id"]:
                order_ids.append(submission["order_id"])
            event = submission["outcome"]["event"]
            if event in ("filled", "partially_filled"):
                filled_ids.append(submission["order_id"] or "")
                if gating and side == "BUY" and leg.get("_increases_exposure"):
                    # Only a CONFIRMED fill may release a dependent short; a
                    # submitted order releases nothing. Only an OPENING hedge
                    # counts: a buy that closes a short defends nothing.
                    hedge_filled += abs(
                        int(submission["outcome"].get("filled_quantity") or 0)
                    )
            if gating and side == "BUY" and leg.get("_increases_exposure"):
                hedge_events.append(str(event))
            if gating and side == "BUY" and not leg.get("_increases_exposure"):
                # A closing BUY is how a SHORT position closes: only a CONFIRMED
                # fill counts as its proven closure, and only proven closure
                # releases the hedge that bounds it.
                if event in ("filled", "partially_filled"):
                    symbol = str(leg.get("tradingsymbol") or "")
                    closed_short_quantities[symbol] = closed_short_quantities.get(
                        symbol, 0
                    ) + abs(int(submission["outcome"].get("filled_quantity") or 0))
            if (
                roll is not None
                and str((roll_ref or {}).get("role") or "") == "open_new"
                and event in ("filled", "partially_filled")
            ):
                # A CONFIRMED replacement execution is the roll's proof. It is
                # recorded durably against the roll (idempotent by paper order),
                # so ``prove_filled`` never has to trust a caller's number and a
                # pre-existing holding can never stand in for the replacement.
                failure = self._record_roll_replacement_fill(
                    plan=plan,
                    roll=roll,
                    leg=leg,
                    submission=submission,
                    actor=actor,
                    step_no=step_no,
                    stamp=_stamp,
                )
                if failure is not None:
                    outcomes.append(failure)
                    failed = True
            if event == "partially_filled":
                partial_order_ids.append(submission["order_id"] or "")
            rejected = rejected or event == "rejected"
            failed = failed or event == "failed"
            filled = filled or event == "filled"

        partial_ids = list(dict.fromkeys(partial_order_ids + self._open_remainders(filled_ids)))
        self._settle_reservation(
            reservation,
            actor,
            filled_ids=filled_ids,
            partial_ids=partial_ids,
            failed=failed,
            submitted_any=submitted_any,
        )

        if failed:
            status = "failed"
        elif filled:
            status = "filled"
        elif rejected:
            status = "rejected"
        else:
            status = "no_op"
        if option_target is not None and option_touches_run:
            self._settle_option_run(
                option_target,
                plan=plan,
                step_order=step_order,
                outcomes=outcomes,
                plan_id=plan_id,
                actor=actor,
            )
        return {
            "plan_id": plan_id,
            "status": status,
            "steps": outcomes,
            "reservation_id": reservation["reservation_id"] if reservation else None,
            "paper_order_ids": order_ids,
        }

    # ---------------------------------------------------------- preconditions

    async def _authorize_staged_increase(
        self,
        plan: Mapping[str, Any],
        reservation: Optional[Dict[str, Any]],
        *,
        leg: Mapping[str, Any],
        quantity: int,
        actor: str,
    ) -> Dict[str, Any]:
        """Prove the account's money is really there before a staged increase.

        The reservation deliberately deferred this increase; the executor is the
        only place that knows the reduction has actually confirmed, so this is
        where the second half of the contract is enforced. A missing price is
        unknown evidence and refuses: a buy whose notional cannot be computed
        cannot be shown to be affordable.
        """
        reservation_id = str((reservation or {}).get("reservation_id") or "")
        if not reservation_id:
            # A reduction-only plan needs no reservation, and a plan with no
            # reservation has no deferred increase to authorize.
            return {"authorized": True}
        price = _as_float(leg.get("reference_price"))
        if price is None or abs(price) <= 0:
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": {
                    "reason": "no_reference_price",
                    "reservation_id": reservation_id,
                },
            }
        service = self._paper_service
        if service is None:
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": {
                    "reason": "no_paper_runtime",
                    "reservation_id": reservation_id,
                },
            }
        try:
            summary = await service.get_account_summary(
                account_scope=str(plan.get("account_id") or "")
            )
            # The runtime's summary is FLAT; tolerate a nested envelope too, but
            # never invent a number: an unreadable figure refuses below.
            available = _as_float(
                summary.get("available_funds")
                if "available_funds" in summary
                else (summary.get("account") or {}).get("available_funds")
            )
        except Exception as exc:  # noqa: BLE001 - unknown money never buys
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": {
                    "reason": "account_funds_unreadable",
                    "error": str(exc),
                    "reservation_id": reservation_id,
                },
            }
        if available is None:
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": {
                    "reason": "account_funds_unknown",
                    "reservation_id": reservation_id,
                },
            }
        notional = abs(float(int(quantity))) * float(price)
        try:
            return self.ledger.authorize_staged_increase(
                plan_id=str(plan.get("plan_id") or ""),
                requirement_inr=notional,
                account_capacity_inr=float(available),
                actor_id=actor,
                evidence={
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "notional_inr": notional,
                    "available_funds_inr": float(available),
                },
            )
        except CapacityExceeded as exc:
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": dict(exc.detail),
            }
        except Exception as exc:  # noqa: BLE001 - never spend on an unproved claim
            return {
                "authorized": False,
                "reason_code": "ACCOUNT_FUNDS_UNSECURED",
                "detail": {"reason": "authorization_failed", "error": str(exc)},
            }

    def _envelope(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        from backend.strategies.proposals import ProposalStore

        envelope = ProposalStore(session_factory=self.session_factory).get_proposal(
            str(plan.get("proposal_id") or "")
        )
        if envelope is None:
            raise ExecutionRefusal(
                "PLAN_NOT_VALIDATED",
                {"plan_id": str(plan.get("plan_id") or ""), "message": "No proposal envelope exists for this plan"},
            )
        return envelope

    def _plan_preconditions(self, plan: Mapping[str, Any], envelope: Mapping[str, Any]) -> None:
        """Plan-identity preconditions. Every exit is named (D-2)."""
        plan_id = str(plan.get("plan_id") or "")

        # 1. Only plan kinds the executor can act on. ``target_weights`` stays a
        #    valid plan kind — it simply has no executor in this phase.
        if plan.get("plan_kind") not in EXECUTABLE_PLAN_KINDS:
            raise ExecutionRefusal(
                "PLAN_KIND_UNSUPPORTED",
                {"plan_id": plan_id, "plan_kind": str(plan.get("plan_kind") or "")},
            )
        # 2. A frozen plan executes only while its envelope is validated.
        if str(envelope.get("status") or "") != "validated":
            raise ExecutionRefusal(
                "PLAN_NOT_VALIDATED",
                {"plan_id": plan_id, "proposal_status": str(envelope.get("status") or "")},
            )

    def _reservation_preconditions(self, reservation: Optional[Dict[str, Any]]) -> None:
        """Reservation preconditions: admission consumed, paper, within validity."""
        plan_id = str(reservation.get("plan_id") or "") if reservation else ""
        # 3. An active reservation must exist: execution CONSUMES admission (D-1).
        if reservation is None or str(reservation.get("status")) not in EXECUTABLE_RESERVATION_STATUSES:
            raise ExecutionRefusal(
                "RESERVATION_REQUIRED",
                {
                    "plan_id": plan_id,
                    "reservation_status": None if reservation is None else str(reservation.get("status")),
                },
            )
        # 4. Paper accounts only, ever. Live enablement is a separate,
        #    not-yet-existing authorization (phase boundary).
        environment = str(reservation.get("execution_environment") or "")
        if environment != PAPER_ENVIRONMENT:
            raise ExecutionRefusal(
                "PAPER_ONLY_EXECUTION",
                {
                    "plan_id": plan_id,
                    "execution_environment": environment,
                    "message": "This phase executes paper accounts only; live is a separate authorization",
                },
            )
        # 5. Validity: an expired reservation is no authority to create exposure.
        valid_until = _as_datetime(reservation.get("valid_until"))
        if valid_until is not None and self._clock() >= valid_until:
            raise ExecutionRefusal(
                "RESERVATION_EXPIRED",
                {
                    "plan_id": plan_id,
                    "reservation_id": str(reservation.get("reservation_id")),
                    "valid_until": valid_until.isoformat(),
                },
            )

    def _binding(self, envelope: Mapping[str, Any], plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The attribution target must exist and agree with the plan's scope."""
        run_id = str(envelope.get("strategy_run_id") or "")
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyRunBinding).where(StrategyRunBinding.strategy_run_id == run_id)
            ).scalar_one_or_none()
        if row is None:
            raise ExecutionRefusal(
                "STRATEGY_RUN_BINDING_MISSING",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "strategy_run_id": run_id,
                    "message": "No bound run exists to attribute the fills to",
                },
            )
        binding = {
            "strategy_run_id": str(row.strategy_run_id),
            "strategy_id": str(row.strategy_id),
            "owner_id": str(row.owner_id),
            "account_id": str(row.account_id),
            "execution_environment": str(row.execution_environment),
        }
        if (
            binding["account_id"] != str(plan.get("account_id") or "")
            or binding["strategy_id"] != str(plan.get("strategy_id") or "")
            or binding["execution_environment"] != PAPER_ENVIRONMENT
        ):
            raise ExecutionRefusal(
                "ACCOUNT_SCOPE_MISMATCH",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "binding_account_id": binding["account_id"],
                    "plan_account_id": str(plan.get("account_id") or ""),
                    "binding_environment": binding["execution_environment"],
                },
            )
        return binding

    # ----------------------------------------------------------------- steps

    # ------------------------------------------------------------ roll seam

    @staticmethod
    def _planner_leg(step: Any) -> Dict[str, Any]:
        """One frozen step in the option engine's planner shape."""
        index, leg, quantity, side = step
        return {
            "leg_id": f"step-{index}",
            "exchange": leg.get("exchange"),
            "tradingsymbol": leg.get("tradingsymbol"),
            "quantity": abs(int(quantity)),
            "transaction_type": side,
        }

    def _ordered_entry_steps(
        self, steps: Sequence[Any], build_entry_order_plan: Any
    ) -> List[Any]:
        """Buy-first entry order, decided BY the option engine's own planner."""
        planned = build_entry_order_plan(
            [self._planner_leg(step) for step in steps], product=""
        )
        rank = {
            str(order.get("leg_id") or ""): index
            for index, order in enumerate(planned)
        }
        return sorted(
            steps, key=lambda step: (rank.get(f"step-{step[0]}", len(rank)), step[0])
        )

    def _ordered_exit_steps(
        self, steps: Sequence[Any], build_structure_exit_orders: Any
    ) -> List[Any]:
        """Close SHORT liabilities first, then release hedges.

        For a closing plan the builder's rule reads: the order that closes a short
        position (a BUY, because the position is negative) precedes the order that
        releases a hedge (a SELL, because the position is positive). The builder
        orders exactly those two classes of positions, so the ordering is
        delegated to it and this method only maps the result back to the frozen
        steps.
        """
        positions = []
        for step in steps:
            current = int(step[1].get("_current_quantity") or 0)
            if current == 0:
                continue
            positions.append(
                {
                    "tradingsymbol": str(step[1].get("tradingsymbol") or ""),
                    "side": "SELL" if current < 0 else "BUY",
                    "quantity": abs(current),
                }
            )
        ordered, _meta = build_structure_exit_orders(positions)
        rank: Dict[str, int] = {}
        for index, order in enumerate(ordered):
            rank.setdefault(str(order.get("tradingsymbol") or ""), index)

        def _key(step: Any):
            symbol = str(step[1].get("tradingsymbol") or "")
            return (
                0 if int(step[2]) > 0 else 1,
                rank.get(symbol, len(rank)),
                step[0],
            )

        return sorted(steps, key=_key)

    @staticmethod
    def _roll_binding(plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The roll role the FROZEN plan declares, or ``None``.

        Read from the resolved artifact the platform froze, never from the
        caller's request, so a plan that names no roll is unaffected and a plan
        that names one cannot have its role changed after the fact.
        """
        from backend.strategies.rolls import ROLL_PLAN_ROLES

        raw = (plan.get("resolved_plan") or {}).get("roll")
        if not isinstance(raw, Mapping) or not raw:
            return None
        role = str(raw.get("role") or "").strip().lower()
        if role not in ROLL_PLAN_ROLES:
            raise ExecutionRefusal(
                "ROLL_PLAN_INVALID",
                {"plan_id": str(plan.get("plan_id") or ""), "roll_role": role},
            )
        roll_id = raw.get("roll_id")
        return {"roll_id": None if roll_id is None else str(roll_id), "role": role}

    def _roll_machine(self) -> Any:
        from backend.strategies.rolls import RollStateMachine

        return RollStateMachine(session_factory=self.session_factory)

    # ------------------------------------------------------ option run seam

    #: The exit-side vocabulary. An exit may only REDUCE this run's own position.
    @staticmethod
    def _run_leg_identity(leg: Mapping[str, Any]) -> str:
        metadata = leg.get("metadata") or {}
        instrument_id = metadata.get("instrument_id") if isinstance(metadata, Mapping) else None
        return str(instrument_id or leg.get("tradingsymbol") or "")

    @staticmethod
    def _option_run_open_by_leg(run: Any) -> Dict[str, int]:
        """The run's OWN open quantity per leg, from its recorded trades only."""
        open_by_leg: Dict[str, int] = {}
        for trade in getattr(run, "trades", []) or []:
            leg_id = str((trade or {}).get("leg_id") or "")
            quantity = int((trade or {}).get("quantity") or 0)
            side = str((trade or {}).get("transaction_type") or "").upper()
            open_by_leg[leg_id] = open_by_leg.get(leg_id, 0) + (
                quantity if side == "BUY" else -quantity
            )
        return open_by_leg

    @staticmethod
    def _option_run_generation(run: Any) -> int:
        """The run's held leg generation. Absent means the first one."""
        metadata = getattr(run, "metadata", None) or {}
        try:
            generation = int(metadata.get("structure_generation") or 1)
        except (TypeError, ValueError):
            return 1
        return generation if generation >= 1 else 1

    @staticmethod
    def _option_run_shape_digest(run: Any) -> str:
        """The shape the run HOLDS now: its own record first, its entry block next.

        A shape-changing adjust rewrites the record, so an adjustment NEVER has to
        re-derive the previous generation's identity from the plan that opened it.
        """
        metadata = getattr(run, "metadata", None) or {}
        recorded = str(metadata.get("structure_digest") or "")
        if recorded:
            return recorded
        return str((getattr(run, "protection", None) or {}).get("structure_digest") or "")

    @staticmethod
    def _option_protection_block(run: Any) -> Optional[Dict[str, Any]]:
        """The run's protection state when it is ACTIVE, else ``None``.

        The read is the worker safety gate's own: ``evaluate_option_protection_state``
        over the run's frozen protection block. Protection counts as ACTIVE when
        it has TRIGGERED, or when it is UNREADABLE - not knowing is never treated
        as "clear" for a step that would increase exposure.
        """
        from backend.options.protection.runtime import evaluate_option_protection_state

        try:
            verdict = evaluate_option_protection_state(run=run)
        except Exception as exc:  # noqa: BLE001 - an unreadable state is never "clear"
            return {"triggered": None, "unreadable": True, "error": type(exc).__name__}
        if not bool(verdict.get("triggered")):
            return None
        return {
            "triggered": True,
            "unreadable": False,
            "matched_rule": verdict.get("matched_rule"),
        }

    @staticmethod
    def _adjusted_protection(run: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The run's protection block, re-pointed at the generation it now holds."""
        resolved = plan.get("resolved_plan") or {}
        protection = dict(getattr(run, "protection", None) or {})
        for key in ("structure_digest", "structure_id", "underlying", "expiry_policy"):
            value = resolved.get(key)
            if value not in (None, ""):
                protection[key] = value
        return protection

    def _option_run_steps(
        self, plan: Mapping[str, Any], target: Mapping[str, Any]
    ) -> List[Any]:
        """One step per frozen leg, sized from THIS RUN's own confirmed fills.

        Entry: target = the frozen leg's signed quantity, current = the run's own
        open quantity for that leg (zero for a fresh run), so the order is the
        delta and never the strategy's aggregate total.

        Exit: target = FLAT for this run's own position on that leg. The frozen
        leg must name that contract, the closing direction must be the opposite
        of the run leg's open side, and a leg whose direction would OPEN (or
        extend) exposure is refused. A repeated or partial exit therefore closes
        only what remains, and can never overclose or touch another structure.

        Adjust: the frozen plan freezes the DESIRED TARGET, so each leg's order is
        ``signed(desired) - the run's own confirmed open``. A run leg the desired
        state omits is removed, and a desired leg the run does not hold is a new
        run leg. The delta is re-derived at every attempt and is never replayed
        from the approved plan, so a fill that landed between approval and
        submission only moves the run toward the target.
        """
        plan_id = str(plan.get("plan_id") or "")
        phase = str(target.get("phase") or "")
        run = target["run"]
        frozen = [dict(leg) for leg in (plan.get("resolved_plan") or {}).get("legs") or []]
        if not frozen:
            raise ExecutionRefusal("OPTION_PLAN_LEGS_MISSING", {"plan_id": plan_id})

        open_by_leg = self._option_run_open_by_leg(run)
        run_legs = {self._run_leg_identity(leg): leg for leg in getattr(run, "legs", []) or []}
        if phase == "adjust":
            return self._adjust_option_run_steps(
                plan,
                target,
                plan_id=plan_id,
                frozen=frozen,
                run=run,
                run_legs=run_legs,
                open_by_leg=open_by_leg,
            )

        steps: List[Any] = []
        for index, leg in enumerate(frozen, start=1):
            identity = str(leg.get("instrument_id") or leg.get("tradingsymbol") or "")
            run_leg = run_legs.get(identity)
            lot = self._pinned_lot(plan, leg)
            if phase == "entry":
                if run_leg is None:
                    # The run was created from these very legs; a mismatch is a
                    # corrupted binding, not something to trade through.
                    raise ExecutionRefusal(
                        "OPTION_RUN_LEG_MISMATCH",
                        {"plan_id": plan_id, "instrument_id": identity},
                    )
                current = int(open_by_leg.get(str(run_leg.get("leg_id")), 0))
                target_quantity = int(
                    leg.get("signed_quantity")
                    if leg.get("signed_quantity") is not None
                    else leg.get("quantity") or 0
                )
                delta = target_quantity - current
                leg["_increases_exposure"] = self._opens_or_grows_exposure(
                    target_quantity, current
                )
            else:
                if run_leg is None:
                    raise ExecutionRefusal(
                        "OPTION_EXIT_LEG_MISMATCH",
                        {"plan_id": plan_id, "instrument_id": identity},
                    )
                current = int(open_by_leg.get(str(run_leg.get("leg_id")), 0))
                self._validate_option_exit_leg(
                    plan_id=plan_id, leg=leg, run_leg=run_leg, current=current
                )
                # Absolute FLAT for this run's own position: never a reversal.
                delta = -current
                leg["_increases_exposure"] = False
            quantity = self._floor_to_lot(delta, lot)
            leg["_current_quantity"] = current
            leg["_pinned_lot"] = lot
            # The RUN's leg identity, not this plan's step id: the run's own open
            # quantity is keyed by it, so an exit's fills must land on it too.
            leg["_run_leg_id"] = str(run_leg.get("leg_id") or f"{plan_id}:{index}")
            side = "BUY" if quantity > 0 else "SELL"
            steps.append((index, leg, quantity, side))
        return steps

    def _validate_option_exit_leg(
        self,
        *,
        plan_id: str,
        leg: Mapping[str, Any],
        run_leg: Mapping[str, Any],
        current: int,
    ) -> None:
        """An exit leg must belong to the run and REDUCE it, never open it."""
        identity = str(leg.get("instrument_id") or leg.get("tradingsymbol") or "")
        if str(run_leg.get("product") or "") != str(leg.get("product") or ""):
            raise ExecutionRefusal(
                "OPTION_EXIT_CONTRACT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "instrument_id": identity,
                    "run_product": str(run_leg.get("product") or ""),
                    "plan_product": str(leg.get("product") or ""),
                },
            )
        if current == 0:
            # Already flat for this run: the step is a no_op, but the DIRECTION
            # still has to be a close (a wrongly-signed exit is a bug, not a noop).
            open_side = str(run_leg.get("transaction_type") or "").upper()
            expected = "BUY" if open_side == "SELL" else "SELL"
            if str(leg.get("side") or "").upper() != expected:
                raise ExecutionRefusal(
                    "OPTION_EXIT_DIRECTION_MISMATCH",
                    {"plan_id": plan_id, "instrument_id": identity, "expected_side": expected},
                )
            return
        open_side = str(run_leg.get("transaction_type") or "").upper()
        closing_side = "BUY" if current < 0 else "SELL"
        if open_side and closing_side == open_side:
            # The run's own leg was opened the same way this "exit" would trade:
            # the binding is inconsistent, so refuse rather than guess.
            raise ExecutionRefusal(
                "OPTION_EXIT_DIRECTION_MISMATCH",
                {
                    "plan_id": plan_id,
                    "instrument_id": identity,
                    "run_leg_side": open_side,
                    "closing_side": closing_side,
                },
            )
        declared = str(leg.get("side") or "").upper()
        if declared and declared != closing_side:
            # A leg that would open or extend exposure is not an exit.
            raise ExecutionRefusal(
                "OPTION_EXIT_WOULD_OPEN",
                {
                    "plan_id": plan_id,
                    "instrument_id": identity,
                    "declared_side": declared,
                    "required_side": closing_side,
                    "run_open_quantity": current,
                },
            )

    def _adjust_option_run_steps(
        self,
        plan: Mapping[str, Any],
        target: Dict[str, Any],
        *,
        plan_id: str,
        frozen: Sequence[Mapping[str, Any]],
        run: Any,
        run_legs: Dict[str, Dict[str, Any]],
        open_by_leg: Mapping[str, int],
    ) -> List[Any]:
        """``signed(desired target) - this RUN's own confirmed open``, per leg.

        The frozen plan carries the target; the run's own trades are what it
        converges FROM. A resize is therefore one delta per contract, and a retry
        after a partial fill can only move the run TOWARD the approved target. A
        run leg the desired state no longer names is removed (target flat), and a
        desired leg the run does not hold becomes a new run leg named
        ``{plan_id}:{index}`` - appended to the run's legs BEFORE the first
        submission, so a fill lands on the leg the run will hold.

        Two shapes are not an adjust and refuse by name here: a REVERSAL on one
        contract (both non-zero with opposite signs - a reduce-to-zero and re-open
        is a re-entry with its own plan), and an expiry change (that is the S4
        roll, whose acquire-prove-release ordering this slice does not implement).

        Two safety gates decide whether the target may be reached AT ALL, before
        a single order is sized:

        * the NAKED gate - a desired state that leaves a short leg with less
          protective long coverage of the same option type than it covers refuses
          ``OPTION_ADJUSTMENT_WOULD_UNHEDGE``, unless the frozen protection
          policy declares the structure naked. The rule is the binding edge's own
          (``option_adjust_would_unhedge``), applied to the TARGET state rather
          than to intermediate steps;
        * the PROTECTION gate - while the strategy's protection for this
          structure is TRIGGERED or UNREADABLE, an adjust that contains ANY
          increase refuses ``OPTION_ADJUSTMENT_PROTECTION_ACTIVE``. A reduce-only
          adjust stays admissible: risk reduction is never blocked.
        """
        resolved = plan.get("resolved_plan") or {}
        from backend.options.execution.plan_binding import option_adjust_would_unhedge

        uncovered = option_adjust_would_unhedge(plan)
        if uncovered is not None:
            raise ExecutionRefusal(
                "OPTION_ADJUSTMENT_WOULD_UNHEDGE",
                {
                    "plan_id": plan_id,
                    "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
                    "phase": "adjust",
                    **uncovered,
                },
            )
        structure_expiry = str(resolved.get("expiry") or "")
        previous_legs = [dict(leg) for leg in getattr(run, "legs", []) or []]
        named: set[str] = set()
        steps: List[Any] = []
        desired_legs: List[Dict[str, Any]] = []
        for index, leg in enumerate(frozen, start=1):
            identity = str(leg.get("instrument_id") or leg.get("tradingsymbol") or "")
            named.add(identity)
            lot = self._pinned_lot(plan, leg)
            leg_expiry = str(leg.get("expiry") or "")
            if structure_expiry and leg_expiry and leg_expiry != structure_expiry:
                raise ExecutionRefusal(
                    "OPTION_ADJUSTMENT_UNSUPPORTED",
                    {
                        "plan_id": plan_id,
                        "instrument_id": identity,
                        "reason": "expiry_change_is_a_roll",
                        "plan_expiry": leg_expiry,
                        "structure_expiry": structure_expiry,
                    },
                )
            run_leg = run_legs.get(identity)
            if (
                run_leg is not None
                and structure_expiry
                and str(run_leg.get("expiry_key") or "") not in ("", structure_expiry)
            ):
                raise ExecutionRefusal(
                    "OPTION_ADJUSTMENT_UNSUPPORTED",
                    {
                        "plan_id": plan_id,
                        "instrument_id": identity,
                        "reason": "expiry_change_is_a_roll",
                        "run_expiry": str(run_leg.get("expiry_key") or ""),
                        "structure_expiry": structure_expiry,
                    },
                )
            if run_leg is None:
                from backend.options.execution.plan_binding import _to_execution_leg

                # The shape the entry edge writes for a leg: same ids, same
                # identity metadata, keyed by the ADJUST plan that opened it.
                run_leg = _to_execution_leg(leg, plan_id=plan_id, index=index)
                run_legs[identity] = run_leg
            current = int(open_by_leg.get(str(run_leg.get("leg_id")), 0))
            target_quantity = int(
                leg.get("signed_quantity")
                if leg.get("signed_quantity") is not None
                else leg.get("quantity") or 0
            )
            if target_quantity != 0 and current != 0 and (target_quantity > 0) != (current > 0):
                raise ExecutionRefusal(
                    "OPTION_ADJUSTMENT_UNSUPPORTED",
                    {
                        "plan_id": plan_id,
                        "instrument_id": identity,
                        "reason": "reversal_on_one_contract",
                        "target_quantity": target_quantity,
                        "run_open_quantity": current,
                        "message": (
                            "closing through flat and re-opening is a re-entry: it needs "
                            "its own plan, never one adjust"
                        ),
                    },
                )
            quantity = self._floor_to_lot(target_quantity - current, lot)
            leg["_current_quantity"] = current
            leg["_pinned_lot"] = lot
            # The RUN's leg identity: its own open quantity and this step's fills
            # are keyed by it, exactly as the entry and exit lanes key them.
            leg["_run_leg_id"] = str(run_leg.get("leg_id") or f"{plan_id}:{index}")
            leg["_increases_exposure"] = self._opens_or_grows_exposure(
                target_quantity, current
            )
            steps.append((index, leg, quantity, "BUY" if quantity > 0 else "SELL"))
            desired_legs.append(
                self._desired_adjust_run_leg(leg, run_leg=run_leg, target_quantity=target_quantity)
            )

        # A leg the run holds that the desired state does not name is REMOVED: the
        # target is flat, so the step closes exactly the run's own open - never a
        # position another structure holds.
        step_no = len(frozen)
        for run_leg in previous_legs:
            if self._run_leg_identity(run_leg) in named:
                continue
            current = int(open_by_leg.get(str(run_leg.get("leg_id")), 0))
            if current == 0:
                continue
            step_no += 1
            removal = self._removal_leg(run_leg, current=current)
            lot = self._pinned_lot(plan, removal)
            removal["_current_quantity"] = current
            removal["_pinned_lot"] = lot
            removal["_run_leg_id"] = str(run_leg.get("leg_id") or f"{plan_id}:{step_no}")
            removal["_increases_exposure"] = False
            quantity = self._floor_to_lot(-current, lot)
            steps.append((step_no, removal, quantity, "BUY" if quantity > 0 else "SELL"))

        # The protection split, decided from the run's own frozen protection block
        # (the worker safety gate's own read) and the DELTA this target implies: a
        # plan that only reduces may always proceed, while any increase is refused
        # while protection is triggered or unreadable.
        protection = self._option_protection_block(run)
        increasing_steps = [
            step for step in steps if bool(step[1].get("_increases_exposure"))
        ]
        if protection is not None and increasing_steps:
            raise ExecutionRefusal(
                "OPTION_ADJUSTMENT_PROTECTION_ACTIVE",
                {
                    "plan_id": plan_id,
                    "option_run_id": str(getattr(run, "strategy_run_id", "") or ""),
                    "option_run_status": str(getattr(run, "status", "") or ""),
                    **protection,
                    "increasing_legs": [
                        {
                            "instrument_id": str(leg.get("instrument_id") or ""),
                            "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                            "side": str(leg.get("side") or ""),
                        }
                        for _index, leg, _quantity, _side in increasing_steps
                    ],
                    "message": (
                        "the strategy's protection for this structure is active; an "
                        "adjust that increases exposure is not taken while protection "
                        "is triggered or unreadable. Split the plan: reductions stay "
                        "admissible."
                    ),
                },
            )

        previous_ids = {str(leg.get("leg_id") or "") for leg in previous_legs}
        for run_leg in desired_legs:
            if str(run_leg.get("leg_id") or "") not in previous_ids:
                # Durable before submission: a fill must land on a leg the run
                # already records, and a retry must re-read (not re-create) it.
                run.legs.append(run_leg)
        target["_adjust_desired_legs"] = desired_legs
        target["_adjust_previous_legs"] = previous_legs
        return steps

    @staticmethod
    def _desired_adjust_run_leg(
        leg: Mapping[str, Any], *, run_leg: Mapping[str, Any], target_quantity: int
    ) -> Dict[str, Any]:
        """The run leg the DESIRED state names, carrying its frozen size."""
        lot_size = int(leg.get("lot_size") or run_leg.get("lot_size") or 0)
        quantity = abs(int(target_quantity))
        return {
            **dict(run_leg),
            "transaction_type": "BUY" if int(target_quantity) > 0 else "SELL",
            "quantity": quantity,
            "lots": quantity // lot_size if lot_size > 0 else run_leg.get("lots"),
        }

    @staticmethod
    def _removal_leg(run_leg: Mapping[str, Any], *, current: int) -> Dict[str, Any]:
        """A frozen-shaped leg that closes the run's own open on a removed leg."""
        metadata = run_leg.get("metadata") or {}
        instrument_id = metadata.get("instrument_id") if isinstance(metadata, Mapping) else None
        return {
            "instrument_id": str(instrument_id or ""),
            "tradingsymbol": str(run_leg.get("tradingsymbol") or ""),
            "broker_symbol": str(run_leg.get("tradingsymbol") or ""),
            "broker_exchange": str(run_leg.get("exchange") or ""),
            "exchange": str(run_leg.get("exchange") or ""),
            "product": run_leg.get("product"),
            "lot_size": run_leg.get("lot_size"),
            "expiry": str(run_leg.get("expiry_key") or ""),
            "side": "BUY" if int(current) < 0 else "SELL",
            "quantity": abs(int(current)),
        }

    def _option_bindings(self) -> Any:
        if self._plan_binding_store is None:
            from backend.options.execution.plan_binding import PlanOptionRunBindingStore

            self._plan_binding_store = PlanOptionRunBindingStore(
                session_factory=self.session_factory
            )
        return self._plan_binding_store

    def _option_runs(self) -> Any:
        if self._option_run_store is None:
            from backend.options.execution.durable_store import DurableOptionRunStore

            self._option_run_store = DurableOptionRunStore(session_factory=self.session_factory)
        return self._option_run_store

    def _resolve_option_target(
        self, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The durable option run this structure executes against (one engine)."""
        from backend.options.execution.plan_binding import (
            PlanBindingRefusal,
            resolve_plan_option_run,
        )

        try:
            return resolve_plan_option_run(
                plan,
                strategy_id=str(plan.get("strategy_id") or ""),
                account_id=str(plan.get("account_id") or ""),
                execution_environment=str(binding.get("execution_environment") or ""),
                worker_run_id=str(binding.get("strategy_run_id") or "") or None,
                binding_store=self._option_bindings(),
                run_store=self._option_runs(),
            )
        except PlanBindingRefusal as exc:
            raise ExecutionRefusal(exc.reason_code, exc.as_detail()) from exc

    def _begin_option_run(
        self, target: Dict[str, Any], *, plan_id: str, actor: str
    ) -> Dict[str, Any]:
        """Take OWNERSHIP of this option run's next transition, before any submit.

        The transition is a compare-and-set on the run's observed status, so two
        plans that target the same run (a second exit, a recreated executor) can
        never both move it: exactly one wins, and the loser refuses. A run already
        in the transient ``exiting`` state is refused outright - a restart must
        not repeat an exit whose outcome is unknown.
        """
        from backend.options.execution.lifecycle import (
            mark_adjusting,
            mark_entering,
            mark_exit_previewed,
            mark_exiting,
        )

        _ = actor
        run = target["run"]
        phase = str(target.get("phase") or "")
        store = self._option_runs()
        observed = str(run.status)
        try:
            if phase == "entry":
                if observed in ("entering", "entered"):
                    # An entry already in flight/complete: nothing to re-open.
                    return target
                if observed not in ("created", "entry_previewed"):
                    raise ExecutionRefusal(
                        "OPTION_RUN_STATE_CHANGED",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                        },
                    )
                next_run = mark_entering(run)
            elif phase == "adjust":
                # One CAS-guarded transition is the adjust's ownership token, so
                # two plans can never mutate one structure concurrently.
                if observed == "adjusting":
                    # This plan's OWN binding already owns the in-flight adjust (a
                    # re-drive): the transition is not re-taken, and the delta is
                    # re-derived from the run's own confirmed fills.
                    owner = str((target.get("binding") or {}).get("plan_id") or "")
                    if owner != plan_id:
                        raise ExecutionRefusal(
                            "OPTION_RUN_ADJUST_IN_FLIGHT",
                            {
                                "plan_id": plan_id,
                                "option_run_id": run.strategy_run_id,
                                "option_run_status": observed,
                                "owning_plan_id": owner,
                                "message": (
                                    "another plan owns this run's in-flight adjust; the "
                                    "transition is taken exactly once"
                                ),
                            },
                        )
                    return target
                if observed != "entered":
                    raise ExecutionRefusal(
                        "OPTION_RUN_STATE_CHANGED",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                            "message": "an adjust may only mutate a run's held structure",
                        },
                    )
                # A stage the platform committed and has not resolved owns the
                # run's next transition, so the structure is not mutated beside it.
                from backend.options.protection.staged_exit import (
                    unresolved_stage_claim,
                )

                unresolved = unresolved_stage_claim(getattr(run, "orders", None) or [])
                if unresolved is not None:
                    raise ExecutionRefusal(
                        "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                            "stage_digest": str(unresolved.get("stage_digest") or ""),
                            "stage_state": str(unresolved.get("state") or ""),
                            "stage_attempt": int(unresolved.get("attempt") or 1),
                            "message": (
                                "a protective exit stage is unresolved for this run; "
                                "it is reconciled from the platform's own pre-send "
                                "records before the structure is mutated"
                            ),
                        },
                    )
                next_run = mark_adjusting(run)
            else:
                # A protective exit stage the platform committed and has not
                # resolved owns the run's next exit. A governed exit submitted
                # beside it would be a second, possibly-overclosing order against
                # a structure whose live stage is still unknown, so it is refused
                # BY NAME and awaits reconciliation of that stage.
                from backend.options.protection.staged_exit import (
                    unresolved_stage_claim,
                )

                unresolved = unresolved_stage_claim(getattr(run, "orders", None) or [])
                if unresolved is not None:
                    raise ExecutionRefusal(
                        "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                            "stage_digest": str(unresolved.get("stage_digest") or ""),
                            "stage_state": str(unresolved.get("state") or ""),
                            "stage_attempt": int(unresolved.get("attempt") or 1),
                            "message": (
                                "a protective exit stage is unresolved for this run; "
                                "it is reconciled from the platform's own pre-send "
                                "records before any other exit is submitted"
                            ),
                        },
                    )
                if observed == "exiting":
                    raise ExecutionRefusal(
                        "OPTION_RUN_EXIT_IN_FLIGHT",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                            "message": (
                                "an exit for this run already owns the transition; an "
                                "unknown outcome is never repeated"
                            ),
                        },
                    )
                if observed in ("created", "entry_previewed", "entering", "partial_entry"):
                    raise ExecutionRefusal(
                        "OPTION_EXIT_BEFORE_ENTRY",
                        {
                            "plan_id": plan_id,
                            "option_run_id": run.strategy_run_id,
                            "option_run_status": observed,
                            "message": "an exit may not precede the entry it closes",
                        },
                    )
                working = run
                if str(working.status) in ("entered", "cleanup_required"):
                    working = mark_exit_previewed(working)
                next_run = mark_exiting(working)
        except ValueError as exc:
            raise ExecutionRefusal(
                "OPTION_RUN_STATE_INVALID",
                {
                    "plan_id": plan_id,
                    "option_run_id": run.strategy_run_id,
                    "option_run_status": observed,
                    "reason": str(exc),
                },
            ) from exc

        if not store.save_run_if_status(next_run, allowed_from=(observed,)):
            # Another plan (or another instance) moved the run first.
            raise ExecutionRefusal(
                "OPTION_RUN_STATE_CHANGED",
                {
                    "plan_id": plan_id,
                    "option_run_id": run.strategy_run_id,
                    "observed_status": observed,
                    "message": "another plan owns this run's transition",
                },
            )
        target["run"] = next_run
        return target

    def _settle_option_run(
        self,
        target: Dict[str, Any],
        *,
        plan: Mapping[str, Any],
        step_order: Sequence[Any],
        outcomes: List[Dict[str, Any]],
        plan_id: str,
        actor: str,
    ) -> None:
        """Write the durable run's orders/trades and its next lifecycle state.

        The transition is derived from the SAME recorded outcomes the trail
        holds: a required leg is complete only on a confirmed fill, an unfinished
        leg stays pending, and a rejection leaves the run in the existing
        cleanup-required state rather than a fabricated "entered".
        """
        from backend.options.execution.lifecycle import (
            mark_adjusted,
            mark_adjusting,
            mark_cleanup_required,
            mark_closed,
            mark_partial_entry,
            mark_partial_exit,
        )

        store = self._option_runs()
        phase = str(target.get("phase") or "")
        legs_by_step = {int(step[0]): step for step in step_order}
        orders: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        completed: List[str] = []
        pending: List[str] = []
        failed: List[str] = []
        for outcome in outcomes:
            step_no = int(outcome.get("step_no") or 0)
            step = legs_by_step.get(step_no)
            if step is None:
                continue
            _index, leg, quantity, side = step
            leg_id = str(leg.get("_run_leg_id") or f"{plan_id}:{step_no}")
            event = str(outcome.get("event") or "")
            filled = int(outcome.get("filled_quantity") or 0)
            order_id = outcome.get("paper_order_id")
            common = {
                "leg_id": leg_id,
                "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                "transaction_type": str(side),
                "quantity": abs(int(quantity)),
                "phase": phase,
                "order_id": order_id,
            }
            if order_id and event in ("filled", "partially_filled", "rejected", "failed"):
                orders.append({**common, "status": event, "filled_quantity": filled})
            if filled and event in ("filled", "partially_filled"):
                trades.append({**common, "quantity": filled})
            if event in ("filled", "no_op"):
                completed.append(leg_id)
            elif event == "partially_filled":
                pending.append(leg_id)
            elif event in ("rejected", "failed"):
                failed.append(leg_id)

        run = target["run"]
        # Record the evidence FIRST: the run's next state is derived from the
        # position it actually holds afterwards, never from the plan's intent.
        if orders:
            run = store.record_orders(run.strategy_run_id, orders)
        if trades:
            run = store.record_trades(run.strategy_run_id, trades)
        try:
            if phase == "entry":
                if failed:
                    run = mark_partial_entry(
                        run,
                        completed_legs=completed,
                        failed_legs=[],
                        pending_legs=list(dict.fromkeys(pending + failed)),
                    )
                    run = mark_cleanup_required(run)
                else:
                    run = mark_partial_entry(
                        run,
                        completed_legs=completed,
                        failed_legs=[],
                        pending_legs=pending,
                    )
            elif phase == "adjust":
                if failed:
                    # A required leg of the new generation was rejected: the run
                    # holds a HALF-APPLIED structure, which is cleanup work, never
                    # a fabricated "entered".
                    run = mark_cleanup_required(run)
                    run.failed_legs = list(failed)
                elif pending:
                    # A withheld or partially filled increase leaves the SAME
                    # generation: the target is unchanged, so a retry re-derives
                    # the delta from the run's own fills and can only converge.
                    run = mark_adjusting(run, pending_legs=list(dict.fromkeys(pending)))
                else:
                    # Every leg landed. The desired state becomes the run's HELD
                    # state, under a new generation, with the previous generation's
                    # legs kept (bounded) so the basis a strategy observed stays
                    # answerable after the fact.
                    previous_legs = [
                        dict(leg) for leg in target.get("_adjust_previous_legs") or []
                    ]
                    desired_legs = [dict(leg) for leg in target.get("_adjust_desired_legs") or []]
                    previous_digest = self._option_run_shape_digest(run)
                    frozen_digest = str(
                        (plan.get("resolved_plan") or {}).get("structure_digest") or ""
                    )
                    metadata = dict(getattr(run, "metadata", None) or {})
                    generation = self._option_run_generation(run)
                    history = list(metadata.get("structure_generation_history") or [])
                    history.append(
                        {
                            "generation": generation,
                            "structure_digest": previous_digest,
                            "legs": previous_legs,
                        }
                    )
                    metadata["structure_generation"] = generation + 1
                    if frozen_digest:
                        # The shape the run HOLDS now. The owned-work snapshot reads
                        # this ahead of the ORIGINATING plan's digest, because an
                        # additive/removal adjust makes the two differ - and the
                        # duplicate gate must compare against what is held.
                        metadata["structure_digest"] = frozen_digest
                    metadata["structure_generation_history"] = history[-10:]
                    run.metadata = metadata
                    run.protection = self._adjusted_protection(run, plan)
                    if desired_legs:
                        run.legs = desired_legs
                    run = mark_adjusted(run, completed_legs=list(dict.fromkeys(completed)))
            else:
                if failed:
                    # The existing exit route's sequence: the run goes to
                    # PARTIAL_EXIT (the only transition EXITING allows besides
                    # EXITED) and the failed legs are recorded ON it, because
                    # EXITING -> CLEANUP_REQUIRED is not a legal transition in the
                    # shared lifecycle table.
                    run = mark_partial_exit(
                        run,
                        remaining_open_legs=list(dict.fromkeys(pending + failed)),
                        failed_legs=[],
                    )
                    run.failed_legs = list(failed)
                else:
                    # Terminal only when the RUN's own legs are all flat: an exit
                    # plan that closed one leg of two leaves the rest open.
                    still_open = [
                        leg_id
                        for leg_id, quantity in self._option_run_open_by_leg(run).items()
                        if int(quantity or 0) != 0
                    ]
                    if still_open or pending:
                        run = mark_partial_exit(
                            run,
                            remaining_open_legs=list(dict.fromkeys(still_open + pending)),
                            failed_legs=[],
                        )
                    else:
                        run = mark_closed(run)
        except ValueError as exc:
            outcomes.append(
                self._record_event(
                    plan_id,
                    step_no=1,
                    event="failed",
                    refusal_reason="OPTION_RUN_STATE_INVALID",
                    actor_id=actor,
                    detail={
                        "option_run_id": run.strategy_run_id,
                        "option_run_status": run.status,
                        "reason": str(exc),
                    },
                )
            )
            return
        target["run"] = store.save_run(run)

    def _roll_preconditions(
        self, plan: Mapping[str, Any], roll_ref: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """The roll contract, enforced BEFORE anything is submitted.

        A plan that carries a role must resolve to a real roll of this strategy
        and account. The closing half is refused until the roll has RELEASED the
        close (which happens only on the full replacement being proven), and the
        acquiring half may only be the plan the roll names.
        """
        plan_id = str(plan.get("plan_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        machine = self._roll_machine()
        if roll_ref.get("roll_id"):
            roll = machine.by_id(strategy_id=strategy_id, roll_id=str(roll_ref["roll_id"]))
        else:
            roll = machine.for_plan(strategy_id=strategy_id, plan_id=plan_id)
        if roll is None:
            raise ExecutionRefusal(
                "ROLL_UNKNOWN",
                {
                    "plan_id": plan_id,
                    "roll_id": roll_ref.get("roll_id"),
                    "roll_role": roll_ref.get("role"),
                    "message": "the plan names a roll this strategy does not have",
                },
            )
        if str(roll["account_id"]) != account_id:
            raise ExecutionRefusal(
                "ROLL_PLAN_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": str(roll["roll_id"]),
                    "roll_account_id": str(roll["account_id"]),
                    "plan_account_id": account_id,
                },
            )
        role = str(roll_ref.get("role") or "")
        if role == "close_old":
            if str(roll["state"]) != "releasing_old":
                raise ExecutionRefusal(
                    "ROLL_CLOSE_NOT_RELEASED",
                    {
                        "plan_id": plan_id,
                        "roll_id": str(roll["roll_id"]),
                        "roll_state": str(roll["state"]),
                        "proven_filled_quantity": int(roll["proven_filled_quantity"]),
                        "required_replacement_quantity": int(
                            roll["required_replacement_quantity"]
                        ),
                        "message": (
                            "the old-contract close may not be submitted before the roll "
                            "releases it (full replacement proven filled)"
                        ),
                    },
                )
        elif str(roll.get("plan_id") or "") not in ("", plan_id):
            raise ExecutionRefusal(
                "ROLL_PLAN_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": str(roll["roll_id"]),
                    "roll_plan_id": str(roll.get("plan_id") or ""),
                },
            )
        # The role is necessary but not sufficient: the plan's own CONTRACT must
        # match the roll's persisted coordinates, side and required quantity, and
        # it may not quietly carry the other half of the roll.
        self._roll_plan_contract(plan, roll, role=role)
        return roll

    #: Coordinate keys a frozen roll plan is allowed to freeze, and how they read
    #: from a resolved leg. Anything else in the coordinate dict is ignored (the
    #: roll is free to store context this check does not own).
    _ROLL_COORDINATE_KEYS = (
        "product",
        "exchange",
        "instrument_type",
        "option_type",
        "strike",
        "expiry",
        "side",
        "quantity",
    )

    def _roll_plan_contract(
        self,
        plan: Mapping[str, Any],
        roll: Mapping[str, Any],
        *,
        role: str,
    ) -> None:
        """The plan must BE the roll half it claims, at the persisted coordinates."""
        plan_id = str(plan.get("plan_id") or "")
        roll_id = str(roll.get("roll_id") or "")
        legs = [dict(leg) for leg in (plan.get("resolved_plan") or {}).get("legs") or []]
        old_id = str(roll.get("old_instrument_id") or "")
        new_id = str(roll.get("new_instrument_id") or "")
        required = int(roll.get("required_replacement_quantity") or 0)
        target_id = new_id if role == "open_new" else old_id
        other_id = old_id if role == "open_new" else new_id
        coordinate = dict(
            (roll.get("new_coordinate") if role == "open_new" else roll.get("old_coordinate"))
            or {}
        )

        if any(str(leg.get("instrument_id") or "") == other_id for leg in legs if other_id):
            # Carrying the roll's other contract is how one plan would acquire AND
            # release in a single submission, outside the ordered roll.
            raise ExecutionRefusal(
                "ROLL_PLAN_CONTRACT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": roll_id,
                    "roll_role": role,
                    "unexpected_instrument_id": other_id,
                    "message": "a roll plan may only address its own contract",
                },
            )
        matches = [
            leg for leg in legs if str(leg.get("instrument_id") or "") == target_id
        ]
        if not matches:
            raise ExecutionRefusal(
                "ROLL_PLAN_CONTRACT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": roll_id,
                    "roll_role": role,
                    "expected_instrument_id": target_id,
                    "plan_instrument_ids": sorted(
                        str(leg.get("instrument_id") or "") for leg in legs
                    ),
                },
            )
        leg = matches[0]
        coordinate_mismatch = {
            key: {"roll": coordinate[key], "plan": leg.get(key)}
            for key in self._ROLL_COORDINATE_KEYS
            if key in coordinate
            and key not in ("side", "quantity")
            and str(leg.get(key)) != str(coordinate[key])
        }
        if coordinate_mismatch:
            raise ExecutionRefusal(
                "ROLL_PLAN_CONTRACT_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": roll_id,
                    "roll_role": role,
                    "mismatched": coordinate_mismatch,
                },
            )

        def _leg_quantity(entry: Mapping[str, Any]) -> int:
            if entry.get("quantity") is not None:
                return int(entry.get("quantity") or 0)
            return abs(int(entry.get("signed_quantity") or 0))

        def _leg_is_sell(entry: Mapping[str, Any]) -> bool:
            side = str(entry.get("side") or "").upper()
            if side in ("BUY", "SELL"):
                return side == "SELL"
            return int(entry.get("signed_quantity") or 0) < 0

        # An ABSOLUTE FLAT target (signed_quantity 0) can only reduce the book, so
        # it is a close by construction: it is not "the wrong way" and it carries
        # no close quantity of its own (the delta comes from the attributed book).
        absolute_flat = (
            leg.get("signed_quantity") is not None
            and int(leg.get("signed_quantity") or 0) == 0
        )
        declared_side = str(coordinate.get("side") or "").upper()
        if declared_side in ("BUY", "SELL"):
            # A persisted side is authoritative: a close is the OPPOSITE direction.
            closing_side = "BUY" if declared_side == "SELL" else "SELL"
            wanted_sell = closing_side == "SELL" if role == "close_old" else declared_side == "SELL"
        else:
            # No persisted side: the roll shape is long-old/long-new, so the
            # acquisition is a BUY and its close is a SELL.
            wanted_sell = role == "close_old"
        if not absolute_flat and _leg_is_sell(leg) != wanted_sell:
            raise ExecutionRefusal(
                "ROLL_PLAN_DIRECTION_MISMATCH",
                {
                    "plan_id": plan_id,
                    "roll_id": roll_id,
                    "roll_role": role,
                    "plan_side": str(leg.get("side") or ""),
                    "plan_signed_quantity": leg.get("signed_quantity"),
                    "roll_side": declared_side or None,
                    "message": "the plan moves the roll's contract the wrong way",
                },
            )
        if role == "open_new":
            quantity = _leg_quantity(leg)
            if quantity != required:
                raise ExecutionRefusal(
                    "ROLL_PLAN_QUANTITY_MISMATCH",
                    {
                        "plan_id": plan_id,
                        "roll_id": roll_id,
                        "roll_role": role,
                        "required_replacement_quantity": required,
                        "plan_quantity": quantity,
                    },
                )
        return None

    def _refuse_ungated_roll_close(
        self, plan: Mapping[str, Any], *, strategy_id: str, account_id: str
    ) -> None:
        """An UNGATED plan may not close a contract an OPEN roll still holds.

        The role is not a caller's choice: a plain plan that names the old
        contract of an open roll would otherwise close that leg while the
        replacement is still being acquired, which is exactly the bypass the roll
        exists to prevent. Binding is by authoritative coordinates (the roll's
        persisted ``old_instrument_id`` for this strategy and account), not by
        anything the plan payload asserts. Independently authorized emergency
        risk reduction is a different, explicitly authorized action - it is not
        this path, and a plan cannot disguise itself as one.
        """
        legs = [
            str(leg.get("instrument_id") or "")
            for leg in (plan.get("resolved_plan") or {}).get("legs") or []
        ]
        if not legs:
            return
        machine = self._roll_machine()
        for instrument_id in legs:
            if not instrument_id:
                continue
            roll = machine.open_for(strategy_id=strategy_id, old_instrument_id=instrument_id)
            if roll is None:
                continue
            raise ExecutionRefusal(
                "ROLL_CLOSE_REQUIRES_BINDING",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": instrument_id,
                    "roll_id": str(roll["roll_id"]),
                    "roll_state": str(roll["state"]),
                    "account_id": account_id,
                    "message": (
                        "this contract is the old leg of an open roll: only the roll's "
                        "released close may move it (bind the plan to the roll)"
                    ),
                },
            )

    def _record_roll_replacement_fill(
        self,
        *,
        plan: Mapping[str, Any],
        roll: Mapping[str, Any],
        leg: Mapping[str, Any],
        submission: Mapping[str, Any],
        actor: str,
        step_no: int,
        stamp: Callable[[], datetime],
    ) -> Optional[Dict[str, Any]]:
        """Record a CONFIRMED replacement execution on the roll (durable proof).

        Returns the refusal row when recording failed (the plan then holds rather
        than pretending the replacement landed), otherwise ``None``.
        """
        from backend.strategies.rolls import RollError

        plan_id = str(plan.get("plan_id") or "")
        instrument_id = str(leg.get("instrument_id") or "")
        if instrument_id != str(roll["new_instrument_id"]):
            return self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                refusal_reason="ROLL_PLAN_MISMATCH",
                actor_id=actor,
                detail={
                    "instrument_id": instrument_id,
                    "new_instrument_id": str(roll["new_instrument_id"]),
                    "message": "only the replacement contract proves the roll",
                },
                at=stamp(),
            )
        try:
            self._roll_machine().record_replacement_fill(
                str(roll["roll_id"]),
                paper_order_id=str(submission.get("order_id") or ""),
                quantity=int((submission.get("outcome") or {}).get("filled_quantity") or 0),
                instrument_id=instrument_id,
                plan_id=plan_id,
                actor_id=actor,
            )
        except RollError as exc:
            return self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                refusal_reason="ROLL_FILL_RECORD_FAILED",
                actor_id=actor,
                detail=exc.as_detail(),
                at=stamp(),
            )
        return None

    def _plan_steps(
        self, plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> List[Any]:
        """Derive the steps from the resolved representation (D-2).

        One step per leg: side + quantity = target - current attributed book,
        floored to the **pinned** lot recorded in the plan. Units are decided at
        freeze time and never re-read from the mutable catalog, so a lot change
        after the plan was frozen cannot silently change the executed quantity; a
        plan that carries no pinned units is refused (``PLAN_UNITS_UNPINNED``)
        rather than reinterpreted.

        Two things distinguish a long-only portfolio target from an explicitly
        signed instruction:

        * a leg with a pinned ``signed_quantity`` (single instrument, intent
          bundle, futures, option structure) is the instruction, so a target
          below zero is a real short and is honoured;
        * a leg sized from a ``target_weight`` (CNC portfolio) is a long-only
          fraction, so a sell is a *reduction* and is clamped at flat - it can
          never cross into a short, and it never touches another book because
          ``_current_book_quantity`` reads only this strategy's projection.

        ``_current_quantity`` and ``_increases_exposure`` travel with the leg as
        evidence for the trail and for the reservation precondition.
        """
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        steps: List[Any] = []
        for index, leg in enumerate(legs, start=1):
            leg = dict(leg)
            current = self._current_book_quantity(plan, leg, binding)
            leg["_current_quantity"] = current
            lot = self._pinned_lot(plan, leg)
            leg["_pinned_lot"] = lot
            if leg.get("signed_quantity") is not None:
                target = int(leg.get("signed_quantity") or 0)
                delta = target - current
            else:
                target = self._sized_quantity(plan, leg, lot)
                leg["_target_quantity"] = target
                # Long-only portfolio target: a sell may reduce to flat, never
                # through it.
                delta = target - current
                if delta < 0:
                    delta = max(delta, -current)
            quantity = self._floor_to_lot(delta, lot)
            leg["_increases_exposure"] = self._opens_or_grows_exposure(target, current)
            side = "BUY" if quantity > 0 else "SELL"
            steps.append((index, leg, quantity, side))
        return steps

    @staticmethod
    def _opens_or_grows_exposure(target: int, current: int) -> bool:
        """Whether reaching ``target`` from ``current`` demands admission (D-6).

        Two shapes need capacity, and only two:

        * the book GROWS (``|target| > |current|``), which includes opening from
          flat - so a new short is admitted like a new long;
        * the trade CROSSES FLAT: a reversal closes one side and opens the other,
          so it creates a new short (or a new long) even when the magnitude does
          not grow. Measuring exposure with the magnitude alone would let
          ``+10 -> -5`` sell 15 with no reservation at all.

        Reducing a same-sign position, and closing to flat, are the only shapes
        that need no reservation.
        """
        target = int(target or 0)
        current = int(current or 0)
        if target == 0:
            return False
        if abs(target) > abs(current):
            return True
        return current != 0 and (target > 0) != (current > 0)

    def _current_book_quantity(
        self, plan: Mapping[str, Any], leg: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> int:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyPositionProjection.net_quantity).where(
                    StrategyPositionProjection.account_id == str(plan.get("account_id") or ""),
                    StrategyPositionProjection.strategy_id == str(plan.get("strategy_id") or ""),
                    StrategyPositionProjection.execution_environment == PAPER_ENVIRONMENT,
                    StrategyPositionProjection.identity_kind == "canonical",
                    StrategyPositionProjection.canonical_instrument_id
                    == str(leg.get("instrument_id") or ""),
                    StrategyPositionProjection.product == str(leg.get("product") or ""),
                )
            ).scalars().all()
        return int(sum(int(value or 0) for value in rows))

    def _pinned_lot(self, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> int:
        """The lot the plan froze for this leg. Never the live catalog's.

        Resolution happened once, against the pinned generation; the executed
        unit must come from that same artifact. A plan without pinned units is a
        plan whose size cannot be honoured, so it refuses by name.
        """
        raw = leg.get("lot_size")
        if raw is None:
            raise ExecutionRefusal(
                "PLAN_UNITS_UNPINNED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "message": "the plan carries no pinned lot size for this leg",
                },
            )
        try:
            lot = int(raw)
        except (TypeError, ValueError) as exc:
            raise ExecutionRefusal(
                "PLAN_UNITS_UNPINNED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "lot_size": str(raw),
                },
            ) from exc
        if lot <= 0:
            raise ExecutionRefusal(
                "PLAN_UNITS_UNPINNED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "lot_size": lot,
                },
            )
        return lot

    def _sized_quantity(
        self, plan: Mapping[str, Any], leg: Mapping[str, Any], lot: int
    ) -> int:
        """Size a weight leg: weight x allocated capital / pinned price, floored.

        Every input comes from the frozen plan or from the strategy's stored
        admission policy - no new market-data dependency. When the capital or the
        price is absent the size is *unknown*, and an unknown size is refused by
        name rather than guessed.
        """
        weight = leg.get("target_weight")
        if weight is None:
            raise ExecutionRefusal(
                "PLAN_SIZING_UNAVAILABLE",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "message": "the leg names neither a signed quantity nor a target weight",
                },
            )
        weight = float(weight)
        logical = dict(plan.get("logical_plan") or {})
        resolved = dict(plan.get("resolved_plan") or {})
        buffer_pct = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
        buffer_pct = 0.0 if buffer_pct is None else float(buffer_pct)
        basis = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
        if basis is None:
            raise ExecutionRefusal(
                "PLAN_CAPITAL_BASIS_UNPINNED",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "message": (
                        "a weight-sized plan must carry the capital basis it was frozen "
                        "with; re-reading the current policy would re-size an approved target"
                    ),
                },
            )
        basis = float(basis)
        if basis <= 0:
            raise ExecutionRefusal(
                "PLAN_CAPITAL_BASIS_UNPINNED",
                {"plan_id": str(plan.get("plan_id") or ""), "capital_basis_inr": basis},
            )
        current = self._current_policy_allocation(plan)
        if current is None or current < basis:
            # Relevant drift: the recorded authority no longer covers the basis
            # this plan was approved against, so the saved target may not execute.
            # A LARGER allocation does not grow the order - the frozen basis is
            # always the sizing input.
            raise ExecutionRefusal(
                "PLAN_CAPITAL_BASIS_DRIFT",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "strategy_id": str(plan.get("strategy_id") or ""),
                    "account_id": str(plan.get("account_id") or ""),
                    "frozen_capital_basis_inr": basis,
                    "current_allocation_inr": current,
                },
            )
        notional = weight * basis * max(0.0, 1.0 - buffer_pct)
        price = leg.get("reference_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            raise ExecutionRefusal(
                "PLAN_REFERENCE_PRICE_UNAVAILABLE",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "instrument_id": str(leg.get("instrument_id") or ""),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "message": "a weight-sized leg needs a pinned reference price",
                },
            )
        units = int(notional // price)
        floored = (units // lot) * lot if lot > 1 else units
        return int(floored)

    def _current_policy_allocation(self, plan: Mapping[str, Any]) -> Optional[float]:
        """The strategy's CURRENT recorded allocation, for drift detection only.

        This value never sizes an order (the plan's frozen basis does). It exists so
        a plan whose recorded authority has since fallen below its frozen basis is
        refused rather than executed against a limit that no longer exists.
        """
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyAdmissionPolicy).where(
                    StrategyAdmissionPolicy.strategy_id == strategy_id
                )
            ).scalar_one_or_none()
        if row is not None and str(row.account_id or "") != account_id:
            # A policy recorded for another account is not this plan's basis.
            row = None
        if row is None:
            return None
        try:
            allocation = None if row.allocation_inr is None else float(row.allocation_inr)
        except (TypeError, ValueError):
            return None
        if allocation is None or allocation <= 0:
            return None
        return float(allocation)

    @staticmethod
    def _floor_to_lot(delta: int, lot: int) -> int:
        if delta == 0 or lot <= 1:
            return delta
        sign = 1 if delta > 0 else -1
        floored = (abs(delta) // lot) * lot
        return sign * floored

    # ------------------------------------------------------------ submission

    async def _submit_step(
        self,
        plan: Mapping[str, Any],
        reservation: Dict[str, Any],
        actor: str,
        *,
        step_no: int,
        leg: Mapping[str, Any],
        quantity: int,
        side: str,
        binding: Mapping[str, Any],
        at: datetime,
    ) -> Dict[str, Any]:
        """Submit one step through the paper runtime; record both trail events.

        The ``submitted`` event and the already-executing check commit in ONE
        transaction under the plan's lock (plus the PostgreSQL advisory lock),
        so concurrent executes of the same plan yield exactly ONE submission.
        """
        plan_id = str(plan.get("plan_id") or "")
        ref = f"plan:{plan_id}:step:{step_no}"
        # The outcome row must sort strictly after its own submission row:
        # created_at is the trail's only ordering, so never reuse the stamp.
        outcome_at = at + timedelta(microseconds=1)
        with self._plan_lock(plan_id):
            with self.session_factory() as session:
                if session.bind.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"plan-exec:{plan_id}"},
                    )
                prior = session.execute(
                    text(
                        "SELECT COUNT(*) FROM strategy_plan_execution_events "
                        "WHERE plan_id = :plan_id AND step_no = :step_no"
                    ),
                    {"plan_id": plan_id, "step_no": int(step_no)},
                ).scalar()
                if int(prior or 0) > 0:
                    raise ExecutionRefusal(
                        "PLAN_ALREADY_EXECUTED",
                        {
                            "plan_id": plan_id,
                            "step_no": int(step_no),
                            "message": (
                                "This step already has an execution trail; a plan executes once"
                            ),
                        },
                    )
                self._write_event(
                    session,
                    plan_id=plan_id,
                    step_no=step_no,
                    event="submitted",
                    actor_id=actor,
                    detail={
                        "instrument_id": leg.get("instrument_id"),
                        "tradingsymbol": leg.get("tradingsymbol"),
                        "side": side,
                        "quantity": quantity,
                        "reservation_id": (reservation or {}).get("reservation_id"),
                        "strategy_run_id": binding["strategy_run_id"],
                    },
                    at=at,
                )
                session.commit()

        # Work exists from the moment capital is committed to the runtime (D-5).
        self.barrier.record_work_event(
            account_id=str(plan.get("account_id") or ""),
            strategy_id=str(plan.get("strategy_id") or ""),
            execution_environment=PAPER_ENVIRONMENT,
            event="work_created",
            ref=ref,
            detail={"plan_id": plan_id, "step_no": step_no, "quantity": quantity, "side": side},
        )

        attribution = {
            "strategy_run_id": binding["strategy_run_id"],
            "strategy_id": str(plan.get("strategy_id") or ""),
            "plan_id": plan_id,
            "reservation_id": (reservation or {}).get("reservation_id"),
            "step_no": step_no,
            "execution_mode": PAPER_ENVIRONMENT,
            "source": "hosted_plan_execution",
        }
        order_payload = {
            "exchange": str(leg.get("broker_exchange") or leg.get("exchange") or ""),
            "tradingsymbol": str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
            "transaction_type": side,
            "product": str(leg.get("product") or ""),
            "order_type": "MARKET",
            # The runtime's quantity is unsigned: the sign travels as the side.
            "quantity": abs(int(quantity)),
        }

        outcome: Dict[str, Any]
        order_id: Optional[str] = None
        try:
            service = self._paper_service
            if service is None and self.paper_service_factory is not None:
                service = self.paper_service_factory()
            if service is None:
                raise RuntimeError("no paper runtime is wired into this executor")
            result = await service.place_order(
                account_scope=str(plan.get("account_id") or ""),
                order_payload=order_payload,
                attribution=attribution,
            )
        except Exception as exc:  # noqa: BLE001 - unknown state, never a guess
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                actor_id=actor,
                detail={"error": str(exc), "ref": ref},
                at=outcome_at,
            )
            return {"outcome": outcome, "order_id": None}

        order = dict(result.get("order") or {})
        order_id = str(order.get("order_id") or "") or None
        status = str(result.get("status") or "")

        if status == "filled":
            filled = int(order.get("filled_quantity") or order.get("quantity") or 0)
            self.barrier.record_work_event(
                account_id=str(plan.get("account_id") or ""),
                strategy_id=str(plan.get("strategy_id") or ""),
                execution_environment=PAPER_ENVIRONMENT,
                event="work_resolved",
                ref=ref,
                detail={"plan_id": plan_id, "step_no": step_no, "outcome": "filled"},
            )
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="filled",
                paper_order_id=order_id,
                filled_quantity=filled,
                actor_id=actor,
                detail={
                    "fill_price": str(order.get("average_price") or ""),
                    "tradingsymbol": order.get("tradingsymbol"),
                },
                at=outcome_at,
            )
        elif status == "partially_filled":
            # Verified progress that is not completion: the order changed the book
            # and still has a remainder. It is deliberately NOT recorded as
            # work_resolved — an open remainder is in flight, and calling it
            # resolved would assert a flatness the account does not have.
            filled = int(order.get("filled_quantity") or 0)
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="partially_filled",
                paper_order_id=order_id,
                filled_quantity=filled,
                actor_id=actor,
                detail={
                    "fill_price": str(order.get("average_price") or ""),
                    "tradingsymbol": order.get("tradingsymbol"),
                    "pending_quantity": order.get("pending_quantity"),
                    "ref": ref,
                },
                at=outcome_at,
            )
        elif status == "rejected":
            reason = str(result.get("reason") or "rejected by the paper runtime")
            self.barrier.record_work_event(
                account_id=str(plan.get("account_id") or ""),
                strategy_id=str(plan.get("strategy_id") or ""),
                execution_environment=PAPER_ENVIRONMENT,
                event="work_resolved",
                ref=ref,
                detail={"plan_id": plan_id, "step_no": step_no, "outcome": "rejected"},
            )
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="rejected",
                paper_order_id=order_id,
                refusal_reason="PAPER_ORDER_REJECTED",
                actor_id=actor,
                detail={"reason": reason, "ref": ref},
                at=outcome_at,
            )
        else:
            # Accepted-but-unterminal (or anything unknown): execution state is
            # genuinely unknown — record ``failed`` honestly and hold capacity.
            outcome = self._record_event(
                plan_id,
                step_no=step_no,
                event="failed",
                paper_order_id=order_id,
                actor_id=actor,
                detail={"runtime_status": status or "unknown", "ref": ref},
                at=outcome_at,
            )
        return {"outcome": outcome, "order_id": order_id}

    # ----------------------------------------------------------- reservation

    def _open_remainders(self, order_ids: Sequence[str]) -> List[str]:
        """Order ids whose paper fill left an unexecuted remainder.

        An open remainder means the order has not finished changing the book, so
        the plan is neither filled nor unfilled — which is exactly the state the
        reservation lifecycle must not treat as 'done'.
        """
        if not order_ids:
            return []
        try:
            remainders = self.fill_progress.open_remainder_for(
                paper_order_ids=list(order_ids)
            )
        except Exception:  # noqa: BLE001 - unreadable progress is not proof of completion
            return list(order_ids)
        return [str(row.paper_order_id) for row in remainders]

    def _settle_reservation(
        self,
        reservation: Dict[str, Any],
        actor: str,
        *,
        filled_ids: List[str],
        failed: bool,
        submitted_any: bool,
        partial_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """Consume on fills, release terminal-unfilled on rejections, hold on doubt (D-4).

        A plan whose every step was ``no_op`` committed no capital, so its
        reservation stays exactly as admission left it — nothing happened.

        Verified progress is the one thing that may extend or consume capacity,
        and an unexecuted remainder is the one thing that may not release it: a
        partially filled rebalance is unresolved work, so its capacity is renewed
        when a tranche actually filled and flagged ``action_required`` when it did
        not. Either way it is held — releasing on an open remainder would hand
        back capacity that is still changing the book.
        """
        if reservation is None:
            return  # a risk-reducing-only bundle never held capacity
        reservation_id = str(reservation.get("reservation_id") or "")
        if not reservation_id:
            return

        remainders = list(partial_ids or [])
        if remainders:
            if filled_ids:
                # Verified progress: a tranche filled and more is outstanding.
                self.ledger.renew(
                    reservation_id,
                    actor_id=actor,
                    detail={"plan_id": reservation.get("plan_id"), "open_remainder": remainders},
                )
            else:
                # Unresolved with nothing proven: hold and ask the owner rather
                # than release capacity on a guess.
                self.ledger.require_action(
                    reservation_id,
                    actor_id=actor,
                    detail={"plan_id": reservation.get("plan_id"), "open_remainder": remainders},
                )
            return

        if filled_ids:
            self.ledger.consume(
                reservation_id,
                actor_id=actor,
                detail={
                    "plan_id": reservation.get("plan_id"),
                    "paper_order_ids": list(filled_ids),
                },
            )
        elif submitted_any and not failed:
            self.ledger.release(reservation_id, reason="terminal_unfilled", actor_id=actor)
        # Anything else (all no_op, or any unknown state) holds the capacity:
        # release requires proof, never a guess.

    # ----------------------------------------------------------- event trail

    def _record_event(
        self,
        plan_id: str,
        *,
        step_no: int,
        event: str,
        actor_id: str,
        detail: Optional[Mapping[str, Any]] = None,
        paper_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        refusal_reason: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.session_factory() as session:
            row = self._write_event(
                session,
                plan_id=plan_id,
                step_no=step_no,
                event=event,
                actor_id=actor_id,
                detail=detail,
                paper_order_id=paper_order_id,
                broker_order_id=broker_order_id,
                filled_quantity=filled_quantity,
                refusal_reason=refusal_reason,
                at=at or self._clock(),
            )
            session.commit()
            return {
                "step_no": int(row.step_no),
                "event": str(row.event),
                "paper_order_id": row.paper_order_id,
                "filled_quantity": row.filled_quantity,
                "refusal_reason": row.refusal_reason,
                "detail": dict(row.detail or {}),
            }

    @staticmethod
    def _write_event(
        session: Any,
        *,
        plan_id: str,
        step_no: int,
        event: str,
        actor_id: str,
        detail: Optional[Mapping[str, Any]] = None,
        paper_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        refusal_reason: Optional[str] = None,
        at: Optional[datetime] = None,
    ):
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        row = StrategyPlanExecutionEvent(
            id=str(uuid.uuid4()),
            plan_id=plan_id,
            step_no=int(step_no),
            event=event,
            paper_order_id=paper_order_id,
            broker_order_id=broker_order_id,
            filled_quantity=filled_quantity,
            refusal_reason=refusal_reason,
            actor_id=actor_id,
            detail=dict(detail or {}),
            created_at=at or _utcnow(),
        )
        session.add(row)
        session.flush()
        return row

    # ------------------------------------------------------------- locking

    def _has_submission(self, plan_id: str) -> bool:
        """Whether the plan has a committed ``submitted`` event (a real attempt)."""
        with self.session_factory() as session:
            row = session.execute(
                text(
                    "SELECT COUNT(*) FROM strategy_plan_execution_events "
                    "WHERE plan_id = :plan_id AND event = 'submitted'"
                ),
                {"plan_id": plan_id},
            ).fetchone()
        return row is not None and int(row[0] or 0) > 0

    def _plan_lock(self, plan_id: str):
        """The in-process serialization point (PostgreSQL adds the advisory lock)."""
        with self._plan_locks_guard:
            lock = self._plan_locks.get(plan_id)
            if lock is None:
                lock = threading.Lock()
                self._plan_locks[plan_id] = lock
            return lock
