from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from .models import SafetyCheckResult
from .protection import BackendProtection
from .run_config import RunConfig

if TYPE_CHECKING:
    from .client import KiteAlgoWorkerClient


JsonDict = dict[str, Any]


@dataclass
class ManagedRun:
    client: "KiteAlgoWorkerClient"
    config: RunConfig
    run: dict[str, Any]
    session_nonce: str | None = None

    @property
    def run_id(self) -> str:
        return str(self.run["strategy_run_id"])

    def refresh(self) -> dict[str, Any]:
        self.run = self.client.get_run(self.run_id)
        return self.run

    def heartbeat(
        self,
        *,
        worker_id: str | None = None,
        status: str = "healthy",
        metrics: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        if self.session_nonce is None:
            raise ValueError("ManagedRun heartbeat requires claimed session nonce")
        return self.client.run_heartbeat(
            self.run_id,
            session_nonce=self.session_nonce,
            worker_id=worker_id,
            status=status,
            metrics=metrics,
        )

    def safety_check(self) -> SafetyCheckResult:
        return self.client.safety_check(self.run_id)

    def place_order(
        self,
        order: Mapping[str, Any],
        *,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
        safety_token: str | None = None,
    ) -> JsonDict:
        return self.client.place_order(
            self.run_id,
            order,
            idempotency_key,
            metadata=metadata,
            safety_token=safety_token,
            session_nonce=self.session_nonce,
        )

    def place_basket(
        self,
        orders: Iterable[Mapping[str, Any]],
        *,
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
        all_or_none: bool = False,
        dry_run: bool = False,
        safety_token: str | None = None,
    ) -> JsonDict:
        return self.client.place_basket(
            self.run_id,
            orders,
            idempotency_key,
            metadata=metadata,
            all_or_none=all_or_none,
            dry_run=dry_run,
            safety_token=safety_token,
            session_nonce=self.session_nonce,
        )

    def patch_risk(self, patch: Mapping[str, Any], *, reason: str | None = None) -> JsonDict:
        return self.client.patch_risk(self.run_id, patch, reason=reason, session_nonce=self.session_nonce)

    def update_backend_protection(
        self,
        protection: BackendProtection,
        *,
        reason: str | None = None,
        reset_trailing: bool = True,
    ) -> JsonDict:
        return self.client.update_backend_protection(
            self.run_id,
            protection,
            reason=reason,
            reset_trailing=reset_trailing,
            session_nonce=self.session_nonce,
        )

    def exit_run(
        self,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> JsonDict:
        return self.client.exit_run(
            self.run_id,
            reason=reason,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            session_nonce=self.session_nonce,
        )
