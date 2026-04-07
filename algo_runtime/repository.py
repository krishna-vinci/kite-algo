from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal
from .models import AlgoCheckpoint, AlgoInstance, AlgoLifecycleState


class AlgoInstanceRepository(Protocol):
    async def list_active_instances(self) -> List[AlgoInstance]: ...

    async def get_instance(self, instance_id: str) -> Optional[AlgoInstance]: ...

    async def save_instance(self, instance: AlgoInstance) -> AlgoInstance: ...

    async def update_status(self, instance_id: str, status: AlgoLifecycleState) -> Optional[AlgoInstance]: ...

    async def get_checkpoint(self, instance_id: str) -> Optional[AlgoCheckpoint]: ...

    async def list_checkpoints(self, instance_ids: List[str]) -> Dict[str, AlgoCheckpoint]: ...

    async def save_checkpoint(self, checkpoint: AlgoCheckpoint) -> AlgoCheckpoint: ...


class InMemoryAlgoRepository:
    def __init__(self) -> None:
        self.instances: Dict[str, AlgoInstance] = {}
        self.checkpoints: Dict[str, AlgoCheckpoint] = {}

    async def list_active_instances(self) -> List[AlgoInstance]:
        active = {
            AlgoLifecycleState.ENABLED,
            AlgoLifecycleState.RUNNING,
            AlgoLifecycleState.PAUSED,
        }
        return [instance.model_copy(deep=True) for instance in self.instances.values() if instance.status in active]

    async def get_instance(self, instance_id: str) -> Optional[AlgoInstance]:
        instance = self.instances.get(instance_id)
        return instance.model_copy(deep=True) if instance else None

    async def save_instance(self, instance: AlgoInstance) -> AlgoInstance:
        stored = instance.model_copy(deep=True)
        self.instances[instance.instance_id] = stored
        return stored.model_copy(deep=True)

    async def update_status(self, instance_id: str, status: AlgoLifecycleState) -> Optional[AlgoInstance]:
        instance = self.instances.get(instance_id)
        if instance is None:
            return None
        updated = instance.model_copy(update={"status": status, "updated_at": datetime.now(timezone.utc)})
        self.instances[instance_id] = updated
        return updated.model_copy(deep=True)

    async def get_checkpoint(self, instance_id: str) -> Optional[AlgoCheckpoint]:
        checkpoint = self.checkpoints.get(instance_id)
        return checkpoint.model_copy(deep=True) if checkpoint else None

    async def save_checkpoint(self, checkpoint: AlgoCheckpoint) -> AlgoCheckpoint:
        stored = checkpoint.model_copy(deep=True)
        self.checkpoints[checkpoint.instance_id] = stored
        return stored.model_copy(deep=True)

    async def list_checkpoints(self, instance_ids: List[str]) -> Dict[str, AlgoCheckpoint]:
        return {
            instance_id: checkpoint.model_copy(deep=True)
            for instance_id in instance_ids
            if (checkpoint := self.checkpoints.get(instance_id)) is not None
        }


