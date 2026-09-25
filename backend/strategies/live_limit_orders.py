"""Bounded LIMIT pricing for gated live legs (C1.2 S4).

Every gated dependent live leg - a C1.1 staged CNC buy, a portfolio dependent
increase, an option short/hedge release leg and a futures-roll release leg - is a
platform-side LIMIT inside a band frozen with the plan. MARKET is never a
fallback: an unbounded market order is exactly the slippage the band exists to
stop, so a price that cannot be derived INSIDE the band is a named refusal.

The derivation (design ``Bounded LIMIT orders``):

* BUY limit is ``min(ask, reference * (1 + max_drift))``;
* SELL limit is ``max(bid, reference * (1 - max_drift))``;
* when the required side of the book is absent, the fresh LTP stands in as the
  reference and the same band still applies;
* the price is rounded toward the PASSIVE side to the broker tick (a BUY floors,
  a SELL ceils), so rounding can never move it across the band;
* the derived price is never allowed outside the band. A book side beyond the
  band is clamped to the band (a passive, bounded order the platform timeout
  owns); a one-sided book whose LTP is ALREADY outside the band has no in-band
  price to send, so it refuses ``LIVE_LIMIT_PRICE_BOUND_EXCEEDED``.
"""

from __future__ import annotations

import math
import os
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Mapping, Optional

# -- named refusals ----------------------------------------------------------

#: No price evidence at all: no usable book side, no positive fresh LTP, or no
#: positive reference price to bound against.
LIVE_LIMIT_PRICE_UNAVAILABLE = "LIVE_LIMIT_PRICE_UNAVAILABLE"
#: The only price evidence lies outside the band frozen with the plan. The bound
#: is never widened to make the leg executable.
LIVE_LIMIT_PRICE_BOUND_EXCEEDED = "LIVE_LIMIT_PRICE_BOUND_EXCEEDED"
#: The broker tick for the instrument is unknown, so no price can be placed on
#: the broker's own grid. Fail closed rather than guess a tick.
LIVE_LIMIT_TICK_UNKNOWN = "LIVE_LIMIT_TICK_UNKNOWN"

#: Order types. LIMIT is the only type a gated dependent leg may ever carry.
ORDER_TYPE_LIMIT = "LIMIT"
ORDER_TYPE_MARKET = "MARKET"

#: ``LIVE_OPTION_LIMIT_MAX_DRIFT_PCT`` (option lane) and its C1.1 sibling
#: ``LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT`` (every other lane). Both are the same
#: bounded-drift notion and default to 0.5%.
DEFAULT_OPTION_LIMIT_MAX_DRIFT_PCT = 0.005
DEFAULT_STAGED_BUY_MAX_PRICE_DRIFT_PCT = 0.005

#: How long a gated LIMIT may work before the platform cancels it. A broker
#: day/TTL validity is not sufficient for an intraday gate.
DEFAULT_GATED_LIMIT_TIMEOUT_SECONDS = 10.0


