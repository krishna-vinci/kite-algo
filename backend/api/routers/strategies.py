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

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from backend.strategies.proposals import plan_invalidation_state
from backend.api.schemas.proposals import (
    ExecutionEventRow,
    ExecutionResponse,
    ExecutionTrailResponse,
    PlanResponse,
    ProposalJournalRow,
    ProposalListResponse,
    ProposalRow,
    RESIDUAL_ACTIONS,
    ResidualDispositionRequest,
    ResidualDispositionResponse,
)
from backend.api.schemas.strategies import (
    AdmissionPolicyRequest,
    AdmissionPolicyResponse,
    AdmissionVerdictResponse,
    AdjustmentCreateRequest,
    AdjustmentLineResponse,
    AdjustmentResponse,
    ApprovalListResponse,
    ApprovalRequestModel,
    ApprovalResponse,
    AuthorizationModeRequest,
    AuthorizationModeResponse,
    AuthorizationStatusResponse,
    ExecutionGrantRequest,
    ExecutionGrantResponse,
    ExecutionGrantRevokeRequest,
    ExecutionGrantRevokeResponse,
    ExecutionRequestDecisionRequest,
    ExecutionRequestDecisionResponse,
    ExecutionRequestListResponse,
    ExecutionRequestRow,
    ReservationListResponse,
    ReservationResponse,
    OptionSettlementEvidenceRow,
    OptionSettlementResponse,
    OptionRunRepairActionRequest,
    OptionRunRepairActionResponse,
    OptionRunRepairAssessmentResponse,
    OptionRunRepairPlanLeg,
    OptionRunRepairUnresolvedStep,
    RollEventRow,
    RollListResponse,
    RollCreateRequest,
    RollResponse,
    RollStallRequest,
    SquareoffEvidenceListResponse,
    SquareoffEvidenceRow,
    SourceReadinessRequest,
    SourceReadinessResponse,
    ExternalAdapterRequest,
    ExternalAdapterResponse,
    ExternalStrategyCreateRequest,
    GrantRequest,
    GrantResponse,
    HostedScheduleOccurrenceResponse,
    HostedScheduleRequest,
    HostedScheduleResponse,
    HostedStrategyOptionsResponse,
    JobDetailResponse,
    JobListResponse,
    JobSummaryResponse,
    PositionListResponse,
    PositionRow,
    ProductStatusUpdateRequest,
    RebuildResponse,
    RunnerProfileResponse,
    ReconciliationActionRequest,
    ReconciliationActionResponse,
    ReconciliationAuditResponse,
    ReconciliationAssessmentResponse,
    ReconciliationInspectionResponse,
    SettlementAssessRequest,
    SettlementAssessmentResponse,
    SettlementAxisResponse,
    ScheduleEnabledRequest,
    JobLogEntryResponse,
    JobLogsResponse,
    RunNotificationEventResponse,
    RunNotificationListResponse,
    RunNowRequest,
    RunNowResponse,
    DeliveryAttemptResponse,
    DeliveryResponse,
    StopJobRequest,
    StopJobResponse,
    StrategyListResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    VersionCreateRequest,
    VersionListResponse,
    VersionResponse,
    parse_strategy_create,
)
from backend.api.services.csrf import enforce_same_origin
from backend.api.services.hosted_strategy_authz import (
    authorize_account_scope,
    authorized_account_scopes,
    is_account_authorized,
)
from backend.api.services.option_run_repair import (
    build_option_run_repair_service,
    option_run_repair_scope,
    record_repair_audit,
    repair_audit_job,
    require_residual_close_available,
    submit_residual_close,
)
from backend.app.auth import AppUser, require_app_user
from backend.strategies import service
from backend.strategies import readiness
from backend.strategies.attribution import (
    EXECUTION_ENVIRONMENTS,
    SqlAttributionStore,
    StrategyAttributionService,
)
from backend.strategies.reconciliation import assess, evidence_digest
from backend.strategies.plan_pipeline import PipelineRefusal, PlanExecutionPipeline
from backend.options.execution.repair import (
    ACTION_CLOSE_RESIDUAL,
    REASON_ACTION_MISMATCH,
    REASON_LIVE_UNSUPPORTED,
    REPAIR_ACTIONS,
    OptionRunRepairRefusal,
)
from backend.strategies.repository import (
    SqlAlchemyStrategyRepository,
    StrategyConflict,
    StrategyDisabled,
    StrategyFenceError,
    StrategyIdempotencyConflict,
    StrategyIdentityError,
    StrategyNotFound,
)

router = APIRouter(prefix="/strategies", tags=["Hosted strategies (operator)"])

logger = logging.getLogger(__name__)

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


def _strategy_out(
    row: Any, *, product_status: Optional[str] = None, adapter_kinds: Optional[List[str]] = None
) -> StrategyResponse:
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
        authorization_mode=str(getattr(row, "authorization_mode", None) or "approval_based"),
        status=row.status,
        product_status=product_status or "active",
        adapter_kinds=list(adapter_kinds or ["hosted"]),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _canonical_out(row: Any, *, adapter_kinds: Optional[List[str]] = None) -> StrategyResponse:
    """Response for a strategy that has no hosted adapter (external-only).

    Canonical fields are the product truth; the hosted-adapter-only fields are
    empty rather than invented.
    """
    return StrategyResponse(
        strategy_id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        template_id="",
        description=None,
        default_execution_mode="",
        default_job_kind="",
        default_account_scope=row.account_scope,
        max_duration_s=0,
        progress_deadline_s=0,
        stale_exit_policy="",
        status="active",
        product_status=row.status,
        adapter_kinds=list(adapter_kinds or ["external"]),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _attribution_store(request: Request) -> "SqlAttributionStore":
    """The attribution store for this app (wired in the app factory)."""
    store = getattr(request.app.state, "attribution_store", None)
    if store is None:
        from backend.app.database import SessionLocal

        store = SqlAttributionStore(session_factory=SessionLocal)
        request.app.state.attribution_store = store
    return store


def _attribution_service(request: Request) -> "StrategyAttributionService":
    """The attribution service for this app (wired in the app factory)."""
    service_ = getattr(request.app.state, "attribution_service", None)
    if service_ is None:
        service_ = StrategyAttributionService(_attribution_store(request))
        request.app.state.attribution_service = service_
    return service_


def _worker_repository(request: Request):
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository

    repo = getattr(request.app.state, "algo_worker_repository", None)
    if repo is None:
        repo = SqlAlchemyAlgoWorkerRepository()
        request.app.state.algo_worker_repository = repo
    return repo


def _environment_param(environment: Optional[str]) -> str:
    value = str(environment or "live").strip().lower()
    if value not in EXECUTION_ENVIRONMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"environment must be one of {', '.join(EXECUTION_ENVIRONMENTS)}",
        )
    return value