def _row_mapping(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if isinstance(row, dict):
        return dict(row)
    return {
        key: getattr(row, key)
        for key in dir(row)
        if not key.startswith("_") and not callable(getattr(row, key))
    }


def _decode_json_field(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


class SqlAlchemyAlgoRepository:
    def __init__(self, session_factory: sessionmaker | Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    async def list_active_instances(self) -> List[AlgoInstance]:
        return await asyncio.to_thread(self._list_active_instances_sync)

    async def get_instance(self, instance_id: str) -> Optional[AlgoInstance]:
        return await asyncio.to_thread(self._get_instance_sync, instance_id)

    async def save_instance(self, instance: AlgoInstance) -> AlgoInstance:
        return await asyncio.to_thread(self._save_instance_sync, instance)

    async def update_status(self, instance_id: str, status: AlgoLifecycleState) -> Optional[AlgoInstance]:
        return await asyncio.to_thread(self._update_status_sync, instance_id, status)

    async def get_checkpoint(self, instance_id: str) -> Optional[AlgoCheckpoint]:
        return await asyncio.to_thread(self._get_checkpoint_sync, instance_id)

    async def save_checkpoint(self, checkpoint: AlgoCheckpoint) -> AlgoCheckpoint:
        return await asyncio.to_thread(self._save_checkpoint_sync, checkpoint)

    async def list_checkpoints(self, instance_ids: List[str]) -> Dict[str, AlgoCheckpoint]:
        if not instance_ids:
            return {}
        return await asyncio.to_thread(self._list_checkpoints_sync, instance_ids)

    def _list_active_instances_sync(self) -> List[AlgoInstance]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT instance_id, algo_type, status, config_json, dependency_spec_json, metadata_json, created_at, updated_at
                    FROM public.algo_instances
                    WHERE status IN ('enabled', 'running', 'paused')
                    ORDER BY created_at ASC
                    """
                )
            ).fetchall()
            return [self._instance_from_row(row) for row in rows]
        finally:
            db.close()

    def _get_instance_sync(self, instance_id: str) -> Optional[AlgoInstance]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT instance_id, algo_type, status, config_json, dependency_spec_json, metadata_json, created_at, updated_at
                    FROM public.algo_instances
                    WHERE instance_id = :instance_id
                    """
                ),
                {"instance_id": instance_id},
            ).fetchone()
            return self._instance_from_row(row) if row else None
        finally:
            db.close()

    def _save_instance_sync(self, instance: AlgoInstance) -> AlgoInstance:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_instances (
                        instance_id,
                        algo_type,
                        status,
                        config_json,
                        dependency_spec_json,
                        metadata_json,
                        created_at,
                        updated_at
                    ) VALUES (
                        :instance_id,
                        :algo_type,
                        :status,
                        CAST(:config_json AS JSONB),
                        CAST(:dependency_spec_json AS JSONB),
                        CAST(:metadata_json AS JSONB),
                        :created_at,
                        :updated_at
                    )
                    ON CONFLICT (instance_id) DO UPDATE SET
                        algo_type = EXCLUDED.algo_type,
                        status = EXCLUDED.status,
                        config_json = EXCLUDED.config_json,
                        dependency_spec_json = EXCLUDED.dependency_spec_json,
                        metadata_json = EXCLUDED.metadata_json,
                        updated_at = EXCLUDED.updated_at
                    RETURNING instance_id, algo_type, status, config_json, dependency_spec_json, metadata_json, created_at, updated_at
                    """
                ),
                {
                    "instance_id": instance.instance_id,
                    "algo_type": instance.algo_type,
                    "status": instance.status.value,
                    "config_json": _json_dumps(instance.config),
                    "dependency_spec_json": instance.dependency_spec.model_dump_json(),
                    "metadata_json": _json_dumps(instance.metadata),
                    "created_at": instance.created_at,
                    "updated_at": datetime.now(timezone.utc),
                },
            ).fetchone()
            db.commit()
            return self._instance_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _update_status_sync(self, instance_id: str, status: AlgoLifecycleState) -> Optional[AlgoInstance]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    UPDATE public.algo_instances
                    SET status = :status,
                        updated_at = :updated_at
                    WHERE instance_id = :instance_id
                    RETURNING instance_id, algo_type, status, config_json, dependency_spec_json, metadata_json, created_at, updated_at
                    """
                ),
                {
                    "instance_id": instance_id,
                    "status": status.value,
                    "updated_at": datetime.now(timezone.utc),
                },
            ).fetchone()
            db.commit()
            return self._instance_from_row(row) if row else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get_checkpoint_sync(self, instance_id: str) -> Optional[AlgoCheckpoint]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT instance_id, last_evaluated_at, last_action_json, state_json, updated_at
                    FROM public.algo_instance_checkpoints
                    WHERE instance_id = :instance_id
                    """
                ),
                {"instance_id": instance_id},
            ).fetchone()
            return self._checkpoint_from_row(row) if row else None
        finally:
            db.close()

    def _save_checkpoint_sync(self, checkpoint: AlgoCheckpoint) -> AlgoCheckpoint:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.algo_instance_checkpoints (
                        instance_id,
                        last_evaluated_at,
                        last_action_json,
                        state_json,
                        updated_at
                    ) VALUES (
                        :instance_id,
                        :last_evaluated_at,
                        CAST(:last_action_json AS JSONB),
                        CAST(:state_json AS JSONB),
                        :updated_at
                    )
                    ON CONFLICT (instance_id) DO UPDATE SET
                        last_evaluated_at = EXCLUDED.last_evaluated_at,
                        last_action_json = EXCLUDED.last_action_json,
                        state_json = EXCLUDED.state_json,
                        updated_at = EXCLUDED.updated_at
                    RETURNING instance_id, last_evaluated_at, last_action_json, state_json, updated_at
                    """
                ),
                {
                    "instance_id": checkpoint.instance_id,
                    "last_evaluated_at": checkpoint.last_evaluated_at,
                    "last_action_json": _json_dumps(checkpoint.last_action),
                    "state_json": _json_dumps(checkpoint.state),
                    "updated_at": datetime.now(timezone.utc),
                },
            ).fetchone()
            db.commit()
            return self._checkpoint_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _list_checkpoints_sync(self, instance_ids: List[str]) -> Dict[str, AlgoCheckpoint]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT instance_id, last_evaluated_at, last_action_json, state_json, updated_at
                    FROM public.algo_instance_checkpoints
                    WHERE instance_id IN :instance_ids
                    """
                ).bindparams(bindparam("instance_ids", expanding=True)),
                {"instance_ids": instance_ids},
            ).fetchall()
            checkpoints = [self._checkpoint_from_row(row) for row in rows]
            return {checkpoint.instance_id: checkpoint for checkpoint in checkpoints}
        finally:
            db.close()

    def _instance_from_row(self, row: Any) -> AlgoInstance:
        payload = _row_mapping(row)
        return AlgoInstance(
            instance_id=str(payload["instance_id"]),
            algo_type=str(payload["algo_type"]),
            status=AlgoLifecycleState(str(payload["status"])),
            config=_decode_json_field(payload.get("config_json")) or {},
            dependency_spec=_decode_json_field(payload.get("dependency_spec_json")) or {},
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.now(timezone.utc),
            updated_at=payload.get("updated_at") or datetime.now(timezone.utc),
        )

    def _checkpoint_from_row(self, row: Any) -> AlgoCheckpoint:
        payload = _row_mapping(row)
        return AlgoCheckpoint(
            instance_id=str(payload["instance_id"]),
            last_evaluated_at=payload.get("last_evaluated_at"),
            last_action=_decode_json_field(payload.get("last_action_json")),
            state=_decode_json_field(payload.get("state_json")) or {},
            updated_at=payload.get("updated_at") or datetime.now(timezone.utc),
        )
