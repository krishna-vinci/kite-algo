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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.strategies.attribution_models import (
    AccountReconciliationVersion,
    StrategyAdmissionPolicy,
    StrategyPositionProjection,
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
    "INSTRUMENT_NOTIONAL_EXCEEDED",
    "GROSS_NOTIONAL_EXCEEDED",
    "MAX_OPEN_INSTRUMENTS_EXCEEDED",
    "ORDER_RATE_EXCEEDED",
    "DAILY_LOSS_BUDGET_UNAVAILABLE",
    "CATALOG_INVALID",
    "SESSION_PRODUCT_INVALID",
    "MARGIN_UNAVAILABLE",
    "MARGIN_QUOTE_STALE",
    "MARGIN_INSUFFICIENT",
)

#: Statuses that hold capacity. ``consumed`` is included deliberately: capital
#: backing an open position is never released because its evaluation expired.
CAPACITY_HOLDING_STATUSES = ("active", "renewed", "consumed", "action_required")

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

        Approximate notional = |quantity| x reference price, with the price coming
        from the plan itself — no new market-data dependency. When a leg carries no
        price the requirement is **unknown**, not zero, and the caller refuses
        rather than under-enforcing a configured limit.
        """
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        per_leg: List[Dict[str, Any]] = []
        total = 0.0
        missing: List[str] = []
        for leg in legs:
            symbol = str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "")
            quantity = abs(
                float(leg.get("signed_quantity", leg.get("target_weight", 0.0)) or 0.0)
            )
            price = _as_float(leg.get("reference_price"))
            if price is None:
                if quantity:
                    missing.append(symbol)
                reference = 0.0
            else:
                reference = abs(price)
            notional = quantity * reference
            total += notional
            per_leg.append({"tradingsymbol": symbol, "quantity": quantity, "notional_inr": notional})
        return {
            "total_notional_inr": total,
            "per_leg": per_leg,
            "reference_price_missing": sorted(set(missing)),
            "instrument_count": len({row["tradingsymbol"] for row in per_leg if row["tradingsymbol"]}),
        }

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
        requirement = float(notional["total_notional_inr"])
        reference_prices = [leg.get("reference_price") for leg in (plan.get("resolved_plan") or {}).get("legs") or []]
        reference_price = next((_as_float(price) for price in reference_prices if _as_float(price)), None)
        detail: Dict[str, Any] = {
            "execution_environment": environment,
            "plan_requirement_inr": requirement,
            "per_leg": notional["per_leg"],
        }

        if policy is not None:
            configured_notional_axes = (
                policy.get("allocation_inr") is not None
                or policy.get("per_instrument_notional_inr") is not None
                or policy.get("gross_notional_inr") is not None
            )
            if configured_notional_axes and notional["reference_price_missing"]:
                # A configured notional limit with no price to compute against is
                # unknown evidence, and unknown evidence never admits.
                return AdmissionVerdict(
                    False,
                    "REFERENCE_PRICE_UNAVAILABLE",
                    {
                        **detail,
                        "missing_for": notional["reference_price_missing"],
                        "message": "A notional limit is configured but the plan carries no reference price",
                    },
                )

        if policy is not None and policy.get("allocation_inr") is not None:
            consumed = self.attributed_consumption(
                strategy_id=strategy_id, account_id=account_id, reference_price=reference_price
            )
            reserved = self.reserved_notional(account_id=account_id, strategy_id=strategy_id)
            projected = consumed + reserved + requirement
            detail.update(
                {
                    "allocation_inr": policy["allocation_inr"],
                    "attributed_consumption_inr": consumed,
                    "active_reserved_inr": reserved,
                    "projected_inr": projected,
                }
            )
            if projected > float(policy["allocation_inr"]):
                return AdmissionVerdict(False, "ALLOCATION_EXCEEDED", detail)

        if policy is not None and policy.get("per_instrument_notional_inr") is not None:
            limit = float(policy["per_instrument_notional_inr"])
            worst = max((row["notional_inr"] for row in notional["per_leg"]), default=0.0)
            detail.update({"per_instrument_limit_inr": limit, "worst_leg_notional_inr": worst})
            if worst > limit:
                return AdmissionVerdict(False, "INSTRUMENT_NOTIONAL_EXCEEDED", detail)

        if policy is not None and policy.get("gross_notional_inr") is not None:
            limit = float(policy["gross_notional_inr"])
            consumed = self.attributed_consumption(
                strategy_id=strategy_id, account_id=account_id, reference_price=reference_price
            )
            gross = consumed + requirement
            detail.update({"gross_limit_inr": limit, "projected_gross_inr": gross})
            if gross > limit:
                return AdmissionVerdict(False, "GROSS_NOTIONAL_EXCEEDED", detail)

        if policy is not None and policy.get("max_open_instruments") is not None:
            limit = int(policy["max_open_instruments"])
            open_instruments = self._open_instrument_count(
                strategy_id=strategy_id, account_id=account_id
            )
            projected_open = open_instruments + int(notional["instrument_count"])
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
            if available is not None and available < requirement:
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
                if available is not None and available < requirement:
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
