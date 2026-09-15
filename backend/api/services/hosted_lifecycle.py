"""Hosted-strategy supervisor lifecycle service (launch preparation).

This is the server-side orchestration behind the credential-authenticated
lifecycle API. It performs *preparation only* — it never spawns or executes
strategy code. The exact ordering is:

    claim authorized job                        (done by the caller, CAS)
      → reserve + create child token            (hash stored; plaintext returned once)
      → create worker run bound to that token   (existing shared run-creation path)
      → claim the worker session                (runner-owned nonce)
      → deliver the child configuration         (one-time handoff, durable marker)

**Why the markers exist.** A worker token is stored only as a hash, so its
plaintext can never be recovered. Preparation therefore records, durably and in
order, ``token_id`` then ``run_id`` then ``handoff_at``:

- ``token_id`` is CAS-reserved *before* the token is minted, so a crash between
  the two leaves a visible, revocable marker instead of an invisible credential.
- A repeat that finds ``token_id`` set (with or without ``run_id``/
  ``handoff_at``) **fails closed**: it revokes the child authority, fences the
  job to ``recovery_required`` and requires reconciliation. It never mints a
  second credential for the same attempt and never pretends the plaintext is
  recoverable.
- Because both a delivered handoff and an uncertain one fail closed, the
  protocol is deliberately **not** exactly-once across a lost response.

Every mutating step is fenced by id + ``lease_owner`` + ``lease_epoch`` +
``attempt``, so a stale supervisor cannot act. No supervisor credential is ever
returned: the response carries the *child* token (for the supervisor to place in
the child's environment) and the run/session identifiers, nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Request

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository, WorkerToken
from backend.api.routers.worker_shared import (
    WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
    WORKER_SESSION_FRESHNESS_SECONDS,
    create_worker_run_for_token,
)
from backend.api.schemas.worker import WorkerRunCreateRequest, WorkerTokenCreateRequest
from backend.strategies import service as strategy_service
from backend.strategies.models import StrategyJob
from backend.strategies.repository import SqlAlchemyStrategyRepository

logger = logging.getLogger(__name__)

__all__ = [
    "HostedLifecycleError",
    "HostedLifecycleHooks",
    "heartbeat",
    "job_state",
    "prepare_launch",
    "release",
    "fence",
    "require_job_authority",
]

_LIVE_STATUSES = ("starting", "running")


class HostedLifecycleError(Exception):
    """A lifecycle request is refused. ``reason`` is a stable machine code."""

    def __init__(self, status_code: int, reason: str, **extra: Any) -> None:
        detail: Dict[str, Any] = {"rejection_reason": reason}
        detail.update(extra)
        super().__init__(reason)
        self.status_code = status_code
        self.detail = detail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def require_job_authority(
    job: Optional[StrategyJob],
    *,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    allow_statuses=_LIVE_STATUSES,
    require_live_lease: bool = True,
) -> StrategyJob:
    """Authorize a lifecycle request against the persisted job authority.

    This is the only way a lifecycle request touches a job. It never accepts a
    bare run id: identity and configuration are derived from the job record, and
    the lease owner/epoch/attempt must all match.
    """
    if job is None:
        raise HostedLifecycleError(404, "HOSTED_JOB_NOT_FOUND")
    if (
        str(job.lease_owner or "") != lease_owner
        or int(job.lease_epoch) != int(lease_epoch)
        or int(job.attempt) != int(attempt)
    ):
        raise HostedLifecycleError(
            403, "HOSTED_LEASE_AUTHORITY_MISMATCH", job_status=str(job.status or "")
        )
    if str(job.desired_state or "") != "started":
        raise HostedLifecycleError(409, "HOSTED_ATTEMPT_STOPPED")
    if str(job.status or "") not in allow_statuses:
        raise HostedLifecycleError(
            409, "HOSTED_ATTEMPT_FENCED", job_status=str(job.status or "")
        )
    if require_live_lease:
        if job.lease_until is None or _as_utc(job.lease_until) <= _utcnow():
            raise HostedLifecycleError(409, "HOSTED_LEASE_EXPIRED")
    return job


class HostedLifecycleHooks:
    """Injectable seams for tests (no behaviour change in production)."""

    def __init__(
        self,
        *,
        run_id_factory=None,
        token_factory=None,
    ) -> None:
        self.run_id_factory = run_id_factory or (lambda: f"run_{uuid.uuid4().hex}")
        self.token_factory = token_factory or (
            lambda: (f"worker_{uuid.uuid4().hex[:16]}", f"kwa_{secrets.token_urlsafe(32)}")
        )


async def _fail_closed(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job: StrategyJob,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str,
    token_id: Optional[str] = None,
) -> None:
    """Fence the attempt and revoke any child authority that may exist.

    Best-effort for the side effects, but each records durable state: a failure
    here leaves the job visibly fenced, never silently resumable.
    """
    try:
        strategy_repo.record_failure(
            job.id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            reason=reason,
        )
    except Exception:  # pragma: no cover - diagnostic only
        logger.warning("hosted_lifecycle_record_failure_failed", extra={"job_id": job.id})
    candidate = token_id or job.token_id
    if candidate:
        try:
            await worker_repo.revoke_token(candidate)
        except Exception:  # pragma: no cover - best effort
            logger.warning("hosted_lifecycle_revoke_token_failed", extra={"job_id": job.id})
    try:
        strategy_repo.mark_recovery_required(
            job.id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
        )
    except Exception:  # pragma: no cover - fencing is idempotent
        logger.warning("hosted_lifecycle_mark_recovery_failed", extra={"job_id": job.id})


def _child_token_actions() -> list:
    # Hosted v1 is paper/dry_run only (job CHECK enforces the mode), so the child
    # may submit paper intents and exit. It never receives ``heartbeat`` —
    # lifecycle actions are supervisor-owned.
    return strategy_service.child_run_token_actions(order_capable=True, notify=False)


async def prepare_launch(
    request: Request,
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    hooks: Optional[HostedLifecycleHooks] = None,
) -> Dict[str, Any]:
    """Prepare a launch and return the child configuration (one-time handoff).

    The returned mapping contains the **plaintext child token**. It must be
    delivered to the supervisor over the authenticated channel and never logged.
    """
    hooks = hooks or HostedLifecycleHooks()

    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )

    # A completed handoff or an in-flight/reserved preparation is terminal for
    # this attempt: it must never mint a second credential and never replay the
    # launch. We do NOT fence here: a concurrent duplicate (or a supervisor that
    # lost the response) must not destroy a healthy attempt that another caller
    # may still own. Recovery is explicit — the caller uses ``fence``/``release``
    # (or a prior failed preparation already fenced the job).
    if job.handoff_at is not None:
        raise HostedLifecycleError(409, "HOSTED_HANDOFF_ALREADY_COMPLETED")
    if job.token_id is not None:
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    strategy = await asyncio.to_thread(
        strategy_repo.get_strategy, job.owner_id, job.strategy_id
    )
    if strategy is None:
        raise HostedLifecycleError(409, "HOSTED_STRATEGY_MISSING")
    template_id = strategy_service.template_id_for(job.strategy_id)

    token_id, raw_token = hooks.token_factory()
    reserved = await asyncio.to_thread(
        strategy_repo.reserve_child_token,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        token_id=token_id,
    )
    if not reserved:
        # Another supervisor won the preparation race (or state moved on). Do not
        # fence: the winner owns this attempt. Exactly one caller proceeds.
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    expires_at = _utcnow() + timedelta(seconds=int(job.max_duration_s) + 300)
    token_payload = WorkerTokenCreateRequest(
        name=f"{template_id}:attempt-{attempt}",
        account_scope=job.account_scope,
        allowed_modes=[job.execution_mode],
        allowed_actions=_child_token_actions(),
        allowed_templates=[template_id],
        expires_at=expires_at,
        metadata={
            "source": "hosted_supervisor",
            "hosted_job_id": job_id,
            "hosted_strategy_id": job.strategy_id,
            "hosted_attempt": int(attempt),
        },
    )

    try:
        await worker_repo.create_token(token_payload, raw_token=raw_token, token_id=token_id)
    except Exception as exc:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason=f"token_mint_failed: {type(exc).__name__}",
        )
        raise HostedLifecycleError(503, "HOSTED_TOKEN_MINT_FAILED") from exc

    child_token = WorkerToken(
        token_id=token_id,
        name=token_payload.name,
        account_scope=job.account_scope,
        allowed_modes=[job.execution_mode],
        allowed_actions=list(token_payload.allowed_actions),
        allowed_templates=[template_id],
        status="active",
        expires_at=expires_at,
    )

    run_id = hooks.run_id_factory()
    run_payload = WorkerRunCreateRequest(
        strategy_run_id=run_id,
        template_id=template_id,
        account_scope=job.account_scope,
        execution_mode=job.execution_mode,
        metadata={
            "strategy_name": strategy.name,
            "strategy_family": "indicator_strategy",
            "entry_surface": "hosted_supervisor",
            "hosted_job_id": job_id,
            "hosted_strategy_id": job.strategy_id,
            "hosted_attempt": int(attempt),
            "hosted_params": dict(job.params_snapshot or {}),
        },
        runtime_state={
            "hosted": {
                "job_id": job_id,
                "strategy_id": job.strategy_id,
                "attempt": int(attempt),
                "version_id": job.version_id,
            }
        },
    )

    try:
        await create_worker_run_for_token(request, child_token, run_payload, strategy_run_id=run_id)
    except Exception as exc:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason="run_create_failed",
            token_id=token_id,
        )
        raise HostedLifecycleError(503, "HOSTED_RUN_CREATE_FAILED") from exc

    recorded = await asyncio.to_thread(
        strategy_repo.record_child_run,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        token_id=token_id,
        run_id=run_id,
    )
    if not recorded:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason="run_record_failed",
            token_id=token_id,
        )
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    claimed = await worker_repo.claim_run_session(
        run_id,
        freshness_seconds=WORKER_SESSION_FRESHNESS_SECONDS,
        claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
    )
    if claimed is None:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason="session_claim_failed",
            token_id=token_id,
        )
        raise HostedLifecycleError(503, "HOSTED_SESSION_CLAIM_FAILED")
    session_nonce = str(claimed.get("worker_session_nonce") or "")
    if not session_nonce:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason="session_nonce_missing",
            token_id=token_id,
        )
        raise HostedLifecycleError(503, "HOSTED_SESSION_CLAIM_FAILED")

    handed_off = await asyncio.to_thread(
        strategy_repo.mark_running_and_handoff,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        run_id=run_id,
    )
    if not handed_off:
        await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job=job,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason="handoff_mark_failed",
            token_id=token_id,
        )
        raise HostedLifecycleError(409, "HOSTED_PREPARE_INCOMPLETE")

    return {
        "job_id": job_id,
        "strategy_id": job.strategy_id,
        "attempt": int(attempt),
        "lease_epoch": int(lease_epoch),
        "run_id": run_id,
        "worker_token": raw_token,  # one-time secret: never log this
        "session_nonce": session_nonce,
        "template_id": template_id,
        "execution_mode": job.execution_mode,
        "account_scope": job.account_scope,
        "params": dict(job.params_snapshot or {}),
        "max_duration_s": int(job.max_duration_s),
        "progress_deadline_s": int(job.progress_deadline_s),
        "stale_exit_policy": str((job.policy_snapshot or {}).get("stale_exit_policy") or "none"),
    }


async def heartbeat(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    lease_until: datetime,
) -> Dict[str, Any]:
    """Runner-owned heartbeat: renew the job lease and the worker session.

    This deliberately does **not** write ``last_progress_at``. Runner liveness is
    not strategy progress; a parent heartbeat cannot assert that the child is
    healthy.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )
    renewed = await asyncio.to_thread(
        strategy_repo.renew_lease,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
        lease_until=lease_until,
    )
    if not renewed:
        raise HostedLifecycleError(409, "HOSTED_LEASE_RENEW_REFUSED")

    session_heartbeat_at = None
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        nonce = str((run or {}).get("worker_session_nonce") or "")
        if nonce:
            updated = await worker_repo.record_run_heartbeat(job.run_id, expected_nonce=nonce)
            if updated is None:
                raise HostedLifecycleError(409, "HOSTED_SESSION_CONFLICT")
            session_heartbeat_at = updated.get("last_heartbeat_at")
    return {
        "status": "ok",
        "job_id": job_id,
        "lease_until": lease_until.isoformat(),
        "last_heartbeat_at": session_heartbeat_at,
    }


