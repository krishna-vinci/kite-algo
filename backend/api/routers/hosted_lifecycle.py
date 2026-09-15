"""Supervisor lifecycle API (credential-authenticated, server-to-server).

Mounted at ``/api/hosted-supervisor`` and exempt from cookie auth because it
authenticates with the narrow supervisor credential
(:mod:`backend.strategies.supervisor_auth`). It is **not** a browser surface and
**not** reachable with an ordinary child worker token.

Endpoints (all require the supervisor credential first, before any job detail is
exposed):

- ``POST /jobs/{job_id}/claim``     — CAS-claim a queued job.
- ``GET  /jobs/{job_id}``           — authoritative job state.
- ``POST /jobs/{job_id}/prepare``   — launch preparation + one-time child handoff.
- ``POST /jobs/{job_id}/heartbeat`` — runner-owned lease + session heartbeat.
- ``POST /jobs/{job_id}/release``   — runner-owned stop (release + revoke).
- ``POST /jobs/{job_id}/fence``     — fence to ``recovery_required`` + revoke.

Every mutation authorizes against the persisted job ``lease_owner`` /
``lease_epoch`` / ``attempt``; the caller never selects a run id, and job
configuration is derived from the record. No supervisor secret is ever returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
from backend.api.routers.worker_shared import _repo as _worker_repository
from backend.api.schemas.hosted_lifecycle import (
    ActionResponse,
    ChildLaunchConfigResponse,
    ClaimJobRequest,
    FenceRequest,
    HeartbeatRequest,
    JobStateResponse,
    PrepareLaunchRequest,
    ReleaseRequest,
)
from backend.api.services import hosted_lifecycle
from backend.strategies.repository import (
    SqlAlchemyStrategyRepository,
    StrategyDisabled,
)
from backend.strategies.service import StrategyValidationError
from backend.strategies.supervisor_auth import require_supervisor

router = APIRouter(
    prefix="/hosted-supervisor",
    tags=["Hosted supervisor lifecycle"],
    dependencies=[Depends(require_supervisor)],
)

__all__ = ["router"]


def _strategies_db(request: Request):
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _strategies_repo(request: Request, session_factory=Depends(_strategies_db)):
    return SqlAlchemyStrategyRepository(session_factory)


def _worker_repo(request: Request) -> SqlAlchemyAlgoWorkerRepository:
    return _worker_repository(request)


def _raise(exc: hosted_lifecycle.HostedLifecycleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/jobs/{job_id}/claim", response_model=JobStateResponse)
async def claim_job(
    job_id: str,
    payload: ClaimJobRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    worker_repo = _worker_repo(request)
    try:
        claimed = strategy_repo.claim_job(
            job_id,
            lease_owner=payload.lease_owner,
            expected_lease_epoch=payload.expected_lease_epoch,
            expected_attempt=payload.expected_attempt,
            lease_until=payload.lease_until,
        )
    except StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StrategyDisabled as exc:
        raise HTTPException(status_code=409, detail="HOSTED_STRATEGY_DISABLED") from exc
    if claimed is None:
        raise HTTPException(status_code=409, detail={"rejection_reason": "HOSTED_CLAIM_CONFLICT"})
    return await hosted_lifecycle.job_state(
        strategy_repo=strategy_repo,
        worker_repo=worker_repo,
        job_id=job_id,
        lease_owner=payload.lease_owner,
        lease_epoch=int(claimed.lease_epoch),
        attempt=payload.expected_attempt,
    )


@router.get("/jobs/{job_id}", response_model=JobStateResponse)
async def get_job_state(
    job_id: str,
    request: Request,
    lease_owner: str,
    lease_epoch: int,
    attempt: int,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    try:
        return await hosted_lifecycle.job_state(
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            attempt=attempt,
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc


@router.post("/jobs/{job_id}/prepare", response_model=ChildLaunchConfigResponse)
async def prepare_launch(
    job_id: str,
    payload: PrepareLaunchRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    try:
        config = await hosted_lifecycle.prepare_launch(
            request,
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=payload.lease_owner,
            lease_epoch=payload.lease_epoch,
            attempt=payload.attempt,
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc
    return ChildLaunchConfigResponse(**config)


@router.post("/jobs/{job_id}/heartbeat", response_model=ActionResponse)
async def heartbeat(
    job_id: str,
    payload: HeartbeatRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    try:
        result = await hosted_lifecycle.heartbeat(
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=payload.lease_owner,
            lease_epoch=payload.lease_epoch,
            attempt=payload.attempt,
            lease_until=payload.lease_until,
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc
    return ActionResponse(**result)


@router.post("/jobs/{job_id}/release", response_model=ActionResponse)
async def release(
    job_id: str,
    payload: ReleaseRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    try:
        result = await hosted_lifecycle.release(
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=payload.lease_owner,
            lease_epoch=payload.lease_epoch,
            attempt=payload.attempt,
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc
    return ActionResponse(**result)


@router.post("/jobs/{job_id}/fence", response_model=ActionResponse)
async def fence(
    job_id: str,
    payload: FenceRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    try:
        result = await hosted_lifecycle.fence(
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=payload.lease_owner,
            lease_epoch=payload.lease_epoch,
            attempt=payload.attempt,
            reason=payload.reason or "fenced_by_supervisor",
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc
    return ActionResponse(**result)


@router.post("/jobs/{job_id}/recover", response_model=ActionResponse)
async def recover(
    job_id: str,
    payload: FenceRequest,
    request: Request,
    strategy_repo: SqlAlchemyStrategyRepository = Depends(_strategies_repo),
):
    """Authenticated lease-loss recovery for an expired attempt.

    Distinct from ``fence`` (which needs a live lease): this authorizes by full
    attempt identity but requires the lease to have expired, and only fences the
    attempt — it never renews or restores execution authority.
    """
    try:
        result = await hosted_lifecycle.expire(
            strategy_repo=strategy_repo,
            worker_repo=_worker_repo(request),
            job_id=job_id,
            lease_owner=payload.lease_owner,
            lease_epoch=payload.lease_epoch,
            attempt=payload.attempt,
            reason=payload.reason or "lease_expired",
        )
    except hosted_lifecycle.HostedLifecycleError as exc:
        raise _raise(exc) from exc
    return ActionResponse(**result)
