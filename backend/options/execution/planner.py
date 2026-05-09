from __future__ import annotations

from typing import Any, Dict, Iterable, List


def sort_orders_buy_first(orders: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [dict(order) for order in orders]
    buys = [order for order in normalized if str(order.get("transaction_type", "")).upper() == "BUY"]
    sells = [order for order in normalized if str(order.get("transaction_type", "")).upper() == "SELL"]
    others = [
        order
        for order in normalized
        if str(order.get("transaction_type", "")).upper() not in {"BUY", "SELL"}
    ]
    return buys + sells + others


def sort_entry_orders_buy_first(orders: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sort_orders_buy_first(orders)


def build_entry_order_plan(
    legs: Iterable[Dict[str, Any]],
    *,
    product: str,
) -> List[Dict[str, Any]]:
    orders: List[Dict[str, Any]] = []
    for index, leg in enumerate(legs):
        quantity = int(leg.get("quantity") or 0)
        if quantity <= 0:
            continue

        transaction_type = str(leg.get("transaction_type") or "").upper()
        if transaction_type not in {"BUY", "SELL"}:
            continue

        order = {
            "leg_id": str(leg.get("leg_id") or f"leg_{index + 1}"),
            "exchange": leg.get("exchange") or "NFO",
            "tradingsymbol": leg.get("tradingsymbol"),
            "quantity": quantity,
            "transaction_type": transaction_type,
            "variety": leg.get("variety") or "regular",
            "order_type": str(leg.get("order_type") or "MARKET").upper(),
            # Product policy for B3: prefer run-level product.
            "product": product,
        }
        for optional_key in ("price", "trigger_price", "market_protection"):
            if leg.get(optional_key) is not None:
                order[optional_key] = leg.get(optional_key)
        orders.append(order)

    return sort_orders_buy_first(orders)
