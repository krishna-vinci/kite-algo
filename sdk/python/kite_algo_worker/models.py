from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Mapping, Optional


def _dump_value(value: Any, *, exclude_none: bool) -> Any:
    if is_dataclass(value):
        payload: Dict[str, Any] = {}
        for item in fields(value):
            field_value = getattr(value, item.name)
            if exclude_none and field_value is None:
                continue
            payload[item.name] = _dump_value(field_value, exclude_none=exclude_none)
        return payload
    if isinstance(value, dict):
        return {key: _dump_value(item, exclude_none=exclude_none) for key, item in value.items() if not (exclude_none and item is None)}
    if isinstance(value, list):
        return [_dump_value(item, exclude_none=exclude_none) for item in value]
    if isinstance(value, tuple):
        return [_dump_value(item, exclude_none=exclude_none) for item in value]
    return value


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return bool(value)


class ModelMixin:
    @classmethod
    def model_validate(cls, payload: Any):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} requires a mapping payload")
        return cls(**dict(payload))

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        _ = mode
        return _dump_value(self, exclude_none=exclude_none)


@dataclass(frozen=True)
class CostContract(ModelMixin):
    margin_required: Any = None
    charges_estimate: Any = None
    total_charges: Any = None
    total_taxes: Any = None


@dataclass(frozen=True)
class WorkerFundsSegment(ModelMixin):
    net: float = 0.0
    available_cash: float = 0.0
    opening_balance: float = 0.0
    live_balance: Optional[float] = None
    collateral: Optional[float] = None
    utilised: float = 0.0
    m2m_realised: float = 0.0
    m2m_unrealised: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "net", _coerce_float(self.net))
        object.__setattr__(self, "available_cash", _coerce_float(self.available_cash))
        object.__setattr__(self, "opening_balance", _coerce_float(self.opening_balance))
        object.__setattr__(self, "live_balance", _coerce_optional_float(self.live_balance))
        object.__setattr__(self, "collateral", _coerce_optional_float(self.collateral))
        object.__setattr__(self, "utilised", _coerce_float(self.utilised))
        object.__setattr__(self, "m2m_realised", _coerce_float(self.m2m_realised))
        object.__setattr__(self, "m2m_unrealised", _coerce_float(self.m2m_unrealised))


@dataclass(frozen=True)
class WorkerFundsSnapshot(ModelMixin):
    account_scope: str
    mode: str
    currency: str = "INR"
    source: str = ""
    segments: Dict[str, WorkerFundsSegment] = field(default_factory=dict)
    allocation: Dict[str, Any] = field(default_factory=dict)
    stale: bool = False
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_scope", str(self.account_scope))
        object.__setattr__(self, "mode", str(self.mode))
        object.__setattr__(self, "currency", str(self.currency))
        object.__setattr__(self, "source", str(self.source))
        segments = {
            str(key): value if isinstance(value, WorkerFundsSegment) else WorkerFundsSegment.model_validate(value)
            for key, value in dict(self.segments or {}).items()
        }
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "allocation", dict(self.allocation or {}))
        object.__setattr__(self, "stale", _coerce_bool(self.stale))
        object.__setattr__(self, "updated_at", str(self.updated_at))


@dataclass(frozen=True)
class WorkerRunPnlTotals(ModelMixin):
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "realized_pnl", _coerce_float(self.realized_pnl))
        object.__setattr__(self, "unrealized_pnl", _coerce_float(self.unrealized_pnl))
        object.__setattr__(self, "gross_pnl", _coerce_float(self.gross_pnl))
        object.__setattr__(self, "charges", _coerce_float(self.charges))
        object.__setattr__(self, "net_pnl", _coerce_float(self.net_pnl))


@dataclass(frozen=True)
class WorkerRunPnlLeg(ModelMixin):
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    net_quantity: int = 0
    side: str = "FLAT"
    average_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    broker_net_quantity: Optional[int] = None
    is_stale: bool = False
    last_reconciled_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_token", _coerce_optional_int(self.instrument_token))
        object.__setattr__(self, "exchange", None if self.exchange is None else str(self.exchange))
        object.__setattr__(self, "tradingsymbol", None if self.tradingsymbol is None else str(self.tradingsymbol))
        object.__setattr__(self, "product", None if self.product is None else str(self.product))
        object.__setattr__(self, "net_quantity", _coerce_int(self.net_quantity))
        object.__setattr__(self, "side", str(self.side))
        object.__setattr__(self, "average_price", _coerce_float(self.average_price))
        object.__setattr__(self, "last_price", _coerce_float(self.last_price))
        object.__setattr__(self, "realized_pnl", _coerce_float(self.realized_pnl))
        object.__setattr__(self, "unrealized_pnl", _coerce_float(self.unrealized_pnl))
        object.__setattr__(self, "gross_pnl", _coerce_float(self.gross_pnl))
        object.__setattr__(self, "charges", _coerce_float(self.charges))
        object.__setattr__(self, "net_pnl", _coerce_float(self.net_pnl))
        object.__setattr__(self, "broker_net_quantity", _coerce_optional_int(self.broker_net_quantity))
        object.__setattr__(self, "is_stale", _coerce_bool(self.is_stale))
        object.__setattr__(self, "last_reconciled_at", None if self.last_reconciled_at is None else str(self.last_reconciled_at))


