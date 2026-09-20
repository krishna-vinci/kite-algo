from __future__ import annotations

from typing import Any, Dict, Iterable, List


def build_grouped_exit_orders(
    positions: Iterable[Dict[str, Any]],
    *,
    order_variety: str = "regular",
    product_override: str | None = None,
    exit_order_type: str = "MARKET",
) -> tuple[List[Dict[str, Any]], int]:
    exit_orders: List[Dict[str, Any]] = []
    skipped_positions = 0

    for payload in positions:
        quantity = abs(_position_quantity(payload))
        tradingsymbol = payload.get("tradingsymbol")
        if quantity == 0 or not tradingsymbol:
            skipped_positions += 1
            continue

        transaction_type = "SELL" if _position_quantity(payload) > 0 else "BUY"
        order_type = str(payload.get("exit_order_type") or payload.get("order_type") or exit_order_type).upper()
        order = {
            "exchange": payload.get("exchange", "NFO"),
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": payload.get("exit_variety") or payload.get("variety") or order_variety,
            "product": product_override or payload.get("product", "MIS"),
            "order_type": order_type,
            "quantity": quantity,
        }
        limit_price = payload.get("exit_price") if payload.get("exit_price") is not None else payload.get("limit_price")
        if order_type == "LIMIT" and limit_price is not None:
            order["price"] = limit_price
        exit_orders.append(order)

        if order_type == "MARKET" and payload.get("apply_market_protection", False):
            market_protection = payload.get("market_protection")
            exit_orders[-1]["market_protection"] = -1 if market_protection is None else market_protection

    return exit_orders, skipped_positions


def _position_quantity(payload: Dict[str, Any]) -> int:
    return int(payload.get("quantity") or payload.get("net_quantity") or 0)


def build_structure_exit_orders(
    legs: Iterable[Dict[str, Any]],
    *,
    closed_short_quantities: Dict[str, int] | None = None,
    order_variety: str = "regular",
    product_override: str | None = None,
    exit_order_type: str = "MARKET",
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Close short liabilities first, and release hedges only against proof (D-6).

    The ordering is the invariant, not a preference. A structure's long legs are
    what bound its liability, so releasing a hedge before its short is closed opens
    a naked window — and the window is not theoretical, it is the maximum-loss shape
    the structure was built to avoid.

    ``closed_short_quantities`` is the PROVEN closure per short leg, read from fill
    quantities. A hedge is released only for the quantity its short is *proven* to
    have closed: an order that was merely submitted releases nothing, because a
    submission is not a position and releasing on one is how a hedged structure
    becomes an unhedged one while every status field still says "submitted".
    """
    proven = {str(key): max(int(value or 0), 0) for key, value in (closed_short_quantities or {}).items()}
    shorts: List[Dict[str, Any]] = []
    longs: List[Dict[str, Any]] = []
    for leg in legs or []:
        payload = dict(leg)
        side = str(payload.get("side") or payload.get("transaction_type") or "").upper()
        if side == "SELL":
            shorts.append(payload)
        elif side == "BUY":
            longs.append(payload)

    ordered: List[Dict[str, Any]] = []
    short_plan: List[Dict[str, Any]] = []
    for payload in shorts:
        quantity = abs(int(payload.get("quantity") or payload.get("net_quantity") or 0))
        if quantity == 0:
            continue
        closed = proven.get(_leg_key(payload), 0)
        remaining = max(quantity - closed, 0)
        short_plan.append(
            {
                "tradingsymbol": payload.get("tradingsymbol"),
                "short_quantity": quantity,
                "proven_closed_quantity": min(closed, quantity),
                "remaining_quantity": remaining,
            }
        )
        if remaining == 0:
            # Already proven flat: there is nothing left to close, and re-sending
            # would open a position in the opposite direction.
            continue
        ordered.append(_close_order(payload, remaining, order_variety, product_override, exit_order_type))

    releases: List[Dict[str, Any]] = []
    withheld: List[Dict[str, Any]] = []
    for payload in longs:
        quantity = abs(int(payload.get("quantity") or payload.get("net_quantity") or 0))
        if quantity == 0:
            continue
        covered = _covered_short_quantity(payload, shorts, short_plan)
        if covered <= 0:
            # No short this hedge answers to is proven closed, so the protection
            # stays exactly where it is.
            withheld.append(
                {
                    "tradingsymbol": payload.get("tradingsymbol"),
                    "quantity": quantity,
                    "reason": "short_not_proven_closed",
                }
            )
            continue
        release_quantity = min(quantity, covered)
        releases.append(
            _close_order(payload, release_quantity, order_variety, product_override, exit_order_type)
        )
        if release_quantity < quantity:
            withheld.append(
                {
                    "tradingsymbol": payload.get("tradingsymbol"),
                    "quantity": quantity - release_quantity,
                    "reason": "short_only_partially_closed",
                }
            )

    # Shorts strictly precede hedges: the list order IS the ordering contract.
    ordered.extend(releases)
    return ordered, {
        "short_plan": short_plan,
        "released_hedges": len(releases),
        "withheld_hedges": withheld,
        "naked_short_quantity": sum(row["remaining_quantity"] for row in short_plan),
    }


def _leg_key(payload: Dict[str, Any]) -> str:
    """The identity proven closure is reported against.

    The tradingsymbol first, because that is what a caller reads off a position when
    it asks whether the short is gone; the internal structure leg id is a fallback
    for legs that carry no symbol.
    """
    return str(payload.get("tradingsymbol") or payload.get("structure_leg_id") or "")


def _close_order(
    payload: Dict[str, Any],
    quantity: int,
    order_variety: str,
    product_override: str | None,
    exit_order_type: str,
) -> Dict[str, Any]:
    """The order that closes one leg, in the direction opposite its position."""
    position = int(payload.get("quantity") or payload.get("net_quantity") or 0)
    order_type = str(
        payload.get("exit_order_type") or payload.get("order_type") or exit_order_type
    ).upper()
    order: Dict[str, Any] = {
        "exchange": payload.get("exchange", "NFO"),
        "tradingsymbol": payload.get("tradingsymbol"),
        "transaction_type": "SELL" if position > 0 else "BUY",
        "variety": payload.get("exit_variety") or payload.get("variety") or order_variety,
        "product": product_override or payload.get("product", "MIS"),
        "order_type": order_type,
        "quantity": int(quantity),
    }
    limit_price = (
        payload.get("exit_price") if payload.get("exit_price") is not None else payload.get("limit_price")
    )
    if order_type == "LIMIT" and limit_price is not None:
        order["price"] = limit_price
    return order


def _covered_short_quantity(
    hedge: Dict[str, Any], shorts: List[Dict[str, Any]], short_plan: List[Dict[str, Any]]
) -> int:
    """How much short this hedge is proven to be able to cover.

    A hedge that names the leg it protects answers for that leg alone; one that does
    not is matched by underlying and expiry, because a hedge for a different expiry
    covers nothing here whatever its symbol looks like.
    """
    targeted = str(hedge.get("hedge_for") or hedge.get("structure_leg_id") or "")
    if targeted:
        for row in short_plan:
            if str(row.get("tradingsymbol")) == targeted:
                return int(row["proven_closed_quantity"])
        return 0
    underlying = str(hedge.get("underlying") or "")
    expiry = str(hedge.get("expiry") or "")
    covered = 0
    for short, row in zip(shorts, short_plan):
        if underlying and str(short.get("underlying") or "") != underlying:
            continue
        if expiry and str(short.get("expiry") or "") != expiry:
            continue
        covered += int(row["proven_closed_quantity"])
    return covered
