from __future__ import annotations

from typing import Any, Dict, Optional


OrderPayload = Dict[str, Any]


def _clean(payload: OrderPayload) -> OrderPayload:
    """Drop fields that were not supplied while preserving explicit false/zero values."""

    return {key: value for key, value in payload.items() if value is not None}


def _common_fields(
    *,
    validity: str = "DAY",
    validity_ttl: Optional[int] = None,
    disclosed_quantity: Optional[int] = None,
    market_protection: Optional[int] = None,
    autoslice: Optional[bool] = None,
    iceberg_legs: Optional[int] = None,
    iceberg_quantity: Optional[int] = None,
    auction_number: Optional[str] = None,
    squareoff: Optional[float] = None,
    stoploss: Optional[float] = None,
    trailing_stoploss: Optional[float] = None,
) -> OrderPayload:
    return _clean(
        {
            "validity": validity,
            "validity_ttl": validity_ttl,
            "disclosed_quantity": disclosed_quantity,
            "market_protection": market_protection,
            "autoslice": autoslice,
            "iceberg_legs": iceberg_legs,
            "iceberg_quantity": iceberg_quantity,
            "auction_number": auction_number,
            "squareoff": squareoff,
            "stoploss": stoploss,
            "trailing_stoploss": trailing_stoploss,
        }
    )


def market_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return _clean(
        {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": variety,
            "product": product,
            "order_type": "MARKET",
            "quantity": quantity,
            **_common_fields(**kwargs),
        }
    )


def limit_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    price: float,
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return _clean(
        {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": variety,
            "product": product,
            "order_type": "LIMIT",
            "quantity": quantity,
            "price": price,
            **_common_fields(**kwargs),
        }
    )


def sl_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    price: float,
    trigger_price: float,
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return _clean(
        {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": variety,
            "product": product,
            "order_type": "SL",
            "quantity": quantity,
            "price": price,
            "trigger_price": trigger_price,
            **_common_fields(**kwargs),
        }
    )


def sl_m_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    trigger_price: float,
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return _clean(
        {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "variety": variety,
            "product": product,
            "order_type": "SL-M",
            "quantity": quantity,
            "trigger_price": trigger_price,
            **_common_fields(**kwargs),
        }
    )


def option_market_order(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    *,
    product: str = "NRML",
    exchange: str = "NFO",
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return market_order(exchange, tradingsymbol, transaction_type, product, quantity, variety, **kwargs)


def equity_market_order(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    *,
    product: str = "CNC",
    exchange: str = "NSE",
    variety: str = "regular",
    **kwargs: Any,
) -> OrderPayload:
    return market_order(exchange, tradingsymbol, transaction_type, product, quantity, variety, **kwargs)


class OrderBuilder:
    """Namespace class for discoverable order helper methods."""

    market_order = staticmethod(market_order)
    limit_order = staticmethod(limit_order)
    sl_order = staticmethod(sl_order)
    sl_m_order = staticmethod(sl_m_order)
    option_market_order = staticmethod(option_market_order)
    equity_market_order = staticmethod(equity_market_order)
