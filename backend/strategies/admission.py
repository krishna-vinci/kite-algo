"""Deterministic admission: ordered checks, named refusals, fail-closed (G9).

Admission is the first gate an exposure-increasing plan passes (R3 §6 flow:
frozen plan → admission verdict → durable reservation → owner approval). Two
properties matter more than any individual control:

* **Determinism.** The checks run in a fixed order and the FIRST refusal wins, so
  the same plan and the same evidence always produce the same verdict and the
  same reason. An operator debugging a refusal never has to guess which control
  fired first.
* **Fail-closed honesty (D-9).** Where V1 cannot prove something — a live
  daily-loss source, a margin quote, a reference price — admission refuses with a
  named reason instead of silently treating the axis as satisfied. An
  unenforced control that looks enforced is worse than a refusal.

A NULL limit means "not enforced" and is deliberately distinguishable from a
limit of zero. Nothing here places orders or sizes anything: deterministic
resizing is a non-goal, and this phase's verdict is consumed by nothing yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    AccountReconciliationVersion,
    StrategyAdmissionPolicy,
    StrategyPositionProjection,
    StrategyProjectionState,
    StrategyReservation,
)

#: Refusal vocabulary, in evaluation order (D-2). The plan's list is verbatim
#: except REFERENCE_PRICE_UNAVAILABLE, which names the case where the notional
#: axes are configured but the arithmetic has no price to work with — refusing is
#: the honest behaviour, and inventing the name is better than under-enforcing.
ADMISSION_REFUSALS = (
    "ADMISSION_POLICY_MISSING",
    "ALLOCATION_EXCEEDED",
    "REFERENCE_PRICE_UNAVAILABLE",
    "POSITION_VALUATION_UNAVAILABLE",
    "STAGED_LIVE_FINANCING_UNSUPPORTED",
    "INSTRUMENT_NOTIONAL_EXCEEDED",
    "GROSS_NOTIONAL_EXCEEDED",
    "MAX_OPEN_INSTRUMENTS_EXCEEDED",
    "ORDER_RATE_EXCEEDED",
    "DAILY_LOSS_BUDGET_UNAVAILABLE",
    "STRATEGY_RISK_POLICY_MISSING",
    "OPTION_STRUCTURE_FAMILY_NOT_ALLOWED",
    "OPTION_NAKED_NOT_PERMITTED",
    "OPTION_MAX_LOSS_EXCEEDED",
    "STRATEGY_NOTIONAL_LIMIT_EXCEEDED",
    "CATALOG_INVALID",
    "SESSION_PRODUCT_INVALID",
    "MARGIN_UNAVAILABLE",
    "MARGIN_QUOTE_STALE",
    "MARGIN_INSUFFICIENT",
)

#: Statuses that hold capacity. ``consumed`` is included deliberately: capital
#: backing an open position is never released because its evaluation expired.
CAPACITY_HOLDING_STATUSES = ("active", "renewed", "consumed", "action_required")

#: Statuses that hold capacity as an UNFILLED commitment. ``consumed`` is NOT
#: here: a consumed reservation became a published position, and counting both
#: would double-charge the same exposure. Consumed history stays auditable in the
#: ledger (``consumed_history_inr``), it is just no longer capacity.
PENDING_COMMITMENT_STATUSES = ("active", "renewed", "action_required")

DEFAULT_MARGIN_MAX_AGE_SECONDS = 60

#: Products admission accepts, per the existing platform vocabulary.
VALID_PRODUCTS = frozenset({"CNC", "MIS", "NRML"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _min_optional(*values: Optional[float]) -> Optional[float]:
    """The smallest value a level states, ignoring the levels that state none."""
    present = [float(value) for value in values if value is not None]
    return min(present) if present else None


@dataclass(frozen=True)
class AdmissionVerdict:
    """A verdict is a value, not an action. Nothing consumes it for orders yet."""

    admitted: bool
    refusal_reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"admitted": self.admitted, "detail": dict(self.detail)}
        if self.refusal_reason:
            payload["rejection_reason"] = self.refusal_reason
        return payload


def margin_max_age_seconds() -> float:
    """The configured freshness bound (D-7); the age itself is policy, not code."""
    raw = os.environ.get("ADMISSION_MARGIN_MAX_AGE_SECONDS")
    if raw is None:
        return float(DEFAULT_MARGIN_MAX_AGE_SECONDS)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_MARGIN_MAX_AGE_SECONDS)


class AdmissionService:
    """Reads policies and evidence; produces verdicts. Writes only policies."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        margin_engine: Any = None,
    ) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._margin_engine = margin_engine

    # -- policy -------------------------------------------------------------

    def policy_for(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyAdmissionPolicy).where(
                    StrategyAdmissionPolicy.strategy_id == str(strategy_id)
                )
            ).scalar_one_or_none()
            return self._policy_view(row) if row is not None else None

    @staticmethod
    def _policy_view(row: StrategyAdmissionPolicy) -> Dict[str, Any]:
        return {
            "strategy_id": str(row.strategy_id),
            "account_id": str(row.account_id),
            "allocation_inr": _as_float(row.allocation_inr),
            "per_instrument_notional_inr": _as_float(row.per_instrument_notional_inr),
            "gross_notional_inr": _as_float(row.gross_notional_inr),
            "max_open_instruments": row.max_open_instruments,
            "admissions_per_window": row.admissions_per_window,
            "admission_window_seconds": row.admission_window_seconds,
            "daily_loss_budget_inr": _as_float(row.daily_loss_budget_inr),
            "updated_by": str(row.updated_by),
        }

    def upsert_policy(
        self,
        *,
        strategy_id: str,
        account_id: str,
        updated_by: str,
        allocation_inr: Optional[float] = None,
        per_instrument_notional_inr: Optional[float] = None,
        gross_notional_inr: Optional[float] = None,
        max_open_instruments: Optional[int] = None,
        admissions_per_window: Optional[int] = None,
        admission_window_seconds: Optional[int] = None,
        daily_loss_budget_inr: Optional[float] = None,
    ) -> Dict[str, Any]:
        session = self.session_factory()
        try:
            row = session.execute(
                select(StrategyAdmissionPolicy).where(
                    StrategyAdmissionPolicy.strategy_id == str(strategy_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = StrategyAdmissionPolicy(
                    strategy_id=str(strategy_id), account_id=str(account_id)
                )
                session.add(row)
            row.account_id = str(account_id)
            row.allocation_inr = allocation_inr
            row.per_instrument_notional_inr = per_instrument_notional_inr
            row.gross_notional_inr = gross_notional_inr
            row.max_open_instruments = max_open_instruments
            row.admissions_per_window = admissions_per_window
            row.admission_window_seconds = admission_window_seconds
            row.daily_loss_budget_inr = daily_loss_budget_inr
            row.updated_by = str(updated_by)
            row.updated_at = _utcnow()
            session.commit()
            return self._policy_view(row)
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- evidence -----------------------------------------------------------

    def plan_notional(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        """The plan's own notional requirement, from its resolved representation.

        A leg that carries a ``signed_quantity`` is |quantity| x reference price.

        A leg that carries a full-snapshot ``target_weight`` is sized against the
        capital basis FROZEN with the plan - ``|weight| x basis x (1 - buffer)`` -
        because a weight is a FRACTION, not a share count. Reading the weight as a
        quantity would under-state the requirement by orders of magnitude and let a
        portfolio plan claim a reservation far smaller than the legs it will place.

        The price comes from the plan itself - no new market-data dependency. When a
        priced leg carries no price, or a weighted leg carries no frozen basis, the
        requirement is **unknown**, not zero, and the caller refuses rather than
        under-enforcing a configured limit.
        """
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        logical = dict(plan.get("logical_plan") or {})
        resolved = dict(plan.get("resolved_plan") or {})
        basis_raw = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
        try:
            capital_basis = None if basis_raw is None else float(basis_raw)
        except (TypeError, ValueError):
            capital_basis = None
        buffer_raw = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
        try:
            buffer_pct = 0.0 if buffer_raw is None else float(buffer_raw)
        except (TypeError, ValueError):
            buffer_pct = 0.0
        per_leg: List[Dict[str, Any]] = []
        total = 0.0
        missing: List[str] = []
        missing_basis: List[str] = []
        for leg in legs:
            symbol = str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "")
            price = _as_float(leg.get("reference_price"))
            weight = leg.get("target_weight")
            if leg.get("signed_quantity") is not None or weight is None:
                quantity = abs(float(leg.get("signed_quantity") or 0.0))
                if price is None:
                    if quantity:
                        missing.append(symbol)
                    notional = 0.0
                else:
                    notional = quantity * abs(price)
            else:
                weight_value = abs(float(weight or 0.0))
                if capital_basis is None:
                    if weight_value:
                        missing_basis.append(symbol)
                    notional = 0.0
                else:
                    notional = weight_value * capital_basis * max(0.0, 1.0 - buffer_pct)
                quantity = (
                    notional / abs(price) if (price is not None and abs(price) > 0) else 0.0
                )
            total += notional
            per_leg.append({"tradingsymbol": symbol, "quantity": quantity, "notional_inr": notional})
        return {
            "total_notional_inr": total,
            "per_leg": per_leg,
            "reference_price_missing": sorted(set(missing)),
            "capital_basis_missing": sorted(set(missing_basis)),
            "instrument_count": len({row["tradingsymbol"] for row in per_leg if row["tradingsymbol"]}),
        }

    # -- post-plan exposure (per instrument, canonical coordinates) ---------

    def capacity_held_inr(
        self,
        *,
        account_id: str,
        strategy_id: str,
        execution_environment: str,
    ) -> Dict[str, float]:
        """Capacity held for ONE strategy in ONE environment (shared rule).

        Delegates to :mod:`backend.strategies.financing` so admission and the
        reservation ledger can never drift: the rule is defined once.
        """
        from backend.strategies.financing import capacity_held

        with self.session_factory() as session:
            return capacity_held(
                session,
                account_id=account_id,
                strategy_id=strategy_id,
                execution_environment=execution_environment,
            )

    def plan_exposure(
        self,
        plan: Mapping[str, Any],
        *,
        execution_environment: str,
    ) -> Dict[str, Any]:
        """Post-plan exposure, from the ONE shared rule in ``financing``.

        The ledger revalidates with exactly this function under its own lock, so
        admission cannot drift from the gate that actually claims capacity.
        """
        from backend.strategies.financing import plan_exposure

        with self.session_factory() as session:
            return plan_exposure(
                session, plan, execution_environment=execution_environment
            )

    def attributed_consumption(
        self, *, strategy_id: str, account_id: str, reference_price: Optional[float]
    ) -> float:
        """Σ approximate notional of the strategy's live book (G1 projection).

        This is *attributed consumption*: exposure the strategy already holds, so
        it counts against the allocation before any new plan may claim capacity.
        """
        price = _as_float(reference_price) or 0.0
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyPositionProjection.net_quantity).where(
                    StrategyPositionProjection.account_id == str(account_id),
                    StrategyPositionProjection.strategy_id == str(strategy_id),
                    StrategyPositionProjection.execution_environment == "live",
                )
            ).scalars().all()
        return float(sum(abs(float(quantity or 0)) for quantity in rows) * abs(price))

    def reserved_notional(self, *, account_id: str, strategy_id: Optional[str] = None) -> float:
        """Capacity currently held by reservations on the account."""
        with self.session_factory() as session:
            query = select(func.coalesce(func.sum(StrategyReservation.reserved_notional_inr), 0.0)).where(
                StrategyReservation.account_id == str(account_id),
                StrategyReservation.status.in_(CAPACITY_HOLDING_STATUSES),
            )
            if strategy_id is not None:
                query = query.where(StrategyReservation.strategy_id == str(strategy_id))
            return float(session.execute(query).scalar() or 0.0)

    def recent_admission_count(
        self, *, strategy_id: str, window_seconds: int, now: Optional[datetime] = None
    ) -> int:
        """Prior admissions in the trailing window, from the reservation ledger.

        The ledger is the right place to count: an admission that produced no
        reservation did not spend anything, and counting attempts would let a
        refused caller exhaust its own rate budget.
        """
        moment = now or _utcnow()
        cutoff = moment - timedelta(seconds=int(window_seconds))
        with self.session_factory() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(StrategyReservation)
                    .where(
                        StrategyReservation.strategy_id == str(strategy_id),
                        StrategyReservation.created_at >= cutoff,
                    )
                ).scalar()
                or 0
            )

    def reconciliation_version(self, *, account_id: str) -> int:
        with self.session_factory() as session:
            row = session.execute(
                select(AccountReconciliationVersion).where(
                    AccountReconciliationVersion.account_id == str(account_id)
                )
            ).scalar_one_or_none()
            return int(row.version) if row is not None else 0

    # -- per-strategy risk policy (B2.5) ------------------------------------

    def declared_risk_policy(self, plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The risk policy the plan's OWN frozen version declared, or ``None``.

        Resolution walks only PERSISTED authority - the plan's proposal, the
        hosted job that produced it and the immutable version that job pinned -
        so a plan is never judged against a policy the strategy adopted later.
        An unreadable chain (an external run with no hosted job, or a store that
        cannot answer) is treated as "no declaration"; the options lane then
        refuses by name, which is the fail-closed reading.
        """
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            return None
        from sqlalchemy import or_

        from backend.strategies.attribution_models import StrategyPlan, StrategyProposal
        from backend.strategies.models import HostedStrategyVersion, StrategyJob

        try:
            with self.session_factory() as session:
                row = session.execute(
                    select(HostedStrategyVersion.risk_policy)
                    .join(
                        StrategyJob,
                        StrategyJob.version_id == HostedStrategyVersion.id,
                    )
                    .join(
                        StrategyProposal,
                        or_(
                            StrategyProposal.job_id == StrategyJob.id,
                            StrategyProposal.strategy_run_id == StrategyJob.run_id,
                        ),
                    )
                    .join(
                        StrategyPlan,
                        StrategyPlan.proposal_id == StrategyProposal.proposal_id,
                    )
                    .where(StrategyPlan.plan_id == plan_id)
                    .order_by(StrategyJob.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
        except SQLAlchemyError:
            return None
        return dict(row) if isinstance(row, Mapping) else None

    def _risk_policy_gate(
        self,
        plan: Mapping[str, Any],
        *,
        policy: Optional[Mapping[str, Any]],
        exposure: Mapping[str, Any],
        notional: Mapping[str, Any],
        detail: Dict[str, Any],
        margin_evidence: Optional[Mapping[str, Any]],
    ) -> Optional[AdmissionVerdict]:
        """Enforce the effective per-strategy risk policy (B2.5).

        The options lane (an ENTRY, or an ADJUST that increases exposure) is
        gated on the whole policy; every other lane is only tightened by the
        notional limit, which composes with the existing allocation/gross checks
        rather than replacing them. EXITS and risk-REDUCING adjustments are never
        blocked: closing risk is not a decision this policy gets to refuse.
        """
        from backend.options.execution.plan_binding import (
            is_option_adjust_plan,
            is_option_entry_plan,
        )
        from backend.strategies.risk_policy import (
            classify_structure_family,
            effective_risk_policy,
            frozen_protection_stop,
            operator_risk_policy,
            platform_risk_ceiling,
            unhedged_target,
            worst_case_loss_inr,
        )

        is_adjust = is_option_adjust_plan(plan)
        is_option = is_adjust or is_option_entry_plan(plan)
        if is_adjust:
            if not any(row.get("increases_exposure") for row in exposure["per_instrument"]):
                detail["risk_policy_applies"] = False
                detail["risk_policy_reason"] = "risk_reducing_adjust"
                return None

        declared = self.declared_risk_policy(plan)
        ceiling = platform_risk_ceiling()
        operator = operator_risk_policy(policy) if is_option else {}
        if not is_option and declared is None and not ceiling:
            return None

        resolved = dict(plan.get("resolved_plan") or {})
        legs = [
            dict(leg)
            for leg in (resolved.get("legs") or [])
            if isinstance(leg, Mapping)
        ]
        if is_option and declared is None:
            return AdmissionVerdict(
                False,
                "STRATEGY_RISK_POLICY_MISSING",
                {
                    **detail,
                    "plan_id": str(plan.get("plan_id") or ""),
                    "message": (
                        "this strategy version declares no risk policy, so it may "
                        "not enter options exposure"
                    ),
                },
            )

        effective = effective_risk_policy(declared, operator, ceiling)
        detail["risk_policy_applies"] = True
        detail["risk_policy"] = {"declared": declared, "effective": effective}

        notional_limit = effective.get("notional_limit_inr")
        if notional_limit is not None:
            target_notional, peak_notional, peak_unknown = self._risk_notionals(
                exposure=exposure, notional=notional
            )
            enforced_notional = (
                target_notional
                if peak_notional is None
                else max(target_notional, peak_notional)
            )
            detail.update(
                {
                    "strategy_notional_limit_inr": notional_limit,
                    "strategy_target_notional_inr": target_notional,
                    "strategy_roll_peak_notional_inr": peak_notional,
                    "strategy_roll_peak_unavailable": peak_unknown,
                    "strategy_enforced_notional_inr": enforced_notional,
                }
            )

        if not is_option:
            if notional_limit is not None:
                if notional["reference_price_missing"] or notional["capital_basis_missing"]:
                    return AdmissionVerdict(
                        False,
                        "REFERENCE_PRICE_UNAVAILABLE",
                        {
                            **detail,
                            "missing_for": notional["reference_price_missing"],
                            "capital_basis_missing_for": notional["capital_basis_missing"],
                            "message": (
                                "a strategy notional limit is declared but the plan "
                                "carries no reference price (or no frozen capital basis)"
                            ),
                        },
                    )
                if detail["strategy_enforced_notional_inr"] > float(notional_limit):
                    return AdmissionVerdict(
                        False, "STRATEGY_NOTIONAL_LIMIT_EXCEEDED", detail
                    )
            return None

        family = classify_structure_family(legs)
        detail["structure_family"] = family
        allowed_families = effective.get("allowed_structure_families")
        if allowed_families is not None and family not in set(allowed_families):
            return AdmissionVerdict(
                False,
                "OPTION_STRUCTURE_FAMILY_NOT_ALLOWED",
                {**detail, "allowed_structure_families": list(allowed_families)},
            )

        allowed_expiry = effective.get("expiry_policy")
        frozen_expiry_policy = str(resolved.get("expiry_policy") or "")
        if allowed_expiry is not None and frozen_expiry_policy:
            detail["expiry_policy"] = frozen_expiry_policy
            if frozen_expiry_policy not in set(allowed_expiry):
                return AdmissionVerdict(
                    False,
                    "OPTION_STRUCTURE_FAMILY_NOT_ALLOWED",
                    {
                        **detail,
                        "allowed_expiry_policies": list(allowed_expiry),
                        "message": (
                            "the frozen expiry policy is not one this version permits"
                        ),
                    },
                )

        protection_policy = resolved.get("protection_policy")
        frozen_naked = bool(
            isinstance(protection_policy, Mapping)
            and protection_policy.get("naked")
        )
        unhedged = unhedged_target(plan)
        detail["naked_permitted"] = bool(effective.get("naked_permitted"))
        if unhedged is not None and not (
            bool(effective.get("naked_permitted")) and frozen_naked
        ):
            return AdmissionVerdict(
                False,
                "OPTION_NAKED_NOT_PERMITTED",
                {
                    **detail,
                    "frozen_naked": frozen_naked,
                    "message": (
                        "the target leaves a short leg uncovered; a version admits "
                        "that only when its policy permits naked exposure AND the "
                        "frozen structure declares it"
                    ),
                    **unhedged,
                },
            )

        worst_loss = worst_case_loss_inr(legs)
        frozen_max_loss = self._frozen_max_loss(resolved)
        loss_ceiling = _min_optional(effective.get("max_loss_inr"), frozen_max_loss)
        detail.update(
            {
                "worst_case_loss_inr": worst_loss,
                "frozen_max_loss_inr": frozen_max_loss,
                "effective_max_loss_inr": loss_ceiling,
            }
        )
        if worst_loss is None:
            # A permitted naked structure has no numeric bound to check, so it
            # must be stoppable instead: the declaration must require a stop and
            # the frozen structure must actually carry one.
            protection = dict(effective.get("protection") or {})
            if not (
                protection.get("stop_required")
                and frozen_protection_stop(protection_policy)
            ):
                return AdmissionVerdict(
                    False,
                    "OPTION_NAKED_NOT_PERMITTED",
                    {
                        **detail,
                        "reason": "unbounded_loss_requires_protection_stop",
                        "message": (
                            "an unbounded structure is only admitted when the policy "
                            "requires a protective stop and the frozen structure "
                            "declares one"
                        ),
                    },
                )
        elif loss_ceiling is not None and worst_loss > float(loss_ceiling):
            return AdmissionVerdict(
                False,
                "OPTION_MAX_LOSS_EXCEEDED",
                {**detail, "max_loss_inr": loss_ceiling},
            )

        if notional_limit is not None:
            if (
                notional["reference_price_missing"]
                or exposure["unvalued"]
                or peak_unknown
            ):
                return AdmissionVerdict(
                    False,
                    "REFERENCE_PRICE_UNAVAILABLE",
                    {
                        **detail,
                        "missing_for": notional["reference_price_missing"],
                        "unvalued": exposure["unvalued"],
                        "reason": (
                            "held_book_unpriceable" if peak_unknown else None
                        ),
                        "message": (
                            "a strategy notional limit is declared but the frozen "
                            "target or the held book carries no usable price; an "
                            "adjust's overlap cannot be bounded against the "
                            "post-plan book, so the limit refuses instead"
                        ),
                    },
                )
            if detail["strategy_enforced_notional_inr"] > float(notional_limit):
                return AdmissionVerdict(False, "STRATEGY_NOTIONAL_LIMIT_EXCEEDED", detail)

        margin_limit = effective.get("margin_limit_inr")
        if margin_limit is not None:
            required_margin = None
            if isinstance(margin_evidence, Mapping):
                required_margin = _as_float(margin_evidence.get("required_margin_inr"))
            if required_margin is None:
                # No margin source is invented here: without evidence the limit
                # is honestly reported as unchecked rather than read as zero.
                detail["margin_check"] = "unavailable"
            else:
                detail.update(
                    {
                        "margin_check": "enforced",
                        "margin_required_inr": required_margin,
                        "margin_limit_inr": float(margin_limit),
                    }
                )
                if required_margin > float(margin_limit):
                    return AdmissionVerdict(
                        False,
                        "MARGIN_INSUFFICIENT",
                        {**detail, "reason": "strategy_margin_limit"},
                    )
        return None

    @staticmethod
    def _risk_notionals(
        *, exposure: Mapping[str, Any], notional: Mapping[str, Any]
    ) -> Tuple[float, Optional[float], bool]:
        """The frozen target's notional, the roll's overlap PEAK, and whether that
        peak is UNKNOWN because a held coordinate the plan does not price cannot
        be valued.

        The peak is what the strategy actually holds during the plan: the current
        book plus the legs the target OPENS on coordinates the book does not
        already carry. It is deliberately not the post-plan book, which has
        already released the old generation - checking against that would let a
        roll's overlap window through unseen.

        The released generation carries no price of its own (the plan that closes
        it never names it), so the peak is reported as unknown rather than
        quietly valued at zero. The caller refuses a configured limit on unknown
        evidence; it never reads "unpriceable" as "nothing".
        """
        target_notional = float(notional["total_notional_inr"] or 0.0)
        held = exposure["current_exposure_inr"]
        opens_new = any(
            row.get("increases_exposure") and int(row.get("current_quantity") or 0) == 0
            for row in exposure["per_instrument"]
        )
        if held is None:
            return target_notional, None, opens_new
        opening = 0.0
        for row in exposure["per_instrument"]:
            if not row.get("increases_exposure"):
                continue
            if int(row.get("current_quantity") or 0) != 0:
                continue
            if row.get("notional_inr") is None:
                return target_notional, None, True
            opening += float(row["notional_inr"])
        return target_notional, float(held) + opening, False

    @staticmethod
    def _frozen_max_loss(resolved: Mapping[str, Any]) -> Optional[float]:
        """The strategy-supplied ``max_loss`` the frozen plan carries, if any."""
        block = resolved.get("max_loss")
        if not isinstance(block, Mapping):
            return None
        return _as_float(block.get("max_loss_inr"))

    # -- the verdict --------------------------------------------------------

    def evaluate(
        self,
        plan: Mapping[str, Any],
        *,
        execution_environment: str = "live",
        now: Optional[datetime] = None,
        margin_evidence: Optional[Mapping[str, Any]] = None,
        paper_funds: Optional[Mapping[str, Any]] = None,
        peak_capacity_inr: Optional[float] = None,
        realized_loss_inr: Optional[float] = None,
        catalog_state: Optional[Mapping[str, Any]] = None,
    ) -> AdmissionVerdict:
        """Evaluate every control in order; the first refusal wins (D-2).

        Evidence is passed in rather than fetched: the same plan and the same
        evidence must always produce the same verdict, and a caller that cannot
        obtain evidence passes ``None`` (which fails closed where it matters)
        rather than having admission reach for the network.
        """
        moment = now or _utcnow()
        environment = str(execution_environment or "live").lower()
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        is_live = environment == "live"

        policy = self.policy_for(strategy_id)
        if policy is None:
            if is_live:
                # A live policy row is mandatory: without a recorded allocation
                # there is nothing to enforce against, and an unenforced limit
                # that looks enforced is worse than a refusal.
                return AdmissionVerdict(
                    False,
                    "ADMISSION_POLICY_MISSING",
                    {"strategy_id": strategy_id, "execution_environment": environment},
                )
        elif is_live and policy.get("allocation_inr") is None:
            return AdmissionVerdict(
                False,
                "ADMISSION_POLICY_MISSING",
                {
                    "strategy_id": strategy_id,
                    "message": "A live strategy requires an allocation_inr in its admission policy",
                },
            )

        notional = self.plan_notional(plan)
        exposure = self.plan_exposure(plan, execution_environment=environment)
        # The enforced requirement is the INCREMENTAL funding the plan needs, not
        # the whole target book: re-applying an unchanged target costs nothing,
        # and a sell leg funds nothing until it actually fills.
        requirement = float(exposure["incremental_funding_inr"])
        capacity = self.capacity_held_inr(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=environment,
        )
        # Capacity held is the strategy's OWN unfilled commitments plus any
        # consumed reservation whose exposure is not yet visible in the published
        # book. A consumed reservation whose fill IS published is carried by the
        # attributed position instead, never charged twice.
        pending = float(capacity["held_inr"])
        # STAGED FINANCING (CNC portfolio lane only): a rebalance that both
        # REDUCES and INCREASES exposure places its reductions first and releases
        # each dependent increase only against a CONFIRMED reduction
        # (``execution.PaperPlanExecutor``), with the paper runtime's own
        # per-order cash check enforcing the actual money. Such a plan must
        # therefore not be refused merely because the account's cash is short of
        # the whole incremental requirement BEFORE the reductions execute - the
        # shortfall is funded by the plan's own confirmed releases, never by a
        # projected sale. Futures rolls (acquire-first) and option structures own
        # their own sequencing and are deliberately excluded, exactly as in the
        # executor.
        from backend.strategies.execution import CNC_REBALANCE_PLAN_KINDS

        resolved_plan = dict(plan.get("resolved_plan") or {})
        # The product must be the CNC cash segment on EVERY leg. An
        # ``intent_bundle`` also carries futures (NRML) and MIS shapes, and those
        # are margined differently and sequenced by their own domain rules; the
        # generic portfolio sell-before-buy staging must never adopt them.
        leg_products = {
            str(leg.get("product") or "").upper()
            for leg in (resolved_plan.get("legs") or [])
        }
        cnc_lane = (
            str(plan.get("plan_kind") or "") in CNC_REBALANCE_PLAN_KINDS
            and not resolved_plan.get("roll")
            and bool(leg_products)
            and leg_products == {"CNC"}
        )
        order_quantities = [
            int(row.get("order_quantity") or 0)
            for row in exposure["per_instrument"]
            if int(row.get("order_quantity") or 0) != 0
        ]
        staged_plan = bool(
            cnc_lane
            and any(quantity < 0 for quantity in order_quantities)
            and any(quantity > 0 for quantity in order_quantities)
        )
        detail: Dict[str, Any] = {
            "execution_environment": environment,
            "plan_requirement_inr": requirement,
            "incremental_funding_inr": requirement,
            "staged_financing_lane": staged_plan,
            # The part of the requirement a staged plan funds from its OWN
            # reductions, and therefore must NOT be reserved against free cash at
            # claim time. It is authorized later, per increase, against confirmed
            # account money.
            "staged_increase_inr": (float(requirement) if staged_plan else None),
            "current_exposure_inr": exposure["current_exposure_inr"],
            "desired_exposure_inr": exposure["desired_exposure_inr"],
            "pending_commitments_inr": pending,
            "unfilled_commitments_inr": capacity["unfilled_commitments_inr"],
            "consumed_unpublished_inr": capacity["consumed_unpublished_inr"],
            "consumed_published_inr": capacity["consumed_published_inr"],
            # ISO string, not a ``datetime``: this detail is persisted verbatim
            # into the execution request's JSON columns, and a raw datetime
            # cannot be serialized by the durable write.
            "projection_published_at": (
                None
                if capacity["projection_published_at"] is None
                else capacity["projection_published_at"].isoformat()
            ),
            "consumed_history_inr": (
                capacity["consumed_published_inr"] + capacity["consumed_unpublished_inr"]
            ),
            "post_instruments": exposure["post_instruments"],
            "per_instrument": exposure["per_instrument"],
            "unresolved_projection_facts": exposure["unresolved_projection_facts"],
            "per_leg": notional["per_leg"],
        }

        if policy is not None:
            configured_notional_axes = (
                policy.get("allocation_inr") is not None
                or policy.get("per_instrument_notional_inr") is not None
                or policy.get("gross_notional_inr") is not None
            )
            if configured_notional_axes and (
                notional["reference_price_missing"] or notional["capital_basis_missing"]
            ):
                # A configured notional limit with no price - or a weighted leg with
                # no frozen capital basis - is unknown evidence, and unknown evidence
                # never admits.
                return AdmissionVerdict(
                    False,
                    "REFERENCE_PRICE_UNAVAILABLE",
                    {
                        **detail,
                        "missing_for": notional["reference_price_missing"],
                        "capital_basis_missing_for": notional["capital_basis_missing"],
                        "message": (
                            "A notional limit is configured but the plan carries no "
                            "reference price (or no frozen capital basis for a weighted leg)"
                        ),
                    },
                )
            if configured_notional_axes and exposure["unvalued"]:
                # An unchanged HELD coordinate the plan does not price is unknown
                # evidence about this strategy's own book: refusing by name is the
                # honest behaviour, and valuing it as zero would under-state the
                # projection.
                return AdmissionVerdict(
                    False,
                    "POSITION_VALUATION_UNAVAILABLE",
                    {
                        **detail,
                        "unvalued": exposure["unvalued"],
                        "message": (
                            "A notional limit is configured but a coordinate in this "
                            "strategy's post-plan book carries no valid price or size"
                        ),
                    },
                )

        if policy is not None and policy.get("allocation_inr") is not None:
            current = float(exposure["current_exposure_inr"] or 0.0)
            desired = float(exposure["desired_exposure_inr"] or 0.0)
            # The budget test is on the DESIRED POST-PLAN book: what the strategy
            # will hold once the plan is done. Testing the pre-plan book plus the
            # whole plan (the old shape) charged a full-target rebalance twice and
            # refused a fully-allocated sell-A/buy-B even when the post-plan book
            # fits the budget.
            projected = desired + pending
            from backend.strategies.financing import staged_funding

            staging = staged_funding(
                allocation_inr=policy["allocation_inr"],
                current_exposure_inr=current,
                pending_commitments_inr=pending,
                incremental_funding_inr=requirement,
            )
            detail.update(
                {
                    "allocation_inr": policy["allocation_inr"],
                    "attributed_consumption_inr": current,
                    "active_reserved_inr": pending,
                    "projected_inr": projected,
                    "post_plan_projected_inr": projected,
                    "pre_plan_projected_inr": current + pending + requirement,
                    **staging,
                }
            )
            if projected > float(policy["allocation_inr"]):
                return AdmissionVerdict(False, "ALLOCATION_EXCEEDED", detail)

        if policy is not None and policy.get("per_instrument_notional_inr") is not None:
            limit = float(policy["per_instrument_notional_inr"])
            # Includes unchanged held coordinates: the limit is on the strategy's
            # own post-plan book, not merely on the legs this plan touches.
            worst = max(
                (float(row["notional_inr"]) for row in exposure["per_instrument"] if row["notional_inr"] is not None),
                default=0.0,
            )
            detail.update({"per_instrument_limit_inr": limit, "worst_leg_notional_inr": worst})
            if worst > limit:
                return AdmissionVerdict(False, "INSTRUMENT_NOTIONAL_EXCEEDED", detail)

        if policy is not None and policy.get("gross_notional_inr") is not None:
            limit = float(policy["gross_notional_inr"])
            gross = float(exposure["desired_exposure_inr"] or 0.0) + pending
            detail.update({"gross_limit_inr": limit, "projected_gross_inr": gross})
            if gross > limit:
                return AdmissionVerdict(False, "GROSS_NOTIONAL_EXCEEDED", detail)

        if policy is not None and policy.get("max_open_instruments") is not None:
            limit = int(policy["max_open_instruments"])
            # The post-plan instrument set already contains the unchanged held
            # coordinates, so this is not "existing + new" (which double-counts a
            # name the plan keeps).
            projected_open = int(exposure["post_instruments"])
            detail.update(
                {"max_open_instruments": limit, "projected_open_instruments": projected_open}
            )
            if projected_open > limit:
                return AdmissionVerdict(False, "MAX_OPEN_INSTRUMENTS_EXCEEDED", detail)

        if policy is not None and policy.get("admissions_per_window") is not None:
            window = int(policy.get("admission_window_seconds") or 3600)
            limit = int(policy["admissions_per_window"])
            recent = self.recent_admission_count(
                strategy_id=strategy_id, window_seconds=window, now=moment
            )
            detail.update(
                {"admissions_per_window": limit, "window_seconds": window, "recent_admissions": recent}
            )
            if recent >= limit:
                return AdmissionVerdict(False, "ORDER_RATE_EXCEEDED", detail)

        if policy is not None and policy.get("daily_loss_budget_inr") is not None:
            if is_live and realized_loss_inr is None:
                # V1 has no attributed live realized-loss source. Treating the
                # budget as satisfied would silently disable a configured limit,
                # so admission refuses until that evidence exists (Project 5+).
                return AdmissionVerdict(
                    False,
                    "DAILY_LOSS_BUDGET_UNAVAILABLE",
                    {
                        **detail,
                        "daily_loss_budget_inr": policy["daily_loss_budget_inr"],
                        "message": (
                            "A live daily-loss budget is configured but no attributed "
                            "realized-loss evidence exists yet; refusing rather than "
                            "silently not enforcing it."
                        ),
                    },
                )
            if realized_loss_inr is not None and abs(float(realized_loss_inr)) > float(
                policy["daily_loss_budget_inr"]
            ):
                return AdmissionVerdict(
                    False,
                    "DAILY_LOSS_BUDGET_UNAVAILABLE",
                    {
                        **detail,
                        "daily_loss_budget_inr": policy["daily_loss_budget_inr"],
                        "realized_loss_inr": float(realized_loss_inr),
                    },
                )

        # The strategy's OWN declared risk policy (B2.5), resolved from the
        # immutable version the plan was produced under and enforced here so
        # request creation, approval and paper/live admission all see one answer.
        risk_refusal = self._risk_policy_gate(
            plan,
            policy=policy,
            exposure=exposure,
            notional=notional,
            detail=detail,
            margin_evidence=margin_evidence,
        )
        if risk_refusal is not None:
            return risk_refusal

        state = dict(catalog_state or {})
        if not state:
            state = self._catalog_state(plan)
        detail["catalog_state"] = state.get("state")
        if str(state.get("state") or "valid") != "valid":
            # Reuses the Phase 3 derived invalidation: only a change RELEVANT to a
            # pinned instrument refuses, so an unrelated catalog update does not.
            return AdmissionVerdict(False, "CATALOG_INVALID", detail)

        product_refusal = self._product_refusal(plan, environment=environment)
        if product_refusal is not None:
            return AdmissionVerdict(False, "SESSION_PRODUCT_INVALID", {**detail, **product_refusal})

        # Futures precheck the PEAK, not a leg: a roll holds the old contract while
        # the replacement is acquired, and discovering that shortfall at the broker
        # is how a roll ends up half executed.
        futures_legs = [
            leg
            for leg in (plan.get("resolved_plan") or {}).get("legs") or []
            if str(leg.get("instrument_type") or "").upper() == "FUT"
        ]
        if futures_legs:
            from backend.strategies.futures_margin import (
                futures_peak_refusal,
                peak_margin_evidence,
            )

            old_legs = list((plan.get("resolved_plan") or {}).get("old_legs") or [])
            peak = peak_margin_evidence(
                new_legs=futures_legs, old_legs=old_legs, margin_engine=self._margin_engine
            )
            detail["peak_margin"] = peak
            # Compared against a MARGIN capacity the caller supplies, never against
            # the notional allocation: those measure different things, and conflating
            # them would refuse a plan the allocation permitted.
            refusal = futures_peak_refusal(peak=peak, available_inr=peak_capacity_inr)
            if refusal is not None:
                return AdmissionVerdict(False, "MARGIN_INSUFFICIENT", {**detail, **refusal})

        if is_live:
            margin = dict(margin_evidence or {})
            if not margin or margin.get("usable") is None:
                return AdmissionVerdict(
                    False,
                    "MARGIN_UNAVAILABLE",
                    {**detail, "message": "No authoritative margin evidence is available"},
                )
            as_of = margin.get("as_of")
            if as_of is None:
                return AdmissionVerdict(
                    False,
                    "MARGIN_QUOTE_STALE",
                    {**detail, "message": "Margin evidence carries no as_of"},
                )
            age = (moment - _coerce_datetime(as_of)).total_seconds()
            detail["margin_age_seconds"] = age
            if age > margin_max_age_seconds():
                return AdmissionVerdict(
                    False,
                    "MARGIN_QUOTE_STALE",
                    {**detail, "margin_age_seconds": age, "max_age_seconds": margin_max_age_seconds()},
                )
            available = _as_float(margin.get("usable"))
            detail["margin_available_inr"] = available
            # The ACCOUNT's own constraint, distinct from the strategy budget: the
            # pipeline hands this to the reservation ledger so two strategies can
            # never reserve the same actual account funds.
            detail["account_available_inr"] = available
            if available is not None and available < requirement:
                if staged_plan:
                    # LIVE staged financing is NOT implemented: the live adapter
                    # has no confirmed-release authorization path, so a live
                    # rebalance whose cash is short refuses by name rather than
                    # trading on money the platform has not proved.
                    return AdmissionVerdict(
                        False,
                        "STAGED_LIVE_FINANCING_UNSUPPORTED",
                        {
                            **detail,
                            "required_inr": requirement,
                            "message": (
                                "A live rebalance whose increases are not covered by "
                                "available margin is refused: staged sell-before-buy "
                                "financing is only implemented for paper"
                            ),
                        },
                    )
                # Authoritative insufficiency is a refusal; the named reason is the
                # margin axis, since the broker said the money is not there.
                return AdmissionVerdict(
                    False,
                    "MARGIN_UNAVAILABLE",
                    {**detail, "required_inr": requirement},
                )
        else:
            funds = dict(paper_funds or {})
            if funds:
                available = _as_float(funds.get("available_funds"))
                detail["paper_available_funds"] = available
                detail["account_available_inr"] = available
                if available is not None and available < requirement:
                    if staged_plan:
                        detail["staged_financing_shortfall_inr"] = float(
                            requirement - available
                        )
                        detail["staged_financing_message"] = (
                            "Paper cash is short of the whole incremental requirement; "
                            "this plan's own confirmed reductions must fund the rest and "
                            "each dependent buy is refused unless they do."
                        )
                        return AdmissionVerdict(True, None, detail)
                    return AdmissionVerdict(
                        False, "MARGIN_UNAVAILABLE", {**detail, "required_inr": requirement}
                    )

        return AdmissionVerdict(True, None, detail)

    # -- helpers ------------------------------------------------------------

    def _catalog_state(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            from backend.strategies.proposals import plan_invalidation_state

            return dict(
                plan_invalidation_state(plan, session_factory=self.session_factory) or {}
            )
        except Exception:  # noqa: BLE001 - an unreadable catalog is not "valid"
            return {"state": "invalidated", "reason": "CATALOG_STATE_UNAVAILABLE"}

    def _product_refusal(
        self, plan: Mapping[str, Any], *, environment: str
    ) -> Optional[Dict[str, Any]]:
        """Static product validity (D16 vocabulary), plus the recorded snapshot.

        Session-open checking is deliberately not invented here: the executed
        session state belongs to the execution phases, and fabricating a market
        clock in admission would make the verdict depend on wall time rather than
        evidence. The product set is validated now and the snapshot is recorded on
        the approval for the phases that can check it authoritatively.
        """
        unknown: List[str] = []
        products: List[str] = []
        for leg in (plan.get("resolved_plan") or {}).get("legs") or []:
            product = str(leg.get("product") or "").upper()
            if not product:
                continue
            products.append(product)
            if product not in VALID_PRODUCTS:
                unknown.append(product)
        if unknown:
            return {
                "invalid_products": sorted(set(unknown)),
                "valid_products": sorted(VALID_PRODUCTS),
            }
        return None

    def _open_instrument_count(self, *, strategy_id: str, account_id: str) -> int:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyPositionProjection.instrument_token).where(
                    StrategyPositionProjection.account_id == str(account_id),
                    StrategyPositionProjection.strategy_id == str(strategy_id),
                    StrategyPositionProjection.execution_environment == "live",
                    StrategyPositionProjection.net_quantity != 0,
                )
            ).scalars().all()
        return len(set(rows))


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text_value = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text_value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _utcnow()


def session_product_snapshot(products: Sequence[str]) -> Dict[str, Any]:
    """The session/product pin an approval records (validated static set)."""
    return {
        "products": sorted({str(product).upper() for product in products}),
        "valid_products": sorted(VALID_PRODUCTS),
    }
