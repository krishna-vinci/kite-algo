from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from .models import SafetyCheckResult, WorkerRunHealthSnapshot
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

    def attribution(self) -> JsonDict:
        """The run's canonical strategy identity, from the PERSISTED binding.

        A hosted run is bound to its canonical strategy by the supervisor, so the
        strategy id, owner and account are platform facts - never something a
        strategy passes in as a parameter. An unbound/legacy run reports
        ``{"attributed": False, ...}`` instead of a guessed identity.
        """
        raw = self.run.get("strategy_attribution")
        if not isinstance(raw, Mapping):
            return {
                "attributed": False,
                "strategy_id": None,
                "owner_id": None,
                "account_id": str(self.run.get("account_scope") or ""),
                "execution_environment": str(self.run.get("execution_mode") or ""),
                "reason": "unattributed",
            }
        return {
            "attributed": True,
            "strategy_id": str(raw.get("strategy_id") or ""),
            "owner_id": str(raw.get("owner_id") or ""),
            "account_id": str(raw.get("account_id") or ""),
            "execution_environment": str(raw.get("execution_environment") or ""),
            "binding_source": str(raw.get("binding_source") or ""),
        }

    def refresh(self) -> dict[str, Any]:
        self.run = self.client.get_run(self.run_id)
        return self.run

    def get_health_snapshot(self) -> WorkerRunHealthSnapshot:
        return self.client.get_run_health_snapshot(self.run_id)

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

    def progress(self, note: str | None = None) -> JsonDict:
        """Report child progress to the hosted attempt.

        Requires the attach run's session nonce. This is the child's own liveness
        signal; it is not a heartbeat and the supervisor never fabricates it.
        """
        if self.session_nonce is None:
            raise ValueError("ManagedRun progress requires a session nonce")
        return self.client.run_progress(self.run_id, session_nonce=self.session_nonce, note=note)

    def notify(
        self,
        text: str,
        *,
        channels: Iterable[str],
        idempotency_key: str,
        subject: str | None = None,
    ) -> JsonDict:
        """Enqueue a run-scoped notification for this hosted attempt.

        Requires the attach run's session nonce. The caller's ``idempotency_key``
        deduplicates a repeat with the same content and conflicts on different
        content; a notification outcome never authorizes trading.
        """
        if self.session_nonce is None:
            raise ValueError("ManagedRun notify requires a session nonce")
        return self.client.notify_run(
            self.run_id,
            channels=channels,
            text=text,
            idempotency_key=idempotency_key,
            subject=subject,
            session_nonce=self.session_nonce,
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

    def log_decision_event(self, **payload: Any) -> JsonDict:
        return self.client.log_decision_event(self.run_id, **payload)

    def list_timeline(self, **params: Any) -> JsonDict:
        return self.client.list_timeline(self.run_id, **params)

    def stream_timeline(self, **params: Any):
        return self.client.stream_timeline(self.run_id, **params)

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

    def submit_proposal(self, payload: Mapping[str, Any]) -> JsonDict:
        """Submit this run's proposal, with the hosted session nonce attached.

        ``strategy_run_id`` defaults to this run: the platform still derives
        strategy, account and the bound evaluation id from its own records, so
        the payload cannot rebind the run.
        """
        body = dict(payload)
        body.setdefault("strategy_run_id", self.run_id)
        return self.client.submit_proposal(body, session_nonce=self.session_nonce)

    # -- governed execution (Phase 2) --------------------------------------

    def request_execution(self, plan_id: str, *, idempotency_key: str) -> JsonDict:
        """Ask the platform to execute one frozen plan of this run.

        Durably idempotent under ``idempotency_key``. Whether this queues or
        waits is decided by the strategy's authorization mode and (for
        autonomous mode) by a matching owner-issued grant - never by this call.
        """
        return self.client.request_execution(
            self.run_id,
            plan_id,
            idempotency_key=idempotency_key,
            session_nonce=self.session_nonce,
        )

    def execution_requests(self, *, limit: int = 50) -> JsonDict:
        """This run's durable execution requests, newest first."""
        return self.client.list_execution_requests(self.run_id, limit=limit)

    def execution_request(self, request_id: str) -> JsonDict:
        """One execution request, scoped to this run."""
        return self.client.get_execution_request(request_id, strategy_run_id=self.run_id)

    def owned_work(self) -> JsonDict:
        """The strategy's own filled positions plus its pending execution work.

        Use this (not an account-net read) to decide whether a further
        adjustment is still outstanding: a repeated observation that sees the
        same pending quantity must not place the same adjustment again.
        """
        return self.client.get_owned_work(self.run_id)

    def submit_and_request_execution(
        self, payload: Mapping[str, Any], *, idempotency_key: str
    ) -> JsonDict:
        """Convenience: submit a proposal, then request execution of its plan.

        Both identities stay durable and separate - the proposal is still one
        evaluation's envelope and the request is still its own idempotent row.
        """
        submitted = self.submit_proposal(payload)
        plan = dict(submitted.get("plan") or {})
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            # The refusal is the caller's answer, not a crash: name the reason the
            # platform gave instead of hiding it behind a bare ValueError.
            refusal = dict(submitted.get("refusal") or {})
            raise ValueError(
                "the proposal produced no frozen plan to request: "
                f"status={submitted.get('status')!r} "
                f"refusal={refusal or submitted.get('detail') or {}}"
            )
        request = self.request_execution(plan_id, idempotency_key=idempotency_key)
        return {"proposal": submitted, "execution_request": request}