@dataclass(frozen=True)
class WorkerRunPnlSnapshot(ModelMixin):
    strategy_run_id: str
    execution_mode: str
    status: str
    currency: str = "INR"
    totals: WorkerRunPnlTotals = field(default_factory=WorkerRunPnlTotals)
    legs: List[WorkerRunPnlLeg] = field(default_factory=list)
    position_count: int = 0
    is_realtime: bool = False
    is_stale: bool = False
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "execution_mode", str(self.execution_mode))
        object.__setattr__(self, "status", str(self.status))
        object.__setattr__(self, "currency", str(self.currency))
        totals = self.totals if isinstance(self.totals, WorkerRunPnlTotals) else WorkerRunPnlTotals.model_validate(self.totals)
        legs = [leg if isinstance(leg, WorkerRunPnlLeg) else WorkerRunPnlLeg.model_validate(leg) for leg in list(self.legs or [])]
        object.__setattr__(self, "totals", totals)
        object.__setattr__(self, "legs", legs)
        object.__setattr__(self, "position_count", _coerce_int(self.position_count))
        object.__setattr__(self, "is_realtime", _coerce_bool(self.is_realtime))
        object.__setattr__(self, "is_stale", _coerce_bool(self.is_stale))
        object.__setattr__(self, "updated_at", str(self.updated_at))


@dataclass(frozen=True)
class PreviewPayload(ModelMixin):
    intent_type: str
    order: Optional[Dict[str, Any]] = None
    basket: Optional[Dict[str, Any]] = None
    cost_contract: Optional[CostContract] = None

    def __post_init__(self) -> None:
        order = dict(self.order or {}) if self.order is not None else None
        basket = dict(self.basket or {}) if self.basket is not None else None
        cost_contract = self.cost_contract
        if cost_contract is not None and not isinstance(cost_contract, CostContract):
            cost_contract = CostContract.model_validate(cost_contract)
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "basket", basket)
        object.__setattr__(self, "cost_contract", cost_contract)


@dataclass(frozen=True)
class OrderPreview(ModelMixin):
    strategy_run_id: str
    mode: str
    preview: PreviewPayload

    def __post_init__(self) -> None:
        preview = self.preview
        if not isinstance(preview, PreviewPayload):
            preview = PreviewPayload.model_validate(preview)
        object.__setattr__(self, "preview", preview)


@dataclass(frozen=True)
class WorkerOrderResult(ModelMixin):
    mode: str
    result: Dict[str, Any]
    strategy_run_id: Optional[str] = None
    order_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", dict(self.result or {}))


@dataclass(frozen=True)
class RunProtectionState(ModelMixin):
    status: Optional[str] = None
    generation: Optional[int] = None
    triggered_rule: Optional[str] = None
    exit_claim_id: Optional[str] = None
    reason: Optional[str] = None
    reset_trailing: Optional[bool] = None
    backend_protection: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def model_validate(cls, payload: Any):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} requires a mapping payload")
        data = dict(payload)
        known = {field.name for field in fields(cls)}
        raw = {key: value for key, value in data.items() if key not in known}
        kwargs = {key: data[key] for key in known if key != "raw" and key in data}
        kwargs["raw"] = raw
        return cls(**kwargs)

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        payload = super().model_dump(exclude_none=exclude_none, mode=mode)
        payload.pop("raw", None)
        if self.raw:
            payload.update(self.raw)
        return payload


@dataclass(frozen=True)
class WorkerOrdersResponse(ModelMixin):
    strategy_run_id: str
    orders: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WorkerTradesResponse(ModelMixin):
    strategy_run_id: str
    trades: List[Dict[str, Any]] = field(default_factory=list)


__all__ = [
    "CostContract",
    "OrderPreview",
    "PreviewPayload",
    "WorkerFundsSegment",
    "WorkerFundsSnapshot",
    "WorkerRunPnlLeg",
    "WorkerRunPnlSnapshot",
    "WorkerRunPnlTotals",
    "RunProtectionState",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerTradesResponse",
]
