from __future__ import annotations

from typing import Any, Dict

from algo_runtime.models import OrderIntent


class PaperIntentHandler:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def handle(self, intent: OrderIntent, *, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return await self.service.execute_intent(intent=intent, instance_context=context or {})


class DryRunIntentHandler:
    async def handle(self, intent: OrderIntent, *, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {
            "mode": "dry_run",
            "intent_type": intent.intent_type,
            "mutated_state": False,
            "payload": dict(intent.payload or {}),
            "context": dict(context or {}),
        }
