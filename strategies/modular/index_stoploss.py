from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algo_runtime.models import NoopAction, NotifyAction, OrderIntent, Snapshot, StatePatchAction


class IndexStoplossConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    index_token: int
    upper_stoploss: Optional[float] = None
    lower_stoploss: Optional[float] = None
    exit_order_type: Literal["MARKET"] = "MARKET"
    order_variety: Literal["regular"] = "regular"
    product_override: Optional[Literal["CNC", "MIS", "NRML", "MTF"]] = None
    order_tag: Optional[str] = None
    all_or_none: bool = False
    dry_run: bool = False
    auto_disable_after_trigger: bool = True

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
    def validate_stoploss_bounds(self) -> "IndexStoplossConfig":
        if self.upper_stoploss is None and self.lower_stoploss is None:
            raise ValueError("At least one of upper_stoploss or lower_stoploss must be set")
        if self.upper_stoploss is not None and self.lower_stoploss is not None and self.lower_stoploss > self.upper_stoploss:
            raise ValueError("lower_stoploss cannot be greater than upper_stoploss")
        return self


class ModularIndexStoplossAlgo:
    ALGO_TYPE = "index_stoploss"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.config = IndexStoplossConfig.model_validate(instance.config)

    async def initialize(self, context: Any) -> None:
        return None

    async def evaluate(self, snapshot: Snapshot, state: Dict[str, Any]) -> List[Any]:
        if state.get("triggered"):
            return [NoopAction(reason="already_triggered")]

        current_price = self._current_index_price(snapshot)
        if current_price is None:
            return [NoopAction(reason="missing_index_price")]

        trigger_reason = self._trigger_reason(current_price)
        if trigger_reason is None:
            return [StatePatchAction(patch={"last_seen_price": current_price, "last_evaluated_at": self._utcnow()})]

        actions: List[Any] = [
            NotifyAction(
                message=f"Index stoploss triggered for {self.instance.instance_id}: {trigger_reason} at {current_price}",
                level="warning",
                metadata={
                    "instance_id": self.instance.instance_id,
                    "trigger_reason": trigger_reason,
                    "trigger_price": current_price,
                },
            )
        ]

        exit_orders, skipped_positions = self._build_exit_orders(snapshot)
        if exit_orders:
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
            if skipped_positions:
                actions.append(
                    NotifyAction(
                        message=f"Skipped {skipped_positions} invalid position(s) while building exit basket for {self.instance.instance_id}",
                        level="warning",
                        metadata={"instance_id": self.instance.instance_id, "skipped_positions": skipped_positions},
                    )
                )
        else:
            actions.append(
                NotifyAction(
                    message=f"Index stoploss triggered for {self.instance.instance_id}, but no filtered positions were available to exit",
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "trigger_reason": trigger_reason},
                )
            )

        patch = {
            "triggered": True,
            "trigger_reason": trigger_reason,
            "trigger_price": current_price,
            "triggered_at": self._utcnow(),
            "last_seen_price": current_price,
        }
        if not self.config.auto_disable_after_trigger:
            patch["triggered"] = False
        actions.append(StatePatchAction(patch=patch))
        return actions

    def _current_index_price(self, snapshot: Snapshot) -> Optional[float]:
        market_ltp = snapshot.market.get("ltp", {})
        if str(self.config.index_token) in market_ltp:
            return float(market_ltp[str(self.config.index_token)])
        tick = snapshot.market.get("ticks", {}).get(str(self.config.index_token))
        if isinstance(tick, dict) and tick.get("last_price") is not None:
            return float(tick["last_price"])
        return None

    def _trigger_reason(self, current_price: float) -> Optional[str]:
        if self.config.upper_stoploss is not None and current_price >= self.config.upper_stoploss:
            return "index_upper_stoploss_triggered"
        if self.config.lower_stoploss is not None and current_price <= self.config.lower_stoploss:
            return "index_lower_stoploss_triggered"
        return None

    def _build_exit_orders(self, snapshot: Snapshot) -> Tuple[List[Dict[str, Any]], int]:
        positions_container = snapshot.positions or {}
        positions = positions_container["filtered"] if "filtered" in positions_container else positions_container.get("all", {}) or {}
        exit_orders: List[Dict[str, Any]] = []
        skipped_positions = 0
        for position in positions.values():
            payload = position if isinstance(position, dict) else position.model_dump(mode="json")
            quantity = int(payload.get("quantity") or 0)
            tradingsymbol = payload.get("tradingsymbol")
            if quantity == 0 or not tradingsymbol:
                skipped_positions += 1
                continue
            transaction_type = "SELL" if quantity > 0 else "BUY"
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
        return exit_orders, skipped_positions

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
