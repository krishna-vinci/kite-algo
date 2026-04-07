from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlgoLifecycleState(str, Enum):
    ENABLED = "enabled"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class TriggerType(str, Enum):
    TICK = "tick"
    CANDLE_CLOSE = "candle_close"
    POSITION_UPDATE = "position_update"
    ORDER_UPDATE = "order_update"
    FILL_UPDATE = "fill_update"
    MANUAL = "manual"


class ActionType(str, Enum):
    NOTIFY = "notify"
    ORDER_INTENT = "order_intent"
    STATE_PATCH = "state_patch"
    NOOP = "noop"


class MarketDataMode(str, Enum):
    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"


class OptionExpiryMode(str, Enum):
    NEAREST = "nearest"
    EXACT = "exact"
    ALL = "all"


class OptionView(str, Enum):
    SNAPSHOT = "snapshot"
    MINI_CHAIN = "mini_chain"
    CHAIN = "chain"


class OrderScope(str, Enum):
    NONE = "none"
    ACCOUNT_RELEVANT = "account_relevant"
    INSTANCE_RELEVANT = "instance_relevant"


class CandleSeriesSpec(BaseModel):
    token: int
    timeframe: str
    lookback: int = 1
    include_forming: bool = False

    @field_validator("token", mode="before")
    @classmethod
    def _normalize_token(cls, value: Any) -> int:
        return int(value)

    @field_validator("timeframe")
    @classmethod
    def _normalize_timeframe(cls, value: str) -> str:
        cleaned = str(value or "").strip().lower()
        if not cleaned:
            raise ValueError("timeframe is required")
        return cleaned

    @field_validator("lookback")
    @classmethod
    def _validate_lookback(cls, value: int) -> int:
        if value < 1:
            raise ValueError("lookback must be >= 1")
        return value

    @property
    def key(self) -> str:
        return f"{self.token}:{self.timeframe}:{self.lookback}:{int(self.include_forming)}"


class IndicatorSpec(BaseModel):
    kind: str
    token: int
    timeframe: str
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", "timeframe")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        cleaned = str(value or "").strip().lower()
        if not cleaned:
            raise ValueError("indicator fields must not be empty")
        return cleaned

    @field_validator("token", mode="before")
    @classmethod
    def _normalize_token(cls, value: Any) -> int:
        return int(value)

    @property
    def key(self) -> str:
        params_key = ",".join(f"{key}={self.params[key]}" for key in sorted(self.params))
        return f"{self.kind}:{self.token}:{self.timeframe}:{params_key}"


