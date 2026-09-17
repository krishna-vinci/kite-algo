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

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from backend.api.schemas.strategies import (
    AdjustmentCreateRequest,
    AdjustmentLineResponse,
    AdjustmentResponse,
    ExternalAdapterRequest,
    ExternalAdapterResponse,
    ExternalStrategyCreateRequest,
    GrantRequest,
    GrantResponse,
    HostedStrategyOptionsResponse,
    JobDetailResponse,
    JobListResponse,
    JobSummaryResponse,
    PositionListResponse,
    PositionRow,
    ProductStatusUpdateRequest,
    RebuildResponse,
    ReconciliationActionRequest,
    ReconciliationActionResponse,
    ReconciliationAuditResponse,
    ReconciliationAssessmentResponse,
    ReconciliationInspectionResponse,
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
    StrategyCreateRequest,
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
from backend.app.auth import AppUser, require_app_user
from backend.strategies import service
from backend.strategies.attribution import (
    EXECUTION_ENVIRONMENTS,
    SqlAttributionStore,
    StrategyAttributionService,
)
from backend.strategies.reconciliation import assess, evidence_digest
from backend.strategies.repository import (
    SqlAlchemyStrategyRepository,
    StrategyConflict,
    StrategyDisabled,
    StrategyFenceError,
    StrategyIdempotencyConflict,
    StrategyIdentityError,
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
    """Reconciliation evidence collector (injectable; read-only services)."""
    collector = getattr(request.app.state, "reconciliation_collector", None)
    if collector is not None:
        return collector
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector

    worker = getattr(request.app.state, "algo_worker_repository", None) or SqlAlchemyAlgoWorkerRepository()
    paper = getattr(request.app.state, "paper_runtime_service", None)
    option_status = getattr(request.app.state, "option_run_status_reader", None)
    return ReconciliationEvidenceCollector(
        worker_repo=worker, paper_runtime=paper, option_status_reader=option_status
    )


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
    """
    return HostedStrategyOptionsResponse(
        account_scopes=authorized_account_scopes(),
        execution_modes=list(service.ALLOWED_EXECUTION_MODES),
        job_kinds=list(service.ALLOWED_JOB_KINDS),
        stale_exit_policies=list(service.ALLOWED_STALE_EXIT_POLICIES),
    )


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

    collector = _collector(request)
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
