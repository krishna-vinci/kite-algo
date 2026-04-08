from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from pydantic import BaseModel

from .dependencies import DependencyAggregator
from .intent_bridge import IntentBridge
from .models import AlgoCheckpoint, AlgoInstance, AlgoLifecycleState, DependencySpec, StatePatchAction, TriggerEvent
from .registry import AlgoRegistry
from .repository import AlgoInstanceRepository
from .snapshot_builder import SnapshotBuilder
from .state_store import InMemoryAlgoStateStore
from .triggers import trigger_matches


logger = logging.getLogger(__name__)


def _action_to_payload(action: Any) -> Any:
    if isinstance(action, BaseModel):
        return action.model_dump(mode="json")
    return action


class AlgoKernel:
    def __init__(
        self,
        *,
        registry: AlgoRegistry,
        repository: AlgoInstanceRepository,
        state_store: InMemoryAlgoStateStore,
        snapshot_builder: SnapshotBuilder | None = None,
        dependency_aggregator: DependencyAggregator | None = None,
        intent_bridge: IntentBridge | None = None,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.state_store = state_store
        self.snapshot_builder = snapshot_builder or SnapshotBuilder()
        self.dependency_aggregator = dependency_aggregator or DependencyAggregator()
        self.intent_bridge = intent_bridge
        self.instances: Dict[str, AlgoInstance] = {}
        self.modules: Dict[str, Any] = {}
        self.instance_runtime: Dict[str, Dict[str, Any]] = {}
        self.last_load_summary: Dict[str, Any] = {"active_count": 0, "loaded_count": 0, "skipped": []}

    async def load_active_instances(self) -> List[AlgoInstance]:
        active_instances = await self.repository.list_active_instances()
        loaded: Dict[str, AlgoInstance] = {}
        loaded_modules: Dict[str, Any] = {}
        skipped: List[Dict[str, str]] = []
        for instance in active_instances:
            if not self.registry.has(instance.algo_type):
                skipped.append({"instance_id": instance.instance_id, "algo_type": instance.algo_type, "reason": "unregistered_type"})
                continue
            loaded[instance.instance_id] = instance
            module = self.registry.create(instance)
            await module.initialize({"instance": instance, "kernel": self})
            loaded_modules[instance.instance_id] = module
            self.instance_runtime[instance.instance_id] = {
                **self.instance_runtime.get(instance.instance_id, {}),
                "lifecycle_state": instance.status.value,
                "pause_reason": instance.metadata.get("pause_reason"),
                "error_reason": instance.metadata.get("error_reason"),
            }
        self.instances = loaded
        self.modules = loaded_modules
        self.last_load_summary = {
            "active_count": len(active_instances),
            "loaded_count": len(loaded),
            "skipped": skipped,
        }
        return list(self.instances.values())

    async def clear_loaded_instances(self) -> None:
        self.instances = {}
        self.modules = {}
        self.instance_runtime = {}

    async def list_instances(self) -> List[AlgoInstance]:
        return list(self.instances.values())

    async def update_instance_status(self, instance_id: str, status: AlgoLifecycleState) -> AlgoInstance | None:
        updated = await self.repository.update_status(instance_id, status)
        if updated is not None:
            self.instances[instance_id] = updated
            self.instance_runtime[instance_id] = {
                **self.instance_runtime.get(instance_id, {}),
                "lifecycle_state": updated.status.value,
                "pause_reason": updated.metadata.get("pause_reason"),
                "error_reason": updated.metadata.get("error_reason"),
            }
        return updated

    async def aggregated_dependencies(self) -> DependencySpec:
        return self.dependency_aggregator.aggregate(instance.dependency_spec for instance in self.instances.values())

    async def dispatch_trigger(self, trigger: TriggerEvent) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for instance in self.instances.values():
            if instance.status not in {AlgoLifecycleState.ENABLED, AlgoLifecycleState.RUNNING}:
                continue
            if not trigger_matches(instance.dependency_spec, trigger):
                continue
            module = self.modules.get(instance.instance_id)
            if module is None:
                logger.warning("Skipping trigger for unloaded algo instance %s", instance.instance_id)
                continue
            original_state = await self.state_store.get_hot_state(instance.instance_id)
            pending_state = dict(original_state)
            try:
                snapshot = await self.snapshot_builder.build_for_instance(instance, trigger)
                actions = await module.evaluate(snapshot, original_state)
                normalized_actions = [] if actions is None else list(actions if isinstance(actions, list) else [actions])
                for action in normalized_actions:
                    if isinstance(action, StatePatchAction):
                        pending_state.update(action.patch)
                execution = (
                    await self.intent_bridge.execute(
                        normalized_actions,
                        execution_mode=instance.execution_mode,
                        context=instance.model_dump(mode="json"),
                    )
                    if self.intent_bridge
                    else None
                )
                await self.state_store.set_hot_state(instance.instance_id, pending_state)
                checkpoint = AlgoCheckpoint(
                    instance_id=instance.instance_id,
                    last_evaluated_at=datetime.now(timezone.utc),
                    last_action=_action_to_payload(normalized_actions[-1]) if normalized_actions else None,
                    state=pending_state,
                    updated_at=datetime.now(timezone.utc),
                )
                await self.state_store.set_checkpoint(checkpoint)
                await self.repository.save_checkpoint(checkpoint)
                self.instance_runtime[instance.instance_id] = {
                    **self.instance_runtime.get(instance.instance_id, {}),
                    "lifecycle_state": instance.status.value,
                    "last_evaluated_at": checkpoint.last_evaluated_at.isoformat() if checkpoint.last_evaluated_at else None,
                    "last_action": checkpoint.last_action,
                    "last_action_count": len(normalized_actions),
                    "last_trigger": trigger.model_dump(mode="json", by_alias=True),
                    "last_execution": execution,
                    "last_error": None,
                    "last_error_at": None,
                }
                results.append(
                    {
                    "instance_id": instance.instance_id,
                    "algo_type": instance.algo_type,
                    "execution_mode": instance.execution_mode.value,
                    "action_count": len(normalized_actions),
                    "actions": [_action_to_payload(action) for action in normalized_actions],
                    "execution": execution,
                    }
                )
            except Exception as exc:
                logger.error("Algo trigger dispatch failed for %s: %s", instance.instance_id, exc, exc_info=True)
                self.instance_runtime[instance.instance_id] = {
                    **self.instance_runtime.get(instance.instance_id, {}),
                    "lifecycle_state": instance.status.value,
                    "last_error": str(exc),
                    "last_error_at": datetime.now(timezone.utc).isoformat(),
                    "last_trigger": trigger.model_dump(mode="json", by_alias=True),
                }
                results.append(
                    {
                        "instance_id": instance.instance_id,
                        "algo_type": instance.algo_type,
                        "execution_mode": instance.execution_mode.value,
                        "action_count": 0,
                        "actions": [],
                        "execution": None,
                        "error": str(exc),
                    }
                )
        return results
