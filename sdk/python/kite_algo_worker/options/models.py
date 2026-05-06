from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import ModelMixin


@dataclass(frozen=True)
class OptionExpirySnapshot(ModelMixin):
    underlying: str
    expiries: List[str] = field(default_factory=list)
    spot_ltp: Optional[float] = None
    updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", str(self.underlying).upper())
        object.__setattr__(self, "expiries", [str(item) for item in list(self.expiries or [])])
        if self.spot_ltp is not None:
            object.__setattr__(self, "spot_ltp", float(self.spot_ltp))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", str(self.updated_at))


@dataclass(frozen=True)
class OptionEntryPreviewRequest(ModelMixin):
    strategy_run_id: str
    orders: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    all_or_none: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "orders", [dict(order) for order in list(self.orders or [])])
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "all_or_none", bool(self.all_or_none))


@dataclass(frozen=True)
class OptionRunCreateRequest(ModelMixin):
    strategy_name: str
    product: str
    legs: List["OptionExecutionLeg"] = field(default_factory=list)
    protection: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_name", str(self.strategy_name))
        product = str(self.product or "").strip().upper()
        object.__setattr__(self, "product", product)
        normalized_legs: List[OptionExecutionLeg] = []
        for leg in list(self.legs or []):
            item = leg if isinstance(leg, OptionExecutionLeg) else OptionExecutionLeg.model_validate(leg)
            if item.product is None:
                item = OptionExecutionLeg.model_validate({**item.model_dump(exclude_none=True), "product": product})
            normalized_legs.append(item)
        object.__setattr__(self, "legs", normalized_legs)
        if self.protection is not None:
            object.__setattr__(self, "protection", dict(self.protection))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class OptionExecutionLeg(ModelMixin):
    leg_id: Optional[str] = None
    tradingsymbol: str = ""
    transaction_type: str = ""
    quantity: int = 0
    exchange: str = "NFO"
    product: Optional[str] = None
    instrument_token: Optional[int] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    expiry_key: Optional[str] = None
    ltp: Optional[float] = None
    lot_size: Optional[int] = None
    lots: Optional[int] = None
    order_type: str = "MARKET"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    market_protection: Optional[int] = None
    exit_order_type: Optional[str] = None
    exit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "leg_id", str(self.leg_id or f"leg_{uuid.uuid4().hex[:8]}"))
        object.__setattr__(self, "tradingsymbol", str(self.tradingsymbol).strip().upper())
        object.__setattr__(self, "transaction_type", str(self.transaction_type).strip().upper())
        object.__setattr__(self, "quantity", int(self.quantity))
        object.__setattr__(self, "exchange", str(self.exchange or "NFO").strip().upper())
        object.__setattr__(self, "product", None if self.product is None else str(self.product).strip().upper())
        object.__setattr__(self, "instrument_token", None if self.instrument_token is None else int(self.instrument_token))
        object.__setattr__(self, "strike", None if self.strike is None else float(self.strike))
        object.__setattr__(self, "option_type", None if self.option_type is None else str(self.option_type).strip().upper())
        object.__setattr__(self, "expiry_key", None if self.expiry_key is None else str(self.expiry_key).strip())
        object.__setattr__(self, "ltp", None if self.ltp is None else float(self.ltp))
        object.__setattr__(self, "lot_size", None if self.lot_size is None else int(self.lot_size))
        object.__setattr__(self, "lots", None if self.lots is None else int(self.lots))
        object.__setattr__(self, "order_type", str(self.order_type or "MARKET").strip().upper())
        object.__setattr__(self, "price", None if self.price is None else float(self.price))
        object.__setattr__(self, "trigger_price", None if self.trigger_price is None else float(self.trigger_price))
        object.__setattr__(self, "market_protection", None if self.market_protection is None else int(self.market_protection))
        object.__setattr__(self, "exit_order_type", None if self.exit_order_type is None else str(self.exit_order_type).strip().upper())
        object.__setattr__(self, "exit_price", None if self.exit_price is None else float(self.exit_price))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class OptionRunActionRequest(ModelMixin):
    mode: Optional[str] = None
    execution_mode: Optional[str] = None
    account_scope: Optional[str] = None
    all_or_none: bool = False
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    order_results: Optional[List[Dict[str, Any]]] = None
    trade_results: Optional[List[Dict[str, Any]]] = None
    safety_token: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", None if self.mode is None else str(self.mode).strip())
        object.__setattr__(self, "execution_mode", None if self.execution_mode is None else str(self.execution_mode).strip())
        object.__setattr__(self, "account_scope", None if self.account_scope is None else str(self.account_scope).strip())
        object.__setattr__(self, "all_or_none", bool(self.all_or_none))
        object.__setattr__(self, "idempotency_key", None if self.idempotency_key is None else str(self.idempotency_key).strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "order_results", None if self.order_results is None else [dict(item) for item in list(self.order_results)])
        object.__setattr__(self, "trade_results", None if self.trade_results is None else [dict(item) for item in list(self.trade_results)])
        object.__setattr__(self, "safety_token", None if self.safety_token is None else str(self.safety_token))
