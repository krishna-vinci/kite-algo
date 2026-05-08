import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    CDS = "CDS"
    MCX = "MCX"

class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class Variety(str, Enum):
    REGULAR = "regular"
    AMO = "amo"
    CO = "co"
    ICEBERG = "iceberg"
    AUCTION = "auction"

class Product(str, Enum):
    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"
    MTF = "MTF"

class PositionType(str, Enum):
    DAY = "day"
    OVERNIGHT = "overnight"

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"

class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    TTL = "TTL"

class PlaceOrderRequest(BaseModel):
    exchange: Exchange
    tradingsymbol: str
    transaction_type: TransactionType
    variety: Variety
    product: Product
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    validity: Validity = Validity.DAY
    validity_ttl: Optional[int] = None
    disclosed_quantity: Optional[int] = None
    tag: Optional[str] = None
    market_protection: Optional[int] = None
    autoslice: Optional[bool] = None
    iceberg_legs: Optional[int] = Field(None, ge=2, le=10)
    iceberg_quantity: Optional[int] = Field(None, gt=0)
    auction_number: Optional[str] = None
    squareoff: Optional[float] = None
    stoploss: Optional[float] = None
    trailing_stoploss: Optional[float] = None
    attribution: Optional[Dict[str, Any]] = None

    @model_validator(mode='after')
    def validate_order_conditions(self) -> 'PlaceOrderRequest':
        if self.order_type == OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("Price must be greater than 0 for LIMIT orders.")
        if self.order_type == OrderType.SL and (self.price is None or self.price <= 0 or self.trigger_price is None or self.trigger_price <= 0):
            raise ValueError("Price and trigger_price must be greater than 0 for SL orders.")
        if self.order_type == OrderType.SL_M and (self.trigger_price is None or self.trigger_price <= 0 or self.price is not None and self.price != 0):
            raise ValueError("Trigger_price must be greater than 0 and price must be 0 or None for SL-M orders.")
        if self.validity == Validity.TTL:
            if self.validity_ttl is None or not (1 <= self.validity_ttl <= 365):
                raise ValueError("validity_ttl must be between 1 and 365 for TTL validity.")
        if self.disclosed_quantity is not None and self.disclosed_quantity > self.quantity:
            raise ValueError("Disclosed quantity cannot be greater than total quantity.")
        if self.market_protection is not None:
            if self.order_type not in [OrderType.MARKET, OrderType.SL_M]:
                raise ValueError("Market protection is only allowed for MARKET and SL-M orders.")
            if not (-1 <= self.market_protection <= 100):
                raise ValueError("Market protection must be between 0 and 100, or -1.")
        return self

    @field_validator('tag')
    def validate_tag(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            if len(v) > 20:
                raise ValueError("Tag must be 20 characters or less.")
            if not re.match(r"^[A-Za-z0-9:_-]*$", v):
                raise ValueError("Tag contains invalid characters. Allowed: A-Z, a-z, 0-9, :, _, -")
        return v

class PlaceOrderResponse(BaseModel):
    order_id: str

class ModifyOrderRequest(BaseModel):
    order_type: Optional[OrderType] = None
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    quantity: Optional[int] = Field(None, gt=0)
    validity: Optional[Validity] = None
    validity_ttl: Optional[int] = None

    @model_validator(mode='after')
    def validate_order_conditions(self) -> 'ModifyOrderRequest':
        if self.order_type == OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("Price must be greater than 0 for LIMIT orders.")
        if self.order_type == OrderType.SL and (self.price is None or self.price <= 0 or self.trigger_price is None or self.trigger_price <= 0):
            raise ValueError("Price and trigger_price must be greater than 0 for SL orders.")
        if self.order_type == OrderType.SL_M and (self.trigger_price is None or self.trigger_price <= 0 or self.price is not None and self.price != 0):
            raise ValueError("Trigger_price must be greater than 0 and price must be 0 or None for SL-M orders.")
        if self.validity == Validity.TTL and (self.validity_ttl is None or self.validity_ttl <= 0):
            raise ValueError("validity_ttl must be greater than 0 for TTL validity.")
        return self

class CancelOrderResponse(BaseModel):
    order_id: str

class ConvertPositionRequest(BaseModel):
    exchange: Exchange
    tradingsymbol: str
    transaction_type: TransactionType
    position_type: PositionType
    quantity: int = Field(gt=0)
    old_product: Product
    new_product: Product

    @model_validator(mode='after')
    def validate_conversion(self) -> 'ConvertPositionRequest':
        if self.old_product == self.new_product:
            raise ValueError("old_product and new_product must be different.")
        return self

class ConvertPositionResponse(BaseModel):
    status: str = "success"
    data: Any

class Order(BaseModel):
    model_config = ConfigDict(extra="allow")
    placed_by: Optional[str] = None
    order_id: str
    exchange_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None
    status: str
    status_message: Optional[str] = None
    status_message_raw: Optional[str] = None
    order_timestamp: datetime
    exchange_update_timestamp: Optional[datetime] = None
    exchange_timestamp: Optional[datetime] = None
    variety: str
    modified: Optional[bool] = None
    exchange: str
    tradingsymbol: str
    instrument_token: int
    order_type: str
    transaction_type: str
    validity: str
    validity_ttl: Optional[int] = None
    product: str
    quantity: int
    disclosed_quantity: int
    price: float
    trigger_price: float
    average_price: float
    filled_quantity: int
    pending_quantity: int
    cancelled_quantity: int
    market_protection: int
    meta: Dict[str, Any]
    tag: Optional[str] = None
    tags: Optional[List[str]] = None
    guid: Optional[str] = None
    account_id: Optional[str] = None

class OrderHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    order_id: str
    status: str
    order_timestamp: str

class OrderMarginInput(BaseModel):
    exchange: Exchange
    tradingsymbol: str
    transaction_type: TransactionType
    variety: Variety
    product: Product
    order_type: OrderType
    quantity: float
    price: Optional[float] = 0
    trigger_price: Optional[float] = 0

class OrderMarginsResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    tradingsymbol: Optional[str] = None
    exchange: Optional[Exchange] = None
    span: float = 0.0
    exposure: float = 0.0
    option_premium: float = 0.0
    additional: float = 0.0
    bo: float = 0.0
    cash: float = 0.0
    var: float = 0.0
    pnl: Dict[str, float] = {"realised": 0.0, "unrealised": 0.0}
    leverage: float = 0.0
    charges: Dict[str, Any] = {}
    total: float = 0.0

    @field_validator("exchange", "tradingsymbol", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "":
            return None
        return v

class BasketMarginsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    initial: OrderMarginsResponseItem
    final: OrderMarginsResponseItem
    orders: List[OrderMarginsResponseItem]
    charges: Dict[str, Any]

class ChargesOrderInput(BaseModel):
    order_id: str
    exchange: Exchange
    tradingsymbol: str
    transaction_type: TransactionType
    variety: Variety
    product: Product
    order_type: OrderType
    quantity: int
    average_price: float

class ChargesOrderResponseItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    transaction_type: TransactionType
    tradingsymbol: str
    exchange: Exchange
    variety: Variety
    product: Product
    order_type: OrderType
    quantity: int
    price: float
    charges: Dict[str, Any]

class Trade(BaseModel):
    model_config = ConfigDict(extra="allow")
    trade_id: str
    order_id: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    transaction_type: str
    product: str
    average_price: float
    quantity: int
    order_timestamp: str | datetime | None = None
    exchange_timestamp: str | datetime | None = None
    fill_timestamp: str | datetime | None = None

    @field_validator("order_timestamp", "exchange_timestamp", "fill_timestamp", mode="before")
    @classmethod
    def _coerce_trade_timestamp(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

class BasketOrderRequest(BaseModel):
    """Request model for placing multiple orders as a basket"""
    orders: List[PlaceOrderRequest]
    all_or_none: bool = False  # If True, attempt rollback on first failure
    dry_run: bool = False  # If True, only preview margins without placing

class BasketOrderResultItem(BaseModel):
    """Result for a single order in the basket"""
    index: int
    tradingsymbol: str
    order_id: Optional[str] = None
    status: str  # "success" or "failed"
    error: Optional[str] = None

class BasketOrderResponse(BaseModel):
    """Response for basket order placement"""
    status: str  # "success", "partial", "failed", or "dry_run"
    results: List[BasketOrderResultItem]
    errors: List[Dict[str, Any]] = []
    margins: Optional[BasketMarginsResponse] = None
    note: Optional[str] = None
    basket_execution_id: Optional[str] = None
    basket_status: Optional[str] = None
    action_required: bool = False
    action_reason: Optional[str] = None

class GTTType(str, Enum):
    """GTT trigger types"""
    SINGLE = "single"
    TWO_LEG = "two-leg"

class GTTStatus(str, Enum):
    """GTT trigger status"""
    ACTIVE = "active"
    TRIGGERED = "triggered"
    DISABLED = "disabled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    DELETED = "deleted"

class GTTOrderType(str, Enum):
    """GTT only supports LIMIT orders"""
    LIMIT = "LIMIT"

class GTTCondition(BaseModel):
    """GTT trigger condition"""
    exchange: Exchange
    tradingsymbol: str
    trigger_values: List[float] = Field(..., min_length=1, max_length=2)
    last_price: float

    @model_validator(mode='after')
    def validate_trigger_values(self) -> 'GTTCondition':
        if len(self.trigger_values) < 1 or len(self.trigger_values) > 2:
            raise ValueError("trigger_values must contain 1 or 2 values")
        return self

class GTTOrder(BaseModel):
    """Single GTT order specification"""
    exchange: Exchange
    tradingsymbol: str
    transaction_type: TransactionType
    quantity: int = Field(gt=0)
    order_type: GTTOrderType = GTTOrderType.LIMIT
    product: Product
    price: float = Field(gt=0)

class PlaceGTTRequest(BaseModel):
    """Request to place a GTT"""
    type: GTTType
    condition: GTTCondition
    orders: List[GTTOrder] = Field(..., min_length=1, max_length=2)

    @model_validator(mode='after')
    def validate_gtt_type(self) -> 'PlaceGTTRequest':
        if self.type == GTTType.SINGLE:
            if len(self.condition.trigger_values) != 1:
                raise ValueError("Single GTT must have exactly 1 trigger value")
            if len(self.orders) != 1:
                raise ValueError("Single GTT must have exactly 1 order")
        elif self.type == GTTType.TWO_LEG:
            if len(self.condition.trigger_values) != 2:
                raise ValueError("Two-leg GTT must have exactly 2 trigger values")
            if len(self.orders) != 2:
                raise ValueError("Two-leg GTT must have exactly 2 orders")
        return self

class ModifyGTTRequest(BaseModel):
    """Request to modify a GTT"""
    type: GTTType
    condition: GTTCondition
    orders: List[GTTOrder] = Field(..., min_length=1, max_length=2)

    @model_validator(mode='after')
    def validate_gtt_type(self) -> 'ModifyGTTRequest':
        if self.type == GTTType.SINGLE:
            if len(self.condition.trigger_values) != 1:
                raise ValueError("Single GTT must have exactly 1 trigger value")
            if len(self.orders) != 1:
                raise ValueError("Single GTT must have exactly 1 order")
        elif self.type == GTTType.TWO_LEG:
            if len(self.condition.trigger_values) != 2:
                raise ValueError("Two-leg GTT must have exactly 2 trigger values")
            if len(self.orders) != 2:
                raise ValueError("Two-leg GTT must have exactly 2 orders")
        return self

class GTTOrderResult(BaseModel):
    """Result of a triggered GTT order"""
    model_config = ConfigDict(extra="allow")
    status: str
    order_id: Optional[str] = None
    rejection_reason: Optional[str] = None

class GTTOrderWithResult(BaseModel):
    """GTT order with execution result"""
    model_config = ConfigDict(extra="allow")
    exchange: str
    tradingsymbol: str
    product: str
    order_type: str
    transaction_type: str
    quantity: int
    price: float
    result: Optional[Dict[str, Any]] = None

class GTTTrigger(BaseModel):
    """GTT trigger response"""
    model_config = ConfigDict(extra="allow")
    id: int
    user_id: Optional[str] = None
    parent_trigger: Optional[int] = None
    type: str
    created_at: str
    updated_at: str
    expires_at: str
    status: str
    condition: Dict[str, Any]
    orders: List[GTTOrderWithResult]
    meta: Optional[Dict[str, Any]] = None

class PlaceGTTResponse(BaseModel):
    """Response after placing a GTT"""
    trigger_id: int

class DeleteGTTResponse(BaseModel):
    """Response after deleting a GTT"""
    trigger_id: int

class PostbackPayload(BaseModel):
    """
    Complete Pydantic model for Kite Connect Postback API payload.
    Matches all fields from the official Kite Connect specification.
    """
    model_config = ConfigDict(extra="allow")
    
    # User and app identification
    user_id: str
    app_id: int
    checksum: str
    placed_by: str
    
    # Order identification
    order_id: str
    exchange_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None
    
    # Order status
    status: str
    status_message: Optional[str] = None
    status_message_raw: Optional[str] = None
    
    # Timestamps (stored as strings in "YYYY-MM-DD HH:MM:SS" format)
    order_timestamp: str
    exchange_update_timestamp: Optional[str] = None
    exchange_timestamp: Optional[str] = None
    
    # Order details
    variety: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    order_type: str
    transaction_type: str
    validity: str
    validity_ttl: Optional[int] = None
    product: str
    
    # Quantities
    quantity: int
    disclosed_quantity: int
    
    # Prices
    price: float
    trigger_price: float
    average_price: float
    
    # Execution details
    filled_quantity: int
    pending_quantity: int
    cancelled_quantity: int
    unfilled_quantity: int
    
    # Additional fields
    market_protection: int
    meta: Dict[str, Any] = Field(default_factory=dict)
    tag: Optional[str] = None
    tags: Optional[List[str]] = None
    guid: Optional[str] = None
    
    def get_event_timestamp(self) -> datetime:
        """Parse order_timestamp string to datetime object"""
        return datetime.strptime(self.order_timestamp, "%Y-%m-%d %H:%M:%S")

class OrderEventResponse(BaseModel):
    """Response model for a stored order event"""
    id: str
    order_id: str
    user_id: str
    status: str
    event_timestamp: datetime
    received_at: datetime
    exchange: Optional[str]
    tradingsymbol: Optional[str]
    instrument_token: Optional[int]
    transaction_type: Optional[str]
    quantity: Optional[int]
    filled_quantity: Optional[int]
    average_price: Optional[float]
    payload: Dict[str, Any]
