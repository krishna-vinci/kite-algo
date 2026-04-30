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
        exit_orders.append(
            {
                "exchange": payload.get("exchange", "NFO"),
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "variety": order_variety,
                "product": product_override or payload.get("product", "MIS"),
                "order_type": exit_order_type,
                "quantity": quantity,
            }
        )

        if str(exit_order_type).upper() == "MARKET" and payload.get("apply_market_protection", False):
            exit_orders[-1]["market_protection"] = -1

    return exit_orders, skipped_positions


def _position_quantity(payload: Dict[str, Any]) -> int:
    return int(payload.get("quantity") or payload.get("net_quantity") or 0)
