from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from .models import AlgoCheckpoint


class InMemoryAlgoStateStore:
    def __init__(self) -> None:
        self._hot_state: Dict[str, Dict[str, Any]] = {}
        self._checkpoints: Dict[str, AlgoCheckpoint] = {}

    async def get_hot_state(self, instance_id: str) -> Dict[str, Any]:
        return deepcopy(self._hot_state.get(instance_id, {}))

    async def patch_hot_state(self, instance_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        state = self._hot_state.setdefault(instance_id, {})
        state.update(patch)
        return deepcopy(state)

    async def set_hot_state(self, instance_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        self._hot_state[instance_id] = deepcopy(state)
        return deepcopy(self._hot_state[instance_id])

    async def clear_hot_state(self, instance_id: str) -> None:
        self._hot_state.pop(instance_id, None)

    async def get_checkpoint(self, instance_id: str) -> Optional[AlgoCheckpoint]:
        checkpoint = self._checkpoints.get(instance_id)
        return checkpoint.model_copy(deep=True) if checkpoint else None

    async def set_checkpoint(self, checkpoint: AlgoCheckpoint) -> AlgoCheckpoint:
        self._checkpoints[checkpoint.instance_id] = checkpoint.model_copy(deep=True)
        return checkpoint
