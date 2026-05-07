from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Mapping, Optional, cast


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


class RawModelMixin(ModelMixin):
    @classmethod
    def model_validate(cls, payload: Any):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__} requires a mapping payload")
        aliases = getattr(cls, "__field_aliases__", {})
        data = {aliases.get(key, key): value for key, value in dict(payload).items()}
        known = {item.name for item in fields(cast(Any, cls))}
        kwargs = {key: data[key] for key in known if key != "raw" and key in data}
        kwargs["raw"] = {key: value for key, value in data.items() if key not in known}
        return cls(**kwargs)

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        payload = super().model_dump(exclude_none=exclude_none, mode=mode)
        payload.pop("raw", None)
        raw = dict(getattr(self, "raw", {}) or {})
        if raw:
            payload.update(raw)
        reverse_aliases = {value: key for key, value in getattr(type(self), "__field_aliases__", {}).items()}
        for internal_name, external_name in reverse_aliases.items():
            if internal_name in payload:
                payload[external_name] = payload.pop(internal_name)
        return payload


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
class RunProtectionState(RawModelMixin):
    status: Optional[str] = None
    generation: Optional[int] = None
    triggered_rule: Optional[str] = None
    exit_claim_id: Optional[str] = None
    reason: Optional[str] = None
    reset_trailing: Optional[bool] = None
    backend_protection: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class WorkerRunHealthSnapshot(ModelMixin):
    strategy_run_id: str
    status: str
    execution_mode: str
    account_scope: Optional[str] = None
    heartbeat_age_sec: Optional[int] = None
    health_status: Optional[str] = None
    session_status: Optional[str] = None
    recovery_status: Optional[str] = None
    recovery_action_required: bool = False
    worker_session_claimed_at: Optional[str] = None
    last_heartbeat_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "status", str(self.status))
        object.__setattr__(self, "execution_mode", str(self.execution_mode))
        object.__setattr__(self, "account_scope", None if self.account_scope is None else str(self.account_scope))
        object.__setattr__(self, "heartbeat_age_sec", _coerce_optional_int(self.heartbeat_age_sec))
        object.__setattr__(self, "health_status", None if self.health_status is None else str(self.health_status))
        object.__setattr__(self, "session_status", None if self.session_status is None else str(self.session_status))
        object.__setattr__(self, "recovery_status", None if self.recovery_status is None else str(self.recovery_status))
        object.__setattr__(self, "recovery_action_required", _coerce_bool(self.recovery_action_required))
        object.__setattr__(self, "worker_session_claimed_at", None if self.worker_session_claimed_at is None else str(self.worker_session_claimed_at))
        object.__setattr__(self, "last_heartbeat_at", None if self.last_heartbeat_at is None else str(self.last_heartbeat_at))


@dataclass(frozen=True)
class WorkerGttWriteResult(ModelMixin):
    trigger_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_id", _coerce_int(self.trigger_id))


@dataclass(frozen=True)
class WorkerGttTrigger(RawModelMixin):
    id: int
    user_id: Optional[str] = None
    parent_trigger: Optional[int] = None
    type: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    status: Optional[str] = None
    condition: Dict[str, Any] = field(default_factory=dict)
    orders: List[Dict[str, Any]] = field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _coerce_int(self.id))
        object.__setattr__(self, "user_id", None if self.user_id is None else str(self.user_id))
        object.__setattr__(self, "parent_trigger", _coerce_optional_int(self.parent_trigger))
        object.__setattr__(self, "type", None if self.type is None else str(self.type))
        object.__setattr__(self, "created_at", None if self.created_at is None else str(self.created_at))
        object.__setattr__(self, "updated_at", None if self.updated_at is None else str(self.updated_at))
        object.__setattr__(self, "expires_at", None if self.expires_at is None else str(self.expires_at))
        object.__setattr__(self, "status", None if self.status is None else str(self.status))
        object.__setattr__(self, "condition", dict(self.condition or {}))
        object.__setattr__(self, "orders", [dict(item) for item in list(self.orders or [])])
        object.__setattr__(self, "meta", dict(self.meta or {}) if self.meta is not None else None)
        object.__setattr__(self, "raw", dict(self.raw or {}))

