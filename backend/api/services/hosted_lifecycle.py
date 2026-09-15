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
from backend.strategies.redaction import redact_text
from backend.strategies.repository import SqlAlchemyStrategyRepository

logger = logging.getLogger(__name__)

#: Bounded log shipping contract (supervisor -> API). Per chunk and per attempt.
LOG_CHUNK_MAX_BYTES = 16 * 1024
LOG_TOTAL_MAX_BYTES = 256 * 1024

__all__ = [
    "HostedLifecycleError",
    "HostedLifecycleHooks",
    "expire",
    "heartbeat",
    "job_source",
    "job_state",
    "list_jobs",
    "prepare_launch",
    "release",
    "fence",
    "report_process_cleanup",
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
    require_started: bool = True,
) -> StrategyJob:
    """Authorize a lifecycle request against the persisted job authority.

    This is the only way a lifecycle request touches a job. It never accepts a
    bare run id: identity and configuration are derived from the job record, and
    the lease owner/epoch/attempt must all match.

    ``require_started=False`` is for **terminal reads only**: after a
    released-unlaunched attempt, ``desired_state`` is ``stopped`` and a state
    read must still succeed. It never relaxes the live-status/lease gates that
    gate mutation, so reads cannot regain mutation authority.
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
    if require_started and str(job.desired_state or "") != "started":
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
    job_id: str,
    token_id: Optional[str],
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str,
) -> Dict[str, Any]:
    """Best-effort cleanup after a failed preparation step.

    Revokes any child authority and fences the attempt to ``recovery_required``.
    It is **best-effort and retryable**: it returns whether the durable fence was
    actually written, and callers must report that honestly rather than claiming
    that a failed write guarantees fencing. ``token_id`` is passed explicitly
    (the reserved identity), because the job object loaded at the start of the
    request does not yet contain it.
    """
    try:
        await asyncio.to_thread(
            strategy_repo.record_failure,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            reason=reason,
        )
    except Exception:  # pragma: no cover - diagnostic only
        logger.warning("hosted_lifecycle_record_failure_failed", extra={"job_id": job_id})

    revoked = False
    if token_id:
        try:
            revoked = (await worker_repo.revoke_token(token_id)) is not None
        except Exception:  # pragma: no cover - best effort
            logger.warning("hosted_lifecycle_revoke_token_failed", extra={"job_id": job_id})

    fenced = False
    try:
        fenced = bool(
            await asyncio.to_thread(
                strategy_repo.mark_recovery_required,
                job_id,
                lease_owner=lease_owner,
                expected_lease_epoch=lease_epoch,
                expected_attempt=attempt,
            )
        )
    except Exception:  # pragma: no cover - fencing is idempotent/retryable
        logger.warning("hosted_lifecycle_mark_recovery_failed", extra={"job_id": job_id})
    return {"fenced": fenced, "revoked": revoked}


def _capability_actions(capabilities: Dict[str, bool]) -> list:
    # Actions are derived from the version/job capability snapshot: trade grants
    # paper order actions, notify grants notifications:publish, data grants the
    # baseline read/log. ``heartbeat`` is never granted (lifecycle is
    # supervisor-owned).
    return strategy_service.capability_actions(capabilities)


def _protection_runtime_state(
    stale_exit_policy: str, progress_deadline_s: int
) -> Optional[Dict[str, Any]]:
    """Map the pinned stale-exit policy through the validated protection model.

    Only ``exit_on_worker_stale`` is a supported hosted policy; it installs the
    existing backend-protection runtime config so run creation validates it
    through the same path as an external worker. The stale threshold is the
    job's **pinned** progress deadline (clamped to the model's accepted range),
    so the policy cannot drift with later config. ``none`` installs nothing.
    """
    if stale_exit_policy == "exit_on_worker_stale":
        stale_sec = min(max(int(progress_deadline_s), 30), 86400)
        return {
            "enabled": True,
            "operations": {"exit_on_worker_stale": True, "worker_stale_sec": stale_sec},
        }
    return None


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

    # Capabilities are parsed from the pinned snapshot BEFORE anything is
    # minted or reserved. An ambiguous/marker-only legacy snapshot fails closed
    # (no trading rights are inferred).
    try:
        capabilities = strategy_service.parse_capability_snapshot(job.capabilities_snapshot)
    except strategy_service.StrategyValidationError as exc:
        raise HostedLifecycleError(
            409, "HOSTED_CAPABILITIES_AMBIGUOUS", detail=str(exc)
        ) from exc

    strategy = await asyncio.to_thread(
        strategy_repo.get_strategy, job.owner_id, job.strategy_id
    )
    if strategy is None:
        raise HostedLifecycleError(409, "HOSTED_STRATEGY_MISSING")
    version = await asyncio.to_thread(
        strategy_repo.get_version_by_id, job.strategy_id, job.version_id
    )
    if version is None:
        raise HostedLifecycleError(409, "HOSTED_VERSION_MISSING")
    template_id = strategy_service.template_id_for(job.strategy_id)

    async def _abort(reason: str, *, token: Optional[str], status_code: int, code: str):
        cleanup = await _fail_closed(
            strategy_repo=strategy_repo,
            worker_repo=worker_repo,
            job_id=job_id,
            token_id=token,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
            reason=reason,
        )
        raise HostedLifecycleError(
            status_code,
            code,
            fencing="confirmed" if cleanup["fenced"] else "unconfirmed",
            token_revoked=cleanup["revoked"],
        )

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
        allowed_actions=_capability_actions(capabilities),
        allowed_templates=[template_id],
        expires_at=expires_at,
        metadata={
            "source": "hosted_supervisor",
            "hosted_job_id": job_id,
            "hosted_strategy_id": job.strategy_id,
            "hosted_attempt": int(attempt),
            "hosted_capabilities": dict(capabilities),
        },
    )

    try:
        await worker_repo.create_token(token_payload, raw_token=raw_token, token_id=token_id)
    except Exception as exc:
        await _abort(
            f"token_mint_failed: {type(exc).__name__}",
            token=token_id,
            status_code=503,
            code="HOSTED_TOKEN_MINT_FAILED",
        )

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

    stale_exit_policy = str((job.policy_snapshot or {}).get("stale_exit_policy") or "none")
    runtime_state: Dict[str, Any] = {
        "hosted": {
            "job_id": job_id,
            "strategy_id": job.strategy_id,
            "attempt": int(attempt),
            "version_id": job.version_id,
            "capabilities": dict(capabilities),
        }
    }
    protection = _protection_runtime_state(stale_exit_policy, job.progress_deadline_s)
    if protection is not None:
        # Installed through the same validated run-creation path as an external
        # worker: run creation normalizes it and seeds the protection state.
        runtime_state["backend_protection"] = protection

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
            "hosted_capabilities": dict(capabilities),
            "hosted_stale_exit_policy": stale_exit_policy,
        },
        runtime_state=runtime_state,
    )

    try:
        await create_worker_run_for_token(request, child_token, run_payload, strategy_run_id=run_id)
    except Exception as exc:
        await _abort(
            "run_create_failed",
            token=token_id,
            status_code=503,
            code="HOSTED_RUN_CREATE_FAILED",
        )

    try:
        recorded = await asyncio.to_thread(
            strategy_repo.record_child_run,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            token_id=token_id,
            run_id=run_id,
        )
    except Exception:
        recorded = False
    if not recorded:
        await _abort(
            "run_record_failed",
            token=token_id,
            status_code=409,
            code="HOSTED_PREPARE_INCOMPLETE",
        )

    try:
        claimed = await worker_repo.claim_run_session(
            run_id,
            freshness_seconds=WORKER_SESSION_FRESHNESS_SECONDS,
            claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
        )
    except Exception:
        claimed = None
    session_nonce = str((claimed or {}).get("worker_session_nonce") or "")
    if not session_nonce:
        await _abort(
            "session_claim_failed",
            token=token_id,
            status_code=503,
            code="HOSTED_SESSION_CLAIM_FAILED",
        )

    try:
        handed_off = await asyncio.to_thread(
            strategy_repo.mark_running_and_handoff,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
            run_id=run_id,
        )
    except Exception:
        handed_off = False
    if not handed_off:
        await _abort(
            "handoff_mark_failed",
            token=token_id,
            status_code=409,
            code="HOSTED_PREPARE_INCOMPLETE",
        )

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
        "stale_exit_policy": stale_exit_policy,
        "version_id": version.id,
        "source_sha256": version.source_sha256,
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
    # A stop request (desired_state=stopped) must not revoke the supervisor's
    # authority to heartbeat/observe; require_started=False keeps these usable.
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
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
    """Runner-owned stop: withdraw authority, then decide replacement safety.

    Stopping code/session authority is **not** exposure reconciliation. The
    server decides the terminal state from persisted evidence, never from a
    caller-supplied "flat" assertion:

    - an **unlaunched** attempt (no credential was ever handed off) could not
      have accepted work, so it is safely marked ``stopped`` and replacement is
      allowed;
    - a **launched** attempt may have accepted work, so it is fenced to
      ``recovery_required`` and its replacement stays blocked until explicit
      reconciliation.

    Either way the session is released and the child token revoked. Open exposure
    is never asserted to be flat.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
    )
    if job.run_id:
        run = await worker_repo.get_run(job.run_id)
        nonce = str((run or {}).get("worker_session_nonce") or "")
        if nonce:
            await worker_repo.release_run_session(job.run_id, expected_nonce=nonce)
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)

    launched = job.handoff_at is not None
    if launched:
        fenced = await asyncio.to_thread(
            strategy_repo.mark_recovery_required,
            job_id,
            lease_owner=lease_owner,
            expected_lease_epoch=lease_epoch,
            expected_attempt=attempt,
        )
        if not fenced:
            raise HostedLifecycleError(409, "HOSTED_RELEASE_REFUSED")
        return {
            "status": "recovery_required",
            "job_id": job_id,
            "replacement_blocked": True,
            "reason": "launched_attempt_requires_reconciliation",
        }

    stopped = await asyncio.to_thread(
        strategy_repo.mark_stopped,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not stopped:
        raise HostedLifecycleError(409, "HOSTED_RELEASE_REFUSED")
    return {
        "status": "stopped",
        "job_id": job_id,
        "replacement_blocked": False,
        "reason": "unlaunched_attempt",
    }


async def expire(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    worker_repo: SqlAlchemyAlgoWorkerRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    reason: str = "lease_expired",
) -> Dict[str, Any]:
    """Authenticated lease-loss recovery for an EXPIRED attempt.

    The ordinary ``fence`` requires a live lease, so an attempt whose lease has
    already lapsed cannot be fenced through it. This path authorizes by full
    identity (id + ``lease_owner`` + epoch + attempt) but **only** accepts an
    expired lease for a live attempt; it never renews a lease and never restores
    execution authority — it durably fences the attempt to ``recovery_required``
    and revokes the child token. A still-live lease is refused (use ``fence``).
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_live_lease=False,
        require_started=False,
    )
    if job.lease_until is not None and _as_utc(job.lease_until) > _utcnow():
        raise HostedLifecycleError(409, "HOSTED_LEASE_STILL_LIVE")
    if job.token_id:
        await worker_repo.revoke_token(job.token_id)
    fenced = await asyncio.to_thread(
        strategy_repo.expire_to_recovery_authorized,
        job_id,
        lease_owner=lease_owner,
        expected_lease_epoch=lease_epoch,
        expected_attempt=attempt,
    )
    if not fenced:
        raise HostedLifecycleError(409, "HOSTED_FENCE_REFUSED")
    return {
        "status": "recovery_required",
        "job_id": job_id,
        "replacement_blocked": True,
        "reason": reason,
    }


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
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        require_started=False,
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
        require_started=False,
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
        "last_progress_at": _as_utc(job.last_progress_at).isoformat() if job.last_progress_at else None,
        "progress_deadline_s": int(job.progress_deadline_s),
        "process_cleanup_state": job.process_cleanup_state,
        "process_cleanup_at": _as_utc(job.process_cleanup_at).isoformat() if job.process_cleanup_at else None,
        "run_status": run_status,
    }


def list_jobs(*, strategy_repo: SqlAlchemyStrategyRepository, statuses=("queued",), limit: int = 50):
    """Narrow discovery: the oldest jobs awaiting a supervisor.

    Read-only and bounded. Only jobs whose ``desired_state`` is ``started`` are
    returned, so a stopped/paused job is never re-offered. This deliberately does
    not implement scheduling — it merely lets a supervisor find work.
    """
    rows = strategy_repo.list_jobs_by_status(tuple(statuses), limit=limit)
    return [
        {
            "job_id": row.id,
            "strategy_id": row.strategy_id,
            "attempt": int(row.attempt),
            "status": row.status,
            "execution_mode": row.execution_mode,
            "lease_epoch": int(row.lease_epoch),
            "created_at": _as_utc(row.created_at).isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def job_source(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
) -> Dict[str, Any]:
    """Deliver the exact pinned source/version/hash to an *authorized* supervisor.

    Requires a **live** lease on a live attempt, so source can only be fetched
    while the supervisor legitimately owns the launch. The source is returned as
    text for the supervisor to persist and hash-verify; it is never imported by
    the API.
    """
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job, lease_owner=lease_owner, lease_epoch=lease_epoch, attempt=attempt
    )
    version = await asyncio.to_thread(
        strategy_repo.get_version_by_id, job.strategy_id, job.version_id
    )
    if version is None:
        raise HostedLifecycleError(409, "HOSTED_VERSION_MISSING")
    return {
        "job_id": job.id,
        "strategy_id": job.strategy_id,
        "version_id": version.id,
        "version": int(version.version),
        "source": version.source,
        "source_sha256": version.source_sha256,
    }


async def report_process_cleanup(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    state: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Record supervisor-owned process-cleanup evidence, bound to the attempt.

    Only the authenticated supervisor reaches this (the child has no lifecycle
    credential), so it cannot forge cleanup evidence. Authority is the same
    lease/attempt identity, allowing a report after the attempt has been fenced
    (the lease owner/epoch/attempt are retained as attribution).
    """
    if state not in ("confirmed", "unresolved"):
        raise HostedLifecycleError(422, "HOSTED_PROCESS_CLEANUP_STATE_INVALID")
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=(
            "starting",
            "running",
            "fencing",
            "recovery_required",
            "stopped",
            "failed",
            "hung",
        ),
        require_live_lease=False,
        require_started=False,
    )
    recorded = await asyncio.to_thread(
        strategy_repo.report_process_cleanup,
        job_id,
        state=state,
        actor=lease_owner,
        expected_attempt=int(attempt),
    )
    if not recorded:
        raise HostedLifecycleError(409, "HOSTED_PROCESS_CLEANUP_REFUSED")
    return {
        "job_id": job_id,
        "attempt": int(attempt),
        "process_cleanup_state": state,
        "note": (str(note)[:200] if note else None),
    }