class OptionReadSpec(BaseModel):
    underlying: str
    expiry_mode: OptionExpiryMode = OptionExpiryMode.NEAREST
    view: OptionView = OptionView.SNAPSHOT
    strikes_around_atm: int = 5
    expiry: Optional[str] = None

    @field_validator("underlying")
    @classmethod
    def _normalize_underlying(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("option read text fields must not be empty")
        return cleaned.upper()

    @field_validator("expiry_mode", "view", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        raw = value.value if isinstance(value, Enum) else value
        cleaned = str(raw or "").strip().lower()
        if not cleaned:
            raise ValueError("option read text fields must not be empty")
        return cleaned

    @field_validator("strikes_around_atm")
    @classmethod
    def _validate_strikes(cls, value: int) -> int:
        if value < 1:
            raise ValueError("strikes_around_atm must be >= 1")
        return value


class PositionFilter(BaseModel):
    exchange: Optional[str] = None
    product: Optional[str] = None
    tradingsymbol: Optional[str] = None
    instrument_tokens: Set[int] = Field(default_factory=set)

    @field_validator("instrument_tokens", mode="before")
    @classmethod
    def _normalize_instrument_tokens(cls, value: Any) -> Set[int]:
        if value in (None, ""):
            return set()
        return {int(item) for item in value}


class DependencySpec(BaseModel):
    market_tokens: Dict[int, MarketDataMode] = Field(default_factory=dict)
    candle_series: List[CandleSeriesSpec] = Field(default_factory=list)
    indicators: List[IndicatorSpec] = Field(default_factory=list)
    option_reads: List[OptionReadSpec] = Field(default_factory=list)
    account_scope: Optional[str] = None
    position_filters: List[PositionFilter] = Field(default_factory=list)
    order_scope: OrderScope = OrderScope.NONE
    triggers: Set[TriggerType] = Field(default_factory=set)

    @field_validator("market_tokens", mode="before")
    @classmethod
    def _normalize_market_tokens(cls, value: Any) -> Dict[int, str]:
        if not value:
            return {}
        normalized: Dict[int, str] = {}
        for token, mode in dict(value).items():
            raw_mode = mode.value if isinstance(mode, Enum) else mode
            normalized[int(token)] = str(raw_mode).strip().lower()
        return normalized

    @field_validator("order_scope", mode="before")
    @classmethod
    def _normalize_order_scope(cls, value: Any) -> str:
        raw = value.value if isinstance(value, Enum) else value
        cleaned = str(raw or "none").strip().lower()
        if not cleaned:
            raise ValueError("order_scope must not be empty")
        return cleaned

    def merged_with(self, other: "DependencySpec") -> "DependencySpec":
        market_tokens = dict(self.market_tokens)
        mode_rank = {MarketDataMode.LTP: 1, MarketDataMode.QUOTE: 2, MarketDataMode.FULL: 3}
        for token, mode in other.market_tokens.items():
            existing = market_tokens.get(token)
            if existing is None or mode_rank.get(mode, 0) > mode_rank.get(existing, 0):
                market_tokens[token] = mode

        candle_series = {item.key: item for item in self.candle_series}
        candle_series.update({item.key: item for item in other.candle_series})

        indicators = {item.key: item for item in self.indicators}
        indicators.update({item.key: item for item in other.indicators})

        option_reads = {
            f"{item.underlying}:{item.expiry_mode.value}:{item.view.value}:{item.strikes_around_atm}:{item.expiry or ''}": item
            for item in self.option_reads
        }
        option_reads.update(
            {
                f"{item.underlying}:{item.expiry_mode.value}:{item.view.value}:{item.strikes_around_atm}:{item.expiry or ''}": item
                for item in other.option_reads
            }
        )

        position_filters = [*self.position_filters, *other.position_filters]
        return DependencySpec(
            market_tokens=market_tokens,
            candle_series=list(candle_series.values()),
            indicators=list(indicators.values()),
            option_reads=list(option_reads.values()),
            account_scope=other.account_scope or self.account_scope,
            position_filters=position_filters,
            order_scope=other.order_scope if other.order_scope != OrderScope.NONE else self.order_scope,
            triggers=set(self.triggers).union(other.triggers),
        )


class TriggerEvent(BaseModel):
    trigger_type: TriggerType = Field(alias="type")
    token: Optional[int] = None
    timeframe: Optional[str] = None
    account_id: Optional[str] = None
    order_id: Optional[str] = None
    reason: Optional[str] = None
    occurred_at: datetime = Field(default_factory=_utcnow)
    payload: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class Snapshot(BaseModel):
    algo_instance_id: str
    algo_type: str
    trigger: TriggerEvent
    meta: Dict[str, Any] = Field(default_factory=dict)
    market: Dict[str, Any] = Field(default_factory=dict)
    candles: Dict[str, Any] = Field(default_factory=dict)
    indicators: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)
    positions: Dict[str, Any] = Field(default_factory=dict)
    orders: Dict[str, Any] = Field(default_factory=dict)


class NotifyAction(BaseModel):
    action_type: Literal[ActionType.NOTIFY] = ActionType.NOTIFY
    message: str
    level: str = "info"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OrderIntent(BaseModel):
    action_type: Literal[ActionType.ORDER_INTENT] = ActionType.ORDER_INTENT
    intent_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None


class StatePatchAction(BaseModel):
    action_type: Literal[ActionType.STATE_PATCH] = ActionType.STATE_PATCH
    patch: Dict[str, Any] = Field(default_factory=dict)


class NoopAction(BaseModel):
    action_type: Literal[ActionType.NOOP] = ActionType.NOOP
    reason: str = "no_action"


class AlgoCheckpoint(BaseModel):
    instance_id: str
    last_evaluated_at: Optional[datetime] = None
    last_action: Optional[Dict[str, Any]] = None
    state: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_utcnow)


class AlgoInstance(BaseModel):
    instance_id: str
    algo_type: str
    status: AlgoLifecycleState = AlgoLifecycleState.ENABLED
    config: Dict[str, Any] = Field(default_factory=dict)
    dependency_spec: DependencySpec = Field(default_factory=DependencySpec)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("instance_id", "algo_type")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("required text field must not be empty")
        return cleaned
