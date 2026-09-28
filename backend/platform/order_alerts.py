"""Live broker order outcome alerts pushed straight to the owner through ntfy.

One synchronous listener is registered on ``MarketDataRuntime``'s order-update
listeners, so every normalized Kite order update for a live order (strategy,
manual or protection exit) flows through ``OrderAlertListener.__call__``.  Only
terminal outcomes alert: fills, rejections, lapses, and cancels that still left a
partial fill.  Paper orders never reach the broker, so they never appear here.

The listener is O(1), does no I/O on the calling thread, and never raises into
the feed.  ``alert_owner_nowait`` owns delivery and its own key cooldown; this
module only decides *what* to send and bounds duplicate ``(order_id, status)``
updates that the broker can resend.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Callable, Sequence

from backend.platform.owner_alerts import alert_owner_nowait

logger = logging.getLogger(__name__)

_DISABLED_VALUES = {"0", "false", "no", "off"}
_DIRECT_STATUSES = {"COMPLETE", "REJECTED", "LAPSED"}
_CANCEL_STATUSES = {"CANCELLED", "CANCELED"}


def order_alerts_enabled() -> bool:
    """Whether live order outcome alerts are enabled (default true)."""

    raw = os.environ.get("ORDER_ALERTS_ENABLED", "true")
    return str(raw).strip().lower() not in _DISABLED_VALUES


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quantity_line(side: str, quantity: int, symbol: str) -> str:
    return " ".join(part for part in (side, str(quantity), symbol) if part)


def _fill_line(
    *,
    exchange: str,
    symbol: str,
    side: str,
    filled: int,
    average_price: float,
    product: str,
    order_type: str,
    order_id: str,
) -> str:
    head = " ".join(part for part in (exchange, symbol, side, str(filled)) if part)
    parts = [head, f"@ {average_price:.2f}"]
    detail = ", ".join(part for part in (product, order_type) if part)
    if detail:
        parts.append(f"({detail})")
    if order_id:
        parts.append(f"order {order_id}")
    return " ".join(parts)


def format_order_alert(payload: dict) -> tuple[str, str, Sequence[str]] | None:
    """Pure decision: (title, message, tags) for a terminal order update, else None."""

    if not isinstance(payload, dict):
        return None
    status = _text(payload.get("status")).upper()
    if not status:
        return None

    try:
        filled = _int(payload.get("filled_quantity"))
        if status in _CANCEL_STATUSES:
            if filled <= 0:
                return None
        elif status not in _DIRECT_STATUSES:
            return None

        side = _text(payload.get("transaction_type")).upper()
        symbol = _text(payload.get("tradingsymbol"))
        exchange = _text(payload.get("exchange"))
        product = _text(payload.get("product")).upper()
        order_type = _text(payload.get("order_type")).upper()
        order_id = _text(payload.get("order_id"))
        quantity = _int(payload.get("quantity")) or filled
        average_price = _float(payload.get("average_price"))
        tag = _text(payload.get("tag"))
        tag_suffix = f" [tag: {tag}]" if tag else ""

        if status == "COMPLETE":
            title = f"Filled: {_quantity_line(side, filled, symbol)}".strip()
            message = _fill_line(
                exchange=exchange,
                symbol=symbol,
                side=side,
                filled=filled,
                average_price=average_price,
                product=product,
                order_type=order_type,
                order_id=order_id,
            )
            message = f"{message}{tag_suffix}".strip()
            return title, message, ["white_check_mark"]

        if status == "REJECTED":
            title = f"Order REJECTED: {_quantity_line(side, quantity, symbol)}".strip()
            reason = _text(payload.get("status_message")) or _text(payload.get("status_message_raw"))
            parts = [reason or "Order rejected by broker"]
            if order_id:
                parts.append(f"(order {order_id})")
            message = f"{' '.join(parts)}{tag_suffix}"
            return title, message, ["rotating_light"]

        if status == "LAPSED":
            title = f"Order LAPSED: {_quantity_line(side, quantity, symbol)}".strip()
            message = f"filled {filled} of {quantity} @ {average_price:.2f}{tag_suffix}"
            return title, message, ["warning"]

        title = f"Partial fill then cancelled: {_quantity_line(side, filled, symbol)}".strip()
        message = f"filled {filled} of {quantity} @ {average_price:.2f}{tag_suffix}"
        return title, message, ["warning"]
    except Exception:  # noqa: BLE001 - formatting must never break the feed
        logger.warning("Failed to format order alert", exc_info=True)
        return None


class OrderAlertListener:
    """Callable order-update listener that alerts the owner on terminal outcomes."""

    def __init__(
        self,
        alert: Callable[..., None] = alert_owner_nowait,
        max_seen: int = 5000,
    ) -> None:
        self._alert = alert
        self._max_seen = max(1, int(max_seen))
        # Bounded FIFO of handled (order_id, status) pairs; Kite can resend them.
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.enabled = order_alerts_enabled()

    def _remember(self, key: tuple[str, str]) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)
        return True

    def __call__(self, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            formatted = format_order_alert(payload)
            if formatted is None:
                return
            order_id = _text(payload.get("order_id"))
            status = _text(payload.get("status")).upper()
            key = f"order:{order_id}:{status}"
            if not self._remember((order_id, status)):
                return
            title, message, tags = formatted
            self._alert(key=key, title=title, message=message, tags=tags)
        except Exception:  # noqa: BLE001 - a listener never raises into the feed
            logger.warning("Order alert listener failed", exc_info=True)
