from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperOrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PaperOrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    SL = "sl"
    SL_M = "sl_m"


class PaperOrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class FundLedgerEntryType(str, Enum):
    CREDIT = "credit"
    DEBIT = "debit"
    RESERVE = "reserve"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"


class PaperScopedModel(BaseModel):
    account_scope: str

    @field_validator("account_scope")
    @classmethod
    def _validate_account_scope(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("account_scope is required")
        return cleaned


class PaperAccount(PaperScopedModel):
    currency: str = "INR"
    starting_balance: Decimal = Decimal("0")
    available_funds: Decimal = Decimal("0")
    blocked_funds: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = ConfigDict(use_enum_values=True)


class PaperOrder(PaperScopedModel):
    order_id: str
    instrument_token: int
    exchange: str = "NSE"
    tradingsymbol: Optional[str] = None
    product: str = "MIS"
    transaction_type: PaperOrderSide
    order_type: PaperOrderType = PaperOrderType.MARKET
    quantity: int
    filled_quantity: int = 0
    pending_quantity: Optional[int] = None
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    average_price: Optional[Decimal] = None
    status: PaperOrderStatus = PaperOrderStatus.PENDING
    placed_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("order_id")
    @classmethod
    def _validate_order_id(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("order_id is required")
        return cleaned

    @model_validator(mode="after")
    def _normalize_quantities(self) -> "PaperOrder":
        if self.quantity <= 0:
            raise ValueError("quantity must be > 0")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity must be >= 0")
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        if self.pending_quantity is None:
            self.pending_quantity = self.quantity - self.filled_quantity
        if self.pending_quantity < 0:
            raise ValueError("pending_quantity must be >= 0")
        return self


class PaperTrade(PaperScopedModel):
    trade_id: str
    order_id: str
    instrument_token: int
    transaction_type: PaperOrderSide
    quantity: int
    price: Decimal
    trade_timestamp: datetime = Field(default_factory=_utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("trade_id", "order_id")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("trade_id and order_id are required")
        return cleaned


class PaperPosition(PaperScopedModel):
    instrument_token: int
    product: str = "MIS"
    exchange: str = "NSE"
    tradingsymbol: Optional[str] = None
    net_quantity: int = 0
    average_price: Decimal = Decimal("0")
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_value: Decimal = Decimal("0")
    sell_value: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    updated_at: datetime = Field(default_factory=_utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PaperPositionLotAttribution(PaperScopedModel):
    lot_id: str
    instrument_token: int
    product: str = "MIS"
    source_trade_id: str
    source_order_id: Optional[str] = None
    open_quantity: int
    remaining_quantity: Optional[int] = None
    entry_price: Decimal
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_lot_quantities(self) -> "PaperPositionLotAttribution":
        if self.open_quantity <= 0:
            raise ValueError("open_quantity must be > 0")
        if self.remaining_quantity is None:
            self.remaining_quantity = self.open_quantity
        if self.remaining_quantity < 0 or self.remaining_quantity > self.open_quantity:
            raise ValueError("remaining_quantity must be between 0 and open_quantity")
        return self


class PaperFundLedgerEntry(PaperScopedModel):
    entry_id: Optional[int] = None
    entry_type: FundLedgerEntryType
    amount: Decimal
    balance_after: Optional[Decimal] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    notes: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)

    model_config = ConfigDict(use_enum_values=True)
