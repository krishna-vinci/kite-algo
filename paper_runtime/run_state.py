from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import PaperOrder, PaperPosition, PaperTrade
from .repository import SqlAlchemyPaperRepository


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _charges_from_metadata(metadata: Dict[str, Any]) -> float:
    cost_contract = metadata.get("cost_contract") if isinstance(metadata.get("cost_contract"), dict) else {}
    if cost_contract:
        return _to_float(cost_contract.get("total_charges"))
    return _to_float(metadata.get("estimated_charges"))


def _side_for_quantity(quantity: int) -> str:
    if quantity > 0:
        return "LONG"
    if quantity < 0:
        return "SHORT"
    return "FLAT"


def _strategy_identity(metadata: Dict[str, Any]) -> str | None:
    for key in ("strategy_run_id", "option_strategy_id", "strategy_id", "algo_instance_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return None


@dataclass
class _LegState:
    instrument_token: int
    product: str
    exchange: Optional[str]
    tradingsymbol: Optional[str]
    net_quantity: int = 0
    average_price: float = 0.0
    realized_pnl: float = 0.0
    charges: float = 0.0
    last_fill_at: Optional[str] = None


class PaperRunStateService:
    def __init__(self, repository: SqlAlchemyPaperRepository) -> None:
        self.repository = repository

    def get_run_state(self, account_scope: str, strategy_run_id: str) -> Optional[Dict[str, Any]]:
        normalized_strategy_run_id = str(strategy_run_id or "").strip()
        if not normalized_strategy_run_id:
            raise ValueError("strategy_run_id is required")

        orders = self.repository.list_orders(account_scope, limit=50000)
        trades = self.repository.list_trades(account_scope, limit=50000)
        positions = self.repository.list_positions(account_scope, only_open=False)

        order_by_id = {order.order_id: order for order in orders}
        relevant_orders = [order for order in orders if _strategy_identity(dict(order.metadata or {})) == normalized_strategy_run_id]
        relevant_trades = [trade for trade in trades if _strategy_identity(dict(trade.metadata or {})) == normalized_strategy_run_id]
        if not relevant_orders and not relevant_trades:
            return None

        position_marks: Dict[Tuple[int, str], PaperPosition] = {
            (int(position.instrument_token), str(position.product)): position
            for position in positions
        }
        leg_states: Dict[Tuple[int, str], _LegState] = {}
        latest_metadata: Dict[str, Any] = {}
        latest_event_at: Optional[str] = None
        stale_reasons: List[str] = []

        for trade in sorted(relevant_trades, key=lambda item: (item.trade_timestamp or _utcnow(), item.trade_id)):
            trade_metadata = dict(trade.metadata or {})
            if trade_metadata:
                latest_metadata.update({key: value for key, value in trade_metadata.items() if value not in (None, "")})
            order = order_by_id.get(trade.order_id)
            product = str(order.product) if order is not None else str(trade_metadata.get("product") or "")
            exchange = str(order.exchange) if order is not None else trade_metadata.get("exchange")
            tradingsymbol = str(order.tradingsymbol) if order is not None else trade_metadata.get("tradingsymbol")
            key = (int(trade.instrument_token), product)
            state = leg_states.setdefault(
                key,
                _LegState(
                    instrument_token=int(trade.instrument_token),
                    product=product,
                    exchange=exchange or None,
                    tradingsymbol=tradingsymbol or None,
                ),
            )
            quantity = int(trade.quantity)
            price = _to_float(trade.price)
            side = str(trade.transaction_type or "").upper()
            state.charges += _charges_from_metadata(trade_metadata)
            if state.net_quantity == 0 or (state.net_quantity > 0 and side == "BUY") or (state.net_quantity < 0 and side == "SELL"):
                existing_abs = abs(state.net_quantity)
                incoming_abs = quantity
                combined = existing_abs + incoming_abs
                state.average_price = price if combined == 0 else ((state.average_price * existing_abs) + (price * incoming_abs)) / combined
                state.net_quantity = state.net_quantity + (quantity if side == "BUY" else -quantity)
            else:
                closing_quantity = min(abs(state.net_quantity), quantity)
                if state.net_quantity > 0 and side == "SELL":
                    state.realized_pnl += (price - state.average_price) * closing_quantity
                elif state.net_quantity < 0 and side == "BUY":
                    state.realized_pnl += (state.average_price - price) * closing_quantity
                remaining_existing = abs(state.net_quantity) - closing_quantity
                remaining_incoming = quantity - closing_quantity
                if remaining_existing > 0:
                    state.net_quantity = remaining_existing if state.net_quantity > 0 else -remaining_existing
                elif remaining_incoming > 0:
                    state.net_quantity = remaining_incoming if side == "BUY" else -remaining_incoming
                    state.average_price = price
                else:
                    state.net_quantity = 0
                    state.average_price = 0.0
            state.last_fill_at = trade.trade_timestamp.isoformat() if trade.trade_timestamp else state.last_fill_at
            latest_event_at = max(filter(None, [latest_event_at, state.last_fill_at]), default=latest_event_at)

        for order in relevant_orders:
            order_metadata = dict(order.metadata or {})
            if order_metadata:
                latest_metadata.update({key: value for key, value in order_metadata.items() if value not in (None, "")})
            order_ts = (order.updated_at or order.placed_at)
            order_iso = order_ts.isoformat() if order_ts else None
            latest_event_at = max(filter(None, [latest_event_at, order_iso]), default=latest_event_at)

        strategy_tag = str(latest_metadata.get("strategy_tag") or "") or None
        algo_instance_id = str(latest_metadata.get("algo_instance_id") or "") or None
        display_name = str(
            latest_metadata.get("strategy_name")
            or (strategy_tag.replace("_", " ").title() if strategy_tag else normalized_strategy_run_id)
        )

        legs_payload: List[Dict[str, Any]] = []
        realized_total = 0.0
        unrealized_total = 0.0
        charges_total = 0.0
        open_leg_count = 0
        net_quantity_total = 0
        last_updated_at: Optional[str] = None
        covered_position_keys: set[Tuple[int, str]] = set()

        for key, leg in leg_states.items():
            covered_position_keys.add(key)
            position = position_marks.get(key)
            last_price = _to_float((position.metadata or {}).get("last_price")) if position is not None else 0.0
            if not last_price:
                last_price = leg.average_price
            if leg.net_quantity != 0:
                if position is None or int(position.net_quantity) == 0:
                    stale_reasons.append(f"missing_account_position:{leg.instrument_token}:{leg.product}")
                elif (leg.net_quantity > 0 > int(position.net_quantity)) or (leg.net_quantity < 0 < int(position.net_quantity)):
                    stale_reasons.append(f"sign_mismatch:{leg.instrument_token}:{leg.product}")
            unrealized = 0.0
            if leg.net_quantity > 0:
                unrealized = (last_price - leg.average_price) * leg.net_quantity
            elif leg.net_quantity < 0:
                unrealized = (leg.average_price - last_price) * abs(leg.net_quantity)
            if leg.net_quantity != 0:
                open_leg_count += 1
            net_quantity_total += leg.net_quantity
            realized_total += leg.realized_pnl
            unrealized_total += unrealized
            charges_total += leg.charges
            position_updated = position.updated_at.isoformat() if position is not None and position.updated_at else None
            last_updated_at = max(filter(None, [last_updated_at, position_updated, leg.last_fill_at]), default=last_updated_at)
            legs_payload.append(
                {
                    "instrument_token": leg.instrument_token,
                    "exchange": leg.exchange or (position.exchange if position is not None else None),
                    "tradingsymbol": leg.tradingsymbol or (position.tradingsymbol if position is not None else None),
                    "product": leg.product,
                    "net_quantity": leg.net_quantity,
                    "side": _side_for_quantity(leg.net_quantity),
                    "average_price": leg.average_price,
                    "last_price": last_price,
                    "realized_pnl": leg.realized_pnl,
                    "unrealized_pnl": unrealized,
                    "gross_pnl": leg.realized_pnl + unrealized,
                    "charges": leg.charges,
                    "net_pnl": leg.realized_pnl + unrealized - leg.charges,
                    "metadata": {
                        key: value
                        for key, value in latest_metadata.items()
                        if key in {"strategy_run_id", "strategy_family", "strategy_name", "algo_instance_id", "strategy_tag", "journal_run_id", "journal_ref", "entry_surface", "source"}
                    },
                }
            )

        timeline = []
        for order in relevant_orders:
            order_ts = (order.placed_at or order.updated_at)
            timeline.append(
                {
                    "kind": "order",
                    "timestamp": order_ts.isoformat() if order_ts else None,
                    "label": f"{order.transaction_type} {order.tradingsymbol or order.instrument_token} {order.status}",
                }
            )
        for trade in relevant_trades:
            timeline.append(
                {
                    "kind": "trade",
                    "timestamp": trade.trade_timestamp.isoformat() if trade.trade_timestamp else None,
                    "label": f"{trade.transaction_type} fill {trade.quantity} @ {float(trade.price):.2f}",
                }
            )

        return {
            "strategy_id": normalized_strategy_run_id,
            "strategy_run_id": normalized_strategy_run_id,
            "display_name": display_name,
            "mode": "paper",
            "strategy_tag": strategy_tag,
            "algo_instance_id": algo_instance_id,
            "strategy_family": str(latest_metadata.get("strategy_family") or "indicator_strategy"),
            "strategy_name": str(latest_metadata.get("strategy_name") or display_name),
            "status": "open" if open_leg_count else "closed",
            "is_open": bool(open_leg_count),
            "leg_count": len(legs_payload),
            "open_leg_count": open_leg_count,
            "net_quantity": net_quantity_total,
            "unrealized_pnl": unrealized_total,
            "realized_pnl": realized_total,
            "charges": charges_total,
            "is_stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "margin_in_use": 0.0,
            "last_updated_at": last_updated_at,
            "last_event_at": latest_event_at or last_updated_at,
            "summary_fields": [],
            "capabilities": {
                "can_edit_risk": bool(latest_metadata.get("can_edit_risk", False)),
                "edit_risk_reason": latest_metadata.get("edit_risk_reason") or "No editable risk controls available",
                "can_exit_strategy": True,
                "exit_reason": latest_metadata.get("exit_reason") or None,
            },
            "positions": legs_payload,
            "orders": [
                {
                    "order_id": order.order_id,
                    "tradingsymbol": order.tradingsymbol,
                    "transaction_type": str(order.transaction_type),
                    "quantity": int(order.quantity),
                    "status": str(order.status),
                    "average_price": float(order.average_price) if order.average_price is not None else None,
                    "placed_at": order.placed_at.isoformat() if order.placed_at else None,
                    "metadata": dict(order.metadata or {}),
                }
                for order in relevant_orders
            ],
            "trades": [
                {
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "instrument_token": trade.instrument_token,
                    "transaction_type": str(trade.transaction_type),
                    "quantity": int(trade.quantity),
                    "price": float(trade.price),
                    "trade_timestamp": trade.trade_timestamp.isoformat() if trade.trade_timestamp else None,
                    "metadata": dict(trade.metadata or {}),
                }
                for trade in relevant_trades
            ],
            "timeline": sorted(timeline, key=lambda item: (item.get("timestamp") or "", item.get("kind") or "")),
        }
