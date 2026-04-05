import logging
import os
import json
import re
import uuid
import asyncio
import hashlib
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import AsyncGenerator, Any, Callable, Dict, List, Optional

import requests
from redis.exceptions import ConnectionError as RedisConnectionError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header, Response
from fastapi.responses import StreamingResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy.orm import Session

from .redis_events import get_redis, publish_event, pubsub_iter
from .instruments_repository import InstrumentsRepository
from .kite_session import KiteSession, get_kite, get_kite_session_id, get_session_account_id
from .order_runtime import PositionPnL, order_event_runtime, realtime_positions_service
from database import get_db

# Module-level logger
logger = logging.getLogger(__name__)

# API_KEY is required for raw provider requests
API_KEY = os.getenv("KITE_API_KEY")
IDEMPOTENCY_PROCESSING_TTL_SECONDS = max(30, int(os.getenv("KITE_ORDER_IDEMPOTENCY_PROCESSING_TTL_SECONDS", "120")))
IDEMPOTENCY_COMPLETED_TTL_SECONDS = max(
    IDEMPOTENCY_PROCESSING_TTL_SECONDS,
    int(os.getenv("KITE_ORDER_IDEMPOTENCY_COMPLETED_TTL_SECONDS", "300")),
)

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
    order_timestamp: datetime
    exchange_timestamp: datetime
    fill_timestamp: datetime

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

def get_correlation_id(request: Request) -> str:
    """Dependency to get or generate a correlation ID."""
    corr_id = request.headers.get("X-Correlation-ID")
    if not corr_id:
        corr_id = str(uuid.uuid4())
    return corr_id


