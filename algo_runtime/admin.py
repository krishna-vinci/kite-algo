from __future__ import annotations

from typing import Any, Dict

from .models import AlgoInstance, AlgoLifecycleState


async def list_instances(service: Any, *, live_worker: Any | None = None) -> Dict[str, Any]:
    instances = await service.kernel.list_instances()
    return {
        "items": [instance.model_dump(mode="json") for instance in instances],
        "live_worker": live_worker.status() if live_worker else None,
    }


async def refresh_runtime(service: Any, *, live_worker: Any | None = None) -> Dict[str, Any]:
    await service.refresh_instances()
    live_status = await live_worker.sync_dependencies() if live_worker else None
    return {
        "runtime": await service.status(),
        "live_worker": live_status,
    }


async def upsert_instance(service: Any, payload: Dict[str, Any] | AlgoInstance, *, live_worker: Any | None = None) -> Dict[str, Any]:
    instance = payload if isinstance(payload, AlgoInstance) else AlgoInstance.model_validate(payload)
    existing = await service.kernel.repository.get_instance(instance.instance_id)
    if existing is not None:
        instance = instance.model_copy(update={"created_at": existing.created_at})
    saved = await service.kernel.repository.save_instance(instance)
    refreshed = await refresh_runtime(service, live_worker=live_worker)
    return {
        "instance": saved.model_dump(mode="json"),
        **refreshed,
    }


async def update_instance_status(
    service: Any,
    *,
    instance_id: str,
    status: AlgoLifecycleState,
    live_worker: Any | None = None,
) -> Dict[str, Any] | None:
    updated = await service.kernel.repository.update_status(instance_id, status)
    if updated is None:
        return None
    refreshed = await refresh_runtime(service, live_worker=live_worker)
    return {
        "instance": updated.model_dump(mode="json"),
        **refreshed,
    }
