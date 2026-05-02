from __future__ import annotations

from typing import Any, Dict

from algo_runtime.execution_attribution import build_execution_attribution
from algo_runtime.models import OrderIntent


class PaperIntentHandler:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def handle(self, intent: OrderIntent, *, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return await self.service.execute_intent(intent=intent, instance_context=context or {})


class DryRunIntentHandler:
    async def handle(self, intent: OrderIntent, *, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        context = dict(context or {})
        metadata = dict(context.get("metadata") or {})
        dependency_spec = dict(context.get("dependency_spec") or {})
        strategy_run_id = str(metadata.get("strategy_run_id") or dependency_spec.get("strategy_run_id") or context.get("instance_id") or "dry-run").strip()
        return {
            "mutated_state": False,
            "mode": "dry_run",
            "status": "validated",
            "intent_type": intent.intent_type,
            "payload": dict(intent.payload or {}),
            "context": context,
            "attribution": build_execution_attribution(
                execution_mode="dry_run",
                strategy_run_id=strategy_run_id,
                strategy_family=str(metadata.get("strategy_family") or dependency_spec.get("strategy_family") or "indicator_strategy"),
                strategy_name=str(metadata.get("strategy_name") or dependency_spec.get("strategy_name") or context.get("algo_type") or strategy_run_id),
                account_ref=str(dependency_spec.get("account_scope") or metadata.get("account_ref") or "kite:paper-dry-run"),
                entry_surface=str(metadata.get("entry_surface") or dependency_spec.get("entry_surface") or "algo_runtime"),
                source=str(metadata.get("source") or dependency_spec.get("source") or "algo_runtime"),
                idempotency_key=str(getattr(intent, "dedupe_key", None) or dependency_spec.get("idempotency_key") or f"{strategy_run_id}:{intent.intent_type}"),
                metadata=metadata,
                extras={
                    "algo_instance_id": context.get("instance_id"),
                    "algo_type": context.get("algo_type"),
                    "strategy_tag": metadata.get("strategy_tag") or context.get("algo_type"),
                },
            ),
        }
