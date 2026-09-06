"""Shared discovery and invocation policy.

Visibility is a convenience for hosts; every dispatch still checks the local
profile and refreshes the worker's current capability response.  The worker
API remains the authoritative revocation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .catalog import TOOL_CATALOG, ToolSpec
from .config import MCPConfig


class PolicyViolation(PermissionError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class BackendCapabilities:
    actions: set[str] | None = None
    modes: set[str] | None = None
    accounts: set[str] | None = None
    templates: set[str] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_health(cls, health: Mapping[str, Any] | None) -> "BackendCapabilities":
        body = dict(health or {})
        nested = body.get("capabilities") if isinstance(body.get("capabilities"), Mapping) else {}

        def values(*keys: str) -> set[str] | None:
            for source in (body, nested):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, (list, tuple, set)):
                        result = {str(item).strip().lower() for item in value if str(item).strip()}
                        return result
            return None

        def scalar_values(*keys: str) -> set[str] | None:
            """Normalize scalar capability fields without making omission permissive.

            The worker health contract currently reports one token account as
            ``account_scope`` (a scalar), while future capability envelopes may
            use ``account_scopes`` or a nested list.  A present empty scalar is
            still an explicit empty capability set.
            """

            for source in (body, nested):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, str):
                        cleaned = value.strip().lower()
                        return {cleaned} if cleaned else set()
            return None

        account_values = values("allowed_accounts", "accounts", "account_scopes")
        if account_values is None:
            account_values = scalar_values("account_scope")

        return cls(
            actions=values("allowed_actions", "actions"),
            modes=values("allowed_modes", "execution_modes", "modes"),
            accounts=account_values,
            templates=values("allowed_templates", "templates"),
            raw=body,
        )


@dataclass
class PolicyService:
    config: MCPConfig
    capabilities: BackendCapabilities = field(default_factory=BackendCapabilities)

    def spec(self, name: str) -> ToolSpec:
        try:
            return TOOL_CATALOG[name]
        except KeyError as exc:
            raise PolicyViolation("unknown_tool", f"tool {name!r} is not in the reviewed catalog") from exc

    def visible(self, name: str) -> bool:
        spec = self.spec(name)
        if spec.effect == "read":
            return True
        if spec.effect == "data_write":
            return self.config.allow_data_refresh and self.config.profile in {"paper", "live"}
        if self.config.profile not in {"paper", "live"}:
            return False
        if spec.live_only and self.config.profile != "live":
            return False
        return True

    def visible_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in TOOL_CATALOG.values() if self.visible(spec.name))

    def backend_visible(self, name: str) -> bool:
        """Return whether a statically visible tool survives health filtering.

        Tool registration is profile-based because construction must not do
        network I/O.  Once the lifespan has a worker health response, FastMCP
        can hide tools whose required action is explicitly revoked.  Argument
        dependent mode/account/template checks still happen at invocation,
        where the concrete selection is available.
        """

        spec = self.spec(name)
        if not self.visible(name):
            return False
        if self.capabilities.actions is not None and spec.required_action:
            if spec.required_action.lower() not in self.capabilities.actions and "*" not in self.capabilities.actions:
                return False
        if self.capabilities.modes is not None and not self.capabilities.modes and spec.effect != "read":
            return False
        if self.capabilities.accounts is not None and not self.capabilities.accounts and spec.scope == "account":
            return False
        return True

    def backend_visible_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in TOOL_CATALOG.values() if self.backend_visible(spec.name))

    async def refresh(self, client: Any) -> BackendCapabilities:
        health = getattr(client, "health", None)
        if callable(health):
            response = await health()
            if isinstance(response, Mapping):
                self.capabilities = BackendCapabilities.from_health(response)
        return self.capabilities

    def _check_profile_arguments(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
        if not self.visible(spec.name):
            raise PolicyViolation("tool_disabled", f"{spec.name} is disabled for the {self.config.profile} profile")
        mode = arguments.get("execution_mode") or arguments.get("mode")
        if mode is not None and str(mode).lower() == "live" and self.config.profile != "live":
            raise PolicyViolation("live_profile_required", "live execution requires KITE_MCP_PROFILE=live")
        if self.capabilities.modes is not None and mode is not None and str(mode).lower() not in self.capabilities.modes:
            raise PolicyViolation("mode_not_allowed", "the selected execution mode is not authorized for this token")
        account = arguments.get("account_scope")
        if account is None:
            account = arguments.get("account")
        if self.capabilities.accounts is not None and account is not None and str(account).lower() not in self.capabilities.accounts:
            raise PolicyViolation("account_not_allowed", "the selected account is not authorized for this token")
        template = arguments.get("template_id")
        # The worker API uses an empty allowed_templates list to mean that no
        # template restriction was configured; preserve that backend semantic.
        if self.capabilities.templates and template is not None and str(template).lower() not in self.capabilities.templates:
            raise PolicyViolation("template_not_allowed", "the selected template is not authorized for this token")
        if spec.live_only and self.config.profile != "live":
            raise PolicyViolation("live_only", f"{spec.name} is account-level and available only in the live profile")

    def _check_backend(self, spec: ToolSpec) -> None:
        if self.capabilities.actions is not None and spec.required_action:
            action = spec.required_action.lower()
            actions = self.capabilities.actions
            if action not in actions and "*" not in actions:
                raise PolicyViolation("backend_action_denied", f"worker token does not allow {spec.required_action}")

    async def authorize(self, name: str, arguments: Mapping[str, Any], client: Any, *, refresh: bool = True) -> ToolSpec:
        spec = self.spec(name)
        # Profile visibility is static and safe to check before contacting the
        # worker.  Dynamic account/mode/template checks must use the freshest
        # health response, otherwise a revoked scope can pass one invocation.
        if not self.visible(spec.name):
            self._check_profile_arguments(spec, arguments)
        if refresh and name != "get_capabilities":
            await self.refresh(client)
        self._check_profile_arguments(spec, arguments)
        self._check_backend(spec)
        return spec


__all__ = ["PolicyViolation", "BackendCapabilities", "PolicyService"]
