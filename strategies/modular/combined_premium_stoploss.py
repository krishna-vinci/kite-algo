from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

from algo_runtime.models import NoopAction, NotifyAction, OrderIntent, Snapshot, StatePatchAction


class CombinedPremiumStoplossConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    underlying: str
    expiry_mode: Literal["nearest", "exact", "all"] = "nearest"
    entry_type: Literal["short", "long"]
    profit_target: Optional[float] = None
    stoploss: Optional[float] = None
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

    @field_validator("underlying")
    @classmethod
    def validate_underlying(cls, value: str) -> str:
        cleaned = str(value or "").strip().upper()
        if not cleaned:
            raise ValueError("underlying is required")
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


class ModularCombinedPremiumStoplossAlgo:
    ALGO_TYPE = "combined_premium_stoploss"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.config = CombinedPremiumStoplossConfig.model_validate(instance.config)

    async def initialize(self, context: Any) -> None:
        return None

    async def evaluate(self, snapshot: Snapshot, state: Dict[str, Any]) -> List[Any]:
        if state.get("triggered"):
            return [NoopAction(reason="already_triggered")]

        current_net_premium = self._current_net_premium(snapshot)
        if current_net_premium is None:
            return [NoopAction(reason="missing_positions_net_premium")]

        initial_net_premium = float(state.get("initial_net_premium") or current_net_premium)
        pnl = (
            initial_net_premium - current_net_premium
            if self.config.entry_type == "short"
            else current_net_premium - initial_net_premium
        )

        trigger_reason: Optional[str] = None
        if self.config.profit_target is not None and pnl >= float(self.config.profit_target):
            trigger_reason = "combined_premium_profit_target"
        elif self.config.stoploss is not None and pnl <= -abs(float(self.config.stoploss)):
            trigger_reason = "combined_premium_stoploss"

        if trigger_reason is None:
            return [
                StatePatchAction(
                    patch={
                        "initial_net_premium": initial_net_premium,
                        "current_net_premium": current_net_premium,
                        "net_pnl": pnl,
                        "last_evaluated_at": self._utcnow(),
                    }
                )
            ]

        actions: List[Any] = [
            NotifyAction(
                message=f"Combined premium trigger for {self.instance.instance_id}: {trigger_reason}",
                level="warning",
                metadata={
                    "instance_id": self.instance.instance_id,
                    "underlying": self.config.underlying,
                    "trigger_reason": trigger_reason,
                    "current_net_premium": current_net_premium,
                    "net_pnl": pnl,
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
                        "current_net_premium": current_net_premium,
                    },
                )
            )
            if skipped_positions:
                actions.append(
                    NotifyAction(
                        message=(
                            f"Skipped {skipped_positions} invalid position(s) while building exit basket "
                            f"for {self.instance.instance_id}"
                        ),
                        level="warning",
                        metadata={"instance_id": self.instance.instance_id, "skipped_positions": skipped_positions},
                    )
                )
        else:
            actions.append(
                NotifyAction(
                    message=(
                        f"Combined premium trigger for {self.instance.instance_id}, "
                        "but no filtered positions were available to exit"
                    ),
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "trigger_reason": trigger_reason},
                )
            )

        patch = {
            "triggered": True,
            "trigger_reason": trigger_reason,
            "triggered_at": self._utcnow(),
            "initial_net_premium": initial_net_premium,
            "current_net_premium": current_net_premium,
            "net_pnl": pnl,
            "last_evaluated_at": self._utcnow(),
        }
        if not self.config.auto_disable_after_trigger:
            patch["triggered"] = False
        actions.append(StatePatchAction(patch=patch))
        return actions

    def _current_net_premium(self, snapshot: Snapshot) -> Optional[float]:
        option_entries = (snapshot.options or {}).items()
        for key, payload in option_entries:
            if not isinstance(payload, dict):
                continue
            if not self._matches_option_key(str(key)):
                continue
            value = payload.get("positions_net_premium")
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return self._net_premium_from_positions(snapshot)

    def _matches_option_key(self, key: str) -> bool:
        parts = key.split(":", 4)
        if len(parts) < 2:
            return False
        underlying, expiry_mode = parts[0], parts[1]
        return underlying == self.config.underlying and expiry_mode == self.config.expiry_mode

    def _net_premium_from_positions(self, snapshot: Snapshot) -> Optional[float]:
        positions_container = snapshot.positions or {}
        positions = positions_container.get("filtered", {}) or positions_container.get("all", {}) or {}
        total = 0.0
        found = False
        for position in positions.values():
            payload = position if isinstance(position, dict) else position.model_dump(mode="json")
            if not self._position_matches_underlying(payload):
                continue
            quantity = int(payload.get("quantity") or 0)
            if quantity == 0:
                continue
            last_price = payload.get("last_price")
            if last_price is None:
                continue
            try:
                total += float(last_price)
                found = True
            except (TypeError, ValueError):
                continue
        return total if found else None

    def _position_matches_underlying(self, payload: Dict[str, Any]) -> bool:
        tradingsymbol = str(payload.get("tradingsymbol") or "").upper()
        return self.config.underlying in tradingsymbol if tradingsymbol else False

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
