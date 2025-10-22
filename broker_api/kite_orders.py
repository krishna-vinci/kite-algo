import logging
import os
import json
import re
import uuid
import asyncio
from datetime import datetime
from enum import Enum
from typing import List, Optional, Any, Dict, AsyncGenerator

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header, Response
from fastapi.responses import StreamingResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import Session

from .redis_events import get_redis
from .instruments_repository import InstrumentsRepository
from database import Base, SessionLocal, get_db

# Module-level logger
logger = logging.getLogger(__name__)

# API_KEY is required by the correct get_kite function
API_KEY = os.getenv("KITE_API_KEY")

# --- Copied Dependencies from broker_api.py to avoid circular import ---

class KiteSession(Base):
    __tablename__ = "kite_sessions"
    session_id = Column(String(36), primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

def get_db() -> Session:
    """Dependency to get a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_kite(request: Request, db: Session = Depends(get_db)) -> KiteConnect:
    """
    Correct dependency that resolves a KiteConnect instance via session ID
    from either X-Session-ID header or kite_session_id cookie.
    """
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    if not sid:
        raise HTTPException(401, "Not authenticated; login first")
    ks = db.query(KiteSession).filter_by(session_id=sid).first()
    if not ks:
        raise HTTPException(401, "Invalid session")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ks.access_token)
    return kite

# --- End of Copied Dependencies ---

# ---------------- Enums ----------------
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

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"

class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    TTL = "TTL"

# ---------------- Pydantic Models ----------------
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
    span: float
    exposure: float
    option_premium: float
    additional: float
    bo: float
    cash: float
    var: float
    pnl: Dict[str, float]
    leverage: float
    charges: Dict[str, Any]
    total: float

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
    order_timestamp: datetime
    exchange_timestamp: datetime
    fill_timestamp: datetime

def get_correlation_id(request: Request) -> str:
    """Dependency to get or generate a correlation ID."""
    corr_id = request.headers.get("X-Correlation-ID")
    if not corr_id:
        corr_id = str(uuid.uuid4())
    return corr_id

# ---------------- Service Layer ----------------
class OrdersService:
    def _log_context(self, corr_id: str, kite: KiteConnect, **kwargs) -> Dict[str, Any]:
        """Builds a structured log context."""
        session_id = kite.access_token[-6:] if kite.access_token else "unknown"
        context = {"correlation_id": corr_id, "session_suffix": session_id}
        context.update(kwargs)
        return context

    def _raw_request(self, method: str, url: str, kite: KiteConnect, corr_id: str, **kwargs) -> Any:
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {API_KEY}:{kite.access_token}",
            "X-Correlation-ID": corr_id
        }
        if 'json' in kwargs:
            headers['Content-Type'] = 'application/json'

        log_ctx = self._log_context(corr_id, kite, method=method, url=url)
        logger.info(f"Raw request sent", extra=log_ctx)

        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            detail = f"Provider error: {e.response.text}"
            logger.error(f"Raw request HTTP error", extra={**log_ctx, "status_code": status_code, "detail": detail})
            if status_code in [400, 404, 409]:
                raise HTTPException(status_code=status_code, detail=detail)
            elif status_code in [502, 503, 504]:
                 raise HTTPException(status_code=status_code, detail="Provider timeout or downtime.")
            else:
                raise HTTPException(status_code=502, detail=detail)
        except Exception as e:
            logger.error(f"Raw request failed", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail="An unexpected error occurred with the provider.")

    async def place_order(
        self,
        kite: KiteConnect,
        req: PlaceOrderRequest,
        corr_id: str,
        idempotency_key: Optional[str] = None,
        session_id: Optional[str] = None,
        response: Response = None,
    ) -> PlaceOrderResponse:
        log_ctx = self._log_context(corr_id, kite, variety=req.variety.value, symbol=req.tradingsymbol)
        
        if idempotency_key and session_id:
            redis = get_redis()
            normalized_body = json.dumps(req.model_dump(), sort_keys=True)
            cache_key = f"idempotency:place_order:{session_id}:{idempotency_key}:{normalized_body}"
            
            try:
                cached_order_id = await redis.get(cache_key)
                if cached_order_id:
                    logger.info("Idempotent replay", extra={**log_ctx, "replay": True, "order_id": cached_order_id})
                    if response:
                        response.headers["Idempotent-Replay"] = "true"
                    return PlaceOrderResponse(order_id=cached_order_id)
            except Exception as e:
                logger.error("Redis GET failed for idempotency check", extra={**log_ctx, "error": str(e)}, exc_info=True)

        logger.info("Placing new order", extra=log_ctx)
        try:
            params = req.model_dump(exclude_none=True)
            variety = params.pop('variety')
            order_id = kite.place_order(variety=variety.value, **params)
            log_ctx["order_id"] = order_id

            if idempotency_key and session_id:
                try:
                    await redis.set(cache_key, order_id, ex=120)
                    logger.info("Cached new order for idempotency", extra=log_ctx)
                except Exception as e:
                    logger.error("Redis SET failed for idempotency cache", extra={**log_ctx, "error": str(e)}, exc_info=True)

            logger.info("Order placed successfully", extra=log_ctx)
            return PlaceOrderResponse(order_id=order_id)
        except Exception as e:
            logger.error("Failed to place order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e))

    def orders(self, kite: KiteConnect, corr_id: str) -> List[Order]:
        log_ctx = self._log_context(corr_id, kite)
        logger.info("Retrieving all orders", extra=log_ctx)
        try:
            order_list = kite.orders()
            return [Order.model_validate(o) for o in order_list]
        except Exception as e:
            logger.error("Failed to retrieve orders", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail="Failed to retrieve orders from provider.")

    def order_history(self, kite: KiteConnect, order_id: str, corr_id: str) -> List[OrderHistoryRecord]:
        log_ctx = self._log_context(corr_id, kite, order_id=order_id)
        logger.info("Retrieving order history", extra=log_ctx)
        try:
            history = kite.order_history(order_id)
            if not history:
                raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found.")
            return [OrderHistoryRecord.model_validate(h) for h in history]
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve order history", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to retrieve order history for {order_id}.")

    def order_snapshot(self, kite: KiteConnect, order_id: str, corr_id: str) -> Any:
        log_ctx = self._log_context(corr_id, kite, order_id=order_id)
        logger.info("Fetching order snapshot", extra=log_ctx)
        try:
            todays_orders = kite.orders()
            for order in todays_orders:
                if order.get("order_id") == order_id:
                    logger.info("Found order in today's book", extra=log_ctx)
                    return Order.model_validate(order)
            
            logger.warning("Order not in book, falling back to history", extra=log_ctx)
            history = kite.order_history(order_id)
            if history:
                last_record = history[-1]
                last_record_dict = last_record.copy()
                last_record_dict["fallback"] = True
                logger.info("Found order in history", extra=log_ctx)
                return Order.model_validate(last_record_dict)

            raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found.")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get order snapshot", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to retrieve snapshot for order {order_id}.")

    def order_trades(self, kite: KiteConnect, order_id: str, corr_id: str) -> List[Trade]:
        log_ctx = self._log_context(corr_id, kite, order_id=order_id)
        logger.info("Retrieving order trades", extra=log_ctx)
        try:
            trades = kite.order_trades(order_id)
            return [Trade.model_validate(t) for t in trades]
        except Exception as e:
            logger.error("Failed to retrieve trades for order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if "not found" in str(e).lower():
                raise HTTPException(status_code=404, detail=f"No trades found for order '{order_id}'.")
            raise HTTPException(status_code=502, detail=f"Failed to retrieve trades for order {order_id}.")

    def trades(self, kite: KiteConnect, corr_id: str) -> List[Trade]:
        log_ctx = self._log_context(corr_id, kite)
        logger.info("Retrieving all trades", extra=log_ctx)
        try:
            trade_list = kite.trades()
            return [Trade.model_validate(t) for t in trade_list]
        except Exception as e:
            logger.error("Failed to retrieve trades", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail="Failed to retrieve trades from provider.")

    def positions(self, kite: KiteConnect, corr_id: str) -> Any:
        log_ctx = self._log_context(corr_id, kite)
        logger.info("Retrieving positions", extra=log_ctx)
        try:
            return kite.positions()
        except Exception as e:
            logger.error("Failed to retrieve positions", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail="Failed to retrieve positions from provider.")

    def modify_order(self, kite: KiteConnect, variety: str, order_id: str, req: ModifyOrderRequest, corr_id: str, parent_order_id: Optional[str] = None) -> dict:
        log_ctx = self._log_context(corr_id, kite, variety=variety, order_id=order_id, parent_order_id=parent_order_id)
        logger.info("Modifying order", extra=log_ctx)
        try:
            payload = req.model_dump(exclude_none=True)
            if parent_order_id:
                payload['parent_order_id'] = parent_order_id
            
            result = self._raw_request("PUT", f"https://api.kite.trade/orders/{variety}/{order_id}", kite, corr_id, json=payload)
            return {"order_id": result.get("data", {}).get("order_id", order_id)}
        except Exception as e:
            logger.error("Failed to modify order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def cancel_order(self, kite: KiteConnect, variety: str, order_id: str, corr_id: str, parent_order_id: Optional[str] = None) -> dict:
        log_ctx = self._log_context(corr_id, kite, variety=variety, order_id=order_id, parent_order_id=parent_order_id)
        logger.info("Cancelling order", extra=log_ctx)
        try:
            params = {}
            if parent_order_id:
                params['parent_order_id'] = parent_order_id

            result = self._raw_request("DELETE", f"https://api.kite.trade/orders/{variety}/{order_id}", kite, corr_id, params=params)
            return {"order_id": result.get("data", {}).get("order_id", order_id)}
        except Exception as e:
            logger.error("Failed to cancel order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if "not found" in str(e).lower():
                 raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found or already closed.")
            raise HTTPException(status_code=400, detail=str(e))

    def order_margins(self, kite: KiteConnect, items: List[OrderMarginInput], corr_id: str, mode: Optional[str]) -> List[OrderMarginsResponseItem]:
        log_ctx = self._log_context(corr_id, kite, item_count=len(items), mode=mode)
        logger.info("Calculating order margins", extra=log_ctx)
        try:
            payload = [item.model_dump() for item in items]
            params = {"mode": mode} if mode else {}
            result = self._raw_request("POST", "https://api.kite.trade/margins/orders", kite, corr_id, json=payload, params=params)
            return [OrderMarginsResponseItem.model_validate(r) for r in result.get("data", [])]
        except Exception as e:
            logger.error("Failed to calculate order margins", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def basket_margins(self, kite: KiteConnect, items: List[OrderMarginInput], consider_positions: bool, corr_id: str, mode: Optional[str]) -> BasketMarginsResponse:
        log_ctx = self._log_context(corr_id, kite, item_count=len(items), consider_positions=consider_positions, mode=mode)
        logger.info("Calculating basket margins", extra=log_ctx)
        try:
            payload = [item.model_dump() for item in items]
            params = {"consider_positions": consider_positions, "mode": mode}
            params = {k: v for k, v in params.items() if v is not None}
            result = self._raw_request("POST", "https://api.kite.trade/margins/basket", kite, corr_id, json=payload, params=params)
            return BasketMarginsResponse.model_validate(result.get("data", {}))
        except Exception as e:
            logger.error("Failed to calculate basket margins", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def charges_orders(self, kite: KiteConnect, items: List[ChargesOrderInput], corr_id: str) -> List[ChargesOrderResponseItem]:
        log_ctx = self._log_context(corr_id, kite, item_count=len(items))
        logger.info("Calculating order charges", extra=log_ctx)
        try:
            payload = [item.model_dump() for item in items]
            result = self._raw_request("POST", "https://api.kite.trade/charges/orders", kite, corr_id, json=payload)
            return [ChargesOrderResponseItem.model_validate(r) for r in result.get("data", [])]
        except Exception as e:
            logger.error("Failed to calculate order charges", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def trigger_range(self, kite: KiteConnect, transaction_type: TransactionType, instruments: List[str], corr_id: str) -> Any:
        log_ctx = self._log_context(corr_id, kite, transaction_type=transaction_type.value, instrument_count=len(instruments))
        logger.info("Fetching trigger range", extra=log_ctx)
        try:
            params = [("i", inst) for inst in instruments]
            result = self._raw_request("GET", f"https://api.kite.trade/market/trigger_range?transaction_type={transaction_type.value}", kite, corr_id, params=params)
            return result.get("data", {})
        except Exception as e:
            logger.error("Failed to fetch trigger range", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

# ---------------- FastAPI Router ----------------
router = APIRouter(tags=["orders"])
service = OrdersService()

@router.post("/orders", response_model=PlaceOrderResponse, description="Place a new order.")
async def place_order(
    req: PlaceOrderRequest,
    request: Request,
    response: Response,
    kite: KiteConnect = Depends(get_kite),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key", description="Client-generated key for idempotent retries."),
    corr_id: str = Depends(get_correlation_id),
):
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    return await service.place_order(kite, req, corr_id, idempotency_key, sid, response)

@router.get("/orders", response_model=List[Order], description="Retrieve the list of all orders for the day.")
def get_orders(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.orders(kite, corr_id)

@router.get("/orders/{order_id}", response_model=Order, description="Retrieve a snapshot of a specific order, with fallback to history.")
def get_order_snapshot(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_snapshot(kite, order_id, corr_id)

@router.get("/orders/{order_id}/history", response_model=List[OrderHistoryRecord], description="Retrieve the history of a specific order.")
def get_order_history(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_history(kite, order_id, corr_id)

@router.get("/orders/{order_id}/trades", response_model=List[Trade], description="Retrieve trades for a specific order.")
def get_order_trades(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_trades(kite, order_id, corr_id)

@router.get("/trades", response_model=List[Trade], description="Retrieve all trades for the day.")
def get_trades(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.trades(kite, corr_id)

@router.get("/positions", response_model=Any, description="Retrieve the current holdings and positions.")
def get_positions(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.positions(kite, corr_id)

# Phase 2 Endpoints
@router.put("/orders/{variety}/{order_id}", response_model=dict, description="Modify an open/pending order.")
def modify_order(
    variety: str,
    order_id: str,
    req: ModifyOrderRequest,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if modifying the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return service.modify_order(kite, variety, order_id, req, corr_id, parent_order_id)

@router.delete("/orders/{variety}/{order_id}", response_model=dict, description="Cancel an open/pending order.")
def cancel_order(
    variety: str,
    order_id: str,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if cancelling the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return service.cancel_order(kite, variety, order_id, corr_id, parent_order_id)

@router.post("/margins/orders", response_model=List[OrderMarginsResponseItem], description="Calculate margins for a list of orders.")
def get_order_margins(items: List[OrderMarginInput], mode: Optional[str] = Query(None, enum=["compact", "full"]), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_margins(kite, items, corr_id, mode)

@router.post("/margins/basket", response_model=BasketMarginsResponse, description="Calculate margins for a basket of orders.")
def get_basket_margins(items: List[OrderMarginInput], consider_positions: bool = Query(True), mode: Optional[str] = Query(None, enum=["compact", "full"]), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.basket_margins(kite, items, consider_positions, corr_id, mode)

@router.post("/charges/orders", response_model=List[ChargesOrderResponseItem], description="Calculate charges for a list of orders.")
def get_charges_orders(items: List[ChargesOrderInput], kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.charges_orders(kite, items, corr_id)

@router.get("/trigger-range", response_model=Any, description="Retrieve the buy/sell trigger range for Cover Orders.")
def get_trigger_range(transaction_type: TransactionType, instruments: List[str] = Query(...), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.trigger_range(kite, transaction_type, instruments, corr_id)


# ═══════════════════════════════════════════════════════════════════════════════
# REAL-TIME POSITIONS TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

class PositionPnL(BaseModel):
    """Real-time position with calculated PnL"""
    instrument_token: int
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int
    multiplier: int = 1
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0
    average_price: float = 0.0
    last_price: float = 0.0
    pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    day_change: float = 0.0
    day_change_percentage: float = 0.0


class RealTimePositionsService:
    """
    Service for real-time position tracking using:
    - Buy/Sell values from orders/trades
    - WebSocket LTP for real-time PnL calculation
    - Formula: pnl = (sellValue - buyValue) + (netQuantity * lastPrice * multiplier)
    """
    
    def __init__(self):
        self.redis_key_prefix = "realtime_positions:"
        self.position_subscribers: Dict[str, asyncio.Queue] = {}
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        logger.info("RealTimePositionsService initialized")
    
    def _get_lot_size(self, instrument_token: int) -> int:
        """Get lot size from database, fallback to 1"""
        try:
            db = next(get_db())
            try:
                repo = InstrumentsRepository(db)
                lot_size = repo.get_lot_size(instrument_token)
                return lot_size if lot_size is not None else 1
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Failed to fetch lot_size for {instrument_token}: {e}")
            return 1
    
    async def initialize_positions(self, kite: KiteConnect, session_id: str, corr_id: str) -> Dict[str, PositionPnL]:
        """
        Initialize positions from Kite API and build tracking state.
        This should be called once per session or when positions need refresh.
        """
        log_ctx = {"correlation_id": corr_id, "session_id": session_id}
        logger.info("Initializing real-time positions", extra=log_ctx)
        
        try:
            # Fetch current positions from Kite
            positions_data = kite.positions()
            
            # Fetch today's trades to calculate buy/sell values accurately
            trades = kite.trades()
            
            # Build position state
            positions_map: Dict[str, PositionPnL] = {}
            
            # Process net positions (end of day positions)
            for pos in positions_data.get('net', []):
                key = f"{pos['exchange']}:{pos['tradingsymbol']}"
                
                # Get lot size/multiplier from database
                multiplier = self._get_lot_size(pos['instrument_token'])
                
                # Calculate buy/sell values from position data
                quantity = pos.get('quantity', 0)
                buy_quantity = pos.get('buy_quantity', 0)
                sell_quantity = pos.get('sell_quantity', 0)
                buy_value = pos.get('buy_value', 0.0)
                sell_value = pos.get('sell_value', 0.0)
                average_price = pos.get('average_price', 0.0)
                last_price = pos.get('last_price', 0.0)
                
                # Calculate PnL using Kite's formula
                # pnl = (sellValue - buyValue) + (netQuantity * lastPrice * multiplier)
                realized_pnl = sell_value - buy_value
                unrealized_pnl = quantity * last_price * multiplier
                total_pnl = realized_pnl + unrealized_pnl
                
                # Day change
                day_change = pos.get('pnl', 0.0)
                close_price = pos.get('close_price', 0.0)
                day_change_pct = 0.0
                if close_price and close_price > 0:
                    day_change_pct = ((last_price - close_price) / close_price) * 100
                
                position = PositionPnL(
                    instrument_token=pos['instrument_token'],
                    tradingsymbol=pos['tradingsymbol'],
                    exchange=pos['exchange'],
                    product=pos['product'],
                    quantity=quantity,
                    multiplier=multiplier,
                    buy_quantity=buy_quantity,
                    sell_quantity=sell_quantity,
                    buy_value=buy_value,
                    sell_value=sell_value,
                    average_price=average_price,
                    last_price=last_price,
                    pnl=total_pnl,
                    realized_pnl=realized_pnl,
                    unrealized_pnl=unrealized_pnl,
                    day_change=day_change,
                    day_change_percentage=day_change_pct
                )
                
                positions_map[key] = position
            
            # Store in Redis for fast access
            redis = get_redis()
            redis_key = f"{self.redis_key_prefix}{session_id}"
            
            # Serialize positions
            positions_json = {k: v.model_dump() for k, v in positions_map.items()}
            await redis.set(redis_key, json.dumps(positions_json), ex=86400)  # 24 hour TTL
            
            logger.info(
                f"Initialized {len(positions_map)} positions",
                extra={**log_ctx, "position_count": len(positions_map)}
            )
            
            return positions_map
            
        except Exception as e:
            logger.error(f"Failed to initialize positions: {e}", extra=log_ctx, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to initialize positions: {e}")
    
    async def get_positions(self, session_id: str, corr_id: str) -> Dict[str, PositionPnL]:
        """
        Get current positions from Redis cache.
        """
        try:
            redis = get_redis()
            redis_key = f"{self.redis_key_prefix}{session_id}"
            
            cached = await redis.get(redis_key)
            if not cached:
                return {}
            
            positions_data = json.loads(cached)
            positions_map = {k: PositionPnL(**v) for k, v in positions_data.items()}
            
            return positions_map
            
        except Exception as e:
            logger.error(f"Failed to get positions: {e}", exc_info=True)
            return {}
    
    async def update_position_ltp(
        self,
        session_id: str,
        instrument_token: int,
        last_price: float,
        corr_id: str
    ) -> Optional[PositionPnL]:
        """
        Update position with new LTP from WebSocket and recalculate PnL.
        """
        try:
            positions = await self.get_positions(session_id, corr_id)
            
            # Find position with matching instrument token
            updated_position = None
            for key, pos in positions.items():
                if pos.instrument_token == instrument_token:
                    # Update LTP
                    pos.last_price = last_price
                    
                    # Recalculate unrealized PnL
                    pos.unrealized_pnl = pos.quantity * last_price * pos.multiplier
                    pos.pnl = pos.realized_pnl + pos.unrealized_pnl
                    
                    # Update day change
                    if pos.average_price > 0:
                        pos.day_change_percentage = ((last_price - pos.average_price) / pos.average_price) * 100
                    
                    positions[key] = pos
                    updated_position = pos
                    break
            
            if updated_position:
                # Save back to Redis
                redis = get_redis()
                redis_key = f"{self.redis_key_prefix}{session_id}"
                positions_json = {k: v.model_dump() for k, v in positions.items()}
                await redis.set(redis_key, json.dumps(positions_json), ex=86400)
                
                # Notify subscribers
                await self._notify_subscribers(session_id, updated_position)
            
            return updated_position
            
        except Exception as e:
            logger.error(f"Failed to update position LTP: {e}", exc_info=True)
            return None
    
    async def update_position_from_order(
        self,
        session_id: str,
        order: Dict[str, Any],
        corr_id: str
    ) -> Optional[PositionPnL]:
        """
        Update position when an order is filled.
        Handles position building and exits.
        """
        try:
            if order.get('status') not in ['COMPLETE', 'OPEN']:
                return None
            
            positions = await self.get_positions(session_id, corr_id)
            key = f"{order['exchange']}:{order['tradingsymbol']}"
            
            # Get or create position
            position = positions.get(key)
            if not position:
                # Fetch lot_size from database for accurate multiplier
                multiplier = self._get_lot_size(order['instrument_token'])
                
                position = PositionPnL(
                    instrument_token=order['instrument_token'],
                    tradingsymbol=order['tradingsymbol'],
                    exchange=order['exchange'],
                    product=order['product'],
                    quantity=0,
                    multiplier=multiplier,
                    buy_quantity=0,
                    sell_quantity=0,
                    buy_value=0.0,
                    sell_value=0.0,
                    average_price=0.0,
                    last_price=order.get('average_price', 0.0),
                    pnl=0.0,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    day_change=0.0,
                    day_change_percentage=0.0
                )
            
            # Update position based on transaction type
            filled_qty = order.get('filled_quantity', 0)
            avg_price = order.get('average_price', 0.0)
            
            if order['transaction_type'] == 'BUY':
                position.buy_quantity += filled_qty
                position.buy_value += filled_qty * avg_price * position.multiplier
                position.quantity += filled_qty
            else:  # SELL
                position.sell_quantity += filled_qty
                position.sell_value += filled_qty * avg_price * position.multiplier
                position.quantity -= filled_qty
            
            # Recalculate average price
            if position.quantity != 0:
                position.average_price = (position.buy_value - position.sell_value) / (position.quantity * position.multiplier)
            
            # Recalculate PnL
            position.realized_pnl = position.sell_value - position.buy_value
            position.unrealized_pnl = position.quantity * position.last_price * position.multiplier
            position.pnl = position.realized_pnl + position.unrealized_pnl
            
            # Handle position exit (quantity becomes 0)
            if position.quantity == 0:
                logger.info(
                    f"Position exited: {key}, Final PnL: {position.pnl}",
                    extra={"correlation_id": corr_id, "session_id": session_id}
                )
                # Remove from active positions
                positions.pop(key, None)
            else:
                positions[key] = position
            
            # Save to Redis
            redis = get_redis()
            redis_key = f"{self.redis_key_prefix}{session_id}"
            positions_json = {k: v.model_dump() for k, v in positions.items()}
            await redis.set(redis_key, json.dumps(positions_json), ex=86400)
            
            # Notify subscribers
            await self._notify_subscribers(session_id, position)
            
            return position
            
        except Exception as e:
            logger.error(f"Failed to update position from order: {e}", exc_info=True)
            return None
    
    async def _notify_subscribers(self, session_id: str, position: PositionPnL):
        """Notify all SSE subscribers of position update"""
        if session_id in self.position_subscribers:
            try:
                queue = self.position_subscribers[session_id]
                await queue.put(position.model_dump())
            except Exception as e:
                logger.error(f"Failed to notify subscribers: {e}")
    
    async def subscribe_to_positions(
        self,
        session_id: str,
        corr_id: str
    ) -> AsyncGenerator[str, None]:
        """
        SSE stream for real-time position updates.
        """
        # Create queue for this subscriber
        queue = asyncio.Queue(maxsize=100)
        self.position_subscribers[session_id] = queue
        
        logger.info(
            f"New position subscriber: {session_id}",
            extra={"correlation_id": corr_id}
        )
        
        try:
            # Send initial positions
            positions = await self.get_positions(session_id, corr_id)
            if positions:
                for pos in positions.values():
                    event_data = f"data: {json.dumps(pos.model_dump())}\n\n"
                    yield event_data
            
            # Stream updates
            while True:
                try:
                    # Wait for update with timeout
                    position_data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_data = f"data: {json.dumps(position_data)}\n\n"
                    yield event_data
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
                    continue
                
        except asyncio.CancelledError:
            logger.info(f"Position subscriber disconnected: {session_id}")
        finally:
            # Cleanup
            self.position_subscribers.pop(session_id, None)


# Global service instance
realtime_positions_service = RealTimePositionsService()


@router.post("/positions/initialize", description="Initialize real-time position tracking from Kite API")
async def initialize_realtime_positions(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Initialize real-time position tracking.
    Fetches current positions from Kite API and sets up tracking state.
    """
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
    
    return {
        "status": "initialized",
        "position_count": len(positions),
        "positions": {k: v.model_dump() for k, v in positions.items()}
    }


@router.get("/positions/realtime", description="Get current real-time positions")
async def get_realtime_positions(
    request: Request,
    corr_id: str = Depends(get_correlation_id)
):
    """
    Get current real-time positions with calculated PnL.
    """
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    positions = await realtime_positions_service.get_positions(sid, corr_id)
    
    # Calculate summary
    total_pnl = sum(pos.pnl for pos in positions.values())
    realized_pnl = sum(pos.realized_pnl for pos in positions.values())
    unrealized_pnl = sum(pos.unrealized_pnl for pos in positions.values())
    
    return {
        "position_count": len(positions),
        "total_pnl": total_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "positions": {k: v.model_dump() for k, v in positions.items()}
    }


@router.get("/positions/stream", description="SSE stream for real-time position updates")
async def stream_realtime_positions(
    request: Request,
    corr_id: str = Depends(get_correlation_id)
):
    """
    Server-Sent Events (SSE) endpoint for real-time position streaming.
    Updates are sent whenever:
    - LTP changes (from WebSocket)
    - Orders are filled
    - Positions are exited
    """
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    return StreamingResponse(
        realtime_positions_service.subscribe_to_positions(sid, corr_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GTT (Good Till Triggered) ORDERS
# ═══════════════════════════════════════════════════════════════════════════════

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


# ---------------- GTT Service Layer ----------------
class GTTService:
    """Service for GTT operations"""
    
    def _log_context(self, corr_id: str, kite: KiteConnect, **kwargs) -> Dict[str, Any]:
        """Builds a structured log context."""
        session_id = kite.access_token[-6:] if kite.access_token else "unknown"
        context = {"correlation_id": corr_id, "session_suffix": session_id}
        context.update(kwargs)
        return context

    def _raw_request(self, method: str, url: str, kite: KiteConnect, corr_id: str, **kwargs) -> Any:
        """Make raw HTTP request to Kite API"""
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {API_KEY}:{kite.access_token}",
            "X-Correlation-ID": corr_id
        }
        if 'json' in kwargs:
            headers['Content-Type'] = 'application/json'

        log_ctx = self._log_context(corr_id, kite, method=method, url=url)
        logger.info(f"GTT raw request sent", extra=log_ctx)

        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            detail = f"Provider error: {e.response.text}"
            logger.error(f"GTT request HTTP error", extra={**log_ctx, "status_code": status_code, "detail": detail})
            if status_code in [400, 404, 409]:
                raise HTTPException(status_code=status_code, detail=detail)
            elif status_code in [502, 503, 504]:
                raise HTTPException(status_code=status_code, detail="Provider timeout or downtime.")
            else:
                raise HTTPException(status_code=502, detail=detail)
        except Exception as e:
            logger.error(f"GTT request failed", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail="An unexpected error occurred with the provider.")

    def place_gtt(self, kite: KiteConnect, req: PlaceGTTRequest, corr_id: str) -> PlaceGTTResponse:
        """Place a GTT trigger"""
        log_ctx = self._log_context(corr_id, kite, gtt_type=req.type.value, symbol=req.condition.tradingsymbol)
        logger.info("Placing GTT trigger", extra=log_ctx)

        try:
            # Prepare payload
            payload = {
                "type": req.type.value,
                "condition": req.condition.model_dump(),
                "orders": [order.model_dump() for order in req.orders]
            }

            result = self._raw_request(
                "POST",
                "https://api.kite.trade/gtt/triggers",
                kite,
                corr_id,
                json=payload
            )

            trigger_id = result.get("data", {}).get("trigger_id")
            log_ctx["trigger_id"] = trigger_id
            logger.info("GTT trigger placed successfully", extra=log_ctx)

            return PlaceGTTResponse(trigger_id=trigger_id)
        except Exception as e:
            logger.error("Failed to place GTT", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def get_gtts(self, kite: KiteConnect, corr_id: str) -> List[GTTTrigger]:
        """Retrieve all GTT triggers"""
        log_ctx = self._log_context(corr_id, kite)
        logger.info("Retrieving all GTT triggers", extra=log_ctx)

        try:
            result = self._raw_request(
                "GET",
                "https://api.kite.trade/gtt/triggers",
                kite,
                corr_id
            )

            triggers = result.get("data", [])
            return [GTTTrigger.model_validate(t) for t in triggers]
        except Exception as e:
            logger.error("Failed to retrieve GTTs", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=502, detail="Failed to retrieve GTT triggers from provider.")
            raise e

    def get_gtt(self, kite: KiteConnect, trigger_id: int, corr_id: str) -> GTTTrigger:
        """Retrieve a specific GTT trigger"""
        log_ctx = self._log_context(corr_id, kite, trigger_id=trigger_id)
        logger.info("Retrieving GTT trigger", extra=log_ctx)

        try:
            result = self._raw_request(
                "GET",
                f"https://api.kite.trade/gtt/triggers/{trigger_id}",
                kite,
                corr_id
            )

            trigger = result.get("data", {})
            if not trigger:
                raise HTTPException(status_code=404, detail=f"GTT trigger {trigger_id} not found")

            return GTTTrigger.model_validate(trigger)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve GTT", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to retrieve GTT trigger {trigger_id}")

    def modify_gtt(self, kite: KiteConnect, trigger_id: int, req: ModifyGTTRequest, corr_id: str) -> PlaceGTTResponse:
        """Modify a GTT trigger"""
        log_ctx = self._log_context(corr_id, kite, trigger_id=trigger_id, gtt_type=req.type.value)
        logger.info("Modifying GTT trigger", extra=log_ctx)

        try:
            # Prepare payload
            payload = {
                "type": req.type.value,
                "condition": req.condition.model_dump(),
                "orders": [order.model_dump() for order in req.orders]
            }

            result = self._raw_request(
                "PUT",
                f"https://api.kite.trade/gtt/triggers/{trigger_id}",
                kite,
                corr_id,
                json=payload
            )

            modified_trigger_id = result.get("data", {}).get("trigger_id")
            logger.info("GTT trigger modified successfully", extra={**log_ctx, "modified_trigger_id": modified_trigger_id})

            return PlaceGTTResponse(trigger_id=modified_trigger_id)
        except Exception as e:
            logger.error("Failed to modify GTT", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    def delete_gtt(self, kite: KiteConnect, trigger_id: int, corr_id: str) -> DeleteGTTResponse:
        """Delete a GTT trigger"""
        log_ctx = self._log_context(corr_id, kite, trigger_id=trigger_id)
        logger.info("Deleting GTT trigger", extra=log_ctx)

        try:
            result = self._raw_request(
                "DELETE",
                f"https://api.kite.trade/gtt/triggers/{trigger_id}",
                kite,
                corr_id
            )

            deleted_trigger_id = result.get("data", {}).get("trigger_id")
            logger.info("GTT trigger deleted successfully", extra={**log_ctx, "deleted_trigger_id": deleted_trigger_id})

            return DeleteGTTResponse(trigger_id=deleted_trigger_id)
        except Exception as e:
            logger.error("Failed to delete GTT", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e


# ---------------- GTT Router Endpoints ----------------
gtt_service = GTTService()

@router.post("/gtt/triggers", response_model=PlaceGTTResponse, description="Place a GTT (Good Till Triggered) order")
def place_gtt_trigger(
    req: PlaceGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Place a GTT trigger.
    
    - **single**: Single trigger value, executes first order when reached
    - **two-leg**: Two trigger values (OCO - One Cancels Other), executes corresponding order
    """
    return gtt_service.place_gtt(kite, req, corr_id)

@router.get("/gtt/triggers", response_model=List[GTTTrigger], description="Retrieve all GTT triggers")
def get_gtt_triggers(
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Retrieve all GTT triggers (active and from last 7 days).
    
    Statuses:
    - active: Trigger is active and monitoring
    - triggered: Trigger was activated
    - disabled: Trigger is disabled, user action needed
    - expired: Trigger expired based on expiry date
    - cancelled: Trigger cancelled by system
    - rejected: Trigger rejected by system
    - deleted: Trigger deleted by user
    """
    return gtt_service.get_gtts(kite, corr_id)

@router.get("/gtt/triggers/{trigger_id}", response_model=GTTTrigger, description="Retrieve a specific GTT trigger")
def get_gtt_trigger(
    trigger_id: int,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """Retrieve details of a specific GTT trigger by ID."""
    return gtt_service.get_gtt(kite, trigger_id, corr_id)

@router.put("/gtt/triggers/{trigger_id}", response_model=PlaceGTTResponse, description="Modify a GTT trigger")
def modify_gtt_trigger(
    trigger_id: int,
    req: ModifyGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Modify an existing GTT trigger.
    
    Recommended: Fetch the trigger using GET /gtt/triggers/{id}, modify values, and send to this endpoint.
    """
    return gtt_service.modify_gtt(kite, trigger_id, req, corr_id)

@router.delete("/gtt/triggers/{trigger_id}", response_model=DeleteGTTResponse, description="Delete a GTT trigger")
def delete_gtt_trigger(
    trigger_id: int,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """Delete an active GTT trigger."""
    return gtt_service.delete_gtt(kite, trigger_id, corr_id)


# ═══════════════════════════════════════════════════════════════════════════════
# KITE CONNECT WEBHOOK / POSTBACK API
# ═══════════════════════════════════════════════════════════════════════════════

import hashlib
from sqlalchemy import text

# API Secret for checksum validation
API_SECRET = os.getenv("KITE_API_SECRET")
ALLOW_WEBHOOK_TEST_MODE = os.getenv("ALLOW_WEBHOOK_TEST_MODE", "false").lower() == "true"


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


class WebhookService:
    """Service for handling Kite Connect webhook/postback events"""
    
    def _compute_checksum(self, order_id: str, order_timestamp: str) -> str:
        """
        Compute SHA-256 checksum for webhook validation.
        Formula: SHA-256(order_id + order_timestamp + api_secret)
        """
        if not API_SECRET:
            raise HTTPException(
                status_code=500,
                detail="API_SECRET not configured"
            )
        
        # Concatenate: order_id + order_timestamp + api_secret
        data_to_hash = f"{order_id}{order_timestamp}{API_SECRET}"
        
        # Compute SHA-256 hash
        checksum = hashlib.sha256(data_to_hash.encode()).hexdigest()
        
        return checksum
    
    def _validate_checksum(
        self,
        payload: PostbackPayload,
        corr_id: str,
        test_mode: bool = False
    ) -> bool:
        """
        Validate webhook checksum to ensure authenticity.
        Returns True if valid, raises HTTPException if invalid.
        """
        if test_mode:
            logger.warning(
                "Webhook checksum validation BYPASSED (test mode)",
                extra={"correlation_id": corr_id, "order_id": payload.order_id}
            )
            return True
        
        # Compute expected checksum
        expected_checksum = self._compute_checksum(
            payload.order_id,
            payload.order_timestamp
        )
        
        # Validate
        if expected_checksum != payload.checksum:
            logger.error(
                "Webhook checksum validation FAILED",
                extra={
                    "correlation_id": corr_id,
                    "order_id": payload.order_id,
                    "user_id": payload.user_id,
                    "expected_checksum": expected_checksum,
                    "received_checksum": payload.checksum
                }
            )
            raise HTTPException(
                status_code=401,
                detail="Checksum validation failed - unauthorized postback"
            )
        
        logger.info(
            "Webhook checksum validation SUCCESS",
            extra={"correlation_id": corr_id, "order_id": payload.order_id}
        )
        return True
    
    async def store_event(
        self,
        payload: PostbackPayload,
        corr_id: str,
        db: Session
    ) -> Optional[str]:
        """
        Store validated webhook event to database with idempotency.
        Returns event ID if stored, None if duplicate.
        """
        try:
            event_id = str(uuid.uuid4())
            event_timestamp = payload.get_event_timestamp()
            
            # Prepare SQL insert with ON CONFLICT for idempotency
            insert_sql = text("""
                INSERT INTO order_events (
                    id, order_id, user_id, status, event_timestamp, received_at,
                    exchange, tradingsymbol, instrument_token, transaction_type,
                    quantity, filled_quantity, average_price, payload_json
                )
                VALUES (
                    :id, :order_id, :user_id, :status, :event_timestamp, NOW(),
                    :exchange, :tradingsymbol, :instrument_token, :transaction_type,
                    :quantity, :filled_quantity, :average_price, :payload_json::jsonb
                )
                ON CONFLICT (order_id, event_timestamp, status) DO NOTHING
                RETURNING id
            """)
            
            result = db.execute(insert_sql, {
                "id": event_id,
                "order_id": payload.order_id,
                "user_id": payload.user_id,
                "status": payload.status,
                "event_timestamp": event_timestamp,
                "exchange": payload.exchange,
                "tradingsymbol": payload.tradingsymbol,
                "instrument_token": payload.instrument_token,
                "transaction_type": payload.transaction_type,
                "quantity": payload.quantity,
                "filled_quantity": payload.filled_quantity,
                "average_price": payload.average_price,
                "payload_json": json.dumps(payload.model_dump())
            })
            
            db.commit()
            
            # Check if row was actually inserted (idempotency check)
            inserted_id = result.fetchone()
            if inserted_id:
                logger.info(
                    "Webhook event stored successfully",
                    extra={
                        "correlation_id": corr_id,
                        "event_id": event_id,
                        "order_id": payload.order_id,
                        "status": payload.status
                    }
                )
                return event_id
            else:
                logger.info(
                    "Duplicate webhook event detected (idempotent)",
                    extra={
                        "correlation_id": corr_id,
                        "order_id": payload.order_id,
                        "status": payload.status
                    }
                )
                return None
                
        except Exception as e:
            db.rollback()
            logger.error(
                "Failed to store webhook event",
                extra={
                    "correlation_id": corr_id,
                    "order_id": payload.order_id,
                    "error": str(e)
                },
                exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to store webhook event: {str(e)}"
            )
    
    async def query_events(
        self,
        db: Session,
        order_id: Optional[str] = None,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[OrderEventResponse]:
        """
        Query stored webhook events with filters and pagination.
        """
        try:
            # Build dynamic query
            conditions = []
            params = {"limit": limit, "offset": offset}
            
            if order_id:
                conditions.append("order_id = :order_id")
                params["order_id"] = order_id
            
            if user_id:
                conditions.append("user_id = :user_id")
                params["user_id"] = user_id
            
            if status:
                conditions.append("status = :status")
                params["status"] = status
            
            if start_date:
                conditions.append("event_timestamp >= :start_date")
                params["start_date"] = start_date
            
            if end_date:
                conditions.append("event_timestamp <= :end_date")
                params["end_date"] = end_date
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            query_sql = text(f"""
                SELECT 
                    id, order_id, user_id, status, event_timestamp, received_at,
                    exchange, tradingsymbol, instrument_token, transaction_type,
                    quantity, filled_quantity, average_price, payload_json
                FROM order_events
                WHERE {where_clause}
                ORDER BY event_timestamp DESC
                LIMIT :limit OFFSET :offset
            """)
            
            result = db.execute(query_sql, params)
            rows = result.fetchall()
            
            # Convert to response models
            events = []
            for row in rows:
                events.append(OrderEventResponse(
                    id=str(row[0]),
                    order_id=row[1],
                    user_id=row[2],
                    status=row[3],
                    event_timestamp=row[4],
                    received_at=row[5],
                    exchange=row[6],
                    tradingsymbol=row[7],
                    instrument_token=row[8],
                    transaction_type=row[9],
                    quantity=row[10],
                    filled_quantity=row[11],
                    average_price=row[12],
                    payload=row[13]
                ))
            
            return events
            
        except Exception as e:
            logger.error(f"Failed to query webhook events: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to query events: {str(e)}"
            )


# ---------------- Webhook Router Endpoints ----------------
webhook_service = WebhookService()


@router.post(
    "/webhooks/orders/postback",
    status_code=200,
    description="Kite Connect Postback webhook endpoint"
)
async def receive_order_postback(
    request: Request,
    db: Session = Depends(get_db),
    x_test_mode: Optional[str] = Header(None, alias="X-Test-Mode"),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Webhook endpoint for receiving Kite Connect order postback notifications.
    
    This endpoint:
    - Receives POST requests with JSON payload from Kite Connect
    - Validates checksum (SHA-256 of order_id + order_timestamp + api_secret)
    - Stores validated events to database with idempotency
    - Returns 200 OK for both new and duplicate events
    - Returns 401 if checksum validation fails
    - Returns 400 if payload is malformed
    
    Test Mode:
    - Set header `X-Test-Mode: true` to bypass checksum validation
    - Only works if environment variable `ALLOW_WEBHOOK_TEST_MODE=true`
    """
    log_ctx = {"correlation_id": corr_id}
    
    try:
        # Read raw body
        body = await request.body()
        body_str = body.decode('utf-8')
        
        # Parse JSON
        try:
            payload_dict = json.loads(body_str)
        except json.JSONDecodeError as e:
            logger.error(
                "Webhook JSON parsing failed",
                extra={**log_ctx, "error": str(e), "body_preview": body_str[:200]}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON payload: {str(e)}"
            )
        
        # Validate with Pydantic
        try:
            payload = PostbackPayload.model_validate(payload_dict)
        except Exception as e:
            logger.error(
                "Webhook payload validation failed",
                extra={**log_ctx, "error": str(e), "payload": payload_dict}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Payload validation failed: {str(e)}"
            )
        
        # Log received postback
        logger.info(
            "Webhook postback received",
            extra={
                **log_ctx,
                "order_id": payload.order_id,
                "status": payload.status,
                "user_id": payload.user_id,
                "tradingsymbol": payload.tradingsymbol
            }
        )
        
        # Check test mode
        test_mode = False
        if x_test_mode and x_test_mode.lower() == "true":
            if not ALLOW_WEBHOOK_TEST_MODE:
                logger.warning(
                    "Test mode requested but not allowed",
                    extra={**log_ctx, "order_id": payload.order_id}
                )
                raise HTTPException(
                    status_code=403,
                    detail="Test mode not enabled on server"
                )
            test_mode = True
        
        # Validate checksum
        webhook_service._validate_checksum(payload, corr_id, test_mode)
        
        # Store event (with idempotency)
        event_id = await webhook_service.store_event(payload, corr_id, db)
        
        # Return success (200 OK for both new and duplicate events)
        return {
            "status": "ok",
            "event_id": event_id,
            "duplicate": event_id is None,
            "order_id": payload.order_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Webhook processing failed",
            extra={**log_ctx, "error": str(e)},
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"Webhook processing failed: {str(e)}"
        )


@router.get(
    "/webhooks/orders/events",
    response_model=List[OrderEventResponse],
    description="Query stored webhook events"
)
async def query_webhook_events(
    order_id: Optional[str] = Query(None, description="Filter by order ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    status: Optional[str] = Query(None, description="Filter by order status"),
    start_date: Optional[datetime] = Query(None, description="Filter by start date (event_timestamp)"),
    end_date: Optional[datetime] = Query(None, description="Filter by end date (event_timestamp)"),
    limit: int = Query(50, ge=1, le=500, description="Number of events to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db)
):
    """
    Query stored webhook events with filters and pagination.
    
    Returns events ordered by event_timestamp descending (most recent first).
    Each event includes the complete postback payload in the `payload` field.
    
    Use pagination (limit/offset) for large result sets.
    """
    return await webhook_service.query_events(
        db=db,
        order_id=order_id,
        user_id=user_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset
    )