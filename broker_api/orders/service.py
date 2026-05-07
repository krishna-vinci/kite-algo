import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Dict, List, Optional

import requests
from fastapi import HTTPException, Request, Response
from kiteconnect import KiteConnect
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from broker_api.core.redis_events import get_redis, publish_event, pubsub_iter
from broker_api.instruments.instruments_repository import InstrumentsRepository
from broker_api.orders.models import *
from broker_api.orders.order_runtime import PositionPnL, order_event_runtime, realtime_positions_service
from broker_api.orders.worker_execution_links import worker_execution_links_store
from broker_api.session.kite_session import KiteSession, get_kite, get_kite_session_id, get_session_account_id

logger = logging.getLogger(__name__)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("KITE_API_KEY")

IDEMPOTENCY_PROCESSING_TTL_SECONDS = max(30, int(os.getenv("KITE_ORDER_IDEMPOTENCY_PROCESSING_TTL_SECONDS", "120")))

IDEMPOTENCY_COMPLETED_TTL_SECONDS = max(
    IDEMPOTENCY_PROCESSING_TTL_SECONDS,
    int(os.getenv("KITE_ORDER_IDEMPOTENCY_COMPLETED_TTL_SECONDS", "300")),
)

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
        response: Optional[Response] = None,
        basket_execution_id: Optional[str] = None,
        basket_leg_index: Optional[int] = None,
    ) -> PlaceOrderResponse:
        log_ctx = self._log_context(corr_id, kite, variety=req.variety.value, symbol=req.tradingsymbol)
        redis_client = None
        cache_key = None
        body_hash = None
        attribution = None
        
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
            attribution_payload = params.pop("attribution", None)
            if session_id:
                from broker_api.orders.live_order_intents import create_live_order_intent, validate_live_order_attribution
                from execution_accounting.kite_costs import build_live_order_cost_contract

                attribution = validate_live_order_attribution({"attribution": attribution_payload or {}})
                if idempotency_key and not attribution.idempotency_key:
                    attribution.idempotency_key = idempotency_key
                params["tag"] = attribution.client_order_ref
                quote_payload = dict(params)
                quote_payload.pop("tag", None)
                cost_contract = build_live_order_cost_contract(
                    kite=kite,
                    orders_service=self,
                    order=quote_payload,
                    corr_id=corr_id,
                )
                bracket_intent_id = None
                if isinstance(attribution_payload, dict):
                    bracket_intent_id = (
                        attribution_payload.get("bracket_intent_id")
                        or (attribution_payload.get("metadata") or {}).get("bracket_intent_id")
                    )
                create_live_order_intent(
                    attribution=attribution,
                    cost_contract=cost_contract.journal_payload(),
                    idempotency_key=idempotency_key,
                    basket_execution_id=basket_execution_id,
                    basket_leg_index=basket_leg_index,
                    bracket_intent_id=str(bracket_intent_id or "").strip() or None,
                )
            variety = params.pop('variety')
            variety_value = variety.value if isinstance(variety, Variety) else str(variety)
            order_id = await run_kite_write_action(
                "place_order",
                corr_id,
                lambda: kite.place_order(variety=variety_value, **params),
                meta=log_ctx,
            )
            log_ctx["order_id"] = order_id
            if attribution and attribution.client_order_ref:
                try:
                    from broker_api.orders.live_order_intents import mark_live_order_intent_placed, seed_live_order_state_projection

                    mark_live_order_intent_placed(client_order_ref=attribution.client_order_ref, broker_order_id=str(order_id))
                    seed_live_order_state_projection(
                        account_id=attribution.account_ref,
                        broker_order_id=str(order_id),
                        status="PLACED",
                        order_payload=params,
                    )
                    worker_execution_links_store.upsert_order_link(
                        strategy_run_id=str(attribution.strategy_run_id),
                        account_id=str(attribution.account_ref),
                        broker_order_id=str(order_id),
                        client_order_ref=str(attribution.client_order_ref),
                        basket_execution_id=basket_execution_id,
                        basket_leg_index=basket_leg_index,
                    )
                except Exception as mark_error:
                    logger.error(
                        "Failed to mark live order accounting state after broker success",
                        extra={**log_ctx, "client_order_ref": attribution.client_order_ref, "error": str(mark_error)},
                        exc_info=True,
                    )

            if redis_client and cache_key and body_hash:
                try:
                    await self._store_completed_idempotent_order(redis_client, cache_key, body_hash, order_id)
                    logger.info("Cached new order for idempotency", extra=log_ctx)
                except Exception as e:
                    logger.error("Redis SET failed for idempotency cache", extra={**log_ctx, "error": str(e)}, exc_info=True)

            logger.info("Order placed successfully", extra=log_ctx)
            return PlaceOrderResponse(order_id=order_id)
        except HTTPException as e:
            if attribution and attribution.client_order_ref and e.status_code in {400, 401, 403, 404, 409, 422}:
                try:
                    from broker_api.orders.live_order_intents import mark_live_order_intent_failed

                    mark_live_order_intent_failed(
                        client_order_ref=attribution.client_order_ref,
                        error={"status_code": e.status_code, "detail": e.detail},
                    )
                except Exception as mark_error:
                    logger.error("Failed to mark live order intent failed", extra={**log_ctx, "error": str(mark_error)}, exc_info=True)
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
            if attribution and attribution.client_order_ref:
                try:
                    from broker_api.orders.live_order_intents import mark_live_order_intent_failed

                    mark_live_order_intent_failed(
                        client_order_ref=attribution.client_order_ref,
                        error={"error": str(e)},
                    )
                except Exception as mark_error:
                    logger.error("Failed to mark live order intent failed", extra={**log_ctx, "error": str(mark_error)}, exc_info=True)
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
        response: Optional[Response] = None,
        basket_execution_id: Optional[str] = None,
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
        basket_snapshot: Optional[Dict[str, Any]] = None

        if session_id:
            from app.database import SessionLocal
            from broker_api.orders.basket_execution import basket_execution_store
            from broker_api.orders.live_order_intents import validate_live_order_attribution

            first_order_payload = req.orders[0].model_dump(mode="json", exclude_none=True)
            first_attribution = validate_live_order_attribution({"attribution": first_order_payload.get("attribution") or {}})
            if not basket_execution_id:
                db = SessionLocal()
                try:
                    basket_snapshot = basket_execution_store.create_live_basket_execution(
                        db,
                        strategy_run_id=first_attribution.strategy_run_id,
                        account_id=first_attribution.account_ref,
                        all_or_none=bool(req.all_or_none),
                        orders=[order.model_dump(mode="json", exclude_none=True) for order in req.orders],
                    )
                    basket_execution_id = str(basket_snapshot.get("basket_execution_id") or "") or None
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

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
                    basket_execution_id=basket_execution_id,
                    basket_leg_index=idx if basket_execution_id else None,
                )

                if basket_execution_id:
                    from app.database import SessionLocal
                    from broker_api.orders.basket_execution import basket_execution_store

                    db = SessionLocal()
                    try:
                        basket_execution_store.mark_leg_working(
                            db,
                            basket_execution_id=basket_execution_id,
                            leg_index=idx,
                            broker_order_id=str(place_result.order_id),
                            client_order_ref=str(getattr(order_req, "tag", "") or "") or None,
                        )
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
                    finally:
                        db.close()
                
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

                if basket_execution_id:
                    from app.database import SessionLocal
                    from broker_api.orders.basket_execution import basket_execution_store

                    db = SessionLocal()
                    try:
                        basket_execution_store.mark_leg_submit_failed(
                            db,
                            basket_execution_id=basket_execution_id,
                            leg_index=idx,
                            error_message=err_msg,
                        )
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
                    finally:
                        db.close()

                # Handle all_or_none: attempt rollback
                if req.all_or_none:
                    logger.info("Attempting rollback due to all_or_none policy", extra=log_ctx)
                    rollback_failed = False
                    for p in placed:
                        try:
                            await self.cancel_order(kite, p["variety"], p["order_id"], corr_id)
                            logger.info(f"Rolled back order {p['order_id']}", extra=log_ctx)
                        except Exception as cancel_error:
                            rollback_failed = True
                            logger.error(
                                f"Rollback failed for order {p['order_id']}",
                                extra={**log_ctx, "error": str(cancel_error)},
                                exc_info=True
                            )

                    if basket_execution_id:
                        from app.database import SessionLocal
                        from broker_api.orders.basket_execution import basket_execution_store

                        db = SessionLocal()
                        try:
                            basket_snapshot = basket_execution_store.finalize_submission(
                                db,
                                basket_execution_id=basket_execution_id,
                                rollback_status="failed" if rollback_failed else "completed",
                                action_required=rollback_failed,
                                action_reason="rollback_incomplete" if rollback_failed else None,
                            )
                            db.commit()
                        except Exception:
                            db.rollback()
                            raise
                        finally:
                            db.close()

                    return BasketOrderResponse(
                        status="failed",
                        results=results,
                        errors=errors,
                        note="Best-effort rollback attempted; some orders may already be executed.",
                        basket_execution_id=basket_execution_id,
                        basket_status=(basket_snapshot or {}).get("status"),
                        action_required=bool((basket_snapshot or {}).get("action_required")),
                        action_reason=(basket_snapshot or {}).get("action_reason"),
                    )

        final_status = "success" if not errors else "partial"
        logger.info(f"Basket order completed with status: {final_status}", extra={**log_ctx, "success_count": len(placed), "error_count": len(errors)})
        if basket_execution_id:
            from app.database import SessionLocal
            from broker_api.orders.basket_execution import basket_execution_store

            db = SessionLocal()
            try:
                basket_snapshot = basket_execution_store.finalize_submission(
                    db,
                    basket_execution_id=basket_execution_id,
                    rollback_status="none",
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        return BasketOrderResponse(
            status=final_status,
            results=results,
            errors=errors,
            basket_execution_id=basket_execution_id,
            basket_status=(basket_snapshot or {}).get("status"),
            action_required=bool((basket_snapshot or {}).get("action_required")),
            action_reason=(basket_snapshot or {}).get("action_reason"),
        )

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

API_SECRET = os.getenv("KITE_API_SECRET")

ALLOW_WEBHOOK_TEST_MODE = os.getenv("ALLOW_WEBHOOK_TEST_MODE", "false").lower() == "true"

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