@dataclass(frozen=True)
class WorkerCandle(RawModelMixin):
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: Optional[float] = None
    is_complete: bool = True
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", str(self.ts))
        object.__setattr__(self, "open", _coerce_float(self.open))
        object.__setattr__(self, "high", _coerce_float(self.high))
        object.__setattr__(self, "low", _coerce_float(self.low))
        object.__setattr__(self, "close", _coerce_float(self.close))
        object.__setattr__(self, "volume", _coerce_float(self.volume))
        object.__setattr__(self, "oi", _coerce_optional_float(self.oi))
        object.__setattr__(self, "is_complete", _coerce_bool(self.is_complete, default=True))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerHistoricalCandles(RawModelMixin):
    __field_aliases__ = {"from": "from_ts", "to": "to_ts"}

    symbol: Optional[str] = None
    instrument_token: Optional[int] = None
    interval: str = ""
    timeframe: Optional[str] = None
    from_ts: Optional[str] = None
    to_ts: Optional[str] = None
    count: Optional[int] = None
    source: Optional[str] = None
    ingestion: Optional[Dict[str, Any]] = None
    current: Optional[WorkerCandle] = None
    candles: List[WorkerCandle] = field(default_factory=list)
    is_stale: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", None if self.symbol is None else str(self.symbol))
        object.__setattr__(self, "instrument_token", _coerce_optional_int(self.instrument_token))
        object.__setattr__(self, "interval", str(self.interval))
        object.__setattr__(self, "timeframe", None if self.timeframe is None else str(self.timeframe))
        object.__setattr__(self, "from_ts", None if self.from_ts is None else str(self.from_ts))
        object.__setattr__(self, "to_ts", None if self.to_ts is None else str(self.to_ts))
        object.__setattr__(self, "count", _coerce_optional_int(self.count))
        object.__setattr__(self, "source", None if self.source is None else str(self.source))
        object.__setattr__(self, "ingestion", dict(self.ingestion or {}) if self.ingestion is not None else None)
        current = self.current if isinstance(self.current, WorkerCandle) else (WorkerCandle.model_validate(self.current) if self.current is not None else None)
        candles = [c if isinstance(c, WorkerCandle) else WorkerCandle.model_validate(c) for c in list(self.candles or [])]
        object.__setattr__(self, "current", current)
        object.__setattr__(self, "candles", candles)
        object.__setattr__(self, "is_stale", _coerce_bool(self.is_stale))
        object.__setattr__(self, "raw", dict(self.raw or {}))

    def model_dump(self, *, exclude_none: bool = True, mode: str = "python") -> Dict[str, Any]:
        payload = super().model_dump(exclude_none=exclude_none, mode=mode)
        if self.raw:
            payload.update(self.raw)
        return payload


