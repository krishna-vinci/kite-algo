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
