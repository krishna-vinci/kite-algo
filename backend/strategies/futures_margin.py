"""Futures margin: the PEAK is prechecked, never discovered (D-3).

An equity position has one margin. A futures roll has two, briefly: while the
replacement is being acquired the strategy holds the old contract *and* the new
one, and the peak is that concurrent pair — not either leg alone. Discovering the
shortfall at the broker is exactly the failure R3 §13's justification boundary
describes, where exiting the wrong leg of a hedged position triggers a margin
increase nobody planned for.

So the peak is computed from evidence before any leg is submitted, with the paper
margin engine providing it while live broker quotes remain future wiring. The
refusal is named and carries the arithmetic: a margin refusal without the numbers
is indistinguishable from a bug.

A preview is not a reservation: this computes and refuses, and holds nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

#: The instrument type this module governs. Everything else keeps its own path.
FUTURES_INSTRUMENT_TYPE = "FUT"

#: The named refusal when the peak does not fit.
MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"


def _as_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - an unreadable price is a zero contribution
        return Decimal("0")


def _leg_side(leg: Mapping[str, Any]) -> str:
    explicit = str(leg.get("side") or "").upper()
    if explicit in ("BUY", "SELL"):
        return explicit
    try:
        signed = int(leg.get("signed_quantity") or 0)
    except (TypeError, ValueError):
        signed = 0
    return "BUY" if signed >= 0 else "SELL"


def futures_leg_margin(leg: Mapping[str, Any], *, margin_engine: Any = None) -> Decimal:
    """The margin one futures leg requires, from the paper engine."""
    quantity = abs(int(leg.get("quantity", leg.get("signed_quantity", 0)) or 0))
    price = _as_decimal(leg.get("reference_price"))
    if quantity == 0 or price <= 0:
        return Decimal("0")
    engine = margin_engine
    if engine is None:
        from backend.paper_runtime.margin_engine import PaperMarginEngine

        engine = PaperMarginEngine()
    return Decimal(
        engine.required_margin(
            side=_leg_side(leg),
            product=str(leg.get("product") or "NRML"),
            quantity=quantity,
            reference_price=price,
            instrument_type=str(leg.get("instrument_type") or FUTURES_INSTRUMENT_TYPE),
        )
        or 0
    )


def peak_margin_evidence(
    *,
    new_legs: List[Mapping[str, Any]],
    old_legs: Optional[List[Mapping[str, Any]]] = None,
    margin_engine: Any = None,
) -> Dict[str, Any]:
    """The peak the strategy must be able to carry, and how it was computed.

    ``old_legs`` is what makes a roll different from an entry: while the replacement
    is acquired the old contract is still held, so the peak is the sum of both sides
    rather than the larger of them.
    """
    new_required = sum(
        (futures_leg_margin(leg, margin_engine=margin_engine) for leg in new_legs),
        Decimal("0"),
    )
    old_required = sum(
        (
            futures_leg_margin(leg, margin_engine=margin_engine)
            for leg in (old_legs or [])
        ),
        Decimal("0"),
    )
    return {
        "new_legs_margin_inr": float(new_required),
        "old_legs_margin_inr": float(old_required),
        "peak_margin_inr": float(new_required + old_required),
        "concurrent": bool(old_legs),
        "leg_count": len(new_legs) + len(old_legs or []),
    }


def futures_peak_refusal(
    *,
    peak: Mapping[str, Any],
    available_inr: Optional[float],
) -> Optional[Dict[str, Any]]:
    """``None`` when the peak fits, else the named refusal with its arithmetic."""
    if available_inr is None:
        return None
    required = float(peak.get("peak_margin_inr") or 0.0)
    if required <= float(available_inr):
        return None
    return {
        "rejection_reason": MARGIN_INSUFFICIENT,
        "required_peak_margin_inr": required,
        "available_inr": float(available_inr),
        "new_legs_margin_inr": float(peak.get("new_legs_margin_inr") or 0.0),
        "old_legs_margin_inr": float(peak.get("old_legs_margin_inr") or 0.0),
        "concurrent": bool(peak.get("concurrent")),
        "message": (
            "The peak margin — the old contract held while the replacement is acquired "
            "— does not fit. Prechecking it is the point: discovering it at the broker "
            "is how a roll ends up half executed."
        ),
    }