class LimitOrderRefusal(Exception):
    """A named refusal from the bounded-LIMIT derivation."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})

    def as_detail(self) -> dict:
        return {"rejection_reason": self.reason_code, **self.detail}


def is_gated_limit_release_rule(rule: Any) -> bool:
    """True when a step's release rule makes it a GATED dependent leg.

    ``immediate`` legs are not gated and keep their current behaviour, and a
    risk-reduction MIS square-off (released by the platform clock rather than by
    a dependent leg's evidence) is not a gated dependent leg either.
    """
    from .live_sequence import (
        RULE_ALL_PREREQUISITES_FILLED,
        RULE_HEDGE_FILL_GATE,
        RULE_HEDGE_RELEASE_WITHHELD,
        RULE_ROLL_CLOSE_RELEASED,
        RULE_STAGED_FUNDING_GATE,
    )

    return str(rule or "") in (
        RULE_STAGED_FUNDING_GATE,
        RULE_ALL_PREREQUISITES_FILLED,
        RULE_ROLL_CLOSE_RELEASED,
        RULE_HEDGE_FILL_GATE,
        RULE_HEDGE_RELEASE_WITHHELD,
    )


# -- configuration -----------------------------------------------------------


def _environ(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _env_pct(environ: Optional[Mapping[str, str]], key: str, default: float) -> float:
    raw = _environ(environ).get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < 0.0:
        return default
    return value


def option_limit_max_drift_pct(environ: Optional[Mapping[str, str]] = None) -> float:
    """The option lane's band half-width (``LIVE_OPTION_LIMIT_MAX_DRIFT_PCT``)."""
    return _env_pct(
        environ, "LIVE_OPTION_LIMIT_MAX_DRIFT_PCT", DEFAULT_OPTION_LIMIT_MAX_DRIFT_PCT
    )


def staged_buy_max_price_drift_pct(environ: Optional[Mapping[str, str]] = None) -> float:
    """C1.1's band half-width, under its established name and default."""
    return _env_pct(
        environ,
        "LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT",
        DEFAULT_STAGED_BUY_MAX_PRICE_DRIFT_PCT,
    )


def gated_limit_timeout_seconds(environ: Optional[Mapping[str, str]] = None) -> float:
    """The timeout for a working gated LIMIT (``LIVE_GATED_LIMIT_TIMEOUT_SECONDS``)."""
    raw = _environ(environ).get("LIVE_GATED_LIMIT_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_GATED_LIMIT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATED_LIMIT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0.0:
        return DEFAULT_GATED_LIMIT_TIMEOUT_SECONDS
    return value


# -- price resolution --------------------------------------------------------


def _positive_number(value: Any) -> Optional[float]:
    """A usable positive finite price, or ``None`` (absence, zero, junk)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def frozen_reference_price(
    *,
    detail: Optional[Mapping[str, Any]] = None,
    notional_inr: Any = None,
    quantity: Any = None,
) -> float:
    """The reference price frozen with the step, or ``0.0`` when unknown.

    The frozen sizing is preferred; a plan whose step carries only a notional
    falls back to ``notional / quantity``, which is the same figure the C1.1
    staged funding gate authorizes against.
    """
    raw = dict(detail or {}).get("reference_price")
    try:
        value = abs(float(raw)) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if value > 0.0:
        return value
    try:
        ordered = abs(int(quantity or 0))
    except (TypeError, ValueError):
        ordered = 0
    if ordered <= 0:
        return 0.0
    try:
        return abs(float(notional_inr or 0.0)) / float(ordered)
    except (TypeError, ValueError):
        return 0.0


def round_toward_passive(price: Any, side: Any, tick_size: Any) -> float:
    """Round ``price`` to the broker tick toward the PASSIVE side of the book.

    A BUY floors (a lower limit is more passive) and a SELL ceils. Rounding this
    way can never move the price across the approved band, which is why the
    derivation rounds and then bounds.
    """
    tick = _positive_number(tick_size)
    if tick is None:
        raise LimitOrderRefusal(
            LIVE_LIMIT_TICK_UNKNOWN,
            {"tick_size": None if tick_size is None else str(tick_size)},
        )
    value = _positive_number(price)
    if value is None:
        raise LimitOrderRefusal(LIVE_LIMIT_PRICE_UNAVAILABLE, {"price": price})
    d_tick = Decimal(str(tick))
    d_price = Decimal(str(value))
    rounding = ROUND_FLOOR if str(side).upper() == "BUY" else ROUND_CEILING
    steps = (d_price / d_tick).to_integral_value(rounding=rounding)
    return float(steps * d_tick)


def _coerce_drift(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
            {"max_drift_pct": str(raw), "reason": "max_drift_pct_not_numeric"},
        ) from None
    if not math.isfinite(value) or value < 0.0:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
            {"max_drift_pct": value, "reason": "max_drift_pct_invalid"},
        )
    return value


def _bounded(value: float, drift: float, direction: str) -> float:
    """``value * (1 +/- drift)`` computed on the decimal grid.

    ``100.0 * 1.005`` is ``100.49999999999999`` in binary floating point, which
    would then floor to the WRONG tick. The band edge is a broker price, so it is
    computed exactly.
    """
    d_value = Decimal(str(value))
    d_rate = Decimal(1) + Decimal(str(drift)) if direction == "BUY" else Decimal(1) - Decimal(str(drift))
    return float(d_value * d_rate)


def derive_bounded_limit(
    side: Any,
    reference_price: Any = None,
    quote: Optional[Mapping[str, Any]] = None,
    max_drift_pct: Any = None,
    tick_size: Any = None,
    *,
    tick_source: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    """Derive one gated leg's bounded LIMIT price, or refuse by name.

    Returns the price together with the whole pre-send price evidence (frozen
    reference, observed quote, chosen price, band, tick) so the durable step
    detail carries exactly what was decided.
    """
    direction = str(side or "").upper()
    if direction not in ("BUY", "SELL"):
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_UNAVAILABLE, {"side": str(side), "reason": "side_unknown"}
        )

    tick = _positive_number(tick_size)
    if tick is None:
        raise LimitOrderRefusal(
            LIVE_LIMIT_TICK_UNKNOWN,
            {"tick_size": None if tick_size is None else str(tick_size), "side": direction},
        )

    reference = _positive_number(reference_price)
    if reference is None:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_UNAVAILABLE,
            {"reference_price": None if reference_price is None else str(reference_price)},
        )

    drift = (
        option_limit_max_drift_pct(environ)
        if max_drift_pct is None
        else _coerce_drift(max_drift_pct)
    )

    book_quote = dict(quote or {})
    ltp = _positive_number(book_quote.get("ltp", book_quote.get("last_price")))
    bid = _positive_number(book_quote.get("bid", book_quote.get("bid_price")))
    ask = _positive_number(book_quote.get("ask", book_quote.get("ask_price")))
    as_of = book_quote.get("as_of")
    as_of_iso = (
        as_of.isoformat() if hasattr(as_of, "isoformat") else (str(as_of) if as_of else None)
    )

    bound = _bounded(reference, drift, direction)
    if bound < 0.0:
        bound = 0.0

    book_side = ask if direction == "BUY" else bid
    if book_side is not None:
        source = "ask" if direction == "BUY" else "bid"
        raw = min(book_side, bound) if direction == "BUY" else max(book_side, bound)
    else:
        # The required side of the book is absent: the fresh LTP stands in as the
        # reference, and the SAME band still applies. A one-sided book whose LTP
        # is already outside the band offers no in-band price at all, so this is
        # the one place the derivation refuses rather than clamps.
        if ltp is None:
            raise LimitOrderRefusal(
                LIVE_LIMIT_PRICE_UNAVAILABLE,
                {
                    "side": direction,
                    "reference_price_inr": reference,
                    "bid": bid,
                    "ask": ask,
                    "ltp": None,
                    "reason": "no_usable_book_side_or_ltp",
                },
            )
        if direction == "BUY" and ltp > bound:
            raise LimitOrderRefusal(
                LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
                {
                    "side": direction,
                    "reference_price_inr": reference,
                    "bound_price_inr": bound,
                    "ltp": ltp,
                    "max_drift_pct": drift,
                    "reason": "ltp_above_the_frozen_buy_band",
                },
            )
        if direction == "SELL" and ltp < bound:
            raise LimitOrderRefusal(
                LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
                {
                    "side": direction,
                    "reference_price_inr": reference,
                    "bound_price_inr": bound,
                    "ltp": ltp,
                    "max_drift_pct": drift,
                    "reason": "ltp_below_the_frozen_sell_band",
                },
            )
        source = "ltp"
        natural = _bounded(ltp, drift, direction)
        raw = min(natural, bound) if direction == "BUY" else max(natural, bound)

    if raw <= 0.0:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_UNAVAILABLE,
            {
                "side": direction,
                "reference_price_inr": reference,
                "bound_price_inr": bound,
                "raw_price_inr": raw,
                "reason": "derived_price_not_positive",
            },
        )

    price = round_toward_passive(raw, direction, tick)

    # Belt and braces: rounding is toward the passive side, so this never fires.
    # If it ever did, the order would sit outside the approved band and must not
    # be sent.
    if direction == "BUY" and price > bound + 1e-9:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
            {"side": direction, "price": price, "bound_price_inr": bound},
        )
    if direction == "SELL" and price < bound - 1e-9:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
            {"side": direction, "price": price, "bound_price_inr": bound},
        )
    if price <= 0.0:
        raise LimitOrderRefusal(
            LIVE_LIMIT_PRICE_UNAVAILABLE,
            {"side": direction, "price": price, "reason": "rounded_price_not_positive"},
        )

    return {
        "order_type": ORDER_TYPE_LIMIT,
        "side": direction,
        "price": float(price),
        "raw_price_inr": float(raw),
        "reference_price_inr": float(reference),
        "bound_price_inr": float(bound),
        "max_drift_pct": float(drift),
        "reference_source": source,
        "bid": bid,
        "ask": ask,
        "ltp": ltp,
        "quote_as_of": as_of_iso,
        "tick_size": float(tick),
        "tick_source": str(tick_source or ""),
    }