async def report_job_logs(
    *,
    strategy_repo: SqlAlchemyStrategyRepository,
    job_id: str,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    chunks,
) -> Dict[str, Any]:
    """Accept bounded, redacted child-log chunks from the supervisor.

    The API never reads the supervisor container's filesystem; the supervisor
    pushes chunks here. Chunks are size-checked and **redacted before storage**.
    Once the per-attempt cap is reached, further chunks are dropped and the
    response reports ``truncated`` rather than silently discarding.
    """
    if not chunks:
        raise HostedLifecycleError(422, "HOSTED_LOGS_EMPTY")
    for chunk in chunks:
        if len(str(chunk).encode("utf-8")) > LOG_CHUNK_MAX_BYTES:
            raise HostedLifecycleError(413, "HOSTED_LOG_CHUNK_TOO_LARGE")
    job = await asyncio.to_thread(strategy_repo.get_job_by_id, job_id)
    job = require_job_authority(
        job,
        lease_owner=lease_owner,
        lease_epoch=lease_epoch,
        attempt=attempt,
        allow_statuses=(
            "starting",
            "running",
            "fencing",
            "recovery_required",
            "stopped",
            "failed",
            "hung",
        ),
        require_live_lease=False,
        require_started=False,
    )
    redacted = [redact_text(str(chunk)) for chunk in chunks]
    result = await asyncio.to_thread(
        strategy_repo.append_job_log,
        job_id,
        attempt=int(attempt),
        chunks=redacted,
        max_total_bytes=LOG_TOTAL_MAX_BYTES,
    )
    return {
        "job_id": job.id,
        "attempt": int(attempt),
        "stored": int(result["stored"]),
        "truncated": bool(result["truncated"]),
        "next_seq": int(result["next_seq"]),
    }
