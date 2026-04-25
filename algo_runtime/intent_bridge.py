from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from fastapi import Response

from broker_api.kite_orders import BasketOrderRequest, OrdersService, PlaceOrderRequest
from broker_api.kite_session import KiteSession, build_kite_client

from database import SessionLocal

from .models import ExecutionMode, NoopAction, NotifyAction, OrderIntent, StatePatchAction


class OrderIntentHandler(Protocol):
    async def handle(self, intent: OrderIntent, *, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...


class KiteOrdersIntentHandler:
    def __init__(self, *, orders_service: Optional[OrdersService] = None, session_factory: Callable[..., Any] = SessionLocal) -> None:
        self.orders_service = orders_service or OrdersService()
        self.session_factory = session_factory

    async def handle(self, intent: OrderIntent, *, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(intent.payload)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("order_intent payload must include session_id")

        kite = await asyncio.to_thread(self._load_kite_client, session_id)
        corr_id = str(payload.get("correlation_id") or f"algo-intent-{uuid.uuid4()}")
        idempotency_key = intent.dedupe_key or payload.get("idempotency_key")
        response = Response()

        if intent.intent_type == "place_order":
            order_payload = payload.get("order") or {}
            req = PlaceOrderRequest.model_validate(order_payload)
            result = await self.orders_service.place_order(
                kite,
                req,
                corr_id,
                idempotency_key=idempotency_key,
                session_id=session_id,
                response=response,
            )
            return {"mode": "live", "intent_type": intent.intent_type, "result": result.model_dump(mode="json")}

        if intent.intent_type == "place_basket":
            basket_payload = payload.get("basket") or payload
            req = BasketOrderRequest.model_validate(basket_payload)
            result = await self.orders_service.place_basket(
                kite,
                req,
                corr_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                response=response,
            )
            return {"mode": "live", "intent_type": intent.intent_type, "result": result.model_dump(mode="json")}

        raise ValueError(f"Unsupported order intent type '{intent.intent_type}'")

    def _load_kite_client(self, session_id: str):
        db = self.session_factory()
        try:
            session = db.query(KiteSession).filter_by(session_id=session_id).first()
            if session is None or not getattr(session, "access_token", None):
                raise ValueError(f"No Kite session found for session_id '{session_id}'")
            return build_kite_client(session.access_token, session_id=session_id)
        finally:
            db.close()


class IntentBridge:
    def __init__(
        self,
        *,
        order_intent_handler: Optional[OrderIntentHandler] = None,
        live_order_intent_handler: Optional[OrderIntentHandler] = None,
        paper_order_intent_handler: Optional[OrderIntentHandler] = None,
        dry_run_order_intent_handler: Optional[OrderIntentHandler] = None,
        notification_handler: Optional[Callable[[NotifyAction], Awaitable[None]]] = None,
        dedupe_cache_limit: int = 5000,
    ) -> None:
        self.order_intent_handler = order_intent_handler
        self.live_order_intent_handler = live_order_intent_handler or order_intent_handler
        self.paper_order_intent_handler = paper_order_intent_handler
        self.dry_run_order_intent_handler = dry_run_order_intent_handler
        self.notification_handler = notification_handler
        self.dedupe_cache_limit = max(100, int(dedupe_cache_limit))
        self._dedupe_results: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def _dedupe_cache_key(self, *, intent: OrderIntent, execution_mode: ExecutionMode, context: Optional[Dict[str, Any]]) -> Optional[str]:
        if not intent.dedupe_key:
            return None
        instance_id = str((context or {}).get("instance_id") or "")
        return f"{execution_mode.value}:{instance_id}:{intent.dedupe_key}"

    def split_actions(
        self,
        actions: Iterable[NotifyAction | OrderIntent | StatePatchAction | NoopAction],
    ) -> Tuple[List[OrderIntent], List[NotifyAction], List[StatePatchAction], List[NoopAction]]:
        order_intents: List[OrderIntent] = []
        notifications: List[NotifyAction] = []
        state_patches: List[StatePatchAction] = []
        noops: List[NoopAction] = []

        for action in actions:
            if isinstance(action, OrderIntent):
                order_intents.append(action)
            elif isinstance(action, NotifyAction):
                notifications.append(action)
            elif isinstance(action, StatePatchAction):
                state_patches.append(action)
            elif isinstance(action, NoopAction):
                noops.append(action)

        return order_intents, notifications, state_patches, noops

    def _handler_for_mode(self, execution_mode: ExecutionMode) -> Optional[OrderIntentHandler]:
        if execution_mode == ExecutionMode.PAPER:
            return self.paper_order_intent_handler
        if execution_mode == ExecutionMode.DRY_RUN:
            return self.dry_run_order_intent_handler
        return self.live_order_intent_handler or self.order_intent_handler

    async def execute(
        self,
        actions: Iterable[NotifyAction | OrderIntent | StatePatchAction | NoopAction],
        *,
        execution_mode: ExecutionMode = ExecutionMode.LIVE,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        order_intents, notifications, state_patches, noops = self.split_actions(actions)

        notification_count = 0
        if self.notification_handler:
            for notification in notifications:
                await self.notification_handler(notification)
                notification_count += 1

        order_results: List[Dict[str, Any]] = []
        for intent in order_intents:
            cache_key = self._dedupe_cache_key(intent=intent, execution_mode=execution_mode, context=context)
            if cache_key and cache_key in self._dedupe_results:
                self._dedupe_results.move_to_end(cache_key)
                order_results.append(
                    {
                        "intent_type": intent.intent_type,
                        "dedupe_key": intent.dedupe_key,
                        "status": "deduped",
                        "result": self._dedupe_results[cache_key],
                    }
                )
                continue

            handler = self._handler_for_mode(execution_mode)
            if handler is None:
                raise ValueError(f"Order intent handler is not configured for execution_mode '{execution_mode.value}'")

            result = await handler.handle(intent, context=context)
            if cache_key:
                self._dedupe_results[cache_key] = result
                self._dedupe_results.move_to_end(cache_key)
                while len(self._dedupe_results) > self.dedupe_cache_limit:
                    self._dedupe_results.popitem(last=False)
            order_results.append(
                {
                    "intent_type": intent.intent_type,
                    "dedupe_key": intent.dedupe_key,
                    "status": "executed",
                    "result": result,
                }
            )

        return {
            "execution_mode": execution_mode.value,
            "order_results": order_results,
            "notification_count": notification_count,
            "state_patch_count": len(state_patches),
            "noop_count": len(noops),
        }
