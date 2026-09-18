"""Expiry cutoff: escalate an unrolled contract, never improvise a close (D-4).

A futures contract has a deadline, and a strategy that has not rolled by then is
holding something the platform has no mandate to trade. So the response to
"expiring, unrolled" is the owner's attention and ``action_required`` — not a close.
The platform does not get to decide what the position was for, and an improvised
liquidation at the last session is the same mistake as the 15:20 MIS liquidation
that Phase 8 exists to refuse.

The window is config, following the square-off schedule's precedent: the bounded
number of days belongs to policy, not to code.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Optional

#: Days before expiry at which an unrolled contract escalates.
DEFAULT_EXPIRY_WARNING_DAYS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def expiry_warning_days() -> int:
    """The configured warning window, in days. Policy, not code."""
    raw = os.environ.get("FUTURES_EXPIRY_WARNING_DAYS")
    if raw is None:
        return DEFAULT_EXPIRY_WARNING_DAYS
    try:
        days = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_EXPIRY_WARNING_DAYS
    return days if days >= 0 else DEFAULT_EXPIRY_WARNING_DAYS


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def days_to_expiry(expiry: Any, *, now: datetime) -> Optional[int]:
    """Whole days until expiry, negative once past it, ``None`` when unreadable."""
    parsed = _as_date(expiry)
    if parsed is None:
        return None
    return (parsed - now.date()).days


def check_expiry_cutoff(
    machine: Any,
    roll_id: str,
    *,
    now: Optional[datetime] = None,
    notify: bool = True,
    notifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    warning_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Escalate an unrolled roll whose contract is inside the warning window.

    Returns what it found and did. It deliberately has no ability to close anything:
    the only transitions it can cause are ``action_required`` and an ``escalated``
    event, so there is no code path here that liquidates a position.
    """
    moment = now or _utcnow()
    window = expiry_warning_days() if warning_days is None else int(warning_days)

    roll = machine.get(roll_id)
    if roll is None:
        return {"escalated": False, "reason": "roll_not_found", "roll_id": str(roll_id)}

    expiries = [
        value
        for value in (
            (roll.get("old_coordinate") or {}).get("expiry"),
            (roll.get("new_coordinate") or {}).get("expiry"),
        )
        if value
    ]
    if not expiries:
        return {
            "escalated": False,
            "reason": "expiry_unavailable",
            "roll_id": str(roll_id),
            "warning_days": window,
        }

    remaining = [days_to_expiry(expiry, now=moment) for expiry in expiries]
    soonest = min((value for value in remaining if value is not None), default=None)
    if soonest is None:
        return {
            "escalated": False,
            "reason": "expiry_unavailable",
            "roll_id": str(roll_id),
            "warning_days": window,
        }
    if soonest > window:
        return {
            "escalated": False,
            "reason": "outside_window",
            "roll_id": str(roll_id),
            "days_to_expiry": soonest,
            "warning_days": window,
        }

    # Inside the window and unrolled: flag it for the owner. ``stall`` is a no-op if
    # the roll already completed, which is why the state is re-read before acting.
    if str(roll.get("state")) in ("acquiring", "proving_filled", "releasing_old"):
        machine.stall(roll_id, reason="expiry_cutoff")

    # ONE notification, through the machine's own escalation. Notifying here too
    # would tell the owner twice about one deadline, and a duplicated alarm is how
    # an operator learns to ignore alarms.
    if notifier is not None:
        machine._notifier = notifier
    if not notify:
        # Suppressing the send must not suppress the record: the deadline still
        # happened, and the trail has to say so.
        machine._notifier = None
    escalated = machine.escalate(roll_id, reason="expiry_cutoff")

    return {
        "escalated": True,
        "reason": "expiry_cutoff",
        "roll_id": str(roll_id),
        "days_to_expiry": soonest,
        "warning_days": window,
        "notified": bool((escalated or {}).get("escalated")),
    }
