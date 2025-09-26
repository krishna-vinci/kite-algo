import logging
import os
import json
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional, Any, Dict

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header, Response
from kiteconnect import KiteConnect
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import Session

from .redis_events import get_redis
from database import Base, SessionLocal

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
    tags: Optional[List[str]] = None
    market_protection: Optional[int] = None
    autoslice: Optional[bool] = None
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
            if len(v) > 64:
                raise ValueError("Tag must be 64 characters or less.")
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