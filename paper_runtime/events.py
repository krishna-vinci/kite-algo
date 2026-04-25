from __future__ import annotations

from typing import Any, Dict


def paper_order_event_payload(*, event_type: str, order: Any) -> Dict[str, Any]:
    return {
        "type": event_type,
        "account_scope": order.account_scope,
        "order_id": order.order_id,
        "instrument_token": order.instrument_token,
        "status": order.status,
        "metadata": dict(order.metadata or {}),
    }


def paper_position_event_payload(*, event_type: str, position: Any) -> Dict[str, Any]:
    return {
        "type": event_type,
        "account_scope": position.account_scope,
        "instrument_token": position.instrument_token,
        "product": position.product,
        "net_quantity": position.net_quantity,
        "average_price": float(position.average_price),
        "unrealized_pnl": float(position.unrealized_pnl),
        "metadata": dict(position.metadata or {}),
    }


def paper_trade_event_payload(*, event_type: str, trade: Any) -> Dict[str, Any]:
    return {
        "type": event_type,
        "account_scope": trade.account_scope,
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "instrument_token": trade.instrument_token,
        "quantity": trade.quantity,
        "price": float(trade.price),
        "metadata": dict(trade.metadata or {}),
    }
