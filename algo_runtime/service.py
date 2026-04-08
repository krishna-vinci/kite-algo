from __future__ import annotations

from typing import Any, Dict

from .kernel import AlgoKernel
from .models import TriggerEvent


class AlgoRuntimeService:
    def __init__(self, kernel: AlgoKernel) -> None:
        self.kernel = kernel
        self._started = False

    async def start(self) -> None:
        await self.kernel.load_active_instances()
        self._started = True

    async def stop(self) -> None:
        await self.kernel.clear_loaded_instances()
        self._started = False

    async def refresh_instances(self) -> None:
        if not self._started:
            raise RuntimeError("Algo runtime service is not started")
        await self.kernel.load_active_instances()

    async def dispatch_trigger(self, trigger: TriggerEvent) -> list[Dict[str, Any]]:
        if not self._started:
            raise RuntimeError("Algo runtime service is not started")
        return await self.kernel.dispatch_trigger(trigger)

    async def status(self) -> Dict[str, Any]:
        instances = await self.kernel.list_instances()
        dependency_summary = self.kernel.dependency_aggregator.summarize(instance.dependency_spec for instance in instances)
        checkpoints = await self.kernel.repository.list_checkpoints([instance.instance_id for instance in instances])
        instance_summaries = []
        for instance in instances:
            checkpoint = checkpoints.get(instance.instance_id)
            runtime_view = self.kernel.instance_runtime.get(instance.instance_id, {})
            instance_summaries.append(
                {
                    "instance_id": instance.instance_id,
                    "algo_type": instance.algo_type,
                    "lifecycle_state": instance.status.value,
                    "execution_mode": instance.execution_mode.value,
                    "pause_reason": instance.metadata.get("pause_reason") or runtime_view.get("pause_reason"),
                    "error_reason": instance.metadata.get("error_reason") or runtime_view.get("error_reason"),
                    "last_evaluated_at": (
                        checkpoint.last_evaluated_at.isoformat() if checkpoint and checkpoint.last_evaluated_at else runtime_view.get("last_evaluated_at")
                    ),
                    "last_action": checkpoint.last_action if checkpoint and checkpoint.last_action is not None else runtime_view.get("last_action"),
                    "last_action_count": runtime_view.get("last_action_count"),
                    "last_trigger": runtime_view.get("last_trigger"),
                    "last_error": runtime_view.get("last_error"),
                    "last_error_at": runtime_view.get("last_error_at"),
                }
            )
        return {
            "started": self._started,
            "instance_count": len(instances),
            "registered_types": list(self.kernel.registry.list_types()),
            "instance_ids": [instance.instance_id for instance in instances],
            "instances": instance_summaries,
            "load_summary": dict(self.kernel.last_load_summary),
            "dependency_summary": {
                **dependency_summary,
                "market_tokens": {str(token): mode.value for token, mode in dependency_summary["market_tokens"].items()},
                "account_scopes": sorted(dependency_summary["account_scopes"]),
                "triggers": sorted(dependency_summary["triggers"]),
            },
        }
