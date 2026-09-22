"""Portfolio compilation: target weights → ordered quantity deltas (D-1).

A ``target_weights`` plan names fractions. Executing it needs three things the
plan alone cannot supply: the strategy's capital, what it already holds, and how
much cash the buys would need at once. This module computes all three and refuses
**by name**, before any order exists, when the answer is "it does not fit".

Two rules make the arithmetic honest:

* **Sells come first.** A rebalance that sells to fund its buys is ordered
  sells-then-buys, but ordering is not funding: the gross cash reservation covers
  every buy upfront, because proceeds that have not filled are not cash. Counting
  them early is how a rebalance ends up half-executed with a naked short of cash.

* **The scope is the instruction.** A member omitted from the payload is target
  ZERO — the strategy sells it — while an instrument outside the pinned scope is
  untouched, because its absence carries no instruction at all. That asymmetry is
  the full-snapshot contract Phase 3 pinned, applied at execution time.

This runs before the executor, not instead of it: the executor still derives its
own final delta from the book at submit time (the book can move between plan and
submission), and this compiler's job is to make the plan *executable and
affordable* and to refuse early when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select, text

from backend.strategies.attribution_models import (
    StrategyAdmissionPolicy,
    StrategyPositionProjection,
)

#: Refusals this compiler can produce, in evaluation order.
PORTFOLIO_REFUSALS = (
    "ADMISSION_POLICY_MISSING",
    "REFERENCE_PRICE_UNAVAILABLE",
    "INSTRUMENT_NOTIONAL_EXCEEDED",
    "GROSS_NOTIONAL_EXCEEDED",
    "MAX_OPEN_INSTRUMENTS_EXCEEDED",
    "INSUFFICIENT_PORTFOLIO_CASH",
)

DEFAULT_LOT_SIZE = 1


@dataclass(frozen=True)
class PortfolioCompilation:
    """The compiled legs plus why they were refused, if they were."""

    legs: List[Dict[str, Any]] = field(default_factory=list)
    target_notional_inr: float = 0.0
    buy_notional_inr: float = 0.0
    sell_notional_inr: float = 0.0
    gross_cash_reservation_inr: float = 0.0
    allocation_capital_inr: Optional[float] = None
    available_cash_inr: Optional[float] = None
    refusal_reason: Optional[str] = None
    refusal_detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "legs": [dict(leg) for leg in self.legs],
            "target_notional_inr": self.target_notional_inr,
            "buy_notional_inr": self.buy_notional_inr,
            "sell_notional_inr": self.sell_notional_inr,
            "gross_cash_reservation_inr": self.gross_cash_reservation_inr,
            "allocation_capital_inr": self.allocation_capital_inr,
            "available_cash_inr": self.available_cash_inr,
            "refusal_reason": self.refusal_reason,
            "refusal_detail": dict(self.refusal_detail),
        }


class WeightsPortfolioCompiler:
    """Compiles a weights plan against the strategy's own attributed book."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        if session_factory is None:
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    # -- reads --------------------------------------------------------------

    def policy_for(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyAdmissionPolicy).where(
                    StrategyAdmissionPolicy.strategy_id == str(strategy_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "allocation_inr": None if row.allocation_inr is None else float(row.allocation_inr),
                "per_instrument_notional_inr": (
                    None
                    if row.per_instrument_notional_inr is None
                    else float(row.per_instrument_notional_inr)
                ),
                "gross_notional_inr": (
                    None if row.gross_notional_inr is None else float(row.gross_notional_inr)
                ),
                "max_open_instruments": row.max_open_instruments,
            }

    def current_book(
        self, *, account_id: str, strategy_id: str, execution_environment: str
    ) -> Dict[str, int]:
        """Held quantity per canonical instrument, from the G1 projection.

        Canonical rows only: an unresolved raw row has no canonical instrument to
        delta against, and inventing one would attribute exposure the platform has
        explicitly declined to attribute.
        """
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    StrategyPositionProjection.canonical_instrument_id,
                    StrategyPositionProjection.product,
                    StrategyPositionProjection.net_quantity,
                ).where(
                    StrategyPositionProjection.account_id == str(account_id),
                    StrategyPositionProjection.strategy_id == str(strategy_id),
                    StrategyPositionProjection.execution_environment == str(execution_environment),
                    StrategyPositionProjection.identity_kind == "canonical",
                )
            ).all()
        book: Dict[str, int] = {}
        for instrument_id, product, quantity in rows:
            key = self._key(str(instrument_id or ""), str(product or ""))
            book[key] = book.get(key, 0) + int(quantity or 0)
        return book

    def lot_size(self, instrument_id: str) -> int:
        """The pinned catalog's lot for the instrument; ``1`` when it provides none."""
        if not instrument_id:
            return DEFAULT_LOT_SIZE
        with self.session_factory() as session:
            row = session.execute(
                text(
                    "SELECT lot_size FROM public.instrument_catalog_records "
                    "WHERE instrument_id = :instrument_id"
                ),
                {"instrument_id": str(instrument_id)},
            ).fetchone()
        try:
            lot = int(row[0]) if row is not None and row[0] is not None else DEFAULT_LOT_SIZE
        except (TypeError, ValueError):
            return DEFAULT_LOT_SIZE
        return lot if lot > 0 else DEFAULT_LOT_SIZE

    # -- compile ------------------------------------------------------------

    def compile(
        self,
        plan: Mapping[str, Any],
        *,
        execution_environment: str = "paper",
        available_cash_inr: Optional[float] = None,
        now: Any = None,
    ) -> PortfolioCompilation:
        """Turn the plan's weights into ordered, affordable quantity deltas."""
        _ = now
        strategy_id = str(plan.get("strategy_id") or "")
        account_id = str(plan.get("account_id") or "")
        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])

        policy = self.policy_for(strategy_id)
        if policy is None or policy.get("allocation_inr") is None:
            # Without a recorded allocation there is no capital to size against,
            # and guessing one would size positions nobody authorised.
            return PortfolioCompilation(
                refusal_reason="ADMISSION_POLICY_MISSING",
                refusal_detail={"strategy_id": strategy_id},
            )
        capital = float(policy["allocation_inr"])

        missing_prices = [
            str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "")
            for leg in legs
            if self._price(leg) is None
        ]
        if missing_prices:
            return PortfolioCompilation(
                allocation_capital_inr=capital,
                refusal_reason="REFERENCE_PRICE_UNAVAILABLE",
                refusal_detail={"missing_for": sorted(set(missing_prices))},
            )

        book = self.current_book(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=str(execution_environment),
        )

        compiled: List[Dict[str, Any]] = []
        target_notional = 0.0
        for leg in legs:
            price = self._price(leg) or 0.0
            weight = float(leg.get("target_weight") or 0.0)
            instrument_id = str(leg.get("instrument_id") or "")
            product = str(leg.get("product") or "")
            key = self._key(instrument_id, product)

            lot = self.lot_size(instrument_id)
            target_quantity = self._target_quantity(
                weight=weight, capital=capital, price=price, lot=lot
            )
            current_quantity = int(book.get(key, 0))
            delta = target_quantity - current_quantity
            side = "BUY" if delta > 0 else ("SELL" if delta < 0 else "FLAT")
            notional = abs(target_quantity) * price
            target_notional += notional

            compiled.append(
                {
                    "instrument_id": instrument_id,
                    "exchange": str(leg.get("exchange") or ""),
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "broker_exchange": str(leg.get("broker_exchange") or ""),
                    "broker_symbol": str(leg.get("broker_symbol") or ""),
                    "broker_token": leg.get("broker_token"),
                    "product": product,
                    "target_weight": weight,
                    "reference_price": price,
                    "lot_size": lot,
                    "target_quantity": target_quantity,
                    "current_quantity": current_quantity,
                    "delta": delta,
                    "signed_quantity": delta,
                    "side": side,
                    "notional_inr": notional,
                    "delta_notional_inr": abs(delta) * price,
                    # The executor reads ``signed_quantity`` as the target; for a
                    # weights plan the target IS the computed quantity.
                    "_portfolio_target": True,
                }
            )

        actionable = [row for row in compiled if row["delta"] != 0]

        # -- the policy axes, checked on the compiled bundle -----------------
        per_instrument_limit = policy.get("per_instrument_notional_inr")
        if per_instrument_limit is not None:
            worst = max((row["notional_inr"] for row in compiled), default=0.0)
            if worst > float(per_instrument_limit):
                return PortfolioCompilation(
                    legs=compiled,
                    target_notional_inr=target_notional,
                    allocation_capital_inr=capital,
                    available_cash_inr=available_cash_inr,
                    refusal_reason="INSTRUMENT_NOTIONAL_EXCEEDED",
                    refusal_detail={
                        "per_instrument_notional_inr": float(per_instrument_limit),
                        "worst_leg_notional_inr": worst,
                    },
                )

        gross_limit = policy.get("gross_notional_inr")
        if gross_limit is not None and target_notional > float(gross_limit):
            return PortfolioCompilation(
                legs=compiled,
                target_notional_inr=target_notional,
                allocation_capital_inr=capital,
                available_cash_inr=available_cash_inr,
                refusal_reason="GROSS_NOTIONAL_EXCEEDED",
                refusal_detail={
                    "gross_notional_inr": float(gross_limit),
                    "target_notional_inr": target_notional,
                },
            )

        open_limit = policy.get("max_open_instruments")
        if open_limit is not None:
            open_instruments = len(
                {
                    row["instrument_id"]
                    for row in compiled
                    if row["target_quantity"] != 0 or row["current_quantity"] != 0
                }
            )
            if open_instruments > int(open_limit):
                return PortfolioCompilation(
                    legs=compiled,
                    target_notional_inr=target_notional,
                    allocation_capital_inr=capital,
                    available_cash_inr=available_cash_inr,
                    refusal_reason="MAX_OPEN_INSTRUMENTS_EXCEEDED",
                    refusal_detail={
                        "max_open_instruments": int(open_limit),
                        "projected_open_instruments": open_instruments,
                    },
                )

        # -- ordering and the gross cash reservation -------------------------
        sells = [row for row in actionable if row["delta"] < 0]
        buys = [row for row in actionable if row["delta"] > 0]
        ordered = sells + buys

        buy_notional = sum(row["delta_notional_inr"] for row in buys)
        sell_notional = sum(row["delta_notional_inr"] for row in sells)
        # EVERY buy is funded upfront: the sells' proceeds are not counted until
        # they have actually filled.
        gross_reservation = buy_notional

        spending_limit = (
            float(available_cash_inr) if available_cash_inr is not None else capital
        )
        if gross_reservation > spending_limit:
            return PortfolioCompilation(
                legs=ordered,
                target_notional_inr=target_notional,
                buy_notional_inr=buy_notional,
                sell_notional_inr=sell_notional,
                gross_cash_reservation_inr=gross_reservation,
                allocation_capital_inr=capital,
                available_cash_inr=available_cash_inr,
                refusal_reason=(
                    "INSUFFICIENT_PORTFOLIO_CASH"
                    if available_cash_inr is not None
                    else "GROSS_NOTIONAL_EXCEEDED"
                ),
                refusal_detail={
                    "required_inr": gross_reservation,
                    "available_inr": spending_limit,
                    "message": (
                        "The buys must be funded upfront; sell proceeds do not count "
                        "before they fill."
                    ),
                },
            )

        return PortfolioCompilation(
            legs=ordered,
            target_notional_inr=target_notional,
            buy_notional_inr=buy_notional,
            sell_notional_inr=sell_notional,
            gross_cash_reservation_inr=gross_reservation,
            allocation_capital_inr=capital,
            available_cash_inr=available_cash_inr,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _key(instrument_id: str, product: str) -> str:
        return f"{instrument_id}|{product}"

    @staticmethod
    def _price(leg: Mapping[str, Any]) -> Optional[float]:
        raw = leg.get("reference_price")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _target_quantity(*, weight: float, capital: float, price: float, lot: int) -> int:
        """``round_to_lot(weight x capital / price)``.

        A weight that resolves to *some* exposure must never floor to zero: the
        caller asked for a position, and silently dropping it would leave the
        book quietly short of its target. So a non-zero weight floors up to one
        lot rather than down to nothing.
        """
        if weight == 0.0 or price <= 0:
            return 0
        raw = weight * capital / price
        lots = int(abs(raw) // lot)
        if lots == 0 and abs(raw) > 0:
            lots = 1
        quantity = lots * lot
        return int(quantity) if raw > 0 else -int(quantity)