@dataclass(frozen=True)
class WorkerOrderSnapshot(RawModelMixin):
    order_id: str
    status: str
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    instrument_token: Optional[int] = None
    transaction_type: Optional[str] = None
    product: Optional[str] = None
    variety: Optional[str] = None
    order_type: Optional[str] = None
    validity: Optional[str] = None
    quantity: Optional[int] = None
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    average_price: Optional[float] = None
    filled_quantity: Optional[int] = None
    pending_quantity: Optional[int] = None
    cancelled_quantity: Optional[int] = None
    order_timestamp: Optional[str] = None
    exchange_update_timestamp: Optional[str] = None
    exchange_timestamp: Optional[str] = None
    status_message: Optional[str] = None
    status_message_raw: Optional[str] = None
    parent_order_id: Optional[str] = None
    tag: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", str(self.order_id))
        object.__setattr__(self, "status", str(self.status))
        object.__setattr__(self, "exchange", None if self.exchange is None else str(self.exchange))
        object.__setattr__(self, "tradingsymbol", None if self.tradingsymbol is None else str(self.tradingsymbol))
        object.__setattr__(self, "instrument_token", _coerce_optional_int(self.instrument_token))
        object.__setattr__(self, "transaction_type", None if self.transaction_type is None else str(self.transaction_type))
        object.__setattr__(self, "product", None if self.product is None else str(self.product))
        object.__setattr__(self, "variety", None if self.variety is None else str(self.variety))
        object.__setattr__(self, "order_type", None if self.order_type is None else str(self.order_type))
        object.__setattr__(self, "validity", None if self.validity is None else str(self.validity))
        object.__setattr__(self, "quantity", _coerce_optional_int(self.quantity))
        object.__setattr__(self, "price", _coerce_optional_float(self.price))
        object.__setattr__(self, "trigger_price", _coerce_optional_float(self.trigger_price))
        object.__setattr__(self, "average_price", _coerce_optional_float(self.average_price))
        object.__setattr__(self, "filled_quantity", _coerce_optional_int(self.filled_quantity))
        object.__setattr__(self, "pending_quantity", _coerce_optional_int(self.pending_quantity))
        object.__setattr__(self, "cancelled_quantity", _coerce_optional_int(self.cancelled_quantity))
        object.__setattr__(self, "order_timestamp", None if self.order_timestamp is None else str(self.order_timestamp))
        object.__setattr__(self, "exchange_update_timestamp", None if self.exchange_update_timestamp is None else str(self.exchange_update_timestamp))
        object.__setattr__(self, "exchange_timestamp", None if self.exchange_timestamp is None else str(self.exchange_timestamp))
        object.__setattr__(self, "status_message", None if self.status_message is None else str(self.status_message))
        object.__setattr__(self, "status_message_raw", None if self.status_message_raw is None else str(self.status_message_raw))
        object.__setattr__(self, "parent_order_id", None if self.parent_order_id is None else str(self.parent_order_id))
        object.__setattr__(self, "tag", None if self.tag is None else str(self.tag))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerTradeSnapshot(RawModelMixin):
    trade_id: str
    order_id: str
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    instrument_token: Optional[int] = None
    transaction_type: Optional[str] = None
    product: Optional[str] = None
    average_price: Optional[float] = None
    quantity: Optional[int] = None
    order_timestamp: Optional[str] = None
    exchange_timestamp: Optional[str] = None
    fill_timestamp: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", str(self.trade_id))
        object.__setattr__(self, "order_id", str(self.order_id))
        object.__setattr__(self, "exchange", None if self.exchange is None else str(self.exchange))
        object.__setattr__(self, "tradingsymbol", None if self.tradingsymbol is None else str(self.tradingsymbol))
        object.__setattr__(self, "instrument_token", _coerce_optional_int(self.instrument_token))
        object.__setattr__(self, "transaction_type", None if self.transaction_type is None else str(self.transaction_type))
        object.__setattr__(self, "product", None if self.product is None else str(self.product))
        object.__setattr__(self, "average_price", _coerce_optional_float(self.average_price))
        object.__setattr__(self, "quantity", _coerce_optional_int(self.quantity))
        object.__setattr__(self, "order_timestamp", None if self.order_timestamp is None else str(self.order_timestamp))
        object.__setattr__(self, "exchange_timestamp", None if self.exchange_timestamp is None else str(self.exchange_timestamp))
        object.__setattr__(self, "fill_timestamp", None if self.fill_timestamp is None else str(self.fill_timestamp))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerOrdersResponse(ModelMixin):
    strategy_run_id: str
    orders: List[WorkerOrderSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        orders = [order if isinstance(order, WorkerOrderSnapshot) else WorkerOrderSnapshot.model_validate(order) for order in list(self.orders or [])]
        object.__setattr__(self, "orders", orders)


@dataclass(frozen=True)
class WorkerTradesResponse(ModelMixin):
    strategy_run_id: str
    trades: List[WorkerTradeSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        trades = [trade if isinstance(trade, WorkerTradeSnapshot) else WorkerTradeSnapshot.model_validate(trade) for trade in list(self.trades or [])]
        object.__setattr__(self, "trades", trades)


@dataclass(frozen=True)
class SafetyCheckResult(ModelMixin):
    strategy_run_id: str
    can_trade: bool
    run_status: str
    safety_token: str | None = None
    token_expires_at: str | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    generic_protection: dict[str, Any] = field(default_factory=dict)
    options_protection: dict[str, Any] = field(default_factory=dict)
    evaluated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "can_trade", bool(self.can_trade))
        object.__setattr__(self, "run_status", str(self.run_status))
        object.__setattr__(self, "safety_token", None if self.safety_token is None else str(self.safety_token))
        object.__setattr__(self, "token_expires_at", None if self.token_expires_at is None else str(self.token_expires_at))
        object.__setattr__(self, "blocking_reasons", [str(item) for item in list(self.blocking_reasons or [])])
        object.__setattr__(self, "generic_protection", dict(self.generic_protection or {}))
        object.__setattr__(self, "options_protection", dict(self.options_protection or {}))
        object.__setattr__(self, "evaluated_at", str(self.evaluated_at))


@dataclass(frozen=True)
class WorkerTimelineEvent(ModelMixin):
    cursor: int
    strategy_run_id: str
    account_id: str
    basket_execution_id: Optional[str] = None
    event_kind: str = "execution"
    event_source: str = "legacy_execution"
    event_type: str = ""
    related_resource_type: Optional[str] = None
    related_resource_id: Optional[str] = None
    summary: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cursor", _coerce_int(self.cursor))
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "account_id", str(self.account_id))
        object.__setattr__(self, "basket_execution_id", None if self.basket_execution_id is None else str(self.basket_execution_id))
        object.__setattr__(self, "event_kind", str(self.event_kind))
        object.__setattr__(self, "event_source", str(self.event_source))
        object.__setattr__(self, "event_type", str(self.event_type))
        object.__setattr__(self, "related_resource_type", None if self.related_resource_type is None else str(self.related_resource_type))
        object.__setattr__(self, "related_resource_id", None if self.related_resource_id is None else str(self.related_resource_id))
        object.__setattr__(self, "summary", None if self.summary is None else str(self.summary))
        object.__setattr__(self, "payload", dict(self.payload or {}))


@dataclass(frozen=True)
class WorkerTimelineResponse(ModelMixin):
    strategy_run_id: str
    after_cursor: int
    last_cursor: int
    events: List[WorkerTimelineEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_run_id", str(self.strategy_run_id))
        object.__setattr__(self, "after_cursor", _coerce_int(self.after_cursor))
        object.__setattr__(self, "last_cursor", _coerce_int(self.last_cursor))
        normalized_events = [
            event if isinstance(event, WorkerTimelineEvent) else WorkerTimelineEvent.model_validate(event)
            for event in list(self.events or [])
        ]
        object.__setattr__(self, "events", normalized_events)


__all__ = [
    "CostContract",
    "WorkerCandle",
    "WorkerHistoricalCandles",
    "WorkerOrderSnapshot",
    "WorkerTradeSnapshot",
    "OrderPreview",
    "PreviewPayload",
    "WorkerFundsSegment",
    "WorkerFundsSnapshot",
    "WorkerRunPnlLeg",
    "WorkerRunPnlSnapshot",
    "WorkerRunPnlTotals",
    "RunProtectionState",
    "SafetyCheckResult",
    "WorkerTimelineEvent",
    "WorkerTimelineResponse",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerTradesResponse",
]
