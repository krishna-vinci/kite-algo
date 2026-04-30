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
    variety: str = "regular",
    **extra: Any,
) -> Dict[str, Any]:
    """Build a simple option leg payload for preview/entry baskets."""

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
    payload.update(extra)
    return payload


__all__ = ["option_leg"]
