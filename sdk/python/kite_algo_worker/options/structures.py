from __future__ import annotations

from typing import Any, Dict


def option_leg(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    *,
    exchange: str = "NFO",
    product: str | None = None,
    order_type: str = "MARKET",
    price: float | None = None,
    trigger_price: float | None = None,
    market_protection: int | None = None,
    exit_order_type: str | None = None,
    exit_price: float | None = None,
    variety: str = "regular",
    **extra: Any,
) -> Dict[str, Any]:
    """Build a simple option leg payload for preview/entry/exit baskets.

    ``price`` is used for LIMIT/SL entry orders. ``exit_order_type`` and
    ``exit_price`` let workers request explicit limit exits in canonical option
    exit previews and protection recommendations without relying on market
    protection behavior.
    """

    payload: Dict[str, Any] = {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type,
        "order_type": order_type,
        "quantity": int(quantity),
        "variety": variety,
    }
    if product is not None:
        payload["product"] = product
    if price is not None:
        payload["price"] = float(price)
    if trigger_price is not None:
        payload["trigger_price"] = float(trigger_price)
    if market_protection is not None:
        payload["market_protection"] = int(market_protection)
    if exit_order_type is not None:
        payload["exit_order_type"] = str(exit_order_type).upper()
    if exit_price is not None:
        payload["exit_price"] = float(exit_price)
    payload.update(extra)
    return payload


__all__ = ["option_leg"]
