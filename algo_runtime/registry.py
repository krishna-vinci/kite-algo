from __future__ import annotations

import inspect
from typing import Any, Dict, Iterable, Protocol, Type

from .models import AlgoInstance


class RegistryError(ValueError):
    """Raised when algo registration or lookup fails."""


class AlgoModule(Protocol):
    ALGO_TYPE: str

    def __init__(self, instance: AlgoInstance, **kwargs: Any) -> None: ...

    async def initialize(self, context: Any) -> None: ...

    async def evaluate(self, snapshot: Any, state: Dict[str, Any]) -> Any: ...


class AlgoRegistry:
    def __init__(self) -> None:
        self._registry: Dict[str, Type[AlgoModule]] = {}

    def register(self, algo_cls: Type[AlgoModule], *, algo_type: str | None = None) -> str:
        resolved_type = (algo_type or getattr(algo_cls, "ALGO_TYPE", "") or "").strip()
        if not resolved_type:
            raise RegistryError("algo class must define ALGO_TYPE or pass algo_type")
        for method_name in ("initialize", "evaluate"):
            method = getattr(algo_cls, method_name, None)
            if not callable(method):
                raise RegistryError(f"algo class '{resolved_type}' is missing callable '{method_name}'")
            if not inspect.iscoroutinefunction(method):
                raise RegistryError(f"algo class '{resolved_type}' must define async '{method_name}'")
        if resolved_type in self._registry:
            raise RegistryError(f"algo type '{resolved_type}' is already registered")
        self._registry[resolved_type] = algo_cls
        return resolved_type

    def get(self, algo_type: str) -> Type[AlgoModule]:
        resolved_type = str(algo_type or "").strip()
        if resolved_type not in self._registry:
            raise RegistryError(f"algo type '{resolved_type}' is not registered")
        return self._registry[resolved_type]

    def create(self, instance: AlgoInstance, **kwargs: Any) -> AlgoModule:
        algo_cls = self.get(instance.algo_type)
        return algo_cls(instance, **kwargs)

    def list_types(self) -> Iterable[str]:
        return tuple(sorted(self._registry.keys()))

    def has(self, algo_type: str) -> bool:
        return str(algo_type or "").strip() in self._registry
