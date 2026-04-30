from __future__ import annotations

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
    legs: List[Dict[str, Any]] = field(default_factory=list)
    protection: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_name", str(self.strategy_name))
        object.__setattr__(self, "product", str(self.product))
        object.__setattr__(self, "legs", [dict(leg) for leg in list(self.legs or [])])
        if self.protection is not None:
            object.__setattr__(self, "protection", dict(self.protection))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