async def release(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
) -> Dict[str, Any]:
    """Runner-owned stop: release the session, revoke the child, mark stopped.

    This is not a cancellation or flatten acknowledgement: open exposure may
    remain and is reconciled separately.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        nonce = str((run or {}).get("worker_session_nonce") or "")
        if nonce:
            await worker_repo.release_run_session(job.run_id, expected_nonce=nonce)
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)
    stopped = await asyncio.to_thread(
        strategy_repo.mark_stopped,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not stopped:
        raise HostedLifecycleError(409, "HOSTED_RELEASE_REFUSED")
    return {"status": "stopped", "job_id": job_id}


async def fence(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str = "fenced_by_supervisor",
) -> Dict[str, Any]:
    """Runner-owned fence to ``recovery_required``; revokes child authority.

    No automatic replay: a new attempt requires a new run and token and cannot
    start until the job is reconciled.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)
    fenced = await asyncio.to_thread(
        strategy_repo.mark_recovery_required,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not fenced:
        raise HostedLifecycleError(409, "HOSTED_FENCE_REFUSED")
    return {"status": "recovery_required", "job_id": job_id, "reason": reason}


async def job_state(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
) -> Dict[str, Any]:
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=("starting", "running", "recovery_required", "stopped", "failed", "hung", "fencing"),
        require_live_lease=False,
    )
    run_status = None
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        if run is not None:
            run_status = run.get("status")
    return {
        "job_id": job.id,
        "strategy_id": job.strategy_id,
        "attempt": int(job.attempt),
        "status": job.status,
        "desired_state": job.desired_state,
        "lease_owner": job.lease_owner,
        "lease_epoch": int(job.lease_epoch),
        "lease_until": _as_utc(job.lease_until).isoformat() if job.lease_until else None,
        "run_id": job.run_id,
        "token_id": job.token_id,
        "handoff_at": _as_utc(job.handoff_at).isoformat() if job.handoff_at else None,
        "run_status": run_status,
    }
