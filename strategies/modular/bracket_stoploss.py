from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algo_runtime.models import NoopAction, NotifyAction, OrderIntent, Snapshot, StatePatchAction


TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}


class BracketStoplossConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    trigger_token: int
    direction: Literal["long", "short"]
    entry_price: float
    stop_distance: float
    target_distance: float
    trailing_distance: Optional[float] = None
    trailing_activation_distance: Optional[float] = None
    exit_order_type: Literal["MARKET"] = "MARKET"
    order_variety: Literal["regular"] = "regular"
    product_override: Optional[Literal["CNC", "MIS", "NRML", "MTF"]] = None
    order_tag: Optional[str] = None
    all_or_none: bool = False
    dry_run: bool = False
    auto_disable_after_trigger: bool = True
    skip_if_exit_order_open: bool = True

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("session_id is required")
        return cleaned

    @field_validator("order_tag")
    @classmethod
    def validate_order_tag(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > 20:
            raise ValueError("order_tag must be 20 characters or less")
        if not re.match(r"^[A-Za-z0-9:_-]*$", cleaned):
            raise ValueError("order_tag contains invalid characters")
        return cleaned

    @model_validator(mode="after")
    def validate_bracket_distances(self) -> "BracketStoplossConfig":
        if self.entry_price <= 0:
            raise ValueError("entry_price must be > 0")
        if self.stop_distance <= 0:
            raise ValueError("stop_distance must be > 0")
        if self.target_distance <= 0:
            raise ValueError("target_distance must be > 0")
        if self.trailing_distance is not None and self.trailing_distance <= 0:
            raise ValueError("trailing_distance must be > 0")
        if self.trailing_activation_distance is not None and self.trailing_activation_distance <= 0:
            raise ValueError("trailing_activation_distance must be > 0")
        if self.trailing_distance is None and self.trailing_activation_distance is not None:
            raise ValueError("trailing_distance is required when trailing_activation_distance is set")
        return self


class ModularBracketStoplossAlgo:
    ALGO_TYPE = "bracket_stoploss"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.config = BracketStoplossConfig.model_validate(instance.config)

    async def initialize(self, context: Any) -> None:
        return None

    async def evaluate(self, snapshot: Snapshot, state: Dict[str, Any]) -> List[Any]:
        if state.get("triggered"):
            return [NoopAction(reason="already_triggered")]

        current_price = self._current_price(snapshot)
        if current_price is None:
            return [NoopAction(reason="missing_trigger_price")]

        bracket = self._current_bracket(current_price, state)
        trigger_reason = self._trigger_reason(current_price, bracket)

        if trigger_reason is None:
            return [
                StatePatchAction(
                    patch={
                        "last_seen_price": current_price,
                        "best_price": bracket["best_price"],
                        "active_stop_price": bracket["stop_price"],
                        "target_price": bracket["target_price"],
                        "last_evaluated_at": self._utcnow(),
                    }
                )
            ]

        actions: List[Any] = [
            NotifyAction(
                message=(
                    f"Bracket stoploss triggered for {self.instance.instance_id}: "
                    f"{trigger_reason} at {current_price}"
                ),
                level="warning",
                metadata={
                    "instance_id": self.instance.instance_id,
                    "trigger_reason": trigger_reason,
                    "trigger_price": current_price,
                    "active_stop_price": bracket["stop_price"],
                    "target_price": bracket["target_price"],
                },
            )
        ]

        exit_orders, skipped_positions, blocked_symbols = self._build_exit_orders(snapshot)
        if blocked_symbols and self.config.skip_if_exit_order_open and not exit_orders:
            actions.append(
                NotifyAction(
                    message=(
                        f"Bracket stoploss triggered for {self.instance.instance_id}, but existing exit orders are already open "
                        f"for {', '.join(sorted(blocked_symbols))}"
                    ),
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "blocked_symbols": sorted(blocked_symbols)},
                )
            )
        elif exit_orders:
            actions.append(
                OrderIntent(
                    intent_type="place_basket",
                    dedupe_key=f"{self.instance.instance_id}:{trigger_reason}",
                    payload={
                        "session_id": self.config.session_id,
                        "basket": {
                            "orders": exit_orders,
                            "all_or_none": self.config.all_or_none,
                            "dry_run": self.config.dry_run,
                        },
                        "trigger_reason": trigger_reason,
                        "trigger_price": current_price,
                    },
                )
            )
        else:
            actions.append(
                NotifyAction(
                    message=(
                        f"Bracket stoploss triggered for {self.instance.instance_id}, but no filtered positions were available to exit"
                    ),
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "trigger_reason": trigger_reason},
                )
            )

        if skipped_positions:
            actions.append(
                NotifyAction(
                    message=(
                        f"Skipped {skipped_positions} invalid position(s) while building exit basket for {self.instance.instance_id}"
                    ),
                    level="warning",
                    metadata={"instance_id": self.instance.instance_id, "skipped_positions": skipped_positions},
                )
            )

        patch = {
            "triggered": True,
            "trigger_reason": trigger_reason,
            "trigger_price": current_price,
            "triggered_at": self._utcnow(),
            "best_price": bracket["best_price"],
            "active_stop_price": bracket["stop_price"],
            "target_price": bracket["target_price"],
            "last_seen_price": current_price,
            "blocked_symbols": sorted(blocked_symbols),
        }
        if not self.config.auto_disable_after_trigger:
            patch["triggered"] = False
        actions.append(StatePatchAction(patch=patch))
        return actions

    def _current_price(self, snapshot: Snapshot) -> Optional[float]:
        market_ltp = snapshot.market.get("ltp", {})
        if str(self.config.trigger_token) in market_ltp:
            return float(market_ltp[str(self.config.trigger_token)])
        tick = snapshot.market.get("ticks", {}).get(str(self.config.trigger_token))
        if isinstance(tick, dict) and tick.get("last_price") is not None:
            return float(tick["last_price"])
        for position in (snapshot.positions or {}).get("filtered", {}).values():
            payload = position if isinstance(position, dict) else position.model_dump(mode="json")
            if int(payload.get("instrument_token") or 0) == self.config.trigger_token and payload.get("last_price") is not None:
                return float(payload["last_price"])
        return None

    def _current_bracket(self, current_price: float, state: Dict[str, Any]) -> Dict[str, float]:
        entry_price = float(self.config.entry_price)
        if self.config.direction == "long":
            best_price = max(float(state.get("best_price") or entry_price), current_price)
            stop_price = entry_price - float(self.config.stop_distance)
            target_price = entry_price + float(self.config.target_distance)
            if self._trailing_active(best_price):
                stop_price = max(stop_price, best_price - float(self.config.trailing_distance))
            return {"best_price": best_price, "stop_price": stop_price, "target_price": target_price}

        best_price = min(float(state.get("best_price") or entry_price), current_price)
        stop_price = entry_price + float(self.config.stop_distance)
        target_price = entry_price - float(self.config.target_distance)
        if self._trailing_active(best_price):
            stop_price = min(stop_price, best_price + float(self.config.trailing_distance))
        return {"best_price": best_price, "stop_price": stop_price, "target_price": target_price}

    def _trailing_active(self, best_price: float) -> bool:
        if self.config.trailing_distance is None or self.config.trailing_activation_distance is None:
            return False
        move = best_price - float(self.config.entry_price) if self.config.direction == "long" else float(self.config.entry_price) - best_price
        return move >= float(self.config.trailing_activation_distance)

    def _trigger_reason(self, current_price: float, bracket: Dict[str, float]) -> Optional[str]:
        if self.config.direction == "long":
            if current_price <= bracket["stop_price"]:
                return "bracket_stoploss_hit"
            if current_price >= bracket["target_price"]:
                return "bracket_target_hit"
            return None
        if current_price >= bracket["stop_price"]:
            return "bracket_stoploss_hit"
        if current_price <= bracket["target_price"]:
            return "bracket_target_hit"
        return None

    def _build_exit_orders(self, snapshot: Snapshot) -> Tuple[List[Dict[str, Any]], int, set[str]]:
        positions = ((snapshot.positions or {}).get("filtered", {})) or {}
        relevant_orders = ((snapshot.orders or {}).get("relevant", [])) or []
        exit_orders: List[Dict[str, Any]] = []
        skipped_positions = 0
        blocked_symbols: set[str] = set()
        for position in positions.values():
            payload = position if isinstance(position, dict) else position.model_dump(mode="json")
            quantity = int(payload.get("quantity") or 0)
            tradingsymbol = payload.get("tradingsymbol")
            if quantity == 0 or not tradingsymbol:
                skipped_positions += 1
                continue
            transaction_type = "SELL" if quantity > 0 else "BUY"
            if self.config.skip_if_exit_order_open and self._has_open_exit_order(relevant_orders, tradingsymbol, transaction_type):
                blocked_symbols.add(tradingsymbol)
                continue
            exit_orders.append(
                {
                    "exchange": payload.get("exchange", "NFO"),
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": transaction_type,
                    "variety": self.config.order_variety,
                    "product": self.config.product_override or payload.get("product", "MIS"),
                    "order_type": self.config.exit_order_type,
                    "quantity": abs(quantity),
                    "tag": self.config.order_tag,
                }
            )
        return exit_orders, skipped_positions, blocked_symbols

    def _has_open_exit_order(self, orders: List[Dict[str, Any]], tradingsymbol: str, transaction_type: str) -> bool:
        for order in orders:
            status = str(order.get("latest_status") or "").upper()
            if status in TERMINAL_ORDER_STATUSES:
                continue
            if order.get("tradingsymbol") != tradingsymbol:
                continue
            if str(order.get("transaction_type") or "").upper() != transaction_type:
                continue
            return True
        return False

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