class KiteWriteThrottler:
    def __init__(self, rate_per_second: float):
        capped_rate = min(10.0, max(1.0, rate_per_second))
        self.rate_per_second = capped_rate
        self.min_interval_seconds = 1.0 / capped_rate
        self.interval_ms = max(1, int(self.min_interval_seconds * 1000))
        self.redis_key = os.getenv("KITE_WRITE_LIMIT_REDIS_KEY", "kite:write_limit:next_slot_ms")
        self.redis_ttl_ms = max(5000, int(os.getenv("KITE_WRITE_LIMIT_REDIS_TTL_MS", "60000")))
        self.require_redis = os.getenv("KITE_WRITE_LIMIT_REQUIRE_REDIS", "true").lower() == "true"
        self.max_wait_seconds = max(1.0, float(os.getenv("KITE_WRITE_LIMIT_MAX_WAIT_SECONDS", "30")))
        self._local_fallback_lock = asyncio.Lock()
        self._local_next_slot_at = 0.0

    _RESERVE_SLOT_SCRIPT = """
local interval_ms = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])
local max_wait_ms = tonumber(ARGV[3])
local t = redis.call('TIME')
local now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local scheduled_ms = now_ms
if current > now_ms then
    scheduled_ms = current
end
local wait_ms = scheduled_ms - now_ms
if wait_ms > max_wait_ms then
    return {-1, now_ms, wait_ms}
end
local next_slot_ms = scheduled_ms + interval_ms
redis.call('PSETEX', KEYS[1], ttl_ms, tostring(next_slot_ms))
return {scheduled_ms, now_ms, wait_ms}
"""

    async def _reserve_local_slot(self) -> tuple[float, int]:
        async with self._local_fallback_lock:
            now = monotonic()
            scheduled = max(now, self._local_next_slot_at)
            wait_seconds = max(0.0, scheduled - now)
            queue_depth = max(0, int(round(wait_seconds / self.min_interval_seconds)))
            if wait_seconds > self.max_wait_seconds:
                raise HTTPException(status_code=503, detail="Order queue is too long. Please retry.")
            self._local_next_slot_at = scheduled + self.min_interval_seconds
            return wait_seconds, queue_depth

    async def _reserve_global_slot(self) -> tuple[float, int]:
        redis = get_redis()
        result = await redis.eval(
            self._RESERVE_SLOT_SCRIPT,
            1,
            self.redis_key,
            self.interval_ms,
            self.redis_ttl_ms,
            int(self.max_wait_seconds * 1000),
        )
        scheduled_ms = int(result[0])
        if scheduled_ms < 0:
            raise HTTPException(status_code=503, detail="Order queue is too long. Please retry.")
        now_ms = int(result[1])
        wait_ms = max(0, int(result[2]))
        queue_depth = max(0, int(wait_ms // self.interval_ms))
        return wait_ms / 1000.0, queue_depth

    async def execute(
        self,
        action_name: str,
        corr_id: str,
        func: Callable[[], Any],
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Any:
        limiter_mode = "redis"
        try:
            wait_seconds, queue_depth = await self._reserve_global_slot()
        except (RedisConnectionError, OSError) as exc:
            if self.require_redis:
                logger.error(
                    "Redis write limiter unavailable; rejecting Kite write",
                    extra={"action": action_name, "correlation_id": corr_id, "error": str(exc), **(meta or {})},
                )
                raise HTTPException(status_code=503, detail="Order dispatcher unavailable. Please retry.")
            limiter_mode = "local-fallback"
            wait_seconds, queue_depth = await self._reserve_local_slot()
        except Exception as exc:
            if self.require_redis:
                logger.error(
                    "Unexpected Redis limiter error; rejecting Kite write",
                    extra={"action": action_name, "correlation_id": corr_id, "error": str(exc), **(meta or {})},
                    exc_info=True,
                )
                raise HTTPException(status_code=503, detail="Order dispatcher unavailable. Please retry.")
            limiter_mode = "local-fallback"
            wait_seconds, queue_depth = await self._reserve_local_slot()

        if wait_seconds > 0:
            logger.info(
                "Throttling Kite write action",
                extra={
                    "action": action_name,
                    "correlation_id": corr_id,
                    "limiter_mode": limiter_mode,
                    "wait_seconds": round(wait_seconds, 4),
                    "queue_depth": queue_depth,
                    **(meta or {}),
                },
            )
            await asyncio.sleep(wait_seconds)

        return await asyncio.to_thread(func)


write_throttler = KiteWriteThrottler(float(os.getenv("KITE_WRITE_OPS_PER_SEC", "9")))


async def run_kite_write_action(
    action_name: str,
    corr_id: str,
    func: Callable[[], Any],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Any:
    return await write_throttler.execute(action_name, corr_id, func, meta=meta)

# ---------------- Service Layer ----------------
class OrdersService:
    def _log_context(self, corr_id: str, kite: KiteConnect, **kwargs) -> Dict[str, Any]:
        """Builds a structured log context."""
        session_id = kite.access_token[-6:] if kite.access_token else "unknown"
        context = {"correlation_id": corr_id, "session_suffix": session_id}
        context.update(kwargs)
        return context

    def _idempotency_redis_key(self, session_id: str, idempotency_key: str) -> str:
        return f"idempotency:place_order:{session_id}:{idempotency_key}"

    def _idempotency_body_hash(self, req: PlaceOrderRequest) -> str:
        normalized_body = json.dumps(req.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()

    async def _begin_idempotent_order(
        self,
        redis_client,
        session_id: str,
        idempotency_key: str,
        body_hash: str,
        response: Optional[Response],
        log_ctx: Dict[str, Any],
    ) -> tuple[Optional[str], Optional[PlaceOrderResponse]]:
        redis_key = self._idempotency_redis_key(session_id, idempotency_key)
        now = datetime.now(timezone.utc).isoformat()
        pending_payload = json.dumps(
            {
                "status": "processing",
                "body_hash": body_hash,
                "created_at": now,
                "updated_at": now,
            }
        )

        claimed = await redis_client.set(redis_key, pending_payload, ex=IDEMPOTENCY_PROCESSING_TTL_SECONDS, nx=True)
        if claimed:
            return redis_key, None

        current_raw = await redis_client.get(redis_key)
        if not current_raw:
            raise HTTPException(status_code=503, detail="Unable to confirm idempotency state. Please retry.")

        try:
            current = json.loads(current_raw)
        except Exception:
            raise HTTPException(status_code=503, detail="Invalid idempotency state. Please retry.")

        if current.get("body_hash") != body_hash:
            raise HTTPException(status_code=409, detail="This idempotency key was already used for a different order request.")

        status = current.get("status")
        if status == "completed" and current.get("order_id"):
            order_id = current["order_id"]
            logger.info("Idempotent replay", extra={**log_ctx, "replay": True, "order_id": order_id})
            if response:
                response.headers["Idempotent-Replay"] = "true"
            return redis_key, PlaceOrderResponse(order_id=order_id)

        raise HTTPException(
            status_code=409,
            detail="An order with this idempotency key is already processing or awaiting verification.",
        )

    async def _store_completed_idempotent_order(self, redis_client, redis_key: str, body_hash: str, order_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            {
                "status": "completed",
                "body_hash": body_hash,
                "order_id": order_id,
                "updated_at": now,
            }
        )
        await redis_client.set(redis_key, payload, ex=IDEMPOTENCY_COMPLETED_TTL_SECONDS)

    async def _store_uncertain_idempotent_order(self, redis_client, redis_key: str, body_hash: str, detail: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            {
                "status": "unknown",
                "body_hash": body_hash,
                "detail": detail[:500],
                "updated_at": now,
            }
        )
        await redis_client.set(redis_key, payload, ex=IDEMPOTENCY_COMPLETED_TTL_SECONDS)

    async def _clear_idempotent_order(self, redis_client, redis_key: str) -> None:
        await redis_client.delete(redis_key)

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
        redis_client = None
        cache_key = None
        body_hash = None
        
        if idempotency_key and session_id:
            try:
                redis_client = get_redis()
                body_hash = self._idempotency_body_hash(req)
                cache_key, replay_response = await self._begin_idempotent_order(
                    redis_client,
                    session_id,
                    idempotency_key,
                    body_hash,
                    response,
                    log_ctx,
                )
                if replay_response:
                    return replay_response
            except HTTPException:
                raise
            except Exception as e:
                logger.error("Redis idempotency guard failed", extra={**log_ctx, "error": str(e)}, exc_info=True)
                raise HTTPException(status_code=503, detail="Idempotency service unavailable. Please retry.")

        logger.info("Placing new order", extra=log_ctx)
        try:
            params = req.model_dump(exclude_none=True)
            variety = params.pop('variety')
            variety_value = variety.value if isinstance(variety, Variety) else str(variety)
            order_id = await run_kite_write_action(
                "place_order",
                corr_id,
                lambda: kite.place_order(variety=variety_value, **params),
                meta=log_ctx,
            )
            log_ctx["order_id"] = order_id

            if redis_client and cache_key and body_hash:
                try:
                    await self._store_completed_idempotent_order(redis_client, cache_key, body_hash, order_id)
                    logger.info("Cached new order for idempotency", extra=log_ctx)
                except Exception as e:
                    logger.error("Redis SET failed for idempotency cache", extra={**log_ctx, "error": str(e)}, exc_info=True)

            logger.info("Order placed successfully", extra=log_ctx)
            return PlaceOrderResponse(order_id=order_id)
        except HTTPException as e:
            if redis_client and cache_key:
                try:
                    if e.status_code in {400, 401, 403, 404, 409, 422}:
                        await self._clear_idempotent_order(redis_client, cache_key)
                    else:
                        await self._store_uncertain_idempotent_order(redis_client, cache_key, body_hash or "", str(e.detail))
                except Exception as redis_error:
                    logger.error("Failed to update idempotency state after HTTP error", extra={**log_ctx, "error": str(redis_error)}, exc_info=True)
            raise
        except Exception as e:
            if redis_client and cache_key:
                try:
                    await self._store_uncertain_idempotent_order(redis_client, cache_key, body_hash or "", str(e))
                except Exception as redis_error:
                    logger.error("Failed to update idempotency state after exception", extra={**log_ctx, "error": str(redis_error)}, exc_info=True)
            logger.error("Failed to place order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

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

    async def convert_position(
        self,
        kite: KiteConnect,
        req: ConvertPositionRequest,
        corr_id: str,
    ) -> ConvertPositionResponse:
        log_ctx = self._log_context(
            corr_id,
            kite,
            exchange=req.exchange.value,
            tradingsymbol=req.tradingsymbol,
            transaction_type=req.transaction_type.value,
            position_type=req.position_type.value,
            quantity=req.quantity,
            old_product=req.old_product.value,
            new_product=req.new_product.value,
        )
        logger.info("Converting position", extra=log_ctx)
        try:
            payload = req.model_dump(mode="python")
            result = await run_kite_write_action(
                "convert_position",
                corr_id,
                lambda: kite.convert_position(**payload),
                meta=log_ctx,
            )
            logger.info("Position converted successfully", extra=log_ctx)
            return ConvertPositionResponse(data=result)
        except Exception as e:
            logger.error("Failed to convert position", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=400, detail=str(e))

    async def modify_order(self, kite: KiteConnect, variety: str, order_id: str, req: ModifyOrderRequest, corr_id: str, parent_order_id: Optional[str] = None) -> dict:
        log_ctx = self._log_context(corr_id, kite, variety=variety, order_id=order_id, parent_order_id=parent_order_id)
        logger.info("Modifying order", extra=log_ctx)
        try:
            payload = req.model_dump(exclude_none=True)
            if parent_order_id:
                payload['parent_order_id'] = parent_order_id
            
            result = await run_kite_write_action(
                "modify_order",
                corr_id,
                lambda: self._raw_request("PUT", f"https://api.kite.trade/orders/{variety}/{order_id}", kite, corr_id, json=payload),
                meta=log_ctx,
            )
            return {"order_id": result.get("data", {}).get("order_id", order_id)}
        except Exception as e:
            logger.error("Failed to modify order", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    async def cancel_order(self, kite: KiteConnect, variety: str, order_id: str, corr_id: str, parent_order_id: Optional[str] = None) -> dict:
        log_ctx = self._log_context(corr_id, kite, variety=variety, order_id=order_id, parent_order_id=parent_order_id)
        logger.info("Cancelling order", extra=log_ctx)
        try:
            params = {}
            if parent_order_id:
                params['parent_order_id'] = parent_order_id

            result = await run_kite_write_action(
                "cancel_order",
                corr_id,
                lambda: self._raw_request("DELETE", f"https://api.kite.trade/orders/{variety}/{order_id}", kite, corr_id, params=params),
                meta=log_ctx,
            )
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

    async def place_basket(
        self,
        kite: KiteConnect,
        req: BasketOrderRequest,
        corr_id: str,
        session_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        response: Response = None,
    ) -> BasketOrderResponse:
        """
        Place a basket of orders sequentially.
        - If dry_run is True, only returns margin preview.
        - If all_or_none is True, attempts best-effort rollback on first failure.
        Note: Market orders may execute immediately; cancellation isn't guaranteed.
        """
        log_ctx = self._log_context(corr_id, kite, order_count=len(req.orders))
        logger.info("Processing basket order request", extra=log_ctx)

        if not req.orders:
            return BasketOrderResponse(status="success", results=[], errors=[])

        # Dry run: preview margins only
        if req.dry_run:
            try:
                margin_items = [
                    OrderMarginInput(
                        exchange=order.exchange,
                        tradingsymbol=order.tradingsymbol,
                        transaction_type=order.transaction_type,
                        variety=order.variety,
                        product=order.product,
                        order_type=order.order_type,
                        quantity=order.quantity,
                        price=order.price or 0,
                        trigger_price=order.trigger_price or 0,
                    )
                    for order in req.orders
                ]
                margins = self.basket_margins(kite, margin_items, consider_positions=True, corr_id=corr_id, mode="compact")
                return BasketOrderResponse(status="dry_run", results=[], margins=margins)
            except Exception as e:
                logger.error("Failed to preview basket margins", extra={**log_ctx, "error": str(e)}, exc_info=True)
                raise HTTPException(status_code=400, detail=f"Failed to preview margins: {str(e)}")

        # Execute orders sequentially
        results: List[BasketOrderResultItem] = []
        placed: List[Dict[str, Any]] = []  # Track placed orders for rollback
        errors: List[Dict[str, Any]] = []

        for idx, order_req in enumerate(req.orders):
            try:
                # Place order using existing service method (with idempotency support)
                child_idempotency_key = f"{idempotency_key}:{idx}" if idempotency_key and session_id else None
                place_result = await self.place_order(
                    kite,
                    order_req,
                    corr_id,
                    idempotency_key=child_idempotency_key,
                    session_id=session_id,
                    response=response,
                )
                
                placed.append({"index": idx, "order_id": place_result.order_id, "variety": order_req.variety.value})
                results.append(
                    BasketOrderResultItem(
                        index=idx,
                        tradingsymbol=order_req.tradingsymbol,
                        order_id=place_result.order_id,
                        status="success"
                    )
                )
                logger.info(
                    f"Basket order {idx+1}/{len(req.orders)} placed",
                    extra={**log_ctx, "order_id": place_result.order_id, "symbol": order_req.tradingsymbol}
                )
            except Exception as e:
                err_msg = str(e)
                logger.error(
                    f"Failed to place basket order {idx+1}/{len(req.orders)}",
                    extra={**log_ctx, "symbol": order_req.tradingsymbol, "error": err_msg},
                    exc_info=True
                )
                
                err = {"index": idx, "tradingsymbol": order_req.tradingsymbol, "error": err_msg}
                errors.append(err)
                results.append(
                    BasketOrderResultItem(
                        index=idx,
                        tradingsymbol=order_req.tradingsymbol,
                        status="failed",
                        error=err_msg
                    )
                )

                # Handle all_or_none: attempt rollback
                if req.all_or_none:
                    logger.info("Attempting rollback due to all_or_none policy", extra=log_ctx)
                    for p in placed:
                        try:
                            await self.cancel_order(kite, p["variety"], p["order_id"], corr_id)
                            logger.info(f"Rolled back order {p['order_id']}", extra=log_ctx)
                        except Exception as cancel_error:
                            logger.error(
                                f"Rollback failed for order {p['order_id']}",
                                extra={**log_ctx, "error": str(cancel_error)},
                                exc_info=True
                            )
                    
                    return BasketOrderResponse(
                        status="failed",
                        results=results,
                        errors=errors,
                        note="Best-effort rollback attempted; some orders may already be executed."
                    )

        final_status = "success" if not errors else "partial"
        logger.info(f"Basket order completed with status: {final_status}", extra={**log_ctx, "success_count": len(placed), "error_count": len(errors)})
        return BasketOrderResponse(status=final_status, results=results, errors=errors)

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
    sid = get_kite_session_id(request)
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

@router.post("/positions/convert", response_model=ConvertPositionResponse, description="Convert an open position from one product type to another.")
async def convert_position(
    req: ConvertPositionRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.convert_position(kite, req, corr_id)

# Phase 2 Endpoints
@router.put("/orders/{variety}/{order_id}", response_model=dict, description="Modify an open/pending order.")
async def modify_order(
    variety: str,
    order_id: str,
    req: ModifyOrderRequest,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if modifying the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.modify_order(kite, variety, order_id, req, corr_id, parent_order_id)

@router.delete("/orders/{variety}/{order_id}", response_model=dict, description="Cancel an open/pending order.")
async def cancel_order(
    variety: str,
    order_id: str,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if cancelling the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.cancel_order(kite, variety, order_id, corr_id, parent_order_id)

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

@router.post("/orders/basket", response_model=BasketOrderResponse, description="Place multiple orders as a basket with optional dry-run and rollback support.")
async def place_basket_orders(
    req: BasketOrderRequest,
    request: Request,
    response: Response,
    kite: KiteConnect = Depends(get_kite),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key", description="Client-generated key for idempotent basket retries."),
    corr_id: str = Depends(get_correlation_id),
):
    """
    Place a basket of orders sequentially.
    - Set dry_run=true to preview margins without placing orders.
    - Set all_or_none=true to attempt rollback on first failure (best-effort).
    """
    sid = get_kite_session_id(request)
    return await service.place_basket(kite, req, corr_id, sid, idempotency_key, response)


# ═══════════════════════════════════════════════════════════════════════════════
# REAL-TIME POSITIONS TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

# Production implementation lives in broker_api.order_runtime.


@router.post("/positions/initialize", description="Initialize real-time position tracking from Kite API")
async def initialize_realtime_positions(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Initialize real-time position tracking.
    Fetches current positions from Kite API and sets up tracking state.
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    try:
        positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    
    return {
        "status": "initialized",
        "account_id": get_session_account_id(db, sid),
        "position_count": len(positions),
        "positions": {k: v.model_dump() for k, v in positions.items()}
    }


@router.get("/positions/realtime", description="Get current real-time positions")
async def get_realtime_positions(
    request: Request,
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Get current real-time positions with calculated PnL.
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    account_id = get_session_account_id(db, sid)
    if not account_id:
        raise HTTPException(409, "Broker account not initialized for this session. Call /positions/initialize first.")

    positions = await realtime_positions_service.get_positions(account_id, corr_id)
    
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
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Server-Sent Events (SSE) endpoint for real-time position streaming.
    Updates are sent whenever:
    - LTP changes (from WebSocket)
    - Orders are filled
    - Positions are exited
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    account_id = get_session_account_id(db, sid)
    if not account_id:
        raise HTTPException(409, "Broker account not initialized for this session. Call /positions/initialize first.")

    return StreamingResponse(
        realtime_positions_service.subscribe_to_positions(account_id, corr_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/positions/reconcile", description="Reconcile real-time positions against broker truth")
async def reconcile_realtime_positions(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id),
):
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")

    account_id = get_session_account_id(db, sid)
    if not account_id:
        try:
            positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        total_pnl = sum(pos.pnl for pos in positions.values())
        return {
            "status": "ok",
            "account_id": next(iter(positions.values())).account_id if positions else None,
            "position_count": len(positions),
            "total_pnl": total_pnl,
            "mode": "initialized",
        }

    count = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)
    positions = await realtime_positions_service.get_positions(account_id, corr_id)
    total_pnl = sum(pos.pnl for pos in positions.values())
    return {
        "status": "ok",
        "account_id": account_id,
        "position_count": count,
        "cached_positions": len(positions),
        "total_pnl": total_pnl,
        "mode": "reconciled",
    }


@router.get("/order-runtime/status", description="Get canonical order runtime status")
async def get_order_runtime_status(
    request: Request,
    db: Session = Depends(get_db),
):
    sid = get_kite_session_id(request)
    account_id = get_session_account_id(db, sid) if sid else None

    counts = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE processing_state = 'pending') AS pending_events,
                COUNT(*) FILTER (WHERE processing_state = 'processing') AS processing_events,
                COUNT(*) FILTER (WHERE processing_state = 'failed') AS failed_events
            FROM canonical_order_events
            """
        )
    ).fetchone()

    dirty_counts = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE dirty_for_trade_sync = TRUE) AS dirty_orders,
                COUNT(*) FILTER (WHERE needs_reconcile = TRUE) AS reconcile_orders
            FROM order_state_projection
            WHERE (:account_id IS NULL OR account_id = :account_id)
            """
        ),
        {"account_id": account_id},
    ).fetchone()

    position_counts = db.execute(
        text(
            """
            SELECT COUNT(*)
            FROM account_positions
            WHERE (:account_id IS NULL OR account_id = :account_id)
              AND net_quantity <> 0
            """
        ),
        {"account_id": account_id},
    ).fetchone()

    return {
        "account_id": account_id,
        "canonical_events": {
            "pending": int(counts[0] or 0),
            "processing": int(counts[1] or 0),
            "failed": int(counts[2] or 0),
        },
        "orders": {
            "dirty_for_trade_sync": int(dirty_counts[0] or 0),
            "needs_reconcile": int(dirty_counts[1] or 0),
        },
        "positions": {
            "open_rows": int(position_counts[0] or 0),
        },
    }


@router.post("/order-runtime/process-now", description="Process canonical events and dirty orders immediately")
async def process_order_runtime_now(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id),
):
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    account_id = get_session_account_id(db, sid)

    processed = await order_event_runtime.process_pending_events(batch_size=100)
    synced = await order_event_runtime.sync_dirty_orders(kite, realtime_positions_service, batch_size=25)
    reconciled = 0
    if account_id:
        reconciled = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)

    return {
        "status": "ok",
        "account_id": account_id,
        "processed_events": processed,
        "synced_orders": synced,
        "reconciled_positions": reconciled,
    }


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

    async def place_gtt(self, kite: KiteConnect, req: PlaceGTTRequest, corr_id: str) -> PlaceGTTResponse:
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

            result = await run_kite_write_action(
                "place_gtt",
                corr_id,
                lambda: self._raw_request(
                    "POST",
                    "https://api.kite.trade/gtt/triggers",
                    kite,
                    corr_id,
                    json=payload,
                ),
                meta=log_ctx,
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

    async def modify_gtt(self, kite: KiteConnect, trigger_id: int, req: ModifyGTTRequest, corr_id: str) -> PlaceGTTResponse:
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

            result = await run_kite_write_action(
                "modify_gtt",
                corr_id,
                lambda: self._raw_request(
                    "PUT",
                    f"https://api.kite.trade/gtt/triggers/{trigger_id}",
                    kite,
                    corr_id,
                    json=payload,
                ),
                meta=log_ctx,
            )

            modified_trigger_id = result.get("data", {}).get("trigger_id")
            logger.info("GTT trigger modified successfully", extra={**log_ctx, "modified_trigger_id": modified_trigger_id})

            return PlaceGTTResponse(trigger_id=modified_trigger_id)
        except Exception as e:
            logger.error("Failed to modify GTT", extra={**log_ctx, "error": str(e)}, exc_info=True)
            if not isinstance(e, HTTPException):
                raise HTTPException(status_code=400, detail=str(e))
            raise e

    async def delete_gtt(self, kite: KiteConnect, trigger_id: int, corr_id: str) -> DeleteGTTResponse:
        """Delete a GTT trigger"""
        log_ctx = self._log_context(corr_id, kite, trigger_id=trigger_id)
        logger.info("Deleting GTT trigger", extra=log_ctx)

        try:
            result = await run_kite_write_action(
                "delete_gtt",
                corr_id,
                lambda: self._raw_request(
                    "DELETE",
                    f"https://api.kite.trade/gtt/triggers/{trigger_id}",
                    kite,
                    corr_id,
                ),
                meta=log_ctx,
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
async def place_gtt_trigger(
    req: PlaceGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Place a GTT trigger.
    
    - **single**: Single trigger value, executes first order when reached
    - **two-leg**: Two trigger values (OCO - One Cancels Other), executes corresponding order
    """
    return await gtt_service.place_gtt(kite, req, corr_id)

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
async def modify_gtt_trigger(
    trigger_id: int,
    req: ModifyGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Modify an existing GTT trigger.
    
    Recommended: Fetch the trigger using GET /gtt/triggers/{id}, modify values, and send to this endpoint.
    """
    return await gtt_service.modify_gtt(kite, trigger_id, req, corr_id)

@router.delete("/gtt/triggers/{trigger_id}", response_model=DeleteGTTResponse, description="Delete a GTT trigger")
async def delete_gtt_trigger(
    trigger_id: int,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """Delete an active GTT trigger."""
    return await gtt_service.delete_gtt(kite, trigger_id, corr_id)


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
            ingest_result = await order_event_runtime.ingest_webhook_event(payload, corr_id, db)
            if ingest_result.get("duplicate"):
                logger.info(
                    "Duplicate webhook event detected (idempotent)",
                    extra={
                        "correlation_id": corr_id,
                        "order_id": payload.order_id,
                        "status": payload.status,
                    }
                )
                return None

            event_id = str(ingest_result.get("canonical_event_id"))
            if event_id:
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
            return None
                 
        except Exception as e:
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
        
        # Store raw + canonical event (with idempotency)
        ingest_result = await order_event_runtime.ingest_webhook_event(payload, corr_id, db)
        db.commit()
        event_id = ingest_result.get("canonical_event_id")

        # Publish SSE event only when inserted (not duplicate)
        if event_id:
            try:
                await publish_event("orders.events", {
                    "source": "webhook",
                    "id": event_id,
                    "order_id": payload.order_id,
                    "user_id": payload.user_id,
                    "status": payload.status,
                    "event_timestamp": payload.get_event_timestamp().isoformat(),
                    "exchange": payload.exchange,
                    "tradingsymbol": payload.tradingsymbol,
                    "instrument_token": payload.instrument_token,
                    "transaction_type": payload.transaction_type,
                    "quantity": payload.quantity,
                    "filled_quantity": payload.filled_quantity,
                    "average_price": payload.average_price,
                    "payload": payload.model_dump()
                })
            except Exception as pe:
                logger.error("Failed to publish webhook order event: %s", pe, exc_info=True)
        
        # Return success (200 OK for both new and duplicate events)
        return {
            "status": "ok",
            "event_id": event_id,
            "duplicate": bool(ingest_result.get("duplicate", event_id is None)),
            "order_id": payload.order_id
        }
        
    except HTTPException:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
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

@router.get("/order-events/stream")
async def sse_order_events(request: Request, source: Optional[str] = Query(None, description="Filter by 'webhook', 'ws' or 'all'")):
    async def event_stream():
        try:
            norm = None
            if source:
                s = source.lower().strip()
                if s == "websocket":
                    s = "ws"
                if s != "all":
                    norm = s
            async for message in pubsub_iter("orders.events"):
                if await request.is_disconnected():
                    break
                if isinstance(message, dict) and message.get("event") == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                if norm and isinstance(message, dict):
                    if message.get("source") != norm:
                        continue
                try:
                    src = message.get("source") if isinstance(message, dict) else None
                    prefix = f"event: {src}\n" if src else ""
                    payload = json.dumps(message)
                    yield f"{prefix}data: {payload}\n\n"
                except Exception:
                    continue
        except asyncio.CancelledError:
            pass
    return StreamingResponse(event_stream(), media_type="text/event-stream")

@router.post("/ws/orders/updates/enable")
async def enable_ws_order_updates(request: Request):
    mgr = getattr(request.app.state, "ws_manager", None)
    if not mgr:
        raise HTTPException(status_code=503, detail="WebSocket manager not available")
    mgr.order_updates_enabled = True
    return {"status": "ok", "enabled": True}

@router.post("/ws/orders/updates/disable")
async def disable_ws_order_updates(request: Request):
    mgr = getattr(request.app.state, "ws_manager", None)
    if not mgr:
        raise HTTPException(status_code=503, detail="WebSocket manager not available")
    mgr.order_updates_enabled = False
    return {"status": "ok", "enabled": False}

@router.get("/ws/orders/updates/status")
async def ws_order_updates_status(request: Request):
    mgr = getattr(request.app.state, "ws_manager", None)
    if not mgr:
        raise HTTPException(status_code=503, detail="WebSocket manager not available")
    return {
        "enabled": bool(getattr(mgr, "order_updates_enabled", False)),
        "ws_status": mgr.get_websocket_status() if hasattr(mgr, "get_websocket_status") else "unknown",
        "last_order_update_at": getattr(mgr, "last_order_update_at", None),
    }

@router.get("/ws/orders/events", response_model=List[OrderEventResponse])
async def get_ws_order_events(
    db: Session = Depends(get_db),
    order_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        conditions = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

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
            FROM ws_order_events
            WHERE {where_clause}
            ORDER BY event_timestamp DESC, created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        result = db.execute(query_sql, params)
        rows = result.fetchall()

        events: List[OrderEventResponse] = []
        for row in rows:
            events.append(OrderEventResponse(
                id=str(row[0]),
                order_id=row[1] or "",
                user_id=row[2] or "",
                status=row[3] or "",
                event_timestamp=row[4],
                received_at=row[5],
                exchange=row[6],
                tradingsymbol=row[7],
                instrument_token=row[8],
                transaction_type=row[9],
                quantity=row[10],
                filled_quantity=row[11],
                average_price=row[12],
                payload=row[13] or {}
            ))

        return events
    except Exception as e:
        logger.error(f"Failed to query WS order events: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to query WS events: {str(e)}")
