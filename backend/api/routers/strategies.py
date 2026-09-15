"""App-authenticated API for the hosted-strategy store (Slice 0).

Scope of this router — deliberately narrow:

- **Store CRUD + immutable versions + owner-scoped reads only.** There is no
  start/stop/claim route here; lifecycle mutation routes arrive with the runner
  slice, so this file must not be read as having implemented them.
- **Cookie auth only.** ``require_app_user`` on every route; the router is under
  ``/api`` (not an ``auth_exempt_path`` prefix) so the global middleware also
  gates it, and no worker token is accepted.
- **Owner is server-derived.** ``owner_id`` is ``app:<username>`` from the
  session; it is never taken from the body. Every read is filtered by it, so a
  foreign id returns 404 rather than leaking existence.
- **Unsafe methods enforce the same-origin assertion** already used by the
  alerts operator surface (``SameSite=None`` on HTTPS removes the cookie
  defense; this is the replacement).
- **No execution.** Source is stored and hashed, never imported or run; no
  worker run or token is created here.

Fencing and the durable ``recovery_required`` state exist in
``backend.strategies.repository`` and are exercised at the service level. They
are **not** yet enforced by an HTTP route — the lifecycle routes do not exist in
this slice, and this docstring does not claim otherwise.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.schemas.strategies import (
    JobDetailResponse,
    JobListResponse,
    JobSummaryResponse,
    ReconciliationActionRequest,
    ReconciliationActionResponse,
    ReconciliationAuditResponse,
    ReconciliationAssessmentResponse,
    ReconciliationInspectionResponse,
    StrategyCreateRequest,
    StrategyListResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    VersionCreateRequest,
    VersionListResponse,
    VersionResponse,
)
from backend.api.services.csrf import enforce_same_origin
from backend.api.services.hosted_strategy_authz import (
    authorize_account_scope,
    is_account_authorized,
)
from backend.app.auth import AppUser, require_app_user
from backend.strategies import service
from backend.strategies.reconciliation import assess
from backend.strategies.repository import (
    SqlAlchemyStrategyRepository,
    StrategyConflict,
)

router = APIRouter(prefix="/strategies", tags=["Hosted strategies (operator)"])

__all__ = ["router"]


def _strategies_db(request: Request):
    """Sessionmaker for the hosted-strategy tables (injectable for tests)."""
    factory = getattr(request.app.state, "strategies_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _repository(request: Request, session_factory: Any = Depends(_strategies_db)):
    return SqlAlchemyStrategyRepository(session_factory)


def _owner_for_user(user: AppUser) -> str:
    """The app owner for this session. Never a client-supplied value.

    Deliberately distinct from any token scope: a browser session and a worker
    token are different identities, and the hosted store is owned by the app
    user, not by an SDK token's scope.
    """
    return f"app:{user.username or 'operator'}"


def require_strategy_owner(request: Request) -> str:
    user = require_app_user(request)
    return _owner_for_user(user)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _strategy_out(row: Any) -> StrategyResponse:
    return StrategyResponse(
        strategy_id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        template_id=row.template_id,
        description=row.description,
        default_execution_mode=row.default_execution_mode,
        default_job_kind=row.default_job_kind,
        default_account_scope=row.default_account_scope,
        max_duration_s=row.max_duration_s,
        progress_deadline_s=row.progress_deadline_s,
        stale_exit_policy=row.stale_exit_policy,
        status=row.status,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _version_out(row: Any) -> VersionResponse:
    return VersionResponse(
        version_id=row.id,
        strategy_id=row.strategy_id,
        version=row.version,
        source=row.source,
        source_sha256=row.source_sha256,
        parameters_schema=dict(row.parameters_schema or {}),
        capabilities_snapshot=dict(row.capabilities_snapshot or {}),
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def _owned_strategy(repo: SqlAlchemyStrategyRepository, owner: str, strategy_id: str):
    row = repo.get_strategy(owner, strategy_id)
    if row is None:
        # Foreign and missing are indistinguishable on purpose.
        raise HTTPException(status_code=404, detail="Strategy not found")
    return row


def _collector(request: Request):
    """Reconciliation evidence collector (injectable; reuses worker services)."""
    collector = getattr(request.app.state, "reconciliation_collector", None)
    if collector is not None:
        return collector
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector

    worker = getattr(request.app.state, "algo_worker_repository", None) or SqlAlchemyAlgoWorkerRepository()
    paper = getattr(request.app.state, "paper_runtime_service", None)
    return ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=paper)


def _job_replacement_blocked(job: Any) -> bool:
    status = str(job.status or "")
    if status in {"queued", "starting", "running"}:
        return True
    return status == "recovery_required" and job.reconciled_at is None


def _job_summary(job: Any) -> JobSummaryResponse:
    return JobSummaryResponse(
        job_id=job.id,
        strategy_id=job.strategy_id,
        owner_id=job.owner_id,
        attempt=int(job.attempt),
        status=job.status,
        desired_state=job.desired_state,
        execution_mode=job.execution_mode,
        account_scope=job.account_scope,
        run_id=job.run_id,
        replacement_blocked=_job_replacement_blocked(job),
        recovery_required_at=_iso(job.recovery_required_at),
        reconciled_at=_iso(job.reconciled_at),
        created_at=_iso(job.created_at),
        updated_at=_iso(job.updated_at),
    )


def _authorized_job(repo: SqlAlchemyStrategyRepository, owner: str, strategy_id: str, job_id: str):
    """Owner-scoped, then account-authorized. Cross-owner ⇒ 404; cross-account ⇒ 403."""
    _owned_strategy(repo, owner, strategy_id)
    job = repo.get_job(owner, job_id)
    if job is None or job.strategy_id != strategy_id:
        raise HTTPException(status_code=404, detail="Job not found")
    # Re-authorize the job's pinned account for the operator's environment.
    authorize_account_scope(str(job.account_scope))
    return job


def _audit_out(row: Any) -> ReconciliationAuditResponse:
    return ReconciliationAuditResponse(
        id=row.id,
        attempt=int(row.attempt),
        outcome=row.outcome,
        reason_code=row.reason_code,
        actor_id=row.actor_id,
        run_id=row.run_id,
        evidence=dict(row.evidence_json or {}),
        created_at=_iso(row.created_at),
    )


@router.post("", response_model=StrategyResponse)
async def create_strategy(
    request: Request,
    payload: StrategyCreateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    enforce_same_origin(request)
    try:
        name = service.validate_name(payload.name)
        # Shape/mode first (422 for a malformed scope), then authorization.
        account_scope = service.validate_account_scope(payload.account_scope, payload.execution_mode)
        if payload.execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError(
                f"execution_mode must be one of {', '.join(service.ALLOWED_EXECUTION_MODES)}"
            )
        if payload.job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError(
                f"job_kind must be one of {', '.join(service.ALLOWED_JOB_KINDS)}"
            )
        # Validate the policy inputs before persisting (explicit config).
        service.build_policy_snapshot(
            stale_exit_policy=payload.stale_exit_policy,
            max_duration_s=payload.max_duration_s,
            progress_deadline_s=payload.progress_deadline_s,
        )
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Server-side account authorization: an unlisted (even well-shaped) account
    # scope is 403 and no row is written. Parsing above is not authorization.
    account_scope = authorize_account_scope(account_scope)

    try:
        row = repo.create_strategy(
            owner_id=owner,
            name=name,
            description=payload.description,
            execution_mode=payload.execution_mode,
            job_kind=payload.job_kind,
            account_scope=account_scope,
            max_duration_s=payload.max_duration_s,
            progress_deadline_s=payload.progress_deadline_s,
            stale_exit_policy=payload.stale_exit_policy,
        )
    except StrategyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _strategy_out(row)


@router.get("", response_model=StrategyListResponse)
async def list_strategies(
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    return StrategyListResponse(strategies=[_strategy_out(row) for row in repo.list_strategies(owner)])


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    return _strategy_out(_owned_strategy(repo, owner, strategy_id))


@router.patch("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: str,
    request: Request,
    payload: StrategyUpdateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Minimal metadata update / disable. Immutable versions are untouched."""
    enforce_same_origin(request)
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = repo.update_strategy(owner, strategy_id, **fields)
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _strategy_out(row)


