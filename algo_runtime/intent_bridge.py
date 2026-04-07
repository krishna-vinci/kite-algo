from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from fastapi import Response

from broker_api.kite_orders import BasketOrderRequest, OrdersService, PlaceOrderRequest
from broker_api.kite_session import KiteSession, build_kite_client

from database import SessionLocal

from .models import NoopAction, NotifyAction, OrderIntent, StatePatchAction


class OrderIntentHandler(Protocol):
    async def handle(self, intent: OrderIntent) -> Dict[str, Any]: ...


class KiteOrdersIntentHandler:
    def __init__(self, *, orders_service: Optional[OrdersService] = None, session_factory: Callable[..., Any] = SessionLocal) -> None:
        self.orders_service = orders_service or OrdersService()
        self.session_factory = session_factory

    async def handle(self, intent: OrderIntent) -> Dict[str, Any]:
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
            return {"intent_type": intent.intent_type, "result": result.model_dump(mode="json")}

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
            return {"intent_type": intent.intent_type, "result": result.model_dump(mode="json")}

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
        notification_handler: Optional[Callable[[NotifyAction], Awaitable[None]]] = None,
    ) -> None:
        self.order_intent_handler = order_intent_handler
        self.notification_handler = notification_handler
        self._dedupe_results: Dict[str, Dict[str, Any]] = {}

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

    async def execute(self, actions: Iterable[NotifyAction | OrderIntent | StatePatchAction | NoopAction]) -> Dict[str, Any]:
        order_intents, notifications, state_patches, noops = self.split_actions(actions)

        notification_count = 0
        if self.notification_handler:
            for notification in notifications:
                await self.notification_handler(notification)
                notification_count += 1

        order_results: List[Dict[str, Any]] = []
        for intent in order_intents:
            if intent.dedupe_key and intent.dedupe_key in self._dedupe_results:
                order_results.append(
                    {
                        "intent_type": intent.intent_type,
                        "dedupe_key": intent.dedupe_key,
                        "status": "deduped",
                        "result": self._dedupe_results[intent.dedupe_key],
                    }
                )
                continue

            if self.order_intent_handler is None:
                raise ValueError("Order intent handler is not configured")

            result = await self.order_intent_handler.handle(intent)
            if intent.dedupe_key:
                self._dedupe_results[intent.dedupe_key] = result
            order_results.append(
                {
                    "intent_type": intent.intent_type,
                    "dedupe_key": intent.dedupe_key,
                    "status": "executed",
                    "result": result,
                }
            )

        return {
            "order_results": order_results,
            "notification_count": notification_count,
            "state_patch_count": len(state_patches),
            "noop_count": len(noops),
        }
