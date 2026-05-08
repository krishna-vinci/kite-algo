from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import ModelMixin
from .protection import BackendProtection


@dataclass(frozen=True)
class RunConfig(ModelMixin):
    template_id: str
    account_scope: str
    execution_mode: str = "paper"
    strategy_run_id: str | None = None
    summary_fields: list[dict[str, Any]] = field(default_factory=list)
    risk_schema: list[dict[str, Any]] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=lambda: ["edit_risk", "exit_strategy"])
    runtime_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    backend_protection: BackendProtection | None = None

    def _replace(self, **updates: Any) -> "RunConfig":
        data = {
            "template_id": self.template_id,
            "account_scope": self.account_scope,
            "execution_mode": self.execution_mode,
            "strategy_run_id": self.strategy_run_id,
            "summary_fields": [dict(item) for item in self.summary_fields],
            "risk_schema": [dict(item) for item in self.risk_schema],
            "allowed_actions": list(self.allowed_actions),
            "runtime_state": dict(self.runtime_state),
            "metadata": dict(self.metadata),
            "backend_protection": self.backend_protection,
        }
        data.update(updates)
        return RunConfig(**data)

    def __post_init__(self) -> None:
        object.__setattr__(self, "template_id", str(self.template_id))
        object.__setattr__(self, "account_scope", str(self.account_scope))
        object.__setattr__(self, "execution_mode", str(self.execution_mode))
        object.__setattr__(self, "strategy_run_id", None if self.strategy_run_id is None else str(self.strategy_run_id))
        object.__setattr__(self, "summary_fields", [dict(item) for item in list(self.summary_fields or [])])
        object.__setattr__(self, "risk_schema", [dict(item) for item in list(self.risk_schema or [])])
        object.__setattr__(self, "allowed_actions", [str(item) for item in list(self.allowed_actions or [])])
        object.__setattr__(self, "runtime_state", dict(self.runtime_state or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def with_summary_field(self, key: str, value: Any, **extra: Any) -> "RunConfig":
        entry: dict[str, Any] = {"key": str(key), "value": value}
        entry.update({k: v for k, v in extra.items()})
        return self._replace(summary_fields=[*self.summary_fields, entry])

    def with_metadata(self, **kwargs: Any) -> "RunConfig":
        return self._replace(metadata={**self.metadata, **kwargs})

    def with_runtime_state_patch(self, **patch: Any) -> "RunConfig":
        return self._replace(runtime_state={**self.runtime_state, **patch})

    def with_risk_patch(self, **patch: Any) -> "RunConfig":
        risk = dict(self.runtime_state.get("risk") or {})
        risk.update(patch)
        runtime_state = dict(self.runtime_state)
        runtime_state["risk"] = risk
        return self._replace(runtime_state=runtime_state)

    def with_allowed_actions(self, *actions: str) -> "RunConfig":
        merged: list[str] = list(self.allowed_actions)
        for action in actions:
            normalized = str(action)
            if normalized not in merged:
                merged.append(normalized)
        return self._replace(allowed_actions=merged)

    def with_backend_protection(self, protection: BackendProtection) -> "RunConfig":
        return self._replace(backend_protection=protection)

    def to_create_run_payload(self) -> dict[str, Any]:
        runtime_state_payload = dict(self.runtime_state)
        if self.backend_protection is not None:
            runtime_state_payload["backend_protection"] = self.backend_protection.to_dict()
        payload: dict[str, Any] = {
            "template_id": self.template_id,
            "account_scope": self.account_scope,
            "execution_mode": self.execution_mode,
            "summary_fields": [dict(item) for item in self.summary_fields],
            "risk_schema": [dict(item) for item in self.risk_schema],
            "allowed_actions": list(self.allowed_actions),
            "runtime_state": runtime_state_payload,
            "metadata": dict(self.metadata),
        }
        if self.strategy_run_id is not None:
            payload["strategy_run_id"] = self.strategy_run_id
        return payload
