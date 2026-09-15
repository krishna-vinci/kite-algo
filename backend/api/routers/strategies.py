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
    StrategyCreateRequest,
    StrategyListResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    VersionCreateRequest,
    VersionListResponse,
    VersionResponse,
)
from backend.api.services.csrf import enforce_same_origin
from backend.api.services.hosted_strategy_authz import authorize_account_scope
from backend.app.auth import AppUser, require_app_user
from backend.strategies import service
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