def _version_out(row: Any) -> VersionResponse:
    return VersionResponse(
        version_id=row.id,
        strategy_id=row.strategy_id,
        version=row.version,
        source=row.source,
        source_sha256=row.source_sha256,
        parameters_schema=dict(row.parameters_schema or {}),
        capabilities_snapshot=dict(row.capabilities_snapshot or {}),
        risk_policy=None if row.risk_policy is None else dict(row.risk_policy),
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def _owned_strategy(repo: SqlAlchemyStrategyRepository, owner: str, strategy_id: str):
    row = repo.get_strategy(owner, strategy_id)
    if row is None:
        # Foreign and missing are indistinguishable on purpose.
        raise HTTPException(status_code=404, detail="Strategy not found")
    return row


def _settlement_barrier(request: Request):
    """The durable execution-settlement barrier over the strategies database.

    Built with the SAME session factory the operator routes use (the canonical
    binding), so a proof covers the book the attempt actually traded. An injected
    ``app.state.settlement_barrier`` wins for tests.
    """
    injected = getattr(request.app.state, "settlement_barrier", None)
    if injected is not None:
        return injected
    from backend.strategies.settlement import ExecutionBarrier

    return ExecutionBarrier(session_factory=_strategies_db(request))


def _collector(request: Request, *, barrier: Any = None):
    """Reconciliation evidence collector (injectable; read-only services)."""
    injected = getattr(request.app.state, "reconciliation_collector", None)
    if injected is not None:
        return injected
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector

    worker = getattr(request.app.state, "algo_worker_repository", None) or SqlAlchemyAlgoWorkerRepository()
    paper = getattr(request.app.state, "paper_runtime_service", None)
    option_status = getattr(request.app.state, "option_run_status_reader", None)
    return ReconciliationEvidenceCollector(
        worker_repo=worker,
        paper_runtime=paper,
        option_status_reader=option_status,
        settlement_barrier=barrier if barrier is not None else _settlement_barrier(request),
        # The LIVE settlement branch reads the platform's own attributed book and
        # account-ingest truth; the paper runtime is never consulted for it.
        session_factory=_strategies_db(request),
    )


def _job_replacement_blocked(job: Any) -> bool:
    status = str(job.status or "")
    if status in {"queued", "starting", "running"}:
        return True
    return status == "recovery_required" and job.reconciled_at is None


async def _finish_predecessor_continuation(
    request: Request,
    repo: SqlAlchemyStrategyRepository,
    owner: str,
    strategy_id: str,
) -> Optional[Dict[str, Any]]:
    """Attempt the automatic evaluation continuation for a blocked predecessor.

    Best-effort by design: the shared Run now path must not fail because the
    continuation service could not run. When the predecessor is eligible its
    block is cleared (with a distinct continuation audit); otherwise nothing
    changes and the caller's own ``STRATEGY_BLOCKED`` refusal stands.
    """
    try:
        from backend.strategies.continuation import COMPLETION_UNKNOWN, ContinuationService

        service = ContinuationService(
            session_factory=_strategies_db(request),
            repository=repo,
        )
        return await asyncio.to_thread(
            service.attempt,
            owner_id=owner,
            strategy_id=strategy_id,
            completion_state=COMPLETION_UNKNOWN,
            actor_id=f"host:run_now:{owner}",
        )
    except Exception:  # noqa: BLE001 - continuation is best-effort on this path
        logger.exception(
            "run_now_continuation_attempt_failed",
            extra={"strategy_id": strategy_id, "owner_id": owner},
        )
        return None


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
    payload: Dict[str, Any] = Body(...),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Create one canonical strategy with exactly one compute adapter.

    Backward compatible: the legacy hosted payload (no ``kind``) keeps its
    request and response contract and writes canonical + hosted adapter
    atomically. ``kind: "external"`` creates the canonical strategy with an
    external adapter instead, and does not accept hosted-only fields.
    """
    enforce_same_origin(request)
    try:
        parsed = parse_strategy_create(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    if isinstance(parsed, ExternalStrategyCreateRequest):
        try:
            name = service.validate_name(parsed.name)
            account_scope = authorize_account_scope(parsed.account_scope)
        except service.StrategyValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            row = repo.create_external_strategy(
                owner_id=owner,
                name=name,
                account_scope=account_scope,
                description=parsed.description,
                config=dict(parsed.external_config or {}),
                created_by=owner,
            )
        except StrategyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _canonical_out(row, adapter_kinds=["external"])

    try:
        name = service.validate_name(parsed.name)
        # Shape/mode first (422 for a malformed scope), then authorization.
        account_scope = service.validate_account_scope(parsed.account_scope, parsed.execution_mode)
        if parsed.execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError(
                f"execution_mode must be one of {', '.join(service.ALLOWED_EXECUTION_MODES)}"
            )
        if parsed.job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError(
                f"job_kind must be one of {', '.join(service.ALLOWED_JOB_KINDS)}"
            )
        # Validate the policy inputs before persisting (explicit config).
        service.build_policy_snapshot(
            stale_exit_policy=parsed.stale_exit_policy,
            max_duration_s=parsed.max_duration_s,
            progress_deadline_s=parsed.progress_deadline_s,
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
            description=parsed.description,
            execution_mode=parsed.execution_mode,
            job_kind=parsed.job_kind,
            account_scope=account_scope,
            max_duration_s=parsed.max_duration_s,
            progress_deadline_s=parsed.progress_deadline_s,
            stale_exit_policy=parsed.stale_exit_policy,
        )
    except StrategyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StrategyIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _strategy_out(row)


@router.get("", response_model=StrategyListResponse)
async def list_strategies(
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    statuses = repo.list_product_statuses(owner)
    kinds = repo.adapter_kinds(owner)
    return StrategyListResponse(
        strategies=[
            _strategy_out(
                row,
                product_status=statuses.get(row.id),
                adapter_kinds=kinds.get(row.id),
            )
            for row in repo.list_strategies(owner)
        ]
    )


@router.get("/options", response_model=HostedStrategyOptionsResponse)
async def get_hosted_options(owner: str = Depends(require_strategy_owner)):
    """Server-authorized selection options for configuring a hosted strategy.

    Account scopes come from the server allowlist (`HOSTED_STRATEGY_ACCOUNT_SCOPES`,
    default-deny) — the browser never invents them.

    ``live`` appears in ``execution_modes`` ONLY when this deployment has hosted
    live enabled, and ``live_lanes`` names the lanes whose builders are registered
    and whose plan kinds the executor admits — empty while live is off. A client
    therefore cannot advertise a mode or a lane the server would refuse.
    """
    from backend.strategies.live_service import hosted_live_lanes
    from backend.strategies.live_settings import hosted_live_enabled

    live_enabled = hosted_live_enabled()
    return HostedStrategyOptionsResponse(
        account_scopes=authorized_account_scopes(),
        execution_modes=[
            mode
            for mode in service.ALLOWED_EXECUTION_MODES
            if mode != "live" or live_enabled
        ],
        job_kinds=list(service.ALLOWED_JOB_KINDS),
        stale_exit_policies=list(service.ALLOWED_STALE_EXIT_POLICIES),
        live_lanes=list(hosted_live_lanes()) if live_enabled else [],
        live_requires_owner_approval=True,
        runner_profile=RunnerProfileResponse(**readiness.profile_payload()),
    )


@router.post("/readiness", response_model=SourceReadinessResponse)
async def check_source_readiness(
    request: Request,
    payload: SourceReadinessRequest,
    owner: str = Depends(require_strategy_owner),
):
    """First-run readiness for a source file, before it is stored or launched.

    Parses the source with ``ast`` and reports the documented runner profile, a
    compatible ``main(ctx)`` entrypoint and any statically visible import the
    profile does not provide. The source is never imported or executed and no
    strategy/version row is written. The same contract is what any UI renders,
    so a "ready" answer means the same thing in every caller — and a check that
    cannot be proven (dynamic imports, guarded optional imports) reports
    ``unknown`` instead of a pass.
    """
    enforce_same_origin(request)
    try:
        result = readiness.assess_source_readiness(payload.source)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SourceReadinessResponse(**result)


def _schedule_mapping(row: Any) -> Dict[str, Any]:
    """The occurrence-relevant fields of a stored schedule, for the runtime."""
    return {
        "id": str(row.id),
        "strategy_id": str(row.strategy_id),
        "schedule_kind": str(row.schedule_kind),
        "at_time": str(row.at_time),
        "timezone": str(row.timezone),
        "day_of_month": row.day_of_month,
        "weekday": row.weekday,
        "calendar_dates": list(row.calendar_dates or []),
    }


def _schedule_out(row: Any, *, repo: SqlAlchemyStrategyRepository) -> HostedScheduleResponse:
    """The stored schedule plus the runtime's own next/last/missed answer.

    ``next_occurrence`` is the forward mirror of the scheduler's due-time rules,
    and the last/missed row comes from the scheduler's durable occurrence table,
    so nothing here re-implements scheduling policy.
    """
    from backend.strategies.scheduling import (
        OVERLAP_POLICY,
        misfire_grace_seconds,
        next_occurrence,
    )

    version = repo.get_version_by_id(str(row.strategy_id), str(row.version_id))
    occurrences = repo.list_schedule_occurrences(str(row.id), limit=1)
    following = next_occurrence(
        _schedule_mapping(row), now=datetime.now(timezone.utc)
    )
    return HostedScheduleResponse(
        schedule_id=str(row.id),
        strategy_id=str(row.strategy_id),
        version_id=str(row.version_id),
        version_number=int(version.version) if version is not None else None,
        account_scope=str(row.account_scope),
        execution_mode=str(row.execution_mode),
        job_kind=str(row.job_kind),
        params_snapshot=dict(row.params_snapshot or {}),
        schedule_kind=str(row.schedule_kind),
        at_time=str(row.at_time),
        weekday=row.weekday,
        day_of_month=row.day_of_month,
        calendar_dates=list(row.calendar_dates or []),
        timezone=str(row.timezone),
        window_end=row.window_end,
        squareoff_at=row.squareoff_at,
        enabled=bool(row.enabled),
        manually_paused=row.manual_paused_at is not None,
        max_duration_s=int(row.max_duration_s),
        progress_deadline_s=int(row.progress_deadline_s),
        misfire_grace_seconds=int(misfire_grace_seconds()),
        overlap_policy=OVERLAP_POLICY,
        next_occurrence_at=following.due_at.isoformat() if following is not None else None,
        next_occurrence_key=following.occurrence_key if following is not None else None,
        last_occurrence=(
            HostedScheduleOccurrenceResponse(**occurrences[0]) if occurrences else None
        ),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


@router.get("/calendar", response_model=Dict[str, Any])
async def get_operator_market_calendar(
    exchange: str = Query(default="NSE", min_length=2, max_length=16),
    segment: str = Query(default="CM", min_length=1, max_length=16),
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    owner: str = Depends(require_strategy_owner),
):
    """Exchange sessions for the operator's own schedule screen.

    The same authoritative calendar service the worker surface reads. Exchange
    and segment are explicit parameters, so nothing here assumes NSE/CM timing
    for an MCX or currency schedule, and missing/uncovered calendar data fails
    closed with a named reason instead of inventing session times.
    """
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (to_date - from_date).days > 370:
        raise HTTPException(status_code=422, detail="calendar range is bounded to 370 days")
    from backend.app.database import get_db_connection
    from backend.broker_api.market.exchange_calendar import (
        CalendarSchemaMigrationRequired,
        CalendarUnavailable,
        get_calendar_sessions,
    )

    conn = get_db_connection()
    try:
        return await asyncio.to_thread(
            get_calendar_sessions,
            conn,
            exchange=exchange.upper(),
            segment=segment.upper(),
            from_date=from_date,
            to_date=to_date,
        )
    except (CalendarUnavailable, CalendarSchemaMigrationRequired) as exc:
        raise HTTPException(status_code=503, detail={"rejection_reason": str(exc)}) from exc
    finally:
        conn.close()


@router.get("/{strategy_id}/schedule", response_model=Optional[HostedScheduleResponse])
async def get_hosted_schedule(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """The strategy's stored schedule, or ``null`` when it has none."""
    _owned_strategy(repo, owner, strategy_id)
    row = repo.get_schedule(strategy_id)
    if row is None:
        return None
    return _schedule_out(row, repo=repo)


@router.put("/{strategy_id}/schedule", response_model=HostedScheduleResponse)
async def put_hosted_schedule(
    strategy_id: str,
    request: Request,
    payload: HostedScheduleRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Create or edit the strategy's single schedule (owner + origin guarded).

    Account, policy and capability snapshots are derived from the strategy and
    the pinned version exactly like a manual launch, so an edited schedule can
    never carry a caller-chosen account or a stale policy.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    _refuse_live_when_disabled(payload.execution_mode, surface="schedule")
    try:
        row = repo.save_schedule(
            strategy_id=strategy_id,
            version_id=payload.version_id,
            owner_id=owner,
            job_kind=payload.job_kind,
            execution_mode=payload.execution_mode,
            params=payload.params,
            schedule_kind=payload.schedule_kind,
            at_time=payload.at_time,
            weekday=payload.weekday,
            day_of_month=payload.day_of_month,
            calendar_dates=payload.calendar_dates,
            timezone=payload.timezone,
            window_end=payload.window_end,
            squareoff_at=payload.squareoff_at,
            enabled=payload.enabled,
        )
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StrategyIdentityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StrategyDisabled as exc:
        raise HTTPException(status_code=409, detail="STRATEGY_DISABLED") from exc
    except StrategyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _schedule_out(row, repo=repo)


@router.post("/{strategy_id}/schedule/enabled", response_model=HostedScheduleResponse)
async def set_hosted_schedule_enabled(
    strategy_id: str,
    request: Request,
    payload: ScheduleEnabledRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Disable or re-enable the schedule. Never silently resumes work.

    Re-enabling applies the SAME persisted readiness checks as creating or
    editing the schedule: the pinned mode must still be offered by this
    deployment (a live schedule cannot be enabled while hosted live is off), the
    pinned account must still be authorized for that mode, and the pinned version
    must still belong to the strategy. A disabled strategy stays refused, and the
    scheduler's launch path applies the same fence a manual launch does, so a
    disabled or stopped strategy cannot be started by a schedule.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    existing = repo.get_schedule(strategy_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if payload.enabled:
        # Enabling is a launch-capability decision, so it re-checks exactly what
        # a launch would: deployment mode availability, then account/mode
        # authorization (never only at create/edit time).
        _refuse_live_when_disabled(existing.execution_mode, surface="schedule_enable")
        try:
            service.validate_account_scope(
                str(existing.account_scope or ""), str(existing.execution_mode or "")
            )
        except service.StrategyValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        authorize_account_scope(str(existing.account_scope or ""))
        if repo.get_version_by_id(strategy_id, str(existing.version_id)) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "rejection_reason": "SCHEDULE_VERSION_MISSING",
                    "message": (
                        "The version this schedule pins is no longer part of this strategy. "
                        "Save the schedule against a current version before enabling it."
                    ),
                },
            )
    try:
        row = repo.set_schedule_enabled(
            strategy_id, owner_id=owner, enabled=payload.enabled, actor=owner
        )
    except StrategyNotFound as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    except StrategyDisabled as exc:
        raise HTTPException(status_code=409, detail="STRATEGY_DISABLED") from exc
    return _schedule_out(row, repo=repo)


@router.get(
    "/{strategy_id}/schedule/occurrences",
    response_model=List[HostedScheduleOccurrenceResponse],
)
async def list_hosted_schedule_occurrences(
    strategy_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Materialised occurrences, newest first (fired, missed and expired)."""
    _owned_strategy(repo, owner, strategy_id)
    row = repo.get_schedule(strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return [
        HostedScheduleOccurrenceResponse(**entry)
        for entry in repo.list_schedule_occurrences(str(row.id), limit=limit)
    ]


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    row = _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    kinds = repo.adapter_kinds(owner).get(strategy_id, ["hosted"])
    return _strategy_out(
        row,
        product_status=canonical.status if canonical is not None else None,
        adapter_kinds=kinds,
    )


@router.patch("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: str,
    request: Request,
    payload: StrategyUpdateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Minimal metadata update / scheduling disable.

    ``status`` here keeps its existing meaning — hosted **scheduling**
    enablement — and is deliberately distinct from the canonical product status
    written by ``PATCH /{strategy_id}/status``. Immutable versions are untouched.
    """
    enforce_same_origin(request)
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = repo.update_strategy(owner, strategy_id, **fields)
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StrategyIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    return _strategy_out(
        row,
        product_status=canonical.status if canonical is not None else None,
        adapter_kinds=repo.adapter_kinds(owner).get(strategy_id),
    )


@router.patch("/{strategy_id}/status", response_model=StrategyResponse)
async def update_product_status(
    strategy_id: str,
    request: Request,
    payload: ProductStatusUpdateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Set the canonical PRODUCT status. Archiving preserves all history."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    row = repo.set_product_status(owner, strategy_id, payload.status)
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    hosted = repo.get_strategy(owner, strategy_id)
    return _strategy_out(
        hosted,
        product_status=row.status,
        adapter_kinds=repo.adapter_kinds(owner).get(strategy_id),
    )


@router.post("/{strategy_id}/adapters/external", response_model=ExternalAdapterResponse)
async def create_external_adapter(
    strategy_id: str,
    request: Request,
    payload: ExternalAdapterRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Attach an external compute adapter to an owner's canonical strategy."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    row = repo.create_external_adapter(
        owner_id=owner,
        strategy_id=strategy_id,
        config=dict(payload.config or {}),
        created_by=owner,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return ExternalAdapterResponse(
        adapter_id=row.id,
        strategy_id=row.strategy_id,
        status=row.status,
        config=dict(row.config_json or {}),
    )


@router.post("/{strategy_id}/grants", response_model=GrantResponse)
async def create_grant(
    strategy_id: str,
    request: Request,
    payload: GrantRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Issue a token→strategy grant (owner only).

    Verifies the actor owns the strategy, the token exists, is active, and its
    account scope EXACTLY matches the canonical strategy account. A worker token
    is a credential, never an owner: no worker-auth route reaches this surface.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    tokens = {str(item.get("token_id")): item for item in await _worker_repository(request).list_tokens()}
    token = tokens.get(payload.token_id)
    if token is None:
        raise HTTPException(
            status_code=404,
            detail={"rejection_reason": "TOKEN_NOT_FOUND", "token_id": payload.token_id},
        )
    if str(token.get("status") or "") != "active":
        raise HTTPException(
            status_code=409,
            detail={"rejection_reason": "TOKEN_NOT_ACTIVE", "token_id": payload.token_id},
        )
    if str(token.get("account_scope") or "") != str(canonical.account_scope):
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "TOKEN_ACCOUNT_MISMATCH",
                "token_id": payload.token_id,
                "strategy_account_scope": str(canonical.account_scope),
            },
        )

    _attribution_store(request).grant_strategy(
        token_id=payload.token_id, strategy_id=strategy_id, granted_by=owner
    )
    return GrantResponse(strategy_id=strategy_id, token_id=payload.token_id, granted_by=owner)


@router.delete("/{strategy_id}/grants/{token_id}", response_model=GrantResponse)
async def revoke_grant(
    strategy_id: str,
    token_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Revoke a grant by stamping ``revoked_at``. History is never deleted."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    revoked = _attribution_store(request).revoke_grant(
        token_id=token_id, strategy_id=strategy_id
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Grant not found")
    return GrantResponse(
        strategy_id=strategy_id, token_id=token_id, granted_by=owner, revoked=True
    )


# ---------------------------------------------------------------------------
# Proposals and frozen plans (G5) — owner reads, deliberately read-only
# ---------------------------------------------------------------------------


def _proposal_store(request: Request, session_factory: Any):
    from backend.strategies.proposals import ProposalStore

    store = getattr(request.app.state, "proposal_store", None)
    if store is None:
        store = ProposalStore(session_factory=session_factory)
    return store


# ---------------------------------------------------------------------------
# Admission, reservations and approvals (G9+G10+G6) — owner surfaces
# ---------------------------------------------------------------------------


def _admission_service(session_factory: Any):
    from backend.strategies.admission import AdmissionService

    return AdmissionService(session_factory=session_factory)


def _reservation_ledger(session_factory: Any):
    from backend.strategies.reservations import ReservationLedger

    return ReservationLedger(session_factory=session_factory)


def _approval_service(session_factory: Any):
    from backend.strategies.approvals import ApprovalService

    return ApprovalService(session_factory=session_factory)


def _authorization_service(session_factory: Any):
    """The governed-execution authorization service (Phase 2).

    Mode, grant and policy decisions live in the service, never in a route.
    """
    from backend.strategies.execution_authorization import ExecutionAuthorizationService

    return ExecutionAuthorizationService(session_factory=session_factory)


def _plan_pipeline(request: Request, session_factory: Any) -> PlanExecutionPipeline:
    """The ONE pipeline the operator routes and the dispatcher both use.

    The router contributes only the collaborators it already owns: the
    app-state executors and its margin reader (which stays patchable here, so
    existing tests keep their seam and there is still one implementation).
    """
    from backend.strategies.plan_pipeline import PlanExecutionPipeline

    return PlanExecutionPipeline(
        session_factory,
        proposal_store=_proposal_store(request, session_factory),
        admission_service=_admission_service(session_factory),
        reservation_ledger=_reservation_ledger(session_factory),
        approval_service=_approval_service(session_factory),
        margin_reader=_live_margin_evidence,
        paper_executor_factory=lambda: _paper_plan_executor(request, session_factory),
        live_executor_factory=lambda: _live_plan_executor(request, session_factory),
    )


def _execution_request_service(request: Request, session_factory: Any):
    from backend.strategies.execution_requests import ExecutionRequestService

    return ExecutionRequestService(
        session_factory,
        pipeline=_plan_pipeline(request, session_factory),
        authorization=_authorization_service(session_factory),
    )


def _plan_or_404(store: Any, *, owner: str, repo: Any, strategy_id: str, plan_id: str) -> Dict[str, Any]:
    _owned_strategy(repo, owner, strategy_id)
    plan = store.get_plan(plan_id)
    if plan is None or str(plan["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    # Every admission, reservation and approval writes state against an account,
    # so the same account authorization the rest of this router applies must hold
    # here too — the account comes from the plan, never from the caller.
    authorize_account_scope(str(plan["account_id"]))
    return plan


def _live_margin_evidence(account_scope: str, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ONE live CNC funding reader, delegated to the pipeline.

    A local copy of this reader is exactly the drift C1.1 §2 removes: the owner
    route and the hosted live executor must evaluate the SAME evidence or an
    admission preview can disagree with the submission it authorises. The seam
    (a two-argument reader the pipeline can be given) is preserved for tests.
    """
    from backend.strategies.plan_pipeline import live_margin_evidence

    return live_margin_evidence(account_scope, plan)


@router.put("/{strategy_id}/admission-policy", response_model=AdmissionPolicyResponse)
async def put_admission_policy(
    strategy_id: str,
    request: Request,
    payload: AdmissionPolicyRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Record the admission policy for this strategy.

    The policy is the *recorded basis* for every verdict: without a stored
    allocation there is nothing to enforce against, which is why a live strategy
    with no policy row is refused ADMISSION_POLICY_MISSING rather than treated as
    unlimited.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    account_scope = str(canonical.account_scope)
    authorize_account_scope(account_scope)
    policy = _admission_service(session_factory).upsert_policy(
        strategy_id=strategy_id,
        account_id=account_scope,
        updated_by=owner,
        **payload.model_dump(),
    )
    return AdmissionPolicyResponse(**policy)


@router.get("/{strategy_id}/admission-policy", response_model=AdmissionPolicyResponse)
async def get_admission_policy(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    _owned_strategy(repo, owner, strategy_id)
    policy = _admission_service(session_factory).policy_for(strategy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Admission policy not found")
    return AdmissionPolicyResponse(**policy)


@router.post("/{strategy_id}/plans/{plan_id}/admission", response_model=AdmissionVerdictResponse)
async def preview_admission(
    strategy_id: str,
    plan_id: str,
    request: Request,
    execution_environment: Optional[str] = None,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Preview the admission verdict. **A preview is not a reservation.**

    Nothing is written: the owner sees exactly what admission would decide, and
    the capacity claim only happens through ``/reserve``.
    """
    enforce_same_origin(request)
    plan = _plan_or_404(_proposal_store(request, session_factory), owner=owner, repo=repo,
                        strategy_id=strategy_id, plan_id=plan_id)
    # ADMISSION is gated too, and the environment is DERIVED from the persisted
    # binding: a request parameter can no longer name a different environment.
    # An explicit disagreeing value is refused rather than ignored.
    environment = _derived_environment(
        session_factory, plan, requested=execution_environment, surface="plan_reserve",
        plan_id=plan_id,
    )
    # A preview is still a decision: an option ENTRY the strategy's own durable
    # work blocks refuses BY NAME here (409), exactly as it would at reserve.
    try:
        return AdmissionVerdictResponse(**_plan_pipeline(request, session_factory).admit(
            plan, environment=environment
        ))
    except PipelineRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


@router.post("/{strategy_id}/plans/{plan_id}/reserve", response_model=ReservationResponse)
async def reserve_plan(
    strategy_id: str,
    plan_id: str,
    request: Request,
    execution_environment: Optional[str] = None,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Admit and claim capacity in one transaction; first claim wins."""
    from backend.strategies.reservations import ReservationError

    enforce_same_origin(request)
    plan = _plan_or_404(_proposal_store(request, session_factory), owner=owner, repo=repo,
                        strategy_id=strategy_id, plan_id=plan_id)
    environment = _derived_environment(
        session_factory, plan, requested=execution_environment, surface="plan_reserve",
        plan_id=plan_id,
    )
    try:
        return ReservationResponse(
            **_plan_pipeline(request, session_factory).reserve(
                plan, environment=environment, actor=owner
            )
        )
    except PipelineRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except ReservationError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


def _roll_view(machine: Any, roll: Dict[str, Any], *, with_events: bool = False) -> RollResponse:
    """One roll. ``close_release_permitted`` is DERIVED from the state, never stored.

    Exposing it means an operator can see the invariant holding rather than having
    to infer it from a state name.
    """
    return RollResponse(
        **roll,
        events=[RollEventRow(**row) for row in machine.events(roll["roll_id"])]
        if with_events
        else [],
        close_release_permitted=str(roll["state"]) == "releasing_old",
    )


@router.get(
    "/{strategy_id}/option-runs/{option_run_id}/settlement",
    response_model=OptionSettlementResponse,
)
async def get_option_run_settlement(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """A run's settlement evidence. Owner-only, read-only.

    ``settled`` is DERIVED from whether authoritative evidence exists, not from a
    stored flag: a stored flag could be set by something other than evidence, and
    this endpoint's whole job is to show that it was not.
    """
    _ = request
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    from backend.options.protection.expiry_policy import OptionSettlementService

    service = OptionSettlementService(session_factory=session_factory)
    rows = service.evidence_for(option_run_id=option_run_id)
    return OptionSettlementResponse(
        option_run_id=option_run_id,
        settled=bool(rows),
        evidence=[OptionSettlementEvidenceRow(**row) for row in rows],
    )


# ---------------------------------------------------------------------------
# Option-run repair (B2.1b) - the governed way out of a partial/cleanup run
# ---------------------------------------------------------------------------


def _option_run_repair_plan_leg(order: Dict[str, Any]) -> OptionRunRepairPlanLeg:
    return OptionRunRepairPlanLeg(
        tradingsymbol=str(order.get("tradingsymbol") or ""),
        transaction_type=str(order.get("transaction_type") or ""),
        quantity=int(order.get("quantity") or 0),
        exchange=None if order.get("exchange") is None else str(order.get("exchange")),
        product=None if order.get("product") is None else str(order.get("product")),
        order_type=None if order.get("order_type") is None else str(order.get("order_type")),
    )


def _option_run_repair_assessment(assessment: Dict[str, Any]) -> OptionRunRepairAssessmentResponse:
    return OptionRunRepairAssessmentResponse(
        option_run_id=str(assessment.get("option_run_id") or ""),
        status=str(assessment.get("status") or ""),
        state=str(assessment.get("state") or ""),
        reason_code=assessment.get("reason_code"),
        reasons=list(assessment.get("reasons") or []),
        evidence_digest=str(assessment.get("evidence_digest") or ""),
        close_plan=[
            _option_run_repair_plan_leg(dict(order or {}))
            for order in (assessment.get("close_plan") or [])
        ],
        evidence=dict(assessment.get("evidence") or {}),
        detail=dict(assessment.get("detail") or {}),
        unresolved_steps=[
            OptionRunRepairUnresolvedStep(
                plan_id=str(step.get("plan_id") or ""),
                step_no=int(step.get("step_no") or 0),
                state=str(step.get("state") or ""),
                order_id=step.get("order_id"),
            )
            for step in (assessment.get("unresolved_steps") or [])
        ],
    )


@router.get(
    "/{strategy_id}/option-runs/{option_run_id}/repair",
    response_model=OptionRunRepairAssessmentResponse,
)
async def inspect_option_run_repair(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The read-only repair verdict for one run. Owner-only.

    Nothing is refreshed here: the verdict is derived from the run's own confirmed
    fills, and the returned digest is what a POST must still match.
    """
    _ = request
    option_run_repair_scope(repo, owner, strategy_id, option_run_id, session_factory)
    service = build_option_run_repair_service(request, session_factory)
    try:
        assessment = service.assessment(option_run_id)
    except OptionRunRepairRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return _option_run_repair_assessment(assessment)


@router.post(
    "/{strategy_id}/option-runs/{option_run_id}/repair",
    response_model=OptionRunRepairActionResponse,
)
async def repair_option_run(
    strategy_id: str,
    option_run_id: str,
    request: Request,
    payload: OptionRunRepairActionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Apply one governed repair, or refuse by name.

    The server alone decides what the run holds: a caller-supplied ``flat`` /
    ``residual`` assertion is never accepted, and the digest the operator read
    must still describe the run. ``close_flat`` closes a provably flat run;
    ``close_residual`` submits the risk-reducing close through the staged
    structure exit and moves the run to ``exiting`` (later fill reconciliation
    completes it). An ambiguous run is refused by name and nothing changes.
    """
    enforce_same_origin(request)
    scope = option_run_repair_scope(repo, owner, strategy_id, option_run_id, session_factory)
    action = str(payload.action or "")
    if action not in REPAIR_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "rejection_reason": REASON_ACTION_MISMATCH,
                "action": action,
                "supported": list(REPAIR_ACTIONS),
            },
        )
    if action == ACTION_CLOSE_RESIDUAL and str(scope["execution_environment"]) == "live":
        # This phase has no live staged-exit submission path the operator route
        # may drive; fail closed instead of inventing one.
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": REASON_LIVE_UNSUPPORTED,
                "option_run_id": str(option_run_id),
                "execution_environment": "live",
                "message": "a live residual close has no governed submission path yet",
            },
        )
    service = build_option_run_repair_service(request, session_factory)
    try:
        next_run, assessment = service.plan(
            option_run_id=option_run_id,
            action=action,
            evidence_digest=str(payload.evidence_digest or ""),
        )
    except OptionRunRepairRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    # Everything that can refuse is checked BEFORE the run moves, so a refusal
    # never leaves a repaired run that the operator cannot see as repaired.
    job = repair_audit_job(repo, next_run)
    if job is None:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "OPTION_RUN_REPAIR_AUDIT_UNAVAILABLE",
                "option_run_id": str(option_run_id),
                "worker_run_id": str(dict(next_run.metadata or {}).get("worker_run_id") or "") or None,
                "message": "this run has no hosted job to record the repair against",
            },
        )
    if action == ACTION_CLOSE_RESIDUAL:
        require_residual_close_available(request, next_run)
    try:
        committed = service.commit(next_run, allowed_from=str(assessment.get("status") or ""))
    except OptionRunRepairRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    submission: Dict[str, Any] = {}
    if action == ACTION_CLOSE_RESIDUAL:
        submission = await submit_residual_close(
            request, session_factory, run=committed, scope=scope
        )
    audit_id = record_repair_audit(
        repo,
        job=job,
        owner=owner,
        strategy_id=strategy_id,
        action=action,
        committed=committed,
        assessment=assessment,
        submission=submission,
    )
    return OptionRunRepairActionResponse(
        option_run_id=str(committed.strategy_run_id),
        action=action,
        state=str(assessment.get("state") or ""),
        run_status=str(committed.status),
        evidence_digest=str(assessment.get("evidence_digest") or ""),
        audit_id=audit_id,
        submission=submission,
    )


@router.get("/{strategy_id}/rolls", response_model=RollListResponse)
async def list_rolls(
    strategy_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Every roll for one strategy, newest first. Owner-only, read-only."""
    _ = request
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    from backend.strategies.rolls import RollStateMachine

    machine = RollStateMachine(session_factory=session_factory)
    return RollListResponse(
        rolls=[
            _roll_view(machine, roll)
            for roll in machine.list_for_strategy(strategy_id=strategy_id)
        ]
    )


@router.get("/{strategy_id}/rolls/{roll_id}", response_model=RollResponse)
async def get_roll(
    strategy_id: str,
    roll_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """One roll with its append-only trail. Owner-only, read-only."""
    _ = request
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    from backend.strategies.rolls import RollStateMachine

    machine = RollStateMachine(session_factory=session_factory)
    roll = machine.get(roll_id)
    # Cross-strategy reads are non-disclosing: the roll exists, but not for this
    # strategy, and the owner asked about this strategy.
    if roll is None or str(roll["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Roll not found")
    return _roll_view(machine, roll, with_events=True)


def _roll_machine(session_factory: Any):
    """The roll state machine over the strategies database (production wiring).

    Constructed with **no notifier**: this path records evidence and decisions,
    and escalation stays with the platform's own notification path rather than
    being triggered by an HTTP request.
    """
    from backend.strategies.rolls import RollStateMachine

    return RollStateMachine(session_factory=session_factory)


def _roll_write_or_409(action: Any) -> Dict[str, Any]:
    """Run one roll transition, mapping the state machine's refusals to 409."""
    from backend.strategies.rolls import RollError

    try:
        return action()
    except RollError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


def _owned_roll(machine: Any, roll_id: str, *, strategy_id: str) -> Dict[str, Any]:
    """A roll of ANOTHER strategy is 404, exactly as the read routes decide."""
    roll = machine.get(roll_id)
    if roll is None or str(roll["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Roll not found")
    return roll


@router.post("/{strategy_id}/rolls", response_model=RollResponse)
async def create_roll(
    strategy_id: str,
    body: RollCreateRequest,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Open a roll: acquire the replacement, prove it, then release the close.

    This is the production entry point for the Project 9 roll object. It places
    no order — the replacement leg is submitted by the child or the operator —
    and it is where the invariant is enforced: the old-contract close step is
    unreachable until the FULL required replacement quantity is **proven** filled
    from the strategy's attributed book (never from an order-status label).
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    roll = _roll_write_or_409(
        lambda: machine.create(
            strategy_id=strategy_id,
            account_id=str(canonical.account_scope),
            old_instrument_id=body.old_instrument_id,
            new_instrument_id=body.new_instrument_id,
            required_replacement_quantity=body.required_replacement_quantity,
            old_coordinate=body.old_coordinate,
            new_coordinate=body.new_coordinate,
            plan_id=body.plan_id,
            peak_margin_evidence=body.peak_margin_evidence,
        )
    )
    return _roll_view(machine, roll, with_events=True)


@router.post("/{strategy_id}/rolls/{roll_id}/acquire", response_model=RollResponse)
async def acquire_roll(
    strategy_id: str,
    roll_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Acknowledge that the replacement leg was submitted (it places nothing)."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    _owned_roll(machine, roll_id, strategy_id=strategy_id)
    roll = _roll_write_or_409(lambda: machine.acquire(roll_id))
    return _roll_view(machine, roll, with_events=True)


@router.post("/{strategy_id}/rolls/{roll_id}/prove-filled", response_model=RollResponse)
async def prove_roll_filled(
    strategy_id: str,
    roll_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Decide proof from THIS roll's own recorded replacement executions.

    No quantity is accepted from the caller: a declared number is not evidence,
    and neither is the raw attributed book (it also carries holdings that predate
    the roll or belong to another decision). The replacement fills are recorded
    by the executor when a paper execution of the roll's replacement leg is
    CONFIRMED. Full proof releases the close; anything less stalls the roll.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    _owned_roll(machine, roll_id, strategy_id=strategy_id)
    roll = _roll_write_or_409(lambda: machine.prove_filled(roll_id))
    return _roll_view(machine, roll, with_events=True)


@router.post("/{strategy_id}/rolls/{roll_id}/release-close", response_model=RollResponse)
async def release_roll_close(
    strategy_id: str,
    roll_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Release the old-contract close — refused unless the replacement is proven."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    _owned_roll(machine, roll_id, strategy_id=strategy_id)
    roll = _roll_write_or_409(lambda: machine.release_close(roll_id))
    return _roll_view(machine, roll, with_events=True)


@router.post("/{strategy_id}/rolls/{roll_id}/old-flat", response_model=RollResponse)
async def complete_roll(
    strategy_id: str,
    roll_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Complete the roll once the OLD book is proven flat (never asserted).

    No quantity is accepted from the caller: flatness is measured from the
    strategy's attributed book, so a roll cannot be completed by saying so.
    """
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    _owned_roll(machine, roll_id, strategy_id=strategy_id)
    roll = _roll_write_or_409(lambda: machine.mark_old_flat(roll_id))
    return _roll_view(machine, roll, with_events=True)


@router.post("/{strategy_id}/rolls/{roll_id}/stall", response_model=RollResponse)
async def stall_roll(
    strategy_id: str,
    roll_id: str,
    body: RollStallRequest,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Flag an in-flight roll for the owner: capacity and attribution stay put."""
    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    machine = _roll_machine(session_factory)
    _owned_roll(machine, roll_id, strategy_id=strategy_id)
    roll = _roll_write_or_409(lambda: machine.stall(roll_id, reason=body.reason))
    return _roll_view(machine, roll, with_events=True)


@router.get("/{strategy_id}/squareoffs", response_model=SquareoffEvidenceListResponse)
async def list_squareoffs(
    strategy_id: str,
    request: Request,
    environment: str = "all",
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The square-off evidence for one strategy. Owner-only, read-only.

    Evidence is an append-only ledger of what the platform did, so there is
    nothing here to edit. ``environment`` is validated rather than ignored: an
    unrecognised value is a mistake, not "all".
    """
    _ = request
    scope = str(environment or "all").lower()
    if scope not in ("all", "paper", "dry_run", "live"):
        raise HTTPException(
            status_code=422,
            detail={
                "rejection_reason": "ENVIRONMENT_INVALID",
                "environment": scope,
                "allowed": ["all", "paper", "dry_run", "live"],
            },
        )
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))

    from backend.strategies.mis_squareoff import MisSquareoffEvidenceStore

    rows = MisSquareoffEvidenceStore(session_factory=session_factory).for_strategy(
        strategy_id=strategy_id
    )
    return SquareoffEvidenceListResponse(
        squareoffs=[SquareoffEvidenceRow(**row) for row in rows]
    )


@router.get("/{strategy_id}/reservations", response_model=ReservationListResponse)
async def list_reservations(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    _owned_strategy(repo, owner, strategy_id)
    rows = _reservation_ledger(session_factory).list_for_strategy(strategy_id=strategy_id)
    return ReservationListResponse(reservations=[ReservationResponse(**row) for row in rows])


@router.post("/{strategy_id}/plans/{plan_id}/approval", response_model=ApprovalResponse)
async def approve_plan(
    strategy_id: str,
    plan_id: str,
    request: Request,
    payload: ApprovalRequestModel,
    execution_environment: Optional[str] = None,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Record the owner's authorisation, bound to every structural pin."""
    from backend.strategies.approvals import ApprovalError

    enforce_same_origin(request)
    plan = _plan_or_404(_proposal_store(request, session_factory), owner=owner, repo=repo,
                        strategy_id=strategy_id, plan_id=plan_id)
    environment = _derived_environment(
        session_factory,
        plan,
        requested=execution_environment,
        surface="plan_approval",
        plan_id=plan_id,
    )
    try:
        approval = _plan_pipeline(request, session_factory).approve(
            plan,
            actor=owner,
            reservation_id=payload.reservation_id,
            environment=environment,
            actor_kind="manual",
            validity_seconds=payload.validity_seconds,
        )
    except ApprovalError as exc:
        status = 403 if exc.reason_code == "APPROVAL_ACTOR_NOT_OWNER" else 409
        raise HTTPException(status_code=status, detail=exc.as_detail()) from exc
    except PipelineRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    if approval is None:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "APPROVAL_NOT_REQUIRED",
                "plan_id": plan_id,
                "environment": environment,
                "message": (
                    "paper and dry-run plans are exempt from the low-level approval row; "
                    "an approval-based paper request waits for its owner decision instead"
                ),
            },
        )
    return ApprovalResponse(
        **approval,
        structural_validity=_approval_service(session_factory).structural_validity(plan, approval),
    )


@router.post("/{strategy_id}/plans/{plan_id}/approval/revoke", response_model=ApprovalResponse)
async def revoke_plan_approval(
    strategy_id: str,
    plan_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Revoke the plan's active approval. Owner-only, and terminal."""
    from backend.strategies.approvals import ApprovalError

    enforce_same_origin(request)
    plan = _plan_or_404(_proposal_store(request, session_factory), owner=owner, repo=repo,
                        strategy_id=strategy_id, plan_id=plan_id)
    service = _approval_service(session_factory)
    active = service.active_for_plan(plan_id)
    if active is None:
        raise HTTPException(status_code=404, detail="No active approval for this plan")
    try:
        revoked = service.revoke(active["approval_id"], actor_id=owner)
    except ApprovalError as exc:
        status = 403 if exc.reason_code == "APPROVAL_ACTOR_NOT_OWNER" else 409
        raise HTTPException(status_code=status, detail=exc.as_detail()) from exc
    return ApprovalResponse(
        **revoked, structural_validity=service.structural_validity(plan, revoked)
    )


# ---------------------------------------------------------------------------
# Governed execution authorization (Phase 2)
#
# The owner's mode, grants and execution-request decisions. Mode/grant writes go
# through the service, which takes the hosted-strategy row lock; the frontend
# never supplies an actor, a source hash, an account or a policy hash.
# ---------------------------------------------------------------------------


def _authorization_http(exc: Any) -> HTTPException:
    return HTTPException(
        status_code=int(getattr(exc, "status_code", 409)),
        detail=exc.as_detail(),
    )


@router.get("/{strategy_id}/authorization", response_model=AuthorizationStatusResponse)
async def get_authorization(
    strategy_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Authorization mode, active grant, current policy evidence and refusals."""
    from backend.strategies.execution_authorization import AuthorizationError

    _owned_strategy(repo, owner, strategy_id)
    try:
        status = _authorization_service(session_factory).status(owner, strategy_id)
    except AuthorizationError as exc:
        raise _authorization_http(exc) from exc
    return AuthorizationStatusResponse(**status)


@router.put("/{strategy_id}/authorization", response_model=AuthorizationModeResponse)
async def put_authorization_mode(
    strategy_id: str,
    request: Request,
    payload: AuthorizationModeRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Select ``approval_based`` (default) or ``autonomous``.

    Selecting autonomous grants nothing: it only makes an owner-issued grant
    usable. Moving back to approval-based supersedes an active grant in the same
    transaction rather than leaving an unusable grant looking live.
    """
    from backend.strategies.execution_authorization import AuthorizationError

    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    try:
        result = _authorization_service(session_factory).set_mode(
            owner, strategy_id, payload.mode, actor=owner, reason=payload.reason
        )
    except AuthorizationError as exc:
        raise _authorization_http(exc) from exc
    return AuthorizationModeResponse(**result)


@router.post("/{strategy_id}/authorization/grants", response_model=ExecutionGrantResponse)
async def create_execution_grant(
    strategy_id: str,
    request: Request,
    payload: ExecutionGrantRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Issue the owner's standing authorisation, or replay an identical request.

    The server derives the version's source hash, the canonical account and the
    policy hash; a repeat with the same idempotency key returns the original
    grant (including its revocation state) and never resurrects it.
    """
    from backend.strategies.execution_authorization import AuthorizationError

    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    authorize_account_scope(str(canonical.account_scope))
    try:
        result = _authorization_service(session_factory).issue_grant(
            owner,
            strategy_id,
            actor=owner,
            idempotency_key=payload.idempotency_key,
            version_id=payload.version_id,
            execution_environment=payload.execution_environment,
            expires_at=payload.expires_at,
        )
    except AuthorizationError as exc:
        raise _authorization_http(exc) from exc
    return ExecutionGrantResponse(
        **result["grant"], idempotent=bool(result.get("idempotent"))
    )


@router.post(
    "/{strategy_id}/authorization/grants/revoke",
    response_model=ExecutionGrantRevokeResponse,
)
async def revoke_execution_grant(
    strategy_id: str,
    request: Request,
    payload: ExecutionGrantRevokeRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Revoke the active grant (or one named grant). Terminal, and audited.

    Revocation denies later dispatch claims. It does not, and is not described
    as, cancelling an order the broker already has.
    """
    from backend.strategies.execution_authorization import AuthorizationError

    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    try:
        result = _authorization_service(session_factory).revoke_grant(
            owner,
            strategy_id,
            actor=owner,
            reason=payload.reason,
            grant_id=payload.grant_id,
        )
    except AuthorizationError as exc:
        raise _authorization_http(exc) from exc
    return ExecutionGrantRevokeResponse(**result)


@router.get(
    "/{strategy_id}/authorization/grants", response_model=List[ExecutionGrantResponse]
)
async def list_execution_grants(
    strategy_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Grant history, newest first (revoked and superseded rows are kept)."""
    _owned_strategy(repo, owner, strategy_id)
    rows = _authorization_service(session_factory).list_grants(strategy_id, limit=limit)
    return [ExecutionGrantResponse(**row) for row in rows]


@router.get("/{strategy_id}/execution-requests", response_model=ExecutionRequestListResponse)
async def list_execution_requests(
    strategy_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The strategy's durable execution requests, newest first."""
    _owned_strategy(repo, owner, strategy_id)
    from backend.strategies.execution_requests import ExecutionRequestService

    service = ExecutionRequestService(
        session_factory, authorization=_authorization_service(session_factory)
    )
    return ExecutionRequestListResponse(
        strategy_id=strategy_id,
        requests=[ExecutionRequestRow(**row) for row in service.list_for_strategy(strategy_id, limit=limit)],
    )


@router.get(
    "/{strategy_id}/execution-requests/{request_id}", response_model=ExecutionRequestRow
)
async def get_execution_request(
    strategy_id: str,
    request_id: str,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    from backend.strategies.execution_requests import ExecutionRequestService

    _owned_strategy(repo, owner, strategy_id)
    service = ExecutionRequestService(session_factory)
    row = service.get_for_owner(owner, strategy_id, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Execution request not found")
    return ExecutionRequestRow(**row)


@router.post(
    "/{strategy_id}/execution-requests/{request_id}/approve",
    response_model=ExecutionRequestDecisionResponse,
)
async def approve_execution_request(
    strategy_id: str,
    request_id: str,
    request: Request,
    payload: ExecutionRequestDecisionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Owner authorises the exact requested plan; the request becomes dispatchable.

    The decision is durable in one transaction, so an approved action cannot be
    lost to a dropped HTTP response, and it queues exactly ONE dispatch.
    """
    from backend.strategies.execution_requests import (
        ExecutionRequestError,
        ExecutionRequestService,
    )

    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    service = ExecutionRequestService(session_factory)
    try:
        result = service.approve(
            request_id, owner_id=owner, strategy_id=strategy_id, actor=owner
        )
    except ExecutionRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return ExecutionRequestDecisionResponse(
        request=ExecutionRequestRow(**result["request"]),
        approved=bool(result.get("approved")),
    )


@router.post(
    "/{strategy_id}/execution-requests/{request_id}/reject",
    response_model=ExecutionRequestDecisionResponse,
)
async def reject_execution_request(
    strategy_id: str,
    request_id: str,
    request: Request,
    payload: ExecutionRequestDecisionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Owner refuses the requested plan. Terminal; nothing is dispatched."""
    from backend.strategies.execution_requests import (
        ExecutionRequestError,
        ExecutionRequestService,
    )

    enforce_same_origin(request)
    _owned_strategy(repo, owner, strategy_id)
    service = ExecutionRequestService(session_factory)
    try:
        result = service.reject(
            request_id,
            owner_id=owner,
            strategy_id=strategy_id,
            actor=owner,
            reason=payload.reason,
        )
    except ExecutionRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    return ExecutionRequestDecisionResponse(
        request=ExecutionRequestRow(**result["request"]),
        rejected=bool(result.get("rejected")),
    )


@router.get("/{strategy_id}/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    strategy_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Approval history with **derived** structural validity per row.

    Approvals are never rewritten, so what changed is the answer to "does this
    still hold" — computed on read against the current pins.
    """
    _owned_strategy(repo, owner, strategy_id)
    service = _approval_service(session_factory)
    store = _proposal_store(request, session_factory)
    rows = service.list_for_strategy(strategy_id=strategy_id)
    out = []
    for row in rows:
        plan = store.get_plan(str(row["plan_id"]))
        validity = (
            service.structural_validity(plan, row)
            if plan is not None
            else {"valid": False, "mismatched_pins": ["PLAN_NOT_FOUND"], "detail": {}}
        )
        out.append(ApprovalResponse(**row, structural_validity=validity))
    return ApprovalListResponse(approvals=out)


@router.get("/{strategy_id}/proposals", response_model=ProposalListResponse)
async def list_strategy_proposals(
    strategy_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The durable proposal trail for one strategy, with its journal.

    Read-only by design: an envelope is an immutable fact and the journal is its
    sequence, so there is nothing here to edit, retry or delete.
    """
    _owned_strategy(repo, owner, strategy_id)
    store = _proposal_store(request, session_factory)
    return ProposalListResponse(
        proposals=[ProposalRow(**row) for row in store.list_proposals(strategy_id=strategy_id)],
        journal=[
            ProposalJournalRow(**row) for row in store.journal(strategy_id=strategy_id)
        ],
    )


@router.get("/{strategy_id}/plans/by-id/{plan_id}", response_model=PlanResponse)
async def get_strategy_plan_by_id(
    strategy_id: str,
    plan_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """One frozen plan looked up by its own id.

    A durable execution request records ``plan_id`` (its proposal id is not part
    of that row), so the operator's plan review needs this lookup to explain what
    a request is about to do. The plan row is immutable; ``invalidation_state``
    is derived on read.
    """
    plan = _plan_or_404(
        _proposal_store(request, session_factory),
        owner=owner,
        repo=repo,
        strategy_id=strategy_id,
        plan_id=plan_id,
    )
    state = plan_invalidation_state(plan, session_factory=session_factory)
    return PlanResponse(**plan, invalidation_state=state)


@router.get("/{strategy_id}/plans/{proposal_id}", response_model=PlanResponse)
async def get_strategy_plan(
    strategy_id: str,
    proposal_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """One frozen plan, plus its *derived* invalidation state.

    The plan row is immutable; ``invalidation_state`` is computed on read against
    the current catalog, so a superseded plan reports why it no longer holds
    while the artifact itself is never rewritten.
    """
    _owned_strategy(repo, owner, strategy_id)
    store = _proposal_store(request, session_factory)
    plan = store.plan_for_proposal(proposal_id)
    if plan is None or str(plan["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    state = plan_invalidation_state(plan, session_factory=session_factory)
    return PlanResponse(**plan, invalidation_state=state)


# ---------------------------------------------------------------------------
# Plan execution (Phase 6 / Project 6, D-7) — paper only, owner-triggered
# ---------------------------------------------------------------------------


def _paper_plan_executor(request: Request, session_factory: Any):
    """The paper plan executor, injectable for tests (app.state override)."""
    from backend.strategies.execution import PaperPlanExecutor

    executor = getattr(request.app.state, "paper_plan_executor", None)
    if executor is not None:
        return executor
    paper = getattr(request.app.state, "paper_runtime_service", None)
    if paper is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    return PaperPlanExecutor(session_factory=session_factory, paper_service=paper)


def _live_plan_executor(request: Request, session_factory: Any):
    """The hosted LIVE plan executor (app.state override wins).

    The deployment setting is enforced inside the executor, so this factory never
    decides whether live is allowed.
    """
    from backend.strategies.live_service import LivePlanExecutor

    executor = getattr(request.app.state, "live_plan_executor", None)
    if executor is not None:
        return executor
    from backend.strategies.execution_requests import ExecutionRequestService

    return LivePlanExecutor(
        session_factory=session_factory,
        authorization=ExecutionRequestService(
            session_factory,
            authorization=_authorization_service(session_factory),
        ),
    )


def _refuse_live_when_disabled(
    execution_mode: Any, *, surface: str, plan_id: str = ""
) -> None:
    """The deployment setting gates LAUNCH and ADMISSION, not only submission.

    Persisted mode constraints and API/SDK validation admit ``live`` so the mode
    is representable, but a deployment that has not enabled hosted live must not
    create live work, reserve live capacity for it, or hand it a child
    credential. This reuses the SAME ``HOSTED_LIVE_ENABLED`` reader the executor
    uses, so there is exactly one answer to "is live on in this deployment".
    """
    if str(execution_mode or "").lower() != "live":
        return
    from backend.strategies.live_settings import (
        hosted_live_disabled_detail,
        hosted_live_enabled,
    )

    if hosted_live_enabled():
        return
    raise HTTPException(
        status_code=409,
        detail={
            "rejection_reason": "LIVE_DISABLED",
            **hosted_live_disabled_detail(plan_id=plan_id, surface=surface),
        },
    )


def _plan_environment(session_factory: Any, plan: Dict[str, Any]) -> str:
    """The plan's environment, from PERSISTED binding authority.

    A request parameter never selects paper vs live; an unresolvable binding is a
    refusal, never a silent default.
    """
    from backend.strategies.live_authority import LiveAuthorityRefusal, plan_binding

    try:
        return str(plan_binding(session_factory, plan=plan).get("execution_environment") or "")
    except LiveAuthorityRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


def _derived_environment(
    session_factory: Any,
    plan: Dict[str, Any],
    *,
    requested: Optional[str],
    surface: str,
    plan_id: str,
) -> str:
    """The environment for this plan, derived; an explicit disagreement is refused.

    Phase 2 closed a real weakness here: ``/reserve`` used to take its
    environment from a query parameter, so a paper plan could be reserved in the
    ``live`` environment. Now the persisted binding decides, the deployment gate
    is applied to the DERIVED environment, and a caller that explicitly names a
    different one gets ``PLAN_ENVIRONMENT_MISMATCH`` instead of a silent switch.
    """
    environment = _plan_environment(session_factory, plan)
    if environment not in ("paper", "dry_run", "live"):
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "PLAN_ENVIRONMENT_UNRESOLVED",
                "plan_id": str(plan_id),
                "environment": environment,
            },
        )
    wanted = None if requested is None else str(requested).strip().lower() or None
    if wanted is not None and wanted != environment:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "PLAN_ENVIRONMENT_MISMATCH",
                "plan_id": str(plan_id),
                "bound_environment": environment,
                "requested_environment": wanted,
                "message": (
                    "the execution environment is derived from the plan's persisted "
                    "run binding and cannot be selected by the caller"
                ),
            },
        )
    _refuse_live_when_disabled(environment, surface=surface, plan_id=str(plan_id))
    return environment


@router.post("/{strategy_id}/plans/{plan_id}/execute", response_model=ExecutionResponse)
async def execute_plan(
    strategy_id: str,
    plan_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Execute one admitted paper plan through the paper runtime.

    Paper accounts only, ever: the executor refuses anything else by name
    (``PAPER_ONLY_EXECUTION``), and live enablement is a separate authorization
    that does not exist yet. Execution CONSUMES the reservation on fills and
    releases it ``terminal_unfilled`` on rejections; every transition — every
    refusal included — lands in the append-only execution trail.
    """
    enforce_same_origin(request)
    plan = _plan_or_404(_proposal_store(request, session_factory), owner=owner, repo=repo,
                        strategy_id=strategy_id, plan_id=plan_id)
    try:
        result = await _plan_pipeline(request, session_factory).execute(plan, actor=owner)
    except PipelineRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    return ExecutionResponse(**result)


@router.post(
    "/{strategy_id}/plans/{plan_id}/residual",
    response_model=ResidualDispositionResponse,
)
async def dispose_residual(
    strategy_id: str,
    plan_id: str,
    request: Request,
    payload: ResidualDispositionRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Bounded, owner-only disposition of a live step that needs repair.

    A terminal broker cancel with a residual leaves the step in
    ``repair_required`` and blocks a quiet proof. This is the ONLY way that step
    stops blocking, and it never fabricates a fill or a rejection: the residual
    quantity, the acting operator and the reason are written to the append-only
    plan trail, the unused capacity is released, and the step's ``work_resolved``
    barrier event is recorded exactly once. The server still decides: the step
    must be in ``repair_required`` (or be a ``releasing`` claim whose send is
    PROVEN never to have reached the broker by the platform's own durable
    pre-send records), and the disposition is refused while the plan's evaluation
    authority could still fill the residual. A ``releasing`` claim whose send was
    attempted and not resolved is never abandoned: when the broker order can be
    found it is ADOPTED onto the claim instead, and otherwise the step stays in
    flight.
    """
    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    enforce_same_origin(request)
    plan = _plan_or_404(
        _proposal_store(request, session_factory),
        owner=owner,
        repo=repo,
        strategy_id=strategy_id,
        plan_id=plan_id,
    )
    if str(payload.action) != "abandon":
        raise HTTPException(
            status_code=422,
            detail={
                "rejection_reason": "RESIDUAL_ACTION_UNSUPPORTED",
                "action": str(payload.action),
                "supported": list(RESIDUAL_ACTIONS),
            },
        )
    service = LiveRepairService(session_factory=session_factory)
    try:
        result = service.abandon_residual(
            plan_id=str(plan["plan_id"]),
            step_no=None if payload.step_no is None else int(payload.step_no),
            actor=owner,
            reason=payload.reason,
        )
    except LiveRepairRefusal as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    return ResidualDispositionResponse(**result)


@router.get("/{strategy_id}/plans/{plan_id}/executions", response_model=ExecutionTrailResponse)
async def list_plan_executions(
    strategy_id: str,
    plan_id: str,
    request: Request,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The plan's append-only execution trail (D-3).

    Read-only by design: the rows are facts, the current step state is derived
    from them, and nothing here can rewrite what happened.
    """
    from sqlalchemy import select

    from backend.strategies.attribution_models import StrategyPlanExecutionEvent

    _owned_strategy(repo, owner, strategy_id)
    plan = _proposal_store(request, session_factory).get_plan(plan_id)
    if plan is None or str(plan["strategy_id"]) != str(strategy_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    with session_factory() as session:
        rows = session.execute(
            select(StrategyPlanExecutionEvent)
            .where(StrategyPlanExecutionEvent.plan_id == plan_id)
            .order_by(
                StrategyPlanExecutionEvent.created_at,
                StrategyPlanExecutionEvent.step_no,
                StrategyPlanExecutionEvent.id,
            )
        ).scalars().all()
        events = [
            ExecutionEventRow(
                id=str(row.id),
                plan_id=str(row.plan_id),
                step_no=int(row.step_no),
                event=str(row.event),
                paper_order_id=row.paper_order_id,
                filled_quantity=row.filled_quantity,
                refusal_reason=row.refusal_reason,
                actor_id=str(row.actor_id),
                detail=dict(row.detail or {}),
                created_at=row.created_at,
            )
            for row in rows
        ]
    return ExecutionTrailResponse(plan_id=plan_id, events=events)


@router.post("/{strategy_id}/adjustments", response_model=AdjustmentResponse)
async def create_adjustment(
    strategy_id: str,
    request: Request,
    payload: AdjustmentCreateRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Record an append-only owner reclassification (account owner only).

    Same authorization as grant issuance: the acting app user must own the
    canonical strategy and be authorized on its account. Original fills are never
    rewritten — the correction is a new line, and a reversal is a new
    opposite-sign adjustment referencing this one in ``evidence``.
    """
    enforce_same_origin(request)
    hosted = _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    account_scope = str(canonical.account_scope)
    if not is_account_authorized(account_scope):
        raise HTTPException(status_code=403, detail="Account scope is not authorized for this operator")
    if str(canonical.status) == "archived":
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "STRATEGY_ARCHIVED",
                "strategy_id": strategy_id,
                "message": "An archived strategy preserves history and accepts no new adjustments.",
            },
        )

    lines = []
    for line in payload.lines:
        if int(line.quantity_delta) == 0:
            raise HTTPException(
                status_code=422,
                detail={"rejection_reason": "ADJUSTMENT_LINE_ZERO_DELTA", "line_no": len(lines) + 1},
            )
        lines.append(line.model_dump())

    record = _attribution_store(request).create_reclassification(
        account_id=account_scope,
        strategy_id=strategy_id,
        owner_id=owner,
        reason_code=payload.reason_code,
        created_by=owner,
        lines=lines,
        evidence=dict(payload.evidence or {}),
    )
    _ = hosted  # ownership already asserted above
    return AdjustmentResponse(
        adjustment_id=record["adjustment_id"],
        strategy_id=strategy_id,
        account_id=record["account_id"],
        adjustment_kind=record["adjustment_kind"],
        reason_code=record["reason_code"],
        created_by=record["created_by"],
        evidence=dict(record["evidence"]),
        created_at=_iso(record["created_at"]),
        lines=[
            AdjustmentLineResponse(
                line_no=line["line_no"],
                instrument_token=line["instrument_token"],
                exchange=line["exchange"],
                tradingsymbol=line["tradingsymbol"],
                product=line["product"],
                quantity_delta=line["quantity_delta"],
                effective_at=_iso(line["effective_at"]),
            )
            for line in record["lines"]
        ],
    )


@router.get("/{strategy_id}/positions", response_model=PositionListResponse)
async def list_positions(
    strategy_id: str,
    request: Request,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """The strategy's projected book for one environment (default ``live``).

    Unresolved rows are returned with their ``unresolved_reason``: exposure that
    could not be mapped to a canonical instrument is surfaced, never hidden.
    """
    hosted = _owned_strategy(repo, owner, strategy_id)
    env = _environment_param(environment)
    rows = await _attribution_service(request).open_positions(
        account_id=str(hosted.default_account_scope),
        strategy_id=strategy_id,
        execution_environment=env,
    )
    return PositionListResponse(
        strategy_id=strategy_id,
        environment=env,
        positions=[
            PositionRow(
                identity_kind=row["identity_kind"],
                identity_key=row["identity_key"],
                product=row["product"],
                instrument_token=row["instrument_token"],
                exchange=row["exchange"],
                tradingsymbol=row["tradingsymbol"],
                net_quantity=row["net_quantity"],
                unresolved_reason=row.get("unresolved_reason"),
            )
            for row in rows
        ],
    )


@router.post("/{strategy_id}/positions/rebuild", response_model=RebuildResponse)
async def rebuild_positions(
    strategy_id: str,
    request: Request,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Explicit full recompute of one book (the operational remedy for anomalies).

    G1 adds no scheduler: publication is on-demand, and a rebuild is always a
    full recompute rather than an incremental patch.
    """
    enforce_same_origin(request)
    hosted = _owned_strategy(repo, owner, strategy_id)
    env = _environment_param(environment)
    result = await _attribution_service(request).publish(
        account_id=str(hosted.default_account_scope),
        strategy_id=strategy_id,
        execution_environment=env,
    )
    return RebuildResponse(
        strategy_id=strategy_id,
        execution_environment=env,
        projection_version=int(result["projection_version"]),
        unchanged=bool(result["unchanged"]),
        folded_facts=int(result.get("folded_facts") or 0),
        unresolved=list(result.get("unresolved") or []),
        anomalies=list(result.get("anomalies") or []),
    )


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
        risk_policy = service.validate_risk_policy(payload.risk_policy)
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        row = repo.create_version(
            strategy_id=strategy_id,
            source=source,
            source_sha256=digest,
            parameters_schema=schema,
            capabilities_snapshot=capabilities,
            risk_policy=risk_policy,
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



def _stop_view(job: Any) -> dict:
    """Distinguish requested/stopping/confirmed/cleanup-unresolved for a stop.

    A terminal job label alone does not prove process cleanup: for a launched
    attempt we require the supervisor's ``process_cleanup_state == 'confirmed'``
    before reporting ``confirmed``.
    """
    launched = job.handoff_at is not None
    requested = job.stop_requested_at is not None or str(job.desired_state or "") == "stopped"
    status = str(job.status or "")
    blocked = _job_replacement_blocked(job)
    if status in {"queued", "starting", "running"}:
        if not requested:
            label = {"queued": "Queued", "starting": "Starting", "running": "Running"}.get(status, status)
            return {"requested": False, "state": "none", "requested_at": None, "requested_by": None,
                    "replacement_blocked": blocked,
                    "note": f"{label}; no stop requested. Stop does not cancel orders or flatten."}
        state = "requested" if (status == "queued" or not launched) else "stopping"
        note = ("Stop requested; the supervisor will stop the child and complete the authorized "
                "terminal transition. Stop does not cancel orders or flatten.")
        return {"requested": True, "state": state, "requested_at": _iso(job.stop_requested_at),
                "requested_by": job.stop_requested_by, "replacement_blocked": blocked, "note": note}
    if status in {"stopped", "recovery_required", "failed"}:
        if not launched:
            return {"requested": requested, "state": "confirmed",
                    "requested_at": _iso(job.stop_requested_at), "requested_by": job.stop_requested_by,
                    "replacement_blocked": blocked,
                    "note": "Stopped before launch; no child process was ever started."}
        if launched and str(job.process_cleanup_state or "") != "confirmed":
            return {"requested": requested, "state": "cleanup_unresolved",
                    "requested_at": _iso(job.stop_requested_at), "requested_by": job.stop_requested_by,
                    "replacement_blocked": blocked,
                    "note": "Terminal, but child process cleanup is not confirmed. Unknown is not 'stopped'."}
        return {"requested": requested, "state": "confirmed", "requested_at": _iso(job.stop_requested_at),
                "requested_by": job.stop_requested_by, "replacement_blocked": blocked,
                "note": "Stopped and process cleanup confirmed."}
    return {"requested": requested, "state": "none", "requested_at": _iso(job.stop_requested_at),
            "requested_by": job.stop_requested_by, "replacement_blocked": blocked, "note": ""}


def _notification_store(request: Request):
    repo = getattr(request.app.state, "notification_repository", None)
    if repo is not None:
        return repo
    from backend.notifications.repository import SqlAlchemyNotificationRepository

    factory = getattr(request.app.state, "alerts_session_factory", None)
    if factory is None:
        from backend.app.database import SessionLocal

        factory = SessionLocal
    return SqlAlchemyNotificationRepository(factory)


def _job_detail(job: Any) -> JobDetailResponse:
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
        stop_requested_at=_iso(job.stop_requested_at),
        stop_requested_by=job.stop_requested_by,
        stop=_stop_view(job),
        logs_discarded=bool(job.logs_discarded),
        logs_source=job.logs_source,
    )

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
    return _job_detail(job)


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

    # D-4: quiescence is PROVEN, never assumed. The operator reconciliation path
    # is where the platform records the durable barrier proof: under the book's
    # advisory lock it enumerates the in-flight sources (run executions, order
    # intents, reconciliation, ingest) and records a proof ONLY when that
    # enumeration is empty. Outstanding work is refused with the work named, and
    # the attempt stays ``recovery_required`` - it is never quietly unblocked.
    barrier = _settlement_barrier(request)
    barrier_state = None
    proof_refusal: Optional[Dict[str, Any]] = None
    proof_required = False
    expected_barrier_version: Optional[int] = None
    try:
        capabilities = service.parse_capability_snapshot(job.capabilities_snapshot)
        trade_capable = bool(capabilities.get("trade"))
    except service.StrategyValidationError:
        trade_capable = True
    environment = str(job.execution_mode or "")
    # A launched, trade-capable attempt must carry a CURRENT durable proof for
    # this exact book through the unblock transaction. LIVE is included: a live
    # attempt is exactly the case where "the two reads matched" proves nothing.
    if trade_capable and job.handoff_at is not None and environment in ("paper", "dry_run", "live"):
        # A launched, trade-capable attempt must carry a CURRENT durable proof for
        # this exact book through the unblock transaction.
        proof_required = True
        try:
            barrier_state = barrier.state(
                account_id=str(job.account_scope or ""),
                strategy_id=str(job.strategy_id or ""),
                execution_environment=environment,
            )
        except Exception as exc:  # noqa: BLE001 - unreadable state is never verified
            barrier_state = None
            proof_refusal = {"reason": "evidence_unavailable", "unavailable": [f"settlement_barrier:{exc}"]}
        if proof_refusal is None and not bool(barrier_state.get("proof_valid")):
            result = barrier.record_proof(
                account_id=str(job.account_scope or ""),
                strategy_id=str(job.strategy_id or ""),
                execution_environment=environment,
                ref=f"reconcile:{job.id}:{int(job.attempt)}",
                detail={"job_id": job.id, "attempt": int(job.attempt), "actor_id": owner},
            )
            if not result.recorded:
                proof_refusal = {
                    "reason": result.reason,
                    "barrier_version": result.barrier_version,
                    "inflight": [item.as_dict() for item in (result.inflight or [])],
                    "unavailable": list(result.unavailable or []),
                }
            else:
                barrier_state = barrier.state(
                    account_id=str(job.account_scope or ""),
                    strategy_id=str(job.strategy_id or ""),
                    execution_environment=environment,
                )
        if barrier_state is not None and bool(barrier_state.get("proof_valid")):
            expected_barrier_version = int(barrier_state.get("barrier_version") or 0)
    if proof_refusal is not None and proof_refusal.get("reason") != "evidence_unavailable":
        # Outstanding work is named here, with its enumeration: the attempt stays
        # blocked and the operator sees exactly what is still in flight. An
        # UNREADABLE barrier is different: nothing can be proved, so the normal
        # assessment runs and fails closed on ``quiescence_state != verified`` -
        # one authority for the decision, never an invented blocker.
        reason_code = "EXECUTION_WORK_OUTSTANDING"
        blocked = repo.record_reconciliation(
            job_id=job.id,
            strategy_id=job.strategy_id,
            owner_id=owner,
            attempt=int(job.attempt),
            run_id=job.run_id,
            outcome="blocked",
            reason_code=reason_code,
            evidence={"settlement_barrier": proof_refusal, "barrier_state": barrier_state},
            actor_id=owner,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": reason_code,
                "case": "blocked",
                "blocking_reasons": [reason_code],
                "settlement_barrier": proof_refusal,
                "audit_id": blocked.id,
            },
        )

    collector = _collector(request, barrier=barrier)
    evidence = await collector.collect(job)
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

    # Re-collect immediately before commit: outstanding/in-flight execution
    # evidence is not versioned by lease-epoch, so a changed digest (or a source
    # that became unavailable) fails closed rather than clearing the block on
    # stale evidence.
    recheck = await collector.collect(job)
    reassessment = assess(recheck)
    if evidence_digest(recheck) != evidence_digest(evidence) or not reassessment.allowed:
        audit = repo.record_reconciliation(
            job_id=job.id,
            strategy_id=job.strategy_id,
            owner_id=owner,
            attempt=int(job.attempt),
            run_id=job.run_id,
            outcome="blocked",
            reason_code="EVIDENCE_CHANGED",
            evidence=recheck.to_dict(),
            actor_id=owner,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "EVIDENCE_CHANGED",
                "message": "execution evidence changed; re-inspect before reconciling",
                "blocking_reasons": reassessment.blocking_reasons,
                "evidence": recheck.to_dict(),
                "audit_id": audit.id,
            },
        )

    # Atomic: CAS on the in-DB evidence + clear the block + append the audit row.
    audit = repo.reconcile_with_audit(
        job.id,
        owner_id=owner,
        expected_lease_epoch=int(job.lease_epoch),
        expected_attempt=int(job.attempt),
        expected_process_cleanup_state=recheck.process_cleanup_state,
        expected_run_id=recheck.run_id,
        reason_code=reassessment.reason_code,
        evidence=recheck.to_dict(),
        actor_id=owner,
        # Narrow TOCTOU guard: the proof is validated INSIDE the unblock
        # transaction, under the book's advisory lock, at the exact version the
        # assessment used. Work committed between the second collect and the CAS
        # therefore invalidates the proof and refuses the unblock.
        settlement_barrier=barrier if proof_required else None,
        barrier_account_id=str(job.account_scope or ""),
        barrier_strategy_id=str(job.strategy_id or ""),
        barrier_environment=environment,
        expected_barrier_version=expected_barrier_version,
        require_barrier_proof=proof_required,
        # A launched, trade-capable attempt has a linked worker run; stop it in
        # the SAME transaction as the unblock (closed-status semantics with
        # closed_at), so replacement can never be cleared while the trading run
        # is still open. Data-only/unlaunched attempts pass no run.
        close_worker_run=bool(proof_required and recheck.run_id),
        worker_run_id=None if not recheck.run_id else str(recheck.run_id),
    )
    if audit is None:
        blocked = repo.record_reconciliation(
            job_id=job.id,
            strategy_id=job.strategy_id,
            owner_id=owner,
            attempt=int(job.attempt),
            run_id=job.run_id,
            outcome="blocked",
            reason_code="RECONCILE_RACE_LOST",
            evidence=recheck.to_dict(),
            actor_id=owner,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "RECONCILE_RACE_LOST",
                "message": "attempt state changed; re-inspect before reconciling",
                "audit_id": blocked.id,
            },
        )

    return ReconciliationActionResponse(
        status="reconciled",
        job_id=job.id,
        attempt=int(job.attempt),
        case=reassessment.case,
        reason_code=reassessment.reason_code,
        replacement_blocked=False,
        blocking_reasons=[],
        evidence=recheck.to_dict(),
        audit_id=audit.id,
    )


def _normalized_launch(repo: SqlAlchemyStrategyRepository, strategy: Any, payload: RunNowRequest) -> dict:
    """Validate and normalize a launch request against the pinned version.

    Bound to the idempotency key so the same key cannot be reused for a
    different version/params/mode/kind.
    """
    execution_mode = payload.execution_mode or strategy.default_execution_mode
    job_kind = payload.job_kind or strategy.default_job_kind
    try:
        service.validate_account_scope(strategy.default_account_scope, execution_mode)
        if execution_mode not in service.ALLOWED_EXECUTION_MODES:
            raise service.StrategyValidationError("unsupported execution_mode")
        if job_kind not in service.ALLOWED_JOB_KINDS:
            raise service.StrategyValidationError("unsupported job_kind")
        version = repo.get_version_by_id(strategy.id, payload.version_id)
        if version is None:
            raise service.StrategyValidationError("version does not belong to this strategy")
        params = service.validate_parameters(version.parameters_schema, payload.params)
    except service.StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "version_id": str(version.id),
        "execution_mode": execution_mode,
        "job_kind": job_kind,
        "params": params,
    }


def _replay_or_conflict(existing: Any, normalized: dict, owner: str, strategy_id: str) -> Any:
    if existing.owner_id != owner or existing.strategy_id != strategy_id:
        raise HTTPException(status_code=409, detail="REPLACEMENT_CONFLICT")
    if (
        str(existing.version_id) == normalized["version_id"]
        and str(existing.execution_mode) == normalized["execution_mode"]
        and str(existing.job_kind) == normalized["job_kind"]
        and dict(existing.params_snapshot or {}) == normalized["params"]
    ):
        return existing
    raise HTTPException(
        status_code=409,
        detail={
            "rejection_reason": "IDEMPOTENCY_CONFLICT",
            "message": "the idempotency key was already used for a different launch request",
        },
    )


@router.post("/{strategy_id}/jobs", response_model=RunNowResponse)
async def run_now(
    strategy_id: str,
    request: Request,
    payload: RunNowRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Operator "Run now": create a queued job through the existing repository.

    Owner is server-derived; the pinned account scope is authorized for the
    requested mode. The version is immutable and params are validated against its
    schema; mode/capabilities/policy snapshots are persisted by the store. The
    response returns the job identity — it does **not** claim the process has
    started. A retry with the same ``idempotency_key`` returns the same job.
    """
    enforce_same_origin(request)
    strategy = _owned_strategy(repo, owner, strategy_id)
    normalized = _normalized_launch(repo, strategy, payload)

    # LAUNCH is gated by the deployment setting as well as by the mode
    # vocabulary: with ``HOSTED_LIVE_ENABLED`` off, a live launch is refused
    # before any job row exists, so a disabled deployment cannot accumulate
    # queued live work that a later flip would start.
    _refuse_live_when_disabled(
        normalized["execution_mode"], surface="job_launch"
    )

    # Server-side authorization: the pinned account scope must be authorized for
    # the operator's environment (independent of the launch request itself).
    authorize_account_scope(strategy.default_account_scope)

    occurrence_key = f"manual:{strategy_id}:{payload.idempotency_key}"
    # Idempotency is checked before the active-job block so a retry of a
    # still-active launch returns the same job instead of being refused. The key
    # is bound to the normalized launch request: an identical request replays;
    # a different request is an explicit conflict.
    existing = repo.get_job_by_occurrence_key(occurrence_key)
    if existing is not None:
        return RunNowResponse(idempotent=True, job=_job_detail(_replay_or_conflict(existing, normalized, owner, strategy_id)))

    # Shared Run now path: finish an eligible predecessor's continuation proof
    # BEFORE the new attempt is refused. This is what lets a healthy finite
    # evaluation hand its held book to the next evaluation even when the host
    # restarted between the supervised release and the proof. An ineligible
    # predecessor is left exactly as it was, and create_job below still refuses
    # with STRATEGY_BLOCKED - the manual reconciliation path is unchanged.
    await _finish_predecessor_continuation(request, repo, owner, strategy_id)

    idempotent = False
    try:
        job = repo.create_job(
            strategy_id=strategy_id,
            version_id=normalized["version_id"],
            owner_id=owner,
            job_kind=normalized["job_kind"],
            execution_mode=normalized["execution_mode"],
            params=normalized["params"],
            occurrence_key=occurrence_key,
        )
    except (service.StrategyValidationError, StrategyIdentityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StrategyDisabled as exc:
        raise HTTPException(status_code=409, detail="STRATEGY_DISABLED") from exc
    except StrategyFenceError as exc:
        raise HTTPException(status_code=409, detail="STRATEGY_BLOCKED") from exc
    except StrategyIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409, detail={"rejection_reason": "IDEMPOTENCY_CONFLICT", "message": str(exc)}
        ) from exc
    except StrategyConflict:
        existing = repo.get_job_by_occurrence_key(occurrence_key)
        if existing is None:
            raise HTTPException(status_code=409, detail="REPLACEMENT_CONFLICT")
        job = _replay_or_conflict(existing, normalized, owner, strategy_id)
        idempotent = True
    return RunNowResponse(idempotent=idempotent, job=_job_detail(job))


@router.post("/{strategy_id}/jobs/{job_id}/stop", response_model=StopJobResponse)
async def stop_job(
    strategy_id: str,
    job_id: str,
    request: Request,
    payload: StopJobRequest,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Operator Stop against an immutable job/attempt.

    Queued work is stopped without launching it; active work receives a durable
    stop request the supervisor observes to perform bounded local cleanup.
    Stop does **not** cancel orders or flatten positions, and it does not clear
    the replacement block: launched work still requires reconciliation.
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

    idempotent = False
    if job.status == "queued":
        stopped = repo.stop_queued_job(
            job.id, owner_id=owner, expected_attempt=int(job.attempt), actor=owner
        )
        if not stopped:
            raise HTTPException(status_code=409, detail={"rejection_reason": "STOP_RACE_LOST"})
    elif job.status in {"starting", "running"}:
        requested = repo.request_stop_active(
            job.id, owner_id=owner, expected_attempt=int(job.attempt), actor=owner
        )
        if not requested:
            refreshed = repo.get_job(owner, job.id)
            if refreshed is None or str(refreshed.desired_state or "") != "stopped":
                raise HTTPException(status_code=409, detail={"rejection_reason": "STOP_RACE_LOST"})
            idempotent = True
    else:
        idempotent = True  # already terminal: nothing to stop

    refreshed = repo.get_job(owner, job.id)
    return StopJobResponse(
        job_id=job.id,
        attempt=int(job.attempt),
        idempotent=idempotent,
        stop=_stop_view(refreshed),
    )


@router.get("/{strategy_id}/jobs/{job_id}/logs", response_model=JobLogsResponse)
async def get_job_logs(
    strategy_id: str,
    job_id: str,
    request: Request,
    after_seq: int = 0,
    limit: int = 200,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Bounded, redacted child logs for an owner/account-authorized job.

    The API never reads the supervisor container's filesystem; logs are pushed
    by the supervisor through the lifecycle API, redacted on ingest and capped.
    Unavailable/truncated states are explicit.
    """
    from backend.api.services.hosted_lifecycle import LOG_TOTAL_MAX_BYTES

    job = _authorized_job(repo, owner, strategy_id, job_id)
    rows = repo.list_job_logs(job.id, after_seq=after_seq, limit=limit)
    total_bytes = repo.job_log_byte_count(job.id)
    entries = [
        JobLogEntryResponse(seq=int(row.seq), content=str(row.content), created_at=_iso(row.created_at))
        for row in rows
    ]
    next_seq = int(rows[-1].seq) if rows else int(after_seq)
    available = total_bytes > 0
    # Truncation reflects actual loss: the persisted discarded flag OR the cap.
    truncated = bool(job.logs_discarded) or total_bytes >= LOG_TOTAL_MAX_BYTES
    if available:
        notice = (
            "Logs were collected after the child terminated (live streaming is not implemented). "
            "Output was discarded at the size cap." if truncated
            else "Logs were collected after the child terminated (live streaming is not implemented)."
        )
    elif job.handoff_at is None:
        notice = "No child was launched for this attempt; no logs exist."
    else:
        notice = "Logs not collected (the supervisor may be unavailable or the child produced no output)."
    return JobLogsResponse(
        job_id=job.id,
        available=available,
        truncated=truncated,
        source=job.logs_source,
        next_seq=next_seq,
        entries=entries,
        notice=notice,
    )


@router.get(
    "/{strategy_id}/jobs/{job_id}/notifications",
    response_model=RunNotificationListResponse,
)
async def get_job_notifications(
    strategy_id: str,
    job_id: str,
    request: Request,
    limit: int = 50,
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
):
    """Owner-scoped run notification events with delivery/attempt history."""
    job = _authorized_job(repo, owner, strategy_id, job_id)
    if not job.run_id:
        return RunNotificationListResponse(job_id=job.id, run_id=None, events=[])
    store = _notification_store(request)
    events = store.list_run_notifications(job.owner_id, str(job.run_id), limit=limit)
    deliveries = store.list_deliveries_for_events([event.id for event in events])
    attempts = store.list_attempts_for_deliveries([delivery.id for delivery in deliveries])
    channels = store.get_channels_by_ids([delivery.channel_id for delivery in deliveries])

    attempts_by_delivery: Dict[str, list] = {}
    for attempt in attempts:
        attempts_by_delivery.setdefault(attempt.delivery_id, []).append(attempt)
    deliveries_by_event: Dict[str, list] = {}
    for delivery in deliveries:
        deliveries_by_event.setdefault(delivery.event_id, []).append(delivery)

    payload_events = []
    for event in events:
        rendered_deliveries = []
        counts: Dict[str, int] = {}
        for delivery in deliveries_by_event.get(event.id, []):
            counts[delivery.status] = counts.get(delivery.status, 0) + 1
            channel = channels.get(delivery.channel_id)
            rendered_deliveries.append(
                DeliveryResponse(
                    delivery_id=delivery.id,
                    channel_id=delivery.channel_id,
                    channel_name=(channel.name if channel is not None else None),
                    status=delivery.status,
                    attempts=int(delivery.attempts or 0),
                    last_error=delivery.last_error,
                    delivered_at=_iso(delivery.delivered_at),
                    attempt_history=[
                        DeliveryAttemptResponse(
                            attempt_no=int(a.attempt_no),
                            outcome=str(a.outcome),
                            detail=str(a.detail or ""),
                            provider_id=a.provider_id,
                            created_at=_iso(a.created_at),
                        )
                        for a in attempts_by_delivery.get(delivery.id, [])
                    ],
                )
            )
        evidence = dict(event.evidence or {})
        payload_events.append(
            RunNotificationEventResponse(
                event_id=event.id,
                run_id=str(event.run_id or ""),
                fired_at=_iso(event.fired_at),
                text=str(evidence.get("text") or ""),
                subject=evidence.get("subject"),
                deliveries=rendered_deliveries,
                delivery_status_counts=counts,
            )
        )
    return RunNotificationListResponse(job_id=job.id, run_id=job.run_id, events=payload_events)


# ---------------------------------------------------------------------------
# Settlement evidence (G7) — owner read + assessment trigger
# ---------------------------------------------------------------------------


def _settlement_service(session_factory: Any):
    from backend.strategies.settlement import SettlementService

    return SettlementService(session_factory=session_factory)


def _authorized_strategy_account(
    repo: SqlAlchemyStrategyRepository, owner: str, strategy_id: str
) -> str:
    """Ownership + account authorization for the settlement surfaces.

    Same discipline as every money-adjacent surface in this router: cross-owner
    is 404 (no existence leak); a canonical account outside the operator's
    authorized scopes is 403. The account comes from the canonical strategy,
    never from the caller.
    """
    _owned_strategy(repo, owner, strategy_id)
    canonical = repo.get_canonical_strategy(owner, strategy_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    account_scope = str(canonical.account_scope)
    authorize_account_scope(account_scope)
    return account_scope


def _settlement_out(assessment: Dict[str, Any]) -> SettlementAssessmentResponse:
    return SettlementAssessmentResponse(
        assessment_id=str(assessment["assessment_id"]),
        strategy_id=str(assessment["strategy_id"]),
        account_id=str(assessment["account_id"]),
        execution_environment=str(assessment["execution_environment"]),
        overall=str(assessment["overall"]),
        barrier_version=int(assessment["barrier_version"]),
        axes={
            name: SettlementAxisResponse(**axis) for name, axis in (assessment.get("axes") or {}).items()
        },
        evidence_digest=str(assessment["evidence_digest"]),
        created_at=_iso(assessment.get("created_at")),
        stale=bool(assessment.get("stale")),
    )


@router.get("/{strategy_id}/settlement", response_model=SettlementAssessmentResponse)
async def get_strategy_settlement(
    strategy_id: str,
    environment: Optional[str] = Query(default=None),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """The latest settlement assessment snapshot for one book (D-5).

    Read-only: the axes and the rollup travel with their evidence digests, and
    ``stale`` is derived on read against the barrier's CURRENT version — a
    ``settled`` snapshot after new work reads as stale, never as fresh.
    """
    _owned_strategy(repo, owner, strategy_id)
    account_scope = _authorized_strategy_account(repo, owner, strategy_id)
    env = _environment_param(environment)
    latest = _settlement_service(session_factory).latest_assessment(
        account_id=account_scope, strategy_id=strategy_id, execution_environment=env
    )
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail="No settlement assessment for this strategy and environment",
        )
    return _settlement_out(latest)


@router.post("/{strategy_id}/settlement/assess", response_model=SettlementAssessmentResponse)
async def assess_strategy_settlement(
    strategy_id: str,
    request: Request,
    payload: SettlementAssessRequest = Body(default=SettlementAssessRequest()),
    owner: str = Depends(require_strategy_owner),
    repo: SqlAlchemyStrategyRepository = Depends(_repository),
    session_factory: Any = Depends(_strategies_db),
):
    """Trigger one four-axis assessment and return the appended snapshot.

    Owner action: the assessment appends evidence, it never mutates the
    barrier, the books or any blocked state — releasing attribution, claims or
    reconciliation blocks stays the consumers' decision, and ``unknown`` never
    releases.
    """
    enforce_same_origin(request)
    account_scope = _authorized_strategy_account(repo, owner, strategy_id)
    env = _environment_param(payload.environment)
    assessment = _settlement_service(session_factory).assess(
        account_id=account_scope, strategy_id=strategy_id, execution_environment=env
    )
    return _settlement_out(assessment)
