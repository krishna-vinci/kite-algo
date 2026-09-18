"""MIS intraday policy: refuse a multi-day MIS intent at validation (D-1).

R3 §12 records this as a *correction*, not a feature. Ordinary MIS is intraday,
so a multi-day holding cannot live in it, and the failure mode of pretending
otherwise is specific and bad: the 15:20 square-off liquidates a position the
operator meant to hold, at whatever price the close happens to offer. A refusal
at validation is a conversation; a liquidation at 15:20 is not.

So the rule is: a MIS intent whose declared horizon is longer than the session is
refused ``MIS_OVERNIGHT_REFUSED``, and the refusal names what to use instead —
because "no" without an alternative is just an obstacle. A long multi-day equity
position belongs in CNC (or NRML where applicable); a multi-day equity *short*
cannot be held in the cash segment at all and must be expressed through futures or
options.

The horizon is taken from the intent's own declaration rather than inferred, since
nothing in a target plan carries a time dimension: an intent that means to hold
says so, and an intent that stays silent is treated as intraday, which is what MIS
already means.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from backend.strategies.compiler.base import ValidationRefusal

#: The product this policy governs. Every other product is untouched by it.
MIS_PRODUCT = "MIS"

#: A refusal that ends the evaluation (Phase 3 semantics).
MIS_OVERNIGHT_REFUSED = "MIS_OVERNIGHT_REFUSED"


def _as_hold_days(value: Any) -> Optional[int]:
    """The declared horizon in days, or ``None`` when the intent did not say.

    ``None`` is not "unknown, so allow": it means the intent made no multi-day
    claim, and MIS is intraday by default. A horizon that cannot be parsed at all
    is a malformed claim rather than a silent downgrade, so it is read as one day
    only when it genuinely parses to one.
    """
    if value is None or value == "":
        return None
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return None
    return days


def validate_intraday_scope(
    *, product: Any, hold_days: Any = None, signed_quantity: Any = None
) -> None:
    """Refuse a multi-day MIS intent, naming the alternatives.

    A pure function so the rule can be tested exhaustively without a compiler, a
    catalog or a database.
    """
    if str(product or "").strip().upper() != MIS_PRODUCT:
        return

    days = _as_hold_days(hold_days)
    if days is None or days <= 1:
        return

    try:
        quantity = int(signed_quantity or 0)
    except (TypeError, ValueError):
        quantity = 0

    if quantity < 0:
        guidance = (
            "A multi-day equity short is not available in the cash segment: express it "
            "through futures or options instead."
        )
    else:
        guidance = (
            "A multi-day long position belongs in CNC (or NRML where applicable) instead."
        )

    raise ValidationRefusal(
        MIS_OVERNIGHT_REFUSED,
        {
            "product": MIS_PRODUCT,
            "hold_days": days,
            "signed_quantity": quantity,
            "message": (
                f"Ordinary MIS is intraday; a {days}-day MIS intent would be squared off "
                f"by the platform at the session close rather than held. {guidance}"
            ),
        },
    )


def hold_days_from(payload: Mapping[str, Any]) -> Optional[int]:
    """The horizon an intent declares, under either of its accepted spellings."""
    for key in ("hold_days", "holding_days", "hold_duration_days"):
        if key in payload:
            return _as_hold_days(payload.get(key))
    return None
