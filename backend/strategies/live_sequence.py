"""Durable multi-step live execution parent and the lane extension contract.

``live_plan_submissions`` is a PER-STEP claim. A portfolio/CNC basket, a MIS
square-off or a futures roll is an ORDERED SET of steps whose release is governed
by dependencies between them, and that protocol cannot live in an ad-hoc
``detail`` blob: it has to be immutable, queryable, and enforceable.

``live_plan_executions`` is that durable parent: exactly ONE row per frozen plan,
carrying the immutable ordered step/dependency specification frozen at FIRST
ADMISSION. It is not a second execution ledger - per-leg claims stay in
``live_plan_submissions``, and this module never duplicates them.

Three properties carry the design:

1. **Materialization is atomic and idempotent.** The parent insert, every step
   claim and the barrier ``work_created`` for each actionable step are written in
   ONE transaction on the caller's canonical book lock. A failure rolls back all
   of it; a concurrent second executor reads the winner's rows instead of
   creating a second parent (``UNIQUE (plan_id)``).

2. **The frozen step sizing is never re-derived.** The delta recorded at first
   admission is what a released step dispatches, even after a partial fill moved
   the attributed book. Re-deriving would re-size an already-approved step.

3. **A dependent step is in flight, never dispatched.** A step whose
   ``depends_on`` prerequisites have not all filled is written as ``withheld``:
   the settlement barrier enumerates it as unresolved work, so a quiet proof can
   never pass while a dependent leg is unreleased.

The extension contract for the next bundle (futures rolls, option structures) is
``register_live_lane``: a lane builder returns the ordered ``StepSpec`` list and
the dependencies it wants (option hedge gating, cross-plan roll full-fill, ...).
The release pass is generic over ``depends_on``; it never assumes
``sells-before-buys`` outside the lane that declares it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text

from backend.app.database import SessionLocal

from .live_adapter import LiveRefusal, LiveSubmissionStore
from .reservations import ReservationLedger
from .settlement import ExecutionBarrier

LIVE_ENVIRONMENT = "live"

# -- lanes -------------------------------------------------------------------

LANE_SINGLE = "single_instrument"
LANE_PORTFOLIO = "target_weights"
LANE_MIS = "mis"
#: Reserved by the extension contract: the next bundle wires these without a new
#: constraint migration. Nothing in THIS bundle produces them.
LANE_FUTURES_ROLL = "futures_roll"
LANE_OPTION_STRUCTURE = "option_structure"

# -- per-step states ---------------------------------------------------------

STEP_PENDING = "pending"
STEP_WITHHELD = "withheld"
STEP_RELEASING = "releasing"

#: Step states that mean the leg can never do more.
STEP_TERMINAL = ("filled", "rejected", "no_op", "residual_abandoned")

#: Step states that are still unresolved work (the settlement barrier's view).
STEP_INFLIGHT = (
    "pending",
    "withheld",
    "releasing",
    "partial",
    "finalizing",
    "rejecting",
    "uncertain",
    "repair_required",
)

# -- per-parent states -------------------------------------------------------

PARENT_PLANNED = "planned"
PARENT_EXECUTING = "executing"
PARENT_SETTLED = "settled"
PARENT_BLOCKED = "blocked"

PARENT_INFLIGHT = (PARENT_PLANNED, PARENT_EXECUTING, PARENT_BLOCKED)

# -- release rules -----------------------------------------------------------

#: The step may be dispatched as soon as its claim is ready (no prerequisites).
RULE_IMMEDIATE = "immediate"
#: Every step in ``depends_on`` must be ``filled`` before this step is released.
RULE_ALL_PREREQUISITES_FILLED = "all_prerequisites_filled"
#: A STAGED CNC portfolio's dependent buy: released only by the staged funding
#: gate (the post-fill funds read and per-buy authorization). C1.1 S1 freezes the
#: rule and fails closed on it; S2 implements the gate body. It is deliberately
#: NOT ``all_prerequisites_filled``: a filled reduction alone never proves the
#: buy is funded.
RULE_STAGED_FUNDING_GATE = "staged_funding_gate"
#: A risk-reducing MIS step: released by the platform's own square-off clock (or
#: an authorising risk-reduction condition), never by a guessed exchange close.
RULE_MIS_SQUAREOFF = "mis_squareoff"
#: A roll's old-contract CLOSE half: released by the roll's OWN state, which is
#: reachable only once the FULL required replacement quantity is proven filled
#: (``RollStateMachine.release_close`` owns that guard; the lane reuses it).
RULE_ROLL_CLOSE_RELEASED = "roll_close_released"
#: An option structure's SHORT ENTRY leg: released only against the confirmed
#: hedge fill the existing ``hedge_fill_gate`` proves.
RULE_HEDGE_FILL_GATE = "hedge_fill_gate"
#: An option structure's HEDGE half of an EXIT: released only against the short
#: it defends being PROVEN closed (the existing exit builder's rule).
RULE_HEDGE_RELEASE_WITHHELD = "hedge_release_withheld"

RELEASE_RULES = (
    RULE_IMMEDIATE,
    RULE_ALL_PREREQUISITES_FILLED,
    RULE_STAGED_FUNDING_GATE,
    RULE_MIS_SQUAREOFF,
    RULE_ROLL_CLOSE_RELEASED,
    RULE_HEDGE_FILL_GATE,
    RULE_HEDGE_RELEASE_WITHHELD,
)

#: Named blockers a refused release records. They are EVIDENCE: the step stays
#: withheld and nothing is submitted.
BLOCKER_PREREQUISITE_UNFILLED = "prerequisite_not_filled"
BLOCKER_DEADLINE_NOT_DUE = "MIS_SQUAREOFF_NOT_DUE"
BLOCKER_ROLL_CLOSE_NOT_RELEASED = "roll_close_not_released"
BLOCKER_HEDGE_NOT_FILLED = "hedge_not_confirmed"
BLOCKER_HEDGE_SHORT_NOT_CLOSED = "hedge_short_not_proven_closed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    if isinstance(value, Sequence):
        return list(value)
    return []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StepSpec:
    """One frozen step of a parent execution. Immutable by construction.

    ``depends_on`` names prerequisite ``step_no`` values; ``release_rule`` says
    how they gate this step. ``lane`` and ``domain`` are metadata the release pass
    and the operators read, never a re-sizing input.
    """

    step_no: int
    step_ref: str
    lane: str
    domain: str
    instrument_id: str
    exchange: str
    tradingsymbol: str
    broker_exchange: str
    broker_symbol: str
    product: str
    variety: str
    side: str
    quantity: int
    lot_size: int
    target_quantity: int
    current_quantity: int
    delta: int
    notional_inr: float
    increases_exposure: bool
    depends_on: Tuple[int, ...] = ()
    release_rule: str = RULE_IMMEDIATE
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step_no": int(self.step_no),
            "step_ref": str(self.step_ref),
            "lane": str(self.lane),
            "domain": str(self.domain),
            "instrument_id": str(self.instrument_id),
            "exchange": str(self.exchange),
            "tradingsymbol": str(self.tradingsymbol),
            "broker_exchange": str(self.broker_exchange),
            "broker_symbol": str(self.broker_symbol),
            "product": str(self.product),
            "variety": str(self.variety),
            "side": str(self.side),
            "quantity": int(self.quantity),
            "lot_size": int(self.lot_size),
            "target_quantity": int(self.target_quantity),
            "current_quantity": int(self.current_quantity),
            "delta": int(self.delta),
            "notional_inr": float(self.notional_inr),
            "increases_exposure": bool(self.increases_exposure),
            "depends_on": [int(value) for value in self.depends_on],
            "release_rule": str(self.release_rule),
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "StepSpec":
        return cls(
            step_no=_as_int(row.get("step_no")),
            step_ref=str(row.get("step_ref") or ""),
            lane=str(row.get("lane") or LANE_SINGLE),
            domain=str(row.get("domain") or ""),
            instrument_id=str(row.get("instrument_id") or ""),
            exchange=str(row.get("exchange") or ""),
            tradingsymbol=str(row.get("tradingsymbol") or ""),
            broker_exchange=str(row.get("broker_exchange") or ""),
            broker_symbol=str(row.get("broker_symbol") or ""),
            product=str(row.get("product") or ""),
            variety=str(row.get("variety") or "regular"),
            side=str(row.get("side") or ""),
            quantity=_as_int(row.get("quantity")),
            lot_size=_as_int(row.get("lot_size"), 1),
            target_quantity=_as_int(row.get("target_quantity")),
            current_quantity=_as_int(row.get("current_quantity")),
            delta=_as_int(row.get("delta")),
            notional_inr=float(row.get("notional_inr") or 0.0),
            increases_exposure=bool(row.get("increases_exposure")),
            depends_on=tuple(_as_int(value) for value in _as_list(row.get("depends_on"))),
            release_rule=str(row.get("release_rule") or RULE_IMMEDIATE),
            detail=_as_dict(row.get("detail")),
        )


@dataclass(frozen=True)
class LaneContext:
    """What a lane builder is given. Everything is already validated."""

    plan: Mapping[str, Any]
    binding: Mapping[str, Any]
    authority: Mapping[str, Any]
    execution_id: str
    #: ``size_leg(leg) -> {"target","current","delta","side","quantity",
    #: "lot_size","increases_exposure"}``, the adapter's frozen sizing path. A
    #: builder never re-implements the attribution/current read.
    size_leg: Callable[[Mapping[str, Any]], Dict[str, Any]]
    #: The attributed CURRENT quantity for one leg, from the platform's own
    #: projection. A lane whose step is an ABSOLUTE FLAT (a roll's old-contract
    #: close) needs the book itself rather than a (target - current) delta: the
    #: frozen leg's signed quantity describes the CONTRACT the plan moves, not the
    #: quantity the book still holds.
    attributed_quantity: Optional[Callable[[Mapping[str, Any]], int]] = None
    #: The durable OPTION RUN a frozen ``option_structure`` plan executes against
    #: (``{"phase": ..., "run": OptionRunState, ...}``), resolved by the existing
    #: plan/run binding. ``None`` on every non-option lane.
    option_target: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]] = None
    #: The EXISTING option engine's own step derivation for that run: one
    #: ``(index, leg, quantity, side)`` per frozen leg, sized from the RUN's own
    #: confirmed executions. Reused verbatim; never re-derived here.
    option_run_steps: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[Any]]] = None
    #: Whether ADMISSION classified this plan as the staged CNC financing lane
    #: (it recorded ``staged_increase_inr``). Only a staged plan's dependent buys
    #: go behind the staged funding gate; every other portfolio plan keeps the
    #: generic ``all_prerequisites_filled`` rule.
    staged_financing: bool = False

    @property
    def plan_id(self) -> str:
        return str(self.plan.get("plan_id") or "")


LaneBuilder = Callable[[LaneContext], List[StepSpec]]

_LANE_BUILDERS: Dict[str, LaneBuilder] = {}


def register_live_lane(lane: str, builder: LaneBuilder) -> None:
    """Register the step builder for one lane (the extension contract).

    The next bundle (futures rolls, option structures) calls this with its own
    builder; the parent table, the materialization transaction, the withheld
    dependency protocol and the release pass are reused unchanged. A builder
    decides its own ``depends_on`` graph - ``sells-before-buys`` is a PORTFOLIO
    rule, not a universal one.
    """
    _LANE_BUILDERS[str(lane)] = builder


def live_lane_builders() -> Dict[str, LaneBuilder]:
    return dict(_LANE_BUILDERS)


#: Public lane names for the capability surface, and the lane implementations each
#: one covers. The single-instrument shape is part of the CNC lane's surface (a MIS
#: product routes to ``mis`` instead), which is why both map to ``cnc``.
PUBLIC_LANE_NAMES = (
    ("cnc", (LANE_PORTFOLIO, LANE_SINGLE)),
    ("mis", (LANE_MIS,)),
    ("futures", (LANE_FUTURES_ROLL,)),
    ("options", (LANE_OPTION_STRUCTURE,)),
)


def hosted_live_lanes() -> List[str]:
    """The live lanes whose builders are ACTUALLY registered.

    Derived from the registry rather than hardcoded, so a lane that has not been
    wired cannot be advertised to a client - the capability surface reports what
    the server will accept, never what it hopes to accept.
    """
    builders = live_lane_builders()
    return [
        name
        for name, lanes in PUBLIC_LANE_NAMES
        if lanes and all(lane in builders for lane in lanes)
    ]


def lane_builder(lane: str) -> Optional[LaneBuilder]:
    return _LANE_BUILDERS.get(str(lane))


# -- lane builders -----------------------------------------------------------


def _instrument_fields(leg: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "instrument_id": str(leg.get("instrument_id") or ""),
        "exchange": str(leg.get("exchange") or ""),
        "tradingsymbol": str(leg.get("tradingsymbol") or ""),
        "broker_exchange": str(leg.get("broker_exchange") or leg.get("exchange") or ""),
        "broker_symbol": str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
        "product": str(leg.get("product") or ""),
        "variety": str(leg.get("variety") or "regular"),
    }


def _step_ref(plan_id: str, step_no: int) -> str:
    return f"live-plan:{plan_id}:step:{int(step_no)}"


def build_single_steps(ctx: LaneContext) -> List[StepSpec]:
    """The one-leg lane: exactly the Phase 1 behaviour, now parented."""
    legs = list((ctx.plan.get("resolved_plan") or {}).get("legs") or [])
    if len(legs) != 1:
        raise LiveRefusal(
            "LIVE_PLAN_COMPOUND_UNSUPPORTED",
            {
                "plan_id": ctx.plan_id,
                "plan_kind": str(ctx.plan.get("plan_kind") or ""),
                "leg_count": len(legs),
            },
        )
    delta = ctx.size_leg(dict(legs[0]))
    return [
        StepSpec(
            step_no=1,
            step_ref=_step_ref(ctx.plan_id, 1),
            lane=LANE_SINGLE,
            domain=str(legs[0].get("product") or "CNC").upper(),
            quantity=abs(_as_int(delta.get("quantity"))),
            side=str(delta.get("side") or ""),
            target_quantity=_as_int(delta.get("target")),
            current_quantity=_as_int(delta.get("current")),
            delta=_as_int(delta.get("delta")),
            lot_size=_as_int(delta.get("lot_size"), 1),
            notional_inr=float(delta.get("notional_inr") or 0.0),
            increases_exposure=bool(delta.get("increases_exposure")),
            depends_on=(),
            release_rule=RULE_IMMEDIATE,
            detail={"sizing": dict(delta)},
            **_instrument_fields(legs[0]),
        )
    ]


def build_mis_steps(ctx: LaneContext) -> List[StepSpec]:
    """The MIS lane: same pipeline, the platform's own square-off decides the exit.

    A risk-INCREASING MIS step is ordinary and ready immediately. A RISK-REDUCING
    MIS step is ``withheld`` under :data:`RULE_MIS_SQUAREOFF`: it is released by
    the platform's square-off clock (reused from the protection runtime's own
    schedule), the MIS stale-worker exit policy, or an operator-requested stop -
    never by a guessed exchange close. Sizing stays the plan's frozen delta and is
    clamped to the strategy's attributed quantity at release.
    """
    legs = list((ctx.plan.get("resolved_plan") or {}).get("legs") or [])
    if len(legs) != 1:
        raise LiveRefusal(
            "LIVE_PLAN_COMPOUND_UNSUPPORTED",
            {"plan_id": ctx.plan_id, "leg_count": len(legs)},
        )
    leg = dict(legs[0])
    product = str(leg.get("product") or "").upper()
    if product != "MIS":
        raise LiveRefusal(
            "LIVE_MIS_PRODUCT_REQUIRED",
            {"plan_id": ctx.plan_id, "product": product},
        )
    delta = ctx.size_leg(leg)
    reducing = bool(delta.get("quantity")) and not bool(delta.get("increases_exposure"))
    return [
        StepSpec(
            step_no=1,
            step_ref=_step_ref(ctx.plan_id, 1),
            lane=LANE_MIS,
            domain="MIS",
            quantity=abs(_as_int(delta.get("quantity"))),
            side=str(delta.get("side") or ""),
            target_quantity=_as_int(delta.get("target")),
            current_quantity=_as_int(delta.get("current")),
            delta=_as_int(delta.get("delta")),
            lot_size=_as_int(delta.get("lot_size"), 1),
            notional_inr=float(delta.get("notional_inr") or 0.0),
            increases_exposure=bool(delta.get("increases_exposure")),
            depends_on=(),
            release_rule=RULE_MIS_SQUAREOFF if reducing else RULE_IMMEDIATE,
            detail={
                "sizing": dict(delta),
                "mis": {
                    "reducing": reducing,
                    "product": "MIS",
                    "exchange": str(leg.get("exchange") or leg.get("broker_exchange") or ""),
                },
            },
            **_instrument_fields(leg),
        )
    ]


def build_portfolio_steps(ctx: LaneContext) -> List[StepSpec]:
    """CNC/portfolio basket: confirmed reductions first, dependent increases after.

    Reuses the frozen full-snapshot target the ``target_weights`` compiler pinned
    (every scope member appears; an omitted member is an explicit zero) and the
    compiler's own rule that the buys must be funded UP FRONT - the sells'
    proceeds are not counted before they fill. Each increasing leg therefore
    ``withheld``s until every reducing leg is ``filled``; the sells themselves are
    ready immediately, because reducing the book is always permitted.

    ``weights.WeightsPortfolioCompiler._target_quantity`` is reused verbatim so
    the live size is the same arithmetic the paper lane and the compiler use.

    A dependent increase of a plan ADMISSION marked as staged (``staged_increase_inr``)
    is released by :data:`RULE_STAGED_FUNDING_GATE`, not by the generic
    prerequisite rule: its reductions filling does not prove the buy is funded.
    Every other portfolio plan keeps ``RULE_ALL_PREREQUISITES_FILLED``.
    """
    from .compiler.weights import WeightsPortfolioCompiler

    plan_id = ctx.plan_id
    legs = list((ctx.plan.get("resolved_plan") or {}).get("legs") or [])
    if not legs:
        raise LiveRefusal("LIVE_PLAN_COMPOSITION_EMPTY", {"plan_id": plan_id})
    resolved = dict(ctx.plan.get("resolved_plan") or {})
    logical = dict(ctx.plan.get("logical_plan") or {})
    capital_raw = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
    try:
        capital = float(capital_raw)
    except (TypeError, ValueError) as exc:
        raise LiveRefusal(
            "LIVE_TARGET_MISSING",
            {
                "plan_id": plan_id,
                "message": (
                    "a weight-sized live plan must carry the capital basis it was "
                    "frozen with; re-reading the current policy would re-size an "
                    "approved target"
                ),
            },
        ) from exc
    if capital <= 0:
        raise LiveRefusal(
            "LIVE_TARGET_MISSING", {"plan_id": plan_id, "capital_basis_inr": capital}
        )
    buffer_raw = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
    try:
        buffer_pct = 0.0 if buffer_raw is None else float(buffer_raw)
    except (TypeError, ValueError) as exc:
        raise LiveRefusal(
            "LIVE_TARGET_MISSING",
            {"plan_id": plan_id, "cash_buffer_pct": buffer_raw},
        ) from exc

    # Reduce before increase: sell legs are materialized first so the withheld
    # buys can name them as prerequisites. A leg whose target is already met is
    # omitted from the ordering entirely (it is a no-op, not work).
    sized: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    for index, leg in enumerate(legs, start=1):
        leg = dict(leg)
        delta = ctx.size_leg(leg)
        sized.append((index, leg, delta))

    reductions = [
        (index, leg, delta)
        for index, leg, delta in sized
        if _as_int(delta.get("quantity")) and not delta.get("increases_exposure")
    ]
    increases = [
        (index, leg, delta)
        for index, leg, delta in sized
        if _as_int(delta.get("quantity")) and delta.get("increases_exposure")
    ]
    prereqs = tuple(sorted(index for index, _leg, _delta in reductions))

    ordered = reductions + increases
    specs: List[StepSpec] = []
    for index, leg, delta in ordered:
        lot = _as_int(delta.get("lot_size"), 1)
        price = leg.get("reference_price")
        try:
            price = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        # Re-read the target through the compiler's own arithmetic so the live
        # and paper lanes can never disagree about a frozen weight.
        target = WeightsPortfolioCompiler._target_quantity(
            weight=float(leg.get("target_weight") or 0.0),
            capital=capital * max(0.0, 1.0 - buffer_pct),
            price=price,
            lot=lot,
        )
        quantity = abs(_as_int(delta.get("quantity")))
        increases = bool(delta.get("increases_exposure"))
        # A portfolio increase is dependent ONLY when there are reductions to fund
        # and sequence it: with no reducing leg there is nothing to wait for, so the
        # buy is ready immediately.
        gated = bool(increases and prereqs)
        # A STAGED plan's dependent buy is released ONLY by the staged funding
        # gate, never by the generic "every prerequisite filled" label: a filled
        # reduction proves the sequence moved, not that the buy is funded.
        if not gated:
            release_rule = RULE_IMMEDIATE
        elif bool(getattr(ctx, "staged_financing", False)):
            release_rule = RULE_STAGED_FUNDING_GATE
        else:
            release_rule = RULE_ALL_PREREQUISITES_FILLED
        specs.append(
            StepSpec(
                step_no=index,
                step_ref=_step_ref(plan_id, index),
                lane=LANE_PORTFOLIO,
                domain=str(leg.get("product") or "CNC").upper(),
                quantity=quantity,
                side=str(delta.get("side") or ""),
                target_quantity=int(target),
                current_quantity=_as_int(delta.get("current")),
                delta=_as_int(delta.get("delta")),
                lot_size=lot,
                notional_inr=float(delta.get("notional_inr") or 0.0),
                increases_exposure=increases,
                depends_on=prereqs if gated else (),
                release_rule=release_rule,
                detail={
                    "sizing": dict(delta),
                    "target_weight": float(leg.get("target_weight") or 0.0),
                    "capital_basis_inr": capital,
                    "cash_buffer_pct": buffer_pct,
                    "explicit_zero": bool(leg.get("explicit_zero")),
                    "portfolio": {
                        "member_hash": str(resolved.get("member_hash") or ""),
                        "universe_revision_id": str(
                            resolved.get("universe_revision_id") or ""
                        ),
                    },
                },
                **_instrument_fields(leg),
            )
        )
    return specs


def build_futures_steps(ctx: LaneContext) -> List[StepSpec]:
    """The futures/roll lane: pinned contract, pinned lot, roll-ordered release.

    A futures plan is ONE pinned contract with a pinned lot size, and its roll
    half travels WITH the frozen plan (``resolved_plan.roll.role``), so the lane
    never learns its role from a caller. Two shapes:

    * ``open_new`` (or a plan that is not part of a roll at all) is an ordinary
      immediate step, sized by the adapter's frozen attribution-based delta.
    * ``close_old`` is the OTHER PLAN of the roll, and it is ``withheld`` under
      :data:`RULE_ROLL_CLOSE_RELEASED`: the roll's own state machine releases the
      close only once the FULL required replacement quantity is proven filled,
      and a partial, rejected or unknown acquisition never reaches that state.

    The close is an ABSOLUTE FLAT for this strategy's own attributed book, not a
    (target - current) delta: the frozen leg names the OLD contract (and its
    side), while the quantity that has to be closed is whatever the strategy
    actually holds. Sizing it from the leg's signed quantity would double the
    close for a long-old roll. The released quantity is clamped to the CURRENT
    attributed quantity again at release time, so one strategy's close can never
    reach another strategy's shares.
    """
    from .execution import PaperPlanExecutor

    legs = list((ctx.plan.get("resolved_plan") or {}).get("legs") or [])
    if len(legs) != 1:
        raise LiveRefusal(
            "LIVE_PLAN_COMPOUND_UNSUPPORTED",
            {"plan_id": ctx.plan_id, "leg_count": len(legs)},
        )
    leg = dict(legs[0])
    roll = PaperPlanExecutor._roll_binding(ctx.plan)
    role = str((roll or {}).get("role") or "")
    lot = _as_int(leg.get("lot_size"), 0)
    if lot <= 0:
        raise LiveRefusal(
            "LIVE_UNITS_UNPINNED",
            {
                "plan_id": ctx.plan_id,
                "instrument_id": str(leg.get("instrument_id") or ""),
                "message": "the frozen futures leg carries no pinned lot to size against",
            },
        )
    roll_detail = {
        "role": role or None,
        "roll_id": None if not roll else roll.get("roll_id"),
        "product": str(leg.get("product") or ""),
        "expiry": str(leg.get("expiry") or ""),
        "lot_size": lot,
        "lots": _as_int(leg.get("lots")),
        "freeze_quantity": leg.get("freeze_quantity"),
        "freeze_source": str(leg.get("freeze_source") or ""),
    }
    if role == "close_old":
        if ctx.attributed_quantity is None:
            raise LiveRefusal(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": ctx.plan_id,
                    "message": (
                        "a roll close is an absolute flat, so it needs the "
                        "authoritative attributed reader to size what it closes"
                    ),
                },
            )
        current = int(ctx.attributed_quantity(leg) or 0)
        # An absolute flat: the quantity to close is the attributed book itself,
        # never a re-derived lot multiple. A partial-lot residual still has to be
        # closed for the roll to be able to prove the old leg flat, and the
        # release pass clamps the released quantity to the book again.
        quantity = abs(current)
        signed = -quantity if current > 0 else quantity
        price_raw = leg.get("reference_price")
        try:
            price = abs(float(price_raw)) if price_raw is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        return [
            StepSpec(
                step_no=1,
                step_ref=_step_ref(ctx.plan_id, 1),
                lane=LANE_FUTURES_ROLL,
                domain=str(leg.get("instrument_type") or "FUT").upper(),
                quantity=int(quantity),
                side="BUY" if signed > 0 else "SELL",
                target_quantity=0,
                current_quantity=int(current),
                delta=int(-current),
                lot_size=int(lot),
                notional_inr=float(int(quantity) * price),
                increases_exposure=False,
                depends_on=(),
                release_rule=RULE_ROLL_CLOSE_RELEASED,
                detail={
                    "sizing": {
                        "target": 0,
                        "current": int(current),
                        "delta": int(-current),
                        "side": "BUY" if signed > 0 else "SELL",
                        "quantity": int(quantity),
                        "lot_size": int(lot),
                        "increases_exposure": False,
                        "absolute_flat": True,
                    },
                    "roll": roll_detail,
                },
                **_instrument_fields(leg),
            )
        ]
    delta = ctx.size_leg(leg)
    return [
        StepSpec(
            step_no=1,
            step_ref=_step_ref(ctx.plan_id, 1),
            lane=LANE_FUTURES_ROLL,
            domain=str(leg.get("instrument_type") or "FUT").upper(),
            quantity=abs(_as_int(delta.get("quantity"))),
            side=str(delta.get("side") or ""),
            target_quantity=_as_int(delta.get("target")),
            current_quantity=_as_int(delta.get("current")),
            delta=_as_int(delta.get("delta")),
            lot_size=_as_int(delta.get("lot_size"), lot),
            notional_inr=float(delta.get("notional_inr") or 0.0),
            increases_exposure=bool(delta.get("increases_exposure")),
            depends_on=(),
            release_rule=RULE_IMMEDIATE,
            detail={"sizing": dict(delta), "roll": roll_detail},
            **_instrument_fields(leg),
        )
    ]


def build_option_steps(ctx: LaneContext) -> List[StepSpec]:
    """The option-structure lane on the EXISTING durable options engine.

    The frozen structure resolves to the durable option run it executes against
    (``option_target``), and every step's quantity comes from the run's OWN
    confirmed executions through the engine's own ``_option_run_steps`` derivation
    - never from the strategy's aggregate projection, which mixes structures that
    share a contract.

    The dependency graph expresses the two invariants the options engine already
    owns:

    * ENTRY puts the long (hedge) legs first; a SHORT entry leg is ``withheld``
      under :data:`RULE_HEDGE_FILL_GATE` behind the hedge step(s) it depends on.
      A structure with no BUY leg is the admitted naked shape, so there is nothing
      to gate against.
    * EXIT closes liabilities first; the hedge half of the exit is ``withheld``
      under :data:`RULE_HEDGE_RELEASE_WITHHELD` behind the short-closing step(s),
      because releasing a hedge before its short is provably closed opens the
      naked window the structure exists to avoid.
    """
    if ctx.option_target is None or ctx.option_run_steps is None:
        raise LiveRefusal(
            "LIVE_OPTION_RUN_UNAVAILABLE",
            {
                "plan_id": ctx.plan_id,
                "message": (
                    "the option lane needs the durable plan/run binding and the "
                    "option engine's own step derivation"
                ),
            },
        )
    target = dict(ctx.option_target(dict(ctx.plan), dict(ctx.binding)))
    phase = str(target.get("phase") or "")
    if phase == "adjust":
        # B2.2 is paper-only; the live lane must refuse an adjust plan by name
        # rather than reuse the entry/exit dependency rules for it.
        raise LiveRefusal(
            "LIVE_OPTION_ADJUST_UNSUPPORTED",
            {
                "plan_id": ctx.plan_id,
                "phase": phase,
                "message": "the live option lane does not support an adjust plan",
            },
        )
    run = target.get("run")
    run_id = str(getattr(run, "strategy_run_id", "") or target.get("option_run_id") or "")
    steps = list(ctx.option_run_steps(dict(ctx.plan), target))
    if not steps:
        raise LiveRefusal("LIVE_PLAN_COMPOSITION_EMPTY", {"plan_id": ctx.plan_id})

    hedge_entries = [
        int(index)
        for index, leg, _quantity, _side in steps
        if bool(leg.get("_increases_exposure")) and str(_side).upper() == "BUY"
    ]
    short_exits = [
        int(index)
        for index, leg, _quantity, _side in steps
        if not bool(leg.get("_increases_exposure"))
        and int(leg.get("_current_quantity") or 0) < 0
    ]

    specs: List[StepSpec] = []
    for index, leg, quantity, side in steps:
        index = int(index)
        leg = dict(leg)
        signed = int(quantity)
        side = str(side).upper()
        increases = bool(leg.get("_increases_exposure"))
        quantity = abs(signed)
        depends_on: Tuple[int, ...] = ()
        rule = RULE_IMMEDIATE
        rule_detail: Dict[str, Any] = {"phase": phase, "run_leg_id": str(leg.get("_run_leg_id") or "")}
        if phase == "entry" and increases and side == "SELL" and hedge_entries:
            depends_on = tuple(sorted(hedge_entries))
            rule = RULE_HEDGE_FILL_GATE
            rule_detail["gated_by"] = "hedge_fill_gate"
            rule_detail["dependent_short_quantity"] = int(quantity)
        elif phase == "exit" and (not increases) and side == "SELL" and short_exits:
            # A SELL that closes a LONG leg releases the hedge the short defends.
            depends_on = tuple(sorted(short_exits))
            rule = RULE_HEDGE_RELEASE_WITHHELD
            rule_detail["gated_by"] = "structure_exit_builder"
            rule_detail["releases_hedge_for"] = [
                str(step[1].get("tradingsymbol") or "") for step in steps if int(step[0]) in short_exits
            ]
        price_raw = leg.get("reference_price")
        try:
            price = abs(float(price_raw)) if price_raw is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        specs.append(
            StepSpec(
                step_no=index,
                step_ref=_step_ref(ctx.plan_id, index),
                lane=LANE_OPTION_STRUCTURE,
                domain=str(leg.get("option_type") or leg.get("instrument_type") or "OPT").upper(),
                quantity=int(quantity),
                side=side,
                target_quantity=(
                    int(leg.get("signed_quantity") or 0) if phase == "entry" else 0
                ),
                current_quantity=int(leg.get("_current_quantity") or 0),
                delta=int(signed),
                lot_size=_as_int(leg.get("_pinned_lot") or leg.get("lot_size"), 1),
                notional_inr=float(int(quantity) * price),
                increases_exposure=increases,
                depends_on=depends_on,
                release_rule=rule,
                detail={
                    "sizing": {
                        "target": (
                            int(leg.get("signed_quantity") or 0) if phase == "entry" else 0
                        ),
                        "current": int(leg.get("_current_quantity") or 0),
                        "delta": int(signed),
                        "side": side,
                        "quantity": int(quantity),
                        "lot_size": _as_int(leg.get("_pinned_lot") or leg.get("lot_size"), 1),
                        "increases_exposure": increases,
                    },
                    "option": {
                        "phase": phase,
                        "option_run_id": run_id,
                        "run_leg_id": str(leg.get("_run_leg_id") or ""),
                        "structure_id": str(leg.get("structure_id") or ""),
                        "structure_digest": str(leg.get("structure_digest") or ""),
                        "expiry": str(leg.get("expiry") or ""),
                        "expiry_policy": str(leg.get("expiry_policy") or ""),
                    },
                    "release": rule_detail,
                },
                **_instrument_fields(leg),
            )
        )
    return specs


register_live_lane(LANE_SINGLE, build_single_steps)
register_live_lane(LANE_MIS, build_mis_steps)
register_live_lane(LANE_PORTFOLIO, build_portfolio_steps)
register_live_lane(LANE_FUTURES_ROLL, build_futures_steps)
register_live_lane(LANE_OPTION_STRUCTURE, build_option_steps)


def build_steps(ctx: LaneContext, lane: str) -> List[StepSpec]:
    builder = lane_builder(lane)
    if builder is None:
        raise LiveRefusal(
            "LIVE_PLAN_KIND_UNSUPPORTED",
            {
                "plan_id": ctx.plan_id,
                "lane": str(lane),
                "supported": sorted(live_lane_builders()),
            },
        )
    return list(builder(ctx))


#: ``plan_kind`` -> lane for the kinds this executor dispatches. A kind that is
#: not here is a named refusal: ``intent_bundle`` is never guessed at.
SUPPORTED_PLAN_KINDS = (
    LANE_SINGLE,
    LANE_PORTFOLIO,
    LANE_FUTURES_ROLL,
    LANE_OPTION_STRUCTURE,
)

#: The plan kinds whose lane IS the plan kind (one contract shape, one lane).
_PLAN_KIND_LANES = {
    "single_instrument": LANE_SINGLE,
    "target_weights": LANE_PORTFOLIO,
    "target_futures": LANE_FUTURES_ROLL,
    "option_structure": LANE_OPTION_STRUCTURE,
}


def lane_for_plan(plan: Mapping[str, Any]) -> str:
    """Which lane a frozen plan belongs to, from PERSISTED plan content only.

    MIS is not a plan kind: it is the ordinary single-instrument shape carried
    under the intraday product, so the product frozen in the plan decides it.
    """
    plan_kind = str(plan.get("plan_kind") or "")
    lane = _PLAN_KIND_LANES.get(plan_kind)
    if lane == LANE_SINGLE:
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        products = {str(leg.get("product") or "").upper() for leg in legs}
        if products == {"MIS"}:
            return LANE_MIS
        return LANE_SINGLE
    if lane is not None:
        return lane
    raise LiveRefusal(
        "LIVE_PLAN_KIND_UNSUPPORTED",
        {
            "plan_id": str(plan.get("plan_id") or ""),
            "plan_kind": plan_kind,
            "supported": list(SUPPORTED_PLAN_KINDS),
            "message": "live support remains incomplete: this plan kind is not carried",
        },
    )


def prerequisites_met(spec: StepSpec, states: Mapping[int, str]) -> bool:
    """Whether every prerequisite of ``spec`` is provably FILLED.

    ``filled`` is the only releasing evidence: a rejected, cancelled, uncertain or
    partially-filled prerequisite has NOT produced the exposure the dependent leg
    was sequenced behind, so the dependent stays withheld.
    """
    return all(str(states.get(int(step_no)) or "") == "filled" for step_no in spec.depends_on)


def required_notional(specs: Sequence[StepSpec]) -> float:
    """The capacity the parent must hold: every pending/unsubmitted increasing leg.

    Reductions need no capacity, but they are not "free": a plan whose increases
    are not covered end to end would let the first leg consume the whole
    reservation and leave later legs unfunded.
    """
    return float(sum(float(spec.notional_inr or 0.0) for spec in specs if spec.increases_exposure))


def capacity_covers(reservation: Mapping[str, Any], specs: Sequence[StepSpec]) -> Tuple[bool, Dict[str, Any]]:
    """Whether the reservation covers every increasing leg of the parent."""
    required = required_notional(specs)
    held = float((reservation or {}).get("reserved_notional_inr") or 0.0)
    return (held + 1e-9 >= required), {
        "required_inr": required,
        "reserved_notional_inr": held,
    }


class LivePlanSequence:
    """Durable parent storage: materialize, read, settle. No dispatch here."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        submissions: Optional[LiveSubmissionStore] = None,
        ledger: Optional[ReservationLedger] = None,
        barrier: Optional[ExecutionBarrier] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.submissions = submissions or LiveSubmissionStore(
            session_factory=self.session_factory
        )
        self.ledger = ledger or ReservationLedger(session_factory=self.session_factory)
        self.barrier = barrier or ExecutionBarrier(session_factory=self.session_factory)
        self._clock = clock or _utcnow

    # ------------------------------------------------------------ reads

    @staticmethod
    def _dialect(session: Any) -> str:
        return LiveSubmissionStore._dialect(session)

    def get_execution(self, plan_id: str, *, db: Any = None) -> Optional[Dict[str, Any]]:
        owns = db is None
        session = db or self.session_factory()
        try:
            row = (
                session.execute(
                    text(
                        """
                        SELECT execution_id, plan_id, strategy_id, account_id,
                               execution_environment, lane, state, step_spec, detail
                        FROM public.live_plan_executions
                        WHERE plan_id = :plan_id
                        """
                    ),
                    {"plan_id": str(plan_id)},
                )
                .mappings()
                .first()
            )
        finally:
            if owns:
                session.close()
        if row is None:
            return None
        return self._execution_row(dict(row))

    @staticmethod
    def _execution_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "execution_id": str(row.get("execution_id") or ""),
            "plan_id": str(row.get("plan_id") or ""),
            "strategy_id": str(row.get("strategy_id") or ""),
            "account_id": str(row.get("account_id") or ""),
            "execution_environment": str(row.get("execution_environment") or ""),
            "lane": str(row.get("lane") or ""),
            "state": str(row.get("state") or ""),
            "step_spec": [StepSpec.from_dict(item) for item in _as_list(row.get("step_spec"))],
            "detail": _as_dict(row.get("detail")),
        }

    def step_states(self, plan_id: str, *, db: Any = None) -> Dict[int, str]:
        owns = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                text(
                    "SELECT step_no, state FROM public.live_plan_submissions "
                    "WHERE plan_id = :plan_id"
                ),
                {"plan_id": str(plan_id)},
            ).fetchall()
        finally:
            if owns:
                session.close()
        return {_as_int(row[0]): str(row[1] or "") for row in rows}

    def leg_outcome(
        self, *, plan_id: str, step_no: int, db: Any = None
    ) -> Optional[Dict[str, Any]]:
        """The parent's own record of one leg's terminal outcome, or ``None``."""
        parent = self.get_execution(plan_id, db=db)
        if parent is None:
            return None
        legs = dict(parent["detail"].get("legs") or {})
        outcome = legs.get(str(int(step_no)))
        return dict(outcome) if isinstance(outcome, Mapping) else None

    # ------------------------------------------------------ materialize

    def materialize(
        self,
        *,
        plan: Mapping[str, Any],
        lane: str,
        step_specs: Sequence[StepSpec],
        detail: Optional[Mapping[str, Any]] = None,
        db: Any,
    ) -> Tuple[Dict[str, Any], bool]:
        """Insert the parent + every step claim + barrier work, in ``db``'s tx.

        The caller has already taken the canonical book lock on ``db``. Returns
        the stored parent and whether THIS call created it. A concurrent caller
        (or a retry) updates zero rows and reads the winner's parent: it never
        materializes a second protocol, and it never re-derives a frozen step.
        """
        plan_id = str(plan.get("plan_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        json_cast = (
            ":{0}" if self._dialect(db) == "sqlite" else "CAST(:{0} AS jsonb)"
        )
        execution_id = f"live_exec_{uuid.uuid4().hex}"
        result = db.execute(
            text(
                f"""
                INSERT INTO public.live_plan_executions (
                    execution_id, plan_id, strategy_id, account_id,
                    execution_environment, lane, state, step_spec, detail
                ) VALUES (
                    :execution_id, :plan_id, :strategy_id, :account_id,
                    :execution_environment, :lane, :state,
                    {json_cast.format('step_spec')},
                    {json_cast.format('detail')}
                )
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),
            {
                "execution_id": execution_id,
                "plan_id": plan_id,
                "strategy_id": strategy_id,
                "account_id": account_id,
                "execution_environment": LIVE_ENVIRONMENT,
                "lane": str(lane),
                "state": PARENT_PLANNED,
                "step_spec": json.dumps([spec.as_dict() for spec in step_specs]),
                "detail": json.dumps(dict(detail or {})),
            },
        )
        created = int(getattr(result, "rowcount", 0) or 0) > 0
        stored = self.get_execution(plan_id, db=db)
        if stored is None:
            raise LiveRefusal("LIVE_SEQUENCE_MISSING", {"plan_id": plan_id})
        if not created:
            # The winner already owns the frozen protocol: a retry reads it.
            return stored, False

        for spec in step_specs:
            if not spec.quantity:
                # Nothing to send: the attributed book already sits at the target.
                state = "no_op"
            elif spec.depends_on or spec.release_rule != RULE_IMMEDIATE:
                # A dependent leg - or a leg the LANE gates on its own condition
                # (the MIS square-off clock) - is in-flight work whose release needs
                # evidence. It is never dispatched at materialization.
                state = STEP_WITHHELD
            else:
                state = STEP_PENDING
            self.submissions.claim(
                plan_id=plan_id,
                step_no=spec.step_no,
                step_ref=spec.step_ref,
                strategy_id=strategy_id,
                account_id=account_id,
                execution_environment=LIVE_ENVIRONMENT,
                delta_snapshot={
                    "target": spec.target_quantity,
                    "current": spec.current_quantity,
                    "delta": spec.delta,
                    "side": spec.side,
                    "quantity": int(spec.quantity),
                    "lot_size": int(spec.lot_size),
                    "notional_inr": float(spec.notional_inr),
                    "increases_exposure": bool(spec.increases_exposure),
                },
                state=state,
                detail={
                    "execution_id": str(stored["execution_id"]),
                    "lane": str(spec.lane),
                    "depends_on": [int(value) for value in spec.depends_on],
                    "release_rule": str(spec.release_rule),
                    "step_spec": spec.as_dict(),
                },
                db=db,
            )
            if spec.quantity:
                # Every step's work is visible from materialization: a withheld
                # dependent is unresolved work, not an absence.
                self.barrier.record_work_event_once(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    execution_environment=LIVE_ENVIRONMENT,
                    event="work_created",
                    ref=spec.step_ref,
                    detail={
                        "plan_id": plan_id,
                        "step_no": int(spec.step_no),
                        "lane": str(spec.lane),
                        "withheld": bool(spec.depends_on),
                    },
                    dedupe_key=f"{plan_id}:{int(spec.step_no)}",
                    db=db,
                )
        return self.get_execution(plan_id, db=db) or stored, True

    # --------------------------------------------------------- settle

    def mark_executing(self, *, plan_id: str) -> None:
        """The parent has committed to sending at least one leg.

        ``planned`` means "materialized, nothing sent yet"; a parent with a ready
        leg moves here as the first dispatch happens, and the sequence pass moves it
        when it releases a withheld dependent leg.
        """
        with self.session_factory() as session:
            now_expr = (
                "CURRENT_TIMESTAMP" if self._dialect(session) == "sqlite" else "NOW()"
            )
            session.execute(
                text(
                    "UPDATE public.live_plan_executions SET state = :state, "
                    f"updated_at = {now_expr} "
                    "WHERE plan_id = :plan_id AND state = :planned"
                ),
                {
                    "state": PARENT_EXECUTING,
                    "planned": PARENT_PLANNED,
                    "plan_id": str(plan_id),
                },
            )
            session.commit()

    def record_leg_outcome(
        self,
        *,
        plan_id: str,
        step_no: int,
        outcome: str,
        filled: int,
        ordered: int,
        db: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """Record ONE leg's terminal outcome on the parent. Never settles.

        Returns the parent view, or ``None`` when this plan has no parent (a
        pre-sequence plan: the caller keeps the legacy per-plan behaviour).
        """
        owns = db is None
        session = db or self.session_factory()
        now_expr = "CURRENT_TIMESTAMP" if self._dialect(session) == "sqlite" else "NOW()"
        json_cast = ":{0}" if self._dialect(session) == "sqlite" else "CAST(:{0} AS jsonb)"
        try:
            parent = self.get_execution(plan_id, db=session)
            if parent is None:
                return None
            detail = dict(parent["detail"])
            legs = dict(detail.get("legs") or {})
            legs[str(int(step_no))] = {
                "outcome": str(outcome),
                "filled_quantity": int(filled),
                "ordered_quantity": int(ordered),
                "at": self._clock().isoformat(),
            }
            detail["legs"] = legs
            session.execute(
                text(
                    f"""
                    UPDATE public.live_plan_executions
                    SET state = :state,
                        detail = {json_cast.format('detail')},
                        updated_at = {now_expr}
                    WHERE plan_id = :plan_id
                    """
                ),
                {
                    "state": (
                        parent["state"]
                        if parent["state"] != PARENT_PLANNED
                        else PARENT_EXECUTING
                    ),
                    "detail": json.dumps(detail),
                    "plan_id": str(plan_id),
                },
            )
            session.flush()
            if owns:
                session.commit()
        except Exception:
            if owns:
                session.rollback()
            raise
        finally:
            if owns:
                session.close()
        return self.get_execution(plan_id)

    def settle_parent_if_complete(
        self, *, plan_id: str, db: Any = None, actor_id: str = "live-sequence"
    ) -> Dict[str, Any]:
        """Settle the parent reservation ONCE, and ONLY on proven terminal legs.

        Three outcomes, and the middle one matters most:

        * every leg terminal and NO fill  -> ``released`` (the capacity backs
          nothing, so the unused allocation goes back to the account);
        * every leg terminal and ANY fill -> ``consumed`` (part of that capacity now
          backs real exposure; the ledger has no partial release, so the honest
          representation is that it is no longer the owner's to reclaim);
        * a leg still pending/withheld    -> ``retained`` (nothing is released or
          consumed: those legs still need the allocation).

        Idempotent and retryable: a failed effect leaves no ``settlement`` recorded,
        so the next pass re-attempts it rather than declaring a state it did not
        reach.
        """
        owns = db is None
        session = db or self.session_factory()
        now_expr = "CURRENT_TIMESTAMP" if self._dialect(session) == "sqlite" else "NOW()"
        json_cast = ":{0}" if self._dialect(session) == "sqlite" else "CAST(:{0} AS jsonb)"
        try:
            parent = self.get_execution(plan_id, db=session)
            if parent is None:
                return {"parent": False, "state": "no_parent", "settled": True}
            if parent["state"] == PARENT_SETTLED and parent["detail"].get("settlement"):
                return {
                    "parent": True,
                    "state": "settled",
                    "settled": True,
                    "settlement": dict(parent["detail"].get("settlement") or {}),
                    "released": str(
                        (parent["detail"].get("settlement") or {}).get("outcome") or ""
                    )
                    == "released",
                }
            detail = dict(parent["detail"])
            legs = dict(detail.get("legs") or {})
            states = self.step_states(plan_id, db=session)

            def _proven_terminal(step_no: int) -> bool:
                # A leg is terminal when the PARENT recorded a terminal outcome for
                # it (the proven evidence, written by the consumer/executor/operator
                # path) or when its claim itself reached a terminal state.
                recorded = str((legs.get(str(int(step_no))) or {}).get("outcome") or "")
                if recorded in STEP_TERMINAL:
                    return True
                return str(states.get(int(step_no)) or "") in STEP_TERMINAL

            outstanding = sorted(
                int(item.step_no)
                for item in parent["step_spec"]
                if not _proven_terminal(int(item.step_no))
            )
            if outstanding:
                return {
                    "parent": True,
                    "state": "retained",
                    "settled": False,
                    "retained": True,
                    "outstanding_legs": outstanding,
                }
            any_filled = any(
                str((legs.get(str(int(item.step_no))) or {}).get("outcome") or "") == "filled"
                or int((legs.get(str(int(item.step_no))) or {}).get("filled_quantity") or 0) > 0
                for item in parent["step_spec"]
            )
            reservation = self.ledger.for_plan(plan_id)
            if reservation is not None and str(reservation.get("status")) != "consumed":
                if any_filled:
                    try:
                        self.ledger.consume(
                            str(reservation["reservation_id"]),
                            actor_id=actor_id,
                            detail={"plan_id": plan_id, "lane": str(parent["lane"]), "legs": legs},
                        )
                    except Exception as exc:  # noqa: BLE001 - retried on the next pass
                        detail["settlement_refused"] = {
                            "outcome": "consume_refused",
                            "error": str(exc),
                        }
                        session.execute(
                            text(
                                f"UPDATE public.live_plan_executions SET detail = "
                                f"{json_cast.format('detail')}, updated_at = {now_expr} "
                                "WHERE plan_id = :plan_id"
                            ),
                            {"detail": json.dumps(detail), "plan_id": str(plan_id)},
                        )
                        if owns:
                            session.commit()
                        return {
                            "parent": True,
                            "state": "retained",
                            "settled": False,
                            "retained": True,
                            "refused": "consume_refused",
                        }
                else:
                    if str(reservation.get("status")) != "released":
                        try:
                            self.ledger.release(
                                str(reservation["reservation_id"]),
                                reason="terminal_unfilled",
                                actor_id=actor_id,
                            )
                        except Exception as exc:  # noqa: BLE001 - retried on the next pass
                            detail["settlement_refused"] = {
                                "outcome": "release_refused",
                                "error": str(exc),
                            }
                            session.execute(
                                text(
                                    f"UPDATE public.live_plan_executions SET detail = "
                                    f"{json_cast.format('detail')}, updated_at = {now_expr} "
                                    "WHERE plan_id = :plan_id"
                                ),
                                {"detail": json.dumps(detail), "plan_id": str(plan_id)},
                            )
                            if owns:
                                session.commit()
                            return {
                                "parent": True,
                                "state": "retained",
                                "settled": False,
                                "retained": True,
                                "refused": "release_refused",
                            }
            settlement = {
                "outcome": "consumed" if any_filled else "released",
                "at": self._clock().isoformat(),
                "lane": str(parent["lane"]),
                "legs": legs,
            }
            detail["settlement"] = settlement
            detail.pop("settlement_refused", None)
            session.execute(
                text(
                    f"""
                    UPDATE public.live_plan_executions
                    SET state = :state,
                        detail = {json_cast.format('detail')},
                        updated_at = {now_expr}
                    WHERE plan_id = :plan_id
                    """
                ),
                {
                    "state": PARENT_SETTLED,
                    "detail": json.dumps(detail),
                    "plan_id": str(plan_id),
                },
            )
            session.flush()
            if owns:
                session.commit()
        except Exception:
            if owns:
                session.rollback()
            raise
        finally:
            if owns:
                session.close()
        return {
            "parent": True,
            "state": "settled",
            "settled": True,
            "settlement": settlement,
            "released": settlement["outcome"] == "released",
            "consumed": settlement["outcome"] == "consumed",
        }

    def declare_leg_terminal(
        self,
        *,
        plan_id: str,
        step_no: int,
        outcome: str,
        filled: int,
        ordered: int,
        db: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """Record one leg's terminal outcome, then settle the parent if complete.

        Returns the parent view, or ``None`` when this plan has no parent (a
        pre-sequence plan: the caller keeps the legacy per-plan behaviour).
        The reservation is settled EXACTLY ONCE, and only when EVERY leg is
        terminal: consuming (or releasing) the parent after the first leg filled
        would take capacity away from the legs that have not been submitted yet.
        """
        parent = self.record_leg_outcome(
            plan_id=plan_id,
            step_no=step_no,
            outcome=outcome,
            filled=filled,
            ordered=ordered,
            db=db,
        )
        if parent is None:
            return None
        self.settle_parent_if_complete(plan_id=plan_id, db=db)
        return self.get_execution(plan_id, db=db)

    # ------------------------------------------------------- release scan

    def releasable_parents(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Parents with unresolved work, oldest first, for the sequence pass."""
        with self.session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT execution_id, plan_id, strategy_id, account_id,
                           execution_environment, lane, state, step_spec, detail
                    FROM public.live_plan_executions
                    WHERE execution_environment = :environment
                      AND state = ANY(:states)
                    ORDER BY updated_at
                    LIMIT :limit
                    """
                ),
                {
                    "environment": LIVE_ENVIRONMENT,
                    "states": list(PARENT_INFLIGHT),
                    "limit": int(limit),
                },
            ).fetchall()
        return [self._execution_row(dict(row._mapping)) for row in rows]

    def withheld_steps(self, plan_id: str) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT submission_id, plan_id, step_no, step_ref, state,
                               strategy_id, account_id, broker_order_ids,
                               delta_snapshot, detail
                        FROM public.live_plan_submissions
                        WHERE plan_id = :plan_id AND state = :state
                        ORDER BY step_no
                        """
                    ),
                    {"plan_id": str(plan_id), "state": STEP_WITHHELD},
                )
                .mappings()
                .all()
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["broker_order_ids"] = [str(v) for v in _as_list(item.get("broker_order_ids"))]
            item["delta_snapshot"] = _as_dict(item.get("delta_snapshot"))
            item["detail"] = _as_dict(item.get("detail"))
            out.append(item)
        return out

    def record_release_blocker(
        self, *, plan_id: str, step_no: int, reason_code: str, detail: Mapping[str, Any]
    ) -> None:
        """Name why a withheld step is NOT released. Places nothing."""
        with self.session_factory() as session:
            json_cast = (
                ":{0}" if self._dialect(session) == "sqlite" else "CAST(:{0} AS jsonb)"
            )
            now_expr = (
                "CURRENT_TIMESTAMP" if self._dialect(session) == "sqlite" else "NOW()"
            )
            session.execute(
                text(
                    f"""
                    UPDATE public.live_plan_submissions
                    SET detail = COALESCE(detail, '{{}}'::jsonb) || {json_cast.format('patch')},
                        updated_at = {now_expr}
                    WHERE plan_id = :plan_id AND step_no = :step_no
                      AND state = :state
                    """
                ),
                {
                    "patch": json.dumps(
                        {
                            "release_blocked": str(reason_code),
                            "release_blocked_detail": dict(detail),
                            "release_blocked_at": self._clock().isoformat(),
                        }
                    ),
                    "plan_id": str(plan_id),
                    "step_no": int(step_no),
                    "state": STEP_WITHHELD,
                },
            )
            session.commit()