@router.post("/{strategy_id}/versions", response_model=VersionResponse)
async def create_version(
    strategy_id: str,
    request: Request,
    payload: VersionCreateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    try:
        source, digest = service.validate_source(payload.source)
        schema = service.validate_parameters_schema(payload.parameters_schema)
        capabilities = service.validate_capabilities(payload.capabilities)
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        row = repo.create_version(
            strategy_id=strategy_id,
            source=source,
            source_sha256=digest,
            parameters_schema=schema,
            capabilities_snapshot=capabilities,
            created_by=owner,
        )
    except StrategyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_out(row)


@router.get("/{strategy_id}/versions", response_model=VersionListResponse)
async def list_versions(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    _owned_strategy(repo, owner, strategy_id)
    return VersionListResponse(versions=[_version_out(row) for row in repo.list_versions(strategy_id)])


@router.get("/{strategy_id}/versions/{version}", response_model=VersionResponse)
async def get_version(
    strategy_id: str,
    version: int,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    _owned_strategy(repo, owner, strategy_id)
    row = repo.get_version(strategy_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return _version_out(row)


# ---------------------------------------------------------------------------
# jobs + operator reconciliation
# ---------------------------------------------------------------------------


@router.get("/{strategy_id}/jobs", response_model=JobListResponse)
async def list_jobs(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Owner-scoped job list. Account-unauthorized jobs are omitted, not leaked."""
    _owned_strategy(repo, owner, strategy_id)
    jobs = [job for job in repo.list_jobs_for_strategy(owner, strategy_id) if is_account_authorized(str(job.account_scope))]
    return JobListResponse(jobs=[_job_summary(job) for job in jobs])


@router.get("/{strategy_id}/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(
    strategy_id: str,
    job_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    job = _authorized_job(repo, owner, strategy_id, job_id)
    base = _job_summary(job)
    return JobDetailResponse(
        **base.model_dump(),
        handoff_at=_iso(job.handoff_at),
        process_cleanup_state=job.process_cleanup_state,
        process_cleanup_at=_iso(job.process_cleanup_at),
        process_cleanup_actor=job.process_cleanup_actor,
        last_progress_at=_iso(job.last_progress_at),
        version_id=job.version_id,
        token_present=bool(job.token_id),
    )


@router.get(
    "/{strategy_id}/jobs/{job_id}/reconciliation",
    response_model=ReconciliationInspectionResponse,
)
async def inspect_reconciliation(
    strategy_id: str,
    job_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Inspect why replacement is blocked and whether evidence supports clearing it."""
    job = _authorized_job(repo, owner, strategy_id, job_id)
    evidence = await _collector(request).collect(job)
    assessment = assess(evidence)
    history = repo.list_reconciliations(job_id)
    return ReconciliationInspectionResponse(
        job_id=job.id,
        strategy_id=job.strategy_id,
        attempt=int(job.attempt),
        replacement_blocked=_job_replacement_blocked(job),
        assessment=ReconciliationAssessmentResponse(**assessment.to_dict()),
        evidence=evidence.to_dict(),
        history=[_audit_out(row) for row in history],
    )


@router.post(
    "/{strategy_id}/jobs/{job_id}/reconciliation",
    response_model=ReconciliationActionResponse,
)
async def reconcile_job(
    strategy_id: str,
    job_id: str,
    request: Request,
    payload: ReconciliationActionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Explicitly reconcile a blocked attempt against immutable identity.

    The server alone decides whether evidence supports unblocking; a
    caller-provided ``flat``/``reconciled`` assertion is not accepted. The
    request pins the attempt (and optionally the lease epoch) so a stale request
    is refused.
    """
    enforce_same_origin(request)
    job = _authorized_job(repo, owner, strategy_id, job_id)

    if int(job.attempt) != int(payload.attempt):
        raise HTTPException(
            status_code=409,
            detail={"rejection_reason": "STALE_ATTEMPT", "current_attempt": int(job.attempt)},
        )
    if payload.lease_epoch is not None and int(job.lease_epoch) != int(payload.lease_epoch):
        raise HTTPException(
            status_code=409,
            detail={"rejection_reason": "STALE_LEASE_EPOCH", "current_lease_epoch": int(job.lease_epoch)},
        )

    evidence = await _collector(request).collect(job)
    assessment = assess(evidence)

    if not assessment.allowed:
        audit = repo.record_reconciliation(
            job_id=job.id,
            strategy_id=job.strategy_id,
            owner_id=owner,
            attempt=int(job.attempt),
            run_id=job.run_id,
            outcome="blocked",
            reason_code=assessment.reason_code,
            evidence=evidence.to_dict(),
            actor_id=owner,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": assessment.reason_code,
                "case": assessment.case,
                "blocking_reasons": assessment.blocking_reasons,
                "notes": assessment.notes,
                "evidence": evidence.to_dict(),
                "audit_id": audit.id,
            },
        )

    # Serialized with job creation via the strategy row lock inside the repo.
    reconciled = repo.reconcile_recovery(
        job.id,
        owner_id=owner,
        expected_lease_epoch=int(job.lease_epoch),
        expected_attempt=int(job.attempt),
    )
    if not reconciled:
        audit = repo.record_reconciliation(
            job_id=job.id,
            strategy_id=job.strategy_id,
            owner_id=owner,
            attempt=int(job.attempt),
            run_id=job.run_id,
            outcome="blocked",
            reason_code="RECONCILE_RACE_LOST",
            evidence=evidence.to_dict(),
            actor_id=owner,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "RECONCILE_RACE_LOST",
                "message": "attempt state changed; re-inspect before reconciling",
                "audit_id": audit.id,
            },
        )

    audit = repo.record_reconciliation(
        job_id=job.id,
        strategy_id=job.strategy_id,
        owner_id=owner,
        attempt=int(job.attempt),
        run_id=job.run_id,
        outcome="reconciled",
        reason_code=assessment.reason_code,
        evidence=evidence.to_dict(),
        actor_id=owner,
    )
    return ReconciliationActionResponse(
        status="reconciled",
        job_id=job.id,
        attempt=int(job.attempt),
        case=assessment.case,
        reason_code=assessment.reason_code,
        replacement_blocked=False,
        blocking_reasons=[],
        evidence=evidence.to_dict(),
        audit_id=audit.id,
    )
