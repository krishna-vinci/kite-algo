"""Hedge fill gating: submitted is not filled, so submission releases nothing (D-5).

A short option position carries unlimited liability; its protective long is what
bounds it. The dangerous moment is the gap between "the hedge order was accepted"
and "the hedge actually filled" — during that gap the structure looks hedged on
paper (an order exists) and is naked in reality (no position exists). Releasing the
short leg in that gap is how a bounded structure becomes an unbounded one without
anyone deciding to take that risk.

So the release of a dependent short is gated on **confirmed fill quantity**, never on
an order status:

* a full hedge fill releases the dependent short in full;
* a partial fill releases **at most** the proportional share, floored — never more
  than the hedge that actually exists;
* a rejection, cancellation, timeout or insufficient funds releases **nothing** and
  raises ``action_required``, because a hedge that did not arrive must be a human's
  decision, not a silent reduction in protection.

There is deliberately no atomic multi-leg claim anywhere in this path. Atomicity
would hide the very window this module exists to measure.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

#: How long a hedge leg may remain unfilled before the structure stops waiting.
HEDGE_FILL_TIMEOUT_ENV = "OPTION_HEDGE_FILL_TIMEOUT_SECONDS"
DEFAULT_HEDGE_FILL_TIMEOUT_SECONDS = 30

#: Outcomes a hedge leg can reach.
HEDGE_OUTCOMES = ("filled", "partially_filled", "rejected", "cancelled", "timeout", "pending")

#: Outcomes that release nothing and need an operator.
BLOCKING_OUTCOMES = ("rejected", "cancelled", "timeout")


def hedge_fill_timeout_seconds() -> int:
    """The configured wait, in seconds. Policy, not code."""
    raw = os.environ.get(HEDGE_FILL_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_HEDGE_FILL_TIMEOUT_SECONDS
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_HEDGE_FILL_TIMEOUT_SECONDS
    return seconds if seconds > 0 else DEFAULT_HEDGE_FILL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class GateDecision:
    """What the gate permits, and why. A refusal is never silent."""

    released_quantity: int = 0
    blocked: bool = False
    action_required: bool = False
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def released(self) -> bool:
        return self.released_quantity > 0

    @property
    def fully_released(self) -> bool:
        return self.released_quantity > 0 and not self.blocked

    def as_dict(self) -> Dict[str, Any]:
        return {
            "released_quantity": self.released_quantity,
            "blocked": self.blocked,
            "action_required": self.action_required,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def hedge_fill_gate(
    *,
    required_hedge_quantity: int,
    confirmed_filled_quantity: int,
    dependent_short_quantity: int,
    outcome: str = "pending",
    elapsed_seconds: Optional[float] = None,
    timeout_seconds: Optional[int] = None,
) -> GateDecision:
    """Decide how much of a dependent short the confirmed hedge fill releases.

    Pure, so the rule can be tested exhaustively without an engine, a broker or a
    clock. The caller supplies the clock reading; this function never reads one.
    """
    required = max(int(required_hedge_quantity or 0), 0)
    filled = max(int(confirmed_filled_quantity or 0), 0)
    dependent = max(int(dependent_short_quantity or 0), 0)
    outcome = str(outcome or "pending").lower()
    timeout = int(timeout_seconds) if timeout_seconds is not None else hedge_fill_timeout_seconds()

    if required <= 0 or dependent <= 0:
        return GateDecision(reason="nothing_to_release")

    if outcome in BLOCKING_OUTCOMES:
        # NO release. A hedge that did not arrive leaves the short defended by
        # nothing, and the operator decides what happens next.
        return GateDecision(
            blocked=True,
            action_required=True,
            reason=f"hedge_{outcome}",
            detail={
                "outcome": outcome,
                "required_hedge_quantity": required,
                "confirmed_filled_quantity": filled,
                "dependent_short_quantity": dependent,
                "message": "No short was released because the hedge did not fill",
            },
        )

    if elapsed_seconds is not None and float(elapsed_seconds) > float(timeout) and filled < required:
        # The same rule as an explicit timeout, because waiting longer cannot
        # conjure a fill and a structure stuck half-hedged is unresolved.
        return GateDecision(
            blocked=True,
            action_required=True,
            reason="hedge_fill_timeout",
            detail={
                "elapsed_seconds": float(elapsed_seconds),
                "timeout_seconds": timeout,
                "confirmed_filled_quantity": filled,
                "required_hedge_quantity": required,
                "message": "The hedge did not fill inside the timeout",
            },
        )

    if filled <= 0:
        # Submitted, accepted, nothing proven: hold, do not release.
        return GateDecision(
            blocked=True,
            reason="no_confirmed_fill",
            detail={"outcome": outcome, "required_hedge_quantity": required},
        )

    if filled >= required:
        return GateDecision(
            released_quantity=dependent,
            reason="hedge_fully_filled",
            detail={"confirmed_filled_quantity": filled, "required_hedge_quantity": required},
        )

    # Partial: AT MOST the proportional share, floored. Flooring matters — releasing
    # the extra unit would leave more short exposed than the hedge covers, which is
    # precisely the naked sliver this gate exists to prevent.
    proportion = filled / required
    released = int(math.floor(dependent * proportion))
    return GateDecision(
        released_quantity=released,
        blocked=released < dependent,
        reason="hedge_partially_filled",
        detail={
            "confirmed_filled_quantity": filled,
            "required_hedge_quantity": required,
            "proportion": proportion,
            "dependent_short_quantity": dependent,
            "unreleased_quantity": dependent - released,
        },
    )


def hedge_legs(legs: Any) -> list:
    """Protective BUY legs: what a structure relies on to bound its liability."""
    return [
        dict(leg)
        for leg in (legs or [])
        if str(leg.get("side") or leg.get("transaction_type") or "").upper() == "BUY"
    ]


def short_legs(legs: Any) -> list:
    """SELL legs: the ones whose release must be gated."""
    return [
        dict(leg)
        for leg in (legs or [])
        if str(leg.get("side") or leg.get("transaction_type") or "").upper() == "SELL"
    ]


def entry_blocked_by_protection(protection_state: Mapping[str, Any]) -> bool:
    """Whether a triggered structure guard stops new legs from being submitted.

    Protection triggering during a partial entry blocks NEW risk; it never blocks
    the exits that reduce what is already on. A guard that stopped exits would trap
    a half-built structure in exactly the shape the guard fired to avoid.
    """
    return str((protection_state or {}).get("status") or "") == "triggered"
