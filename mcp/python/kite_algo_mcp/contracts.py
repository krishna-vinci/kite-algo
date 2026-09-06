"""Typed MCP boundary contracts.

The worker SDK accepts mappings in a few places for backwards compatibility.
The MCP boundary does not: these models are deliberately explicit and reject
unknown fields before an SDK call is made.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_SYMBOLS = 50
MAX_CANDLES = 1_000
MAX_PAGE = 100
MAX_EVENTS = 100
MAX_BASKET_LEGS = 50
MAX_RESULT_BYTES = 256 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolError(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False
    outcome_unknown: bool = False
    reconcile_with: str | None = Field(default=None, max_length=100)
    identifiers: dict[str, str | int] = Field(default_factory=dict, max_length=10)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error"]
    data: Any = None
    error: ToolError | None = None
    warnings: list[str] = Field(default_factory=list, max_length=20)
    truncated: bool = False
    next_cursor: str | None = None

    @model_validator(mode="after")
    def _valid_envelope(self) -> "ToolResult":
        if self.status == "ok" and self.error is not None:
            raise ValueError("successful results cannot contain an error")
        if self.status == "error" and self.error is None:
            raise ValueError("error results require an error detail")
        return self


class SymbolRequest(StrictModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS)

    @model_validator(mode="after")
    def _clean_symbols(self) -> "SymbolRequest":
        cleaned = [str(item).strip().upper() for item in self.symbols if str(item).strip()]
        if not cleaned or len(cleaned) > MAX_SYMBOLS:
            raise ValueError(f"symbols must contain 1..{MAX_SYMBOLS} non-empty values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("symbols must not contain duplicates")
        self.symbols = cleaned
        return self


class InstrumentRequest(StrictModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=40)
    instrument_token: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _target_required(self) -> "InstrumentRequest":
        if self.symbol is None and self.instrument_token is None:
            raise ValueError("symbol or instrument_token is required")
        if self.symbol is not None:
            self.symbol = self.symbol.strip().upper()
        return self

    def value(self) -> str | int:
        return self.instrument_token if self.instrument_token is not None else str(self.symbol)


class SearchInstrumentsRequest(StrictModel):
    query: str = Field(min_length=1, max_length=80)
    exchange: str | None = Field(default=None, min_length=1, max_length=12)
    limit: int = Field(default=20, ge=1, le=MAX_PAGE)


class CandleRequest(StrictModel):
    instrument: str = Field(min_length=1, max_length=40)
    interval: str = Field(default="5minute", min_length=1, max_length=20)
    lookback: int = Field(default=50, ge=1, le=MAX_CANDLES)


class HistoricalCandleRequest(StrictModel):
    instrument: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(default="day", min_length=1, max_length=20)
    from_date: date | None = None
    to_date: date | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=MAX_CANDLES)
    request_history: bool = False

    @model_validator(mode="after")
    def _date_range(self) -> "HistoricalCandleRequest":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must not be after to_date")
        if not self.request_history:
            # A normal historical read must not silently opt into ingestion.
            return self
        if self.lookback_days is not None and (self.from_date or self.to_date):
            raise ValueError("use lookback_days or an explicit date range, not both")
        return self


class CalendarRequest(StrictModel):
    from_date: date
    to_date: date
    exchange: str = Field(default="NSE", min_length=1, max_length=12)
    segment: str = Field(default="CM", min_length=1, max_length=12)

    @model_validator(mode="after")
    def _date_range(self) -> "CalendarRequest":
        if self.from_date > self.to_date:
            raise ValueError("from_date must not be after to_date")
        return self


class IndexRequest(StrictModel):
    source_list: Literal["nifty50", "nifty500", "niftybank"]


class FundamentalsScopeRequest(StrictModel):
    symbols: list[str] | None = Field(default=None, max_length=MAX_SYMBOLS)
    index: Literal["nifty50", "nifty500"] | None = None

    @model_validator(mode="after")
    def _scope(self) -> "FundamentalsScopeRequest":
        if bool(self.symbols) == bool(self.index):
            raise ValueError("provide exactly one of symbols or index")
        if self.symbols:
            normalized = [item.strip().upper() for item in self.symbols if item.strip()]
            if not normalized or len(normalized) > MAX_SYMBOLS:
                raise ValueError(f"symbols must contain 1..{MAX_SYMBOLS} values")
            self.symbols = normalized
        return self


class FundamentalsStatementRequest(StrictModel):
    symbol: str = Field(min_length=1, max_length=40)
    dataset: str = Field(min_length=1, max_length=40)
    statement_scope: str = Field(default="consolidated", min_length=1, max_length=30)


class FundamentalsRefreshRequest(FundamentalsScopeRequest):
    mode: Literal["incremental", "full"] = "incremental"


class RunSelector(StrictModel):
    strategy_run_id: str = Field(min_length=1, max_length=120)


class RunListRequest(StrictModel):
    limit: int = Field(default=25, ge=1, le=MAX_PAGE)
    cursor: str | None = Field(default=None, max_length=512)


class CreateRunRequest(StrictModel):
    template_id: str = Field(min_length=1, max_length=120)
    account_scope: str = Field(min_length=1, max_length=120)
    execution_mode: Literal["paper", "live", "dry_run"] = "paper"
    strategy_run_id: str | None = Field(default=None, max_length=120)


class OrderRequest(StrictModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=40)
    instrument_token: int | None = Field(default=None, ge=1)
    exchange: str = Field(default="NSE", min_length=1, max_length=12)
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=1_000_000)
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "MARKET"
    product: Literal["CNC", "MIS", "NRML"] = "CNC"
    validity: Literal["DAY", "IOC"] = "DAY"
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    disclosed_quantity: int | None = Field(default=None, ge=0)
    tag: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _order_shape(self) -> "OrderRequest":
        if self.symbol is None and self.instrument_token is None:
            raise ValueError("symbol or instrument_token is required")
        if self.symbol is not None:
            self.symbol = self.symbol.strip().upper()
        if self.order_type == "LIMIT" and self.price is None:
            raise ValueError("LIMIT orders require price")
        if self.order_type in {"SL", "SL-M"} and self.trigger_price is None:
            raise ValueError("stop-loss orders require trigger_price")
        if self.disclosed_quantity and self.disclosed_quantity > self.quantity:
            raise ValueError("disclosed_quantity must not exceed quantity")
        return self

    def sdk_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True, mode="json")
        if self.instrument_token is not None:
            payload.pop("symbol", None)
        return payload


class BasketRequest(RunSelector):
    orders: list[OrderRequest] = Field(min_length=1, max_length=MAX_BASKET_LEGS)
    idempotency_key: str = Field(min_length=8, max_length=160)
    all_or_none: bool = False


class OrderActionRequest(RunSelector):
    order_id: str = Field(min_length=1, max_length=120)
    variety: str = Field(default="regular", min_length=1, max_length=30)


class OrderModifyRequest(OrderActionRequest):
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] | None = None
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, gt=0, le=1_000_000)
    validity: Literal["DAY", "IOC"] | None = None
    validity_ttl: int | None = Field(default=None, ge=0, le=86_400)


class PlaceOrderRequest(RunSelector):
    order: OrderRequest
    idempotency_key: str = Field(min_length=8, max_length=160)


class BracketRequest(RunSelector):
    entry_order: OrderRequest
    stoploss: OrderRequest
    target: OrderRequest | None = None
    idempotency_key: str = Field(min_length=8, max_length=160)


class ExitRunRequest(RunSelector):
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class RiskRequest(RunSelector):
    max_daily_loss: float | None = Field(default=None, ge=0)
    max_position_value: float | None = Field(default=None, ge=0)
    max_open_orders: int | None = Field(default=None, ge=0, le=10_000)
    reason: str | None = Field(default=None, max_length=500)

    def patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude={"strategy_run_id", "reason"}, mode="json")


class ProtectionRequest(RunSelector):
    enabled: bool = True
    mode: str = Field(default="exposure", min_length=1, max_length=40)
    stoploss_pct: float | None = Field(default=None, gt=0)
    target_pct: float | None = Field(default=None, gt=0)
    trailing_activate_pct: float | None = Field(default=None, gt=0)
    trailing_drawdown_pct: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=500)

    def backend_payload(self) -> dict[str, Any]:
        basket = self.model_dump(
            exclude_none=True,
            exclude={"strategy_run_id", "enabled", "mode", "reason"},
            mode="json",
        )
        payload: dict[str, Any] = {"enabled": self.enabled, "mode": self.mode, "version": 1}
        if basket:
            payload["basket"] = basket
        return payload


class DecisionRequest(RunSelector):
    decision_type: Literal["signal", "entry", "exit", "risk_update"]
    summary: str = Field(min_length=1, max_length=1_000)


class PageRequest(RunSelector):
    limit: int = Field(default=100, ge=1, le=MAX_EVENTS)
    after_cursor: int = Field(default=0, ge=0)


class GttOrder(StrictModel):
    exchange: str = Field(default="NSE", min_length=1, max_length=12)
    tradingsymbol: str = Field(min_length=1, max_length=40)
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=1_000_000)
    order_type: Literal["LIMIT"] = "LIMIT"
    product: Literal["CNC", "MIS", "NRML"] = "CNC"
    price: float = Field(gt=0)


class GttRequest(StrictModel):
    type: Literal["single", "two-leg"]
    tradingsymbol: str = Field(min_length=1, max_length=40)
    trigger_values: list[float] = Field(min_length=1, max_length=2)
    last_price: float = Field(gt=0)
    orders: list[GttOrder] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _gtt_shape(self) -> "GttRequest":
        if self.type == "single" and (len(self.trigger_values) != 1 or len(self.orders) != 1):
            raise ValueError("single GTT requires one trigger value and one order")
        if self.type == "two-leg" and (len(self.trigger_values) != 2 or len(self.orders) != 2):
            raise ValueError("two-leg GTT requires two trigger values and two orders")
        return self

    def sdk_payload(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "condition": {
                "exchange": self.orders[0].exchange,
                "tradingsymbol": self.tradingsymbol,
                "trigger_values": self.trigger_values,
                "last_price": self.last_price,
            },
            "orders": [item.model_dump(mode="json") for item in self.orders],
        }


class OptionSelector(StrictModel):
    kind: Literal["exact", "offset", "delta", "spread"] = "exact"
    option_type: Literal["CE", "PE"] | None = None
    strike: float | None = Field(default=None, gt=0)
    offset: str | None = Field(default=None, max_length=20)
    delta_target: float | None = Field(default=None, gt=-1, lt=1)
    spread_type: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _selector_shape(self) -> "OptionSelector":
        if self.kind == "exact" and self.strike is None:
            raise ValueError("exact option selectors require strike")
        if self.kind == "offset" and not self.offset:
            raise ValueError("offset option selectors require offset")
        if self.kind == "delta" and self.delta_target is None:
            raise ValueError("delta option selectors require delta_target")
        return self

    def sdk_selection(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "option_type": self.option_type,
                "strike": self.strike,
                "offset": self.offset,
                "delta_target": self.delta_target,
                "spread_type": self.spread_type,
            }.items()
            if value is not None
        }


class OptionRequest(StrictModel):
    underlying: str = Field(min_length=1, max_length=30)
    expiry: str | None = Field(default=None, max_length=30)
    selector: OptionSelector | None = None
    quantity_lots: int = Field(default=1, ge=1, le=100)
    transaction_type: Literal["BUY", "SELL"] = "BUY"
    product: Literal["NRML", "MIS"] = "NRML"


class OptionRunRequest(StrictModel):
    strategy_name: str = Field(min_length=1, max_length=120)
    strategy_run_id: str | None = Field(default=None, max_length=120)
    underlying: str = Field(min_length=1, max_length=30)
    expiry: str = Field(min_length=1, max_length=30)
    legs: list[OptionSelector] = Field(min_length=1, max_length=20)
    product: Literal["NRML", "MIS"] = "NRML"
    transaction_type: Literal["BUY", "SELL"] = "BUY"
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class OptionCreateRunRequest(OptionRunRequest):
    """Create an option run only inside an explicitly authorized worker run."""

    strategy_run_id: str = Field(min_length=1, max_length=120)
    account_scope: str = Field(min_length=1, max_length=120)
    execution_mode: Literal["paper", "live", "dry_run"]


class OptionActionRequest(RunSelector):
    execution_mode: Literal["paper", "live", "dry_run"] | None = None
    account_scope: str | None = Field(default=None, max_length=120)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)
    all_or_none: bool = False


class OptionWriteActionRequest(OptionActionRequest):
    """Option entry/exit requests must carry a stable submission key."""

    execution_mode: Literal["paper", "live", "dry_run"]
    account_scope: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=160)


class OptionMetricSnapshot(StrictModel):
    timestamp: str | None = None
    spot: float | None = None
    underlying_price: float | None = None
    price: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    pnl: float | None = None


class OptionReplayRequest(RunSelector):
    metric_snapshots: list[OptionMetricSnapshot] = Field(min_length=1, max_length=MAX_EVENTS)


class IndicatorBar(StrictModel):
    timestamp: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    is_complete: bool = True


class IndicatorRequest(StrictModel):
    name: Literal[
        "sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
        "cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
        "vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
    ]
    bars: list[IndicatorBar] = Field(min_length=1, max_length=MAX_CANDLES)
    period: int = Field(default=14, ge=1, le=500)
    fast_period: int = Field(default=12, ge=1, le=500)
    slow_period: int = Field(default=26, ge=1, le=500)
    signal_period: int = Field(default=9, ge=1, le=500)
    multiplier: float = Field(default=2.0, gt=0, le=10)
    include_forming: bool = False

    @model_validator(mode="after")
    def _periods(self) -> "IndicatorRequest":
        if self.fast_period >= self.slow_period and self.name in {"macd", "ppo"}:
            raise ValueError("fast_period must be less than slow_period")
        if not self.include_forming:
            self.bars = [bar for bar in self.bars if bar.is_complete]
        if not self.bars:
            raise ValueError("bars must contain at least one completed candle")
        return self


__all__ = [
    "MAX_SYMBOLS", "MAX_CANDLES", "MAX_PAGE", "MAX_EVENTS", "MAX_BASKET_LEGS", "MAX_RESULT_BYTES",
    "ToolError", "ToolResult", "SymbolRequest", "InstrumentRequest", "SearchInstrumentsRequest",
    "CandleRequest", "HistoricalCandleRequest", "CalendarRequest", "IndexRequest",
    "FundamentalsScopeRequest", "FundamentalsStatementRequest", "FundamentalsRefreshRequest",
    "RunSelector", "RunListRequest", "CreateRunRequest", "OrderRequest", "BasketRequest",
    "OrderActionRequest", "OrderModifyRequest", "PlaceOrderRequest", "ExitRunRequest", "RiskRequest",
    "ProtectionRequest", "DecisionRequest", "PageRequest", "BracketRequest", "GttOrder", "GttRequest", "OptionSelector",
    "OptionRequest", "OptionRunRequest", "OptionCreateRunRequest", "OptionActionRequest", "OptionWriteActionRequest", "OptionMetricSnapshot", "OptionReplayRequest", "IndicatorBar", "IndicatorRequest",
]
