"""Worker API for universe membership (alerts platform).

Endpoints under ``/universes`` (this router is mounted at ``/api/worker``
like every other worker router, so the full path is ``/api/worker/universes``).
Auth uses the shared ``require_worker_token`` dependency plus per-action
checks (``workflows:read`` / ``workflows:write``), replicated from
``backend.api.routers.worker_workflows``: the bearer token is resolved via
``require_worker_token`` (401 on missing/invalid/expired) and the action is
enforced with ``_require_action`` (403 when the token's allowed_actions do
not include it). The owner is derived from the token's account scope exactly
the same way, so one worker token can never read or resolve another owner's
universes (portfolio membership especially).

The sessionmaker is injected: ``app.state.alerts_session_factory`` wins, else
the global ``SessionLocal`` (same mechanism as ``worker_workflows``). Tests
swap it via ``app.dependency_overrides``. A fully wired
:class:`~backend.workflows.universes.UniverseService` may be provided via
``app.state.universe_service``; otherwise one is built from the injected
session factory (with the optional ``app.state.portfolio_membership_provider``
callable wired through so the API layer can supply the real, owner-scoped
broker holdings fetch without importing ``backend.broker_api`` here).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.routers.worker_shared import _require_action, require_worker_token
from backend.api.schemas.universes import (
    PreviewResponse,
    ResolveResponse,
    UniverseCreateRequest,
    UniverseDetailResponse,
    UniverseListResponse,
    UniverseMutationResponse,
    UniversePreviewRequest,
    UniverseRevisionItem,
    UniverseRevisionSummary,
    UniverseRevisionsResponse,
    UniverseSummary,
)
from backend.workflows.universes import (
    UniverseError,
    UniverseExistsError,
    UniverseNotFoundError,
    UniverseService,
    UniverseSourceUnavailable,
    UniverseValidationError,
)

router = APIRouter(prefix="/worker/universes", tags=["Worker Universes"])

__all__ = ["router", "_universes_db", "_universe_service"]


# ---------------------------------------------------------------------------
# injectable dependencies (same mechanism as worker_workflows)
# ---------------------------------------------------------------------------


def _universes_db(request: Request):
    """Sessionmaker for the universe tables (injectable for tests)."""
    factory = getattr(request.app.state, "alerts_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _universe_service(
    request: Request,
    session_factory: Any = Depends(_universes_db),
) -> UniverseService:
    service = getattr(request.app.state, "universe_service", None)
    if service is not None:
        return service
    return UniverseService(
        session_factory,
        portfolio_provider=getattr(request.app.state, "portfolio_membership_provider", None),
    )


# ---------------------------------------------------------------------------
# helpers (replicated from worker_workflows)
# ---------------------------------------------------------------------------


def _owner_id_for_token(token: Any) -> str:
    scope = str(getattr(token, "account_scope", "") or "").strip()
    return scope or f"worker:{getattr(token, 'token_id', '')}"


async def _authorize(request: Request, action: str) -> Tuple[Any, str]:
    token = await require_worker_token(request)
    _require_action(token, action)
    return token, _owner_id_for_token(token)


def _is_catalog_unavailable(exc: Exception) -> bool:
    # Lazy import: backend.broker_api is never pulled in at module import
    # time (circular-import safety, mirroring backend.workflows.universes).
    try:
        from backend.broker_api.instruments.catalog import CatalogUnavailableError
    except Exception:
        return False
    return isinstance(exc, CatalogUnavailableError)


def _call(service_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a service call, mapping typed errors onto HTTP statuses.

    A catalog-wide failure (``CatalogUnavailableError``) becomes 503 and must
    never leave a persisted empty revision behind (the service guarantees
    that); anything unexpected propagates.
    """
    try:
        return service_fn(*args, **kwargs)
    except UniverseValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UniverseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UniverseExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UniverseSourceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UniverseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        if _is_catalog_unavailable(exc):
            raise HTTPException(
                status_code=503, detail=f"instrument catalog unavailable: {exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


async def create_universe(
    request: Request,
    payload: UniverseCreateRequest,
    service: UniverseService = Depends(_universe_service),
):
    _, owner_id = await _authorize(request, "workflows:write")
    created = _call(
        service.create_universe,
        owner_id,
        payload.name,
        payload.kind,
        dict(payload.source_config or {}),
    )
    return UniverseMutationResponse(
        universe_id=str(created["universe_id"]),
        name=str(created["name"]),
        kind=str(created["kind"]),
        source_config=dict(created.get("source_config") or {}),
        enabled=bool(created.get("enabled", True)),
        created_at=created.get("created_at"),
        updated_at=created.get("updated_at"),
    )


async def list_universes(
    request: Request,
    service: UniverseService = Depends(_universe_service),
):
    _, owner_id = await _authorize(request, "workflows:read")
    universes = _call(service.list_universes, owner_id)
    items = []
    for universe in universes:
        latest = _call(service.latest_revision, owner_id, str(universe["name"]))
        items.append(
            UniverseSummary(
                universe_id=str(universe["universe_id"]),
                name=str(universe["name"]),
                kind=str(universe["kind"]),
                source_config=dict(universe.get("source_config") or {}),
                enabled=bool(universe.get("enabled", True)),
                created_at=universe.get("created_at"),
                updated_at=universe.get("updated_at"),
                latest_revision=_revision_summary(latest),
            )
        )
    return UniverseListResponse(universes=items)


async def get_universe(
    request: Request,
    name: str,
    service: UniverseService = Depends(_universe_service),
):
    _, owner_id = await _authorize(request, "workflows:read")
    universe = _call(service.get_universe, owner_id, name)
    latest = _call(service.latest_revision, owner_id, name)
    return UniverseDetailResponse(
        universe_id=str(universe["universe_id"]),
        name=str(universe["name"]),
        kind=str(universe["kind"]),
        source_config=dict(universe.get("source_config") or {}),
        enabled=bool(universe.get("enabled", True)),
        created_at=universe.get("created_at"),
        updated_at=universe.get("updated_at"),
        latest_revision=_revision_summary(latest),
        latest_members=list(latest["members"]) if latest else None,
        latest_coverage=dict(latest["coverage"]) if latest else None,
    )


async def resolve_universe(
    request: Request,
    name: str,
    service: UniverseService = Depends(_universe_service),
):
    _, owner_id = await _authorize(request, "workflows:write")
    resolved = _call(service.resolve_membership, owner_id, name)
    return ResolveResponse(
        universe_id=str(resolved["universe_id"]),
        name=str(resolved["name"]),
        kind=str(resolved["kind"]),
        revision=int(resolved["revision"]),
        members=list(resolved["members"]),
        rejected=list(resolved["rejected"]),
        source_generation=resolved.get("source_generation"),
        coverage=dict(resolved["coverage"]),
    )


async def preview_universe(
    request: Request,
    payload: UniversePreviewRequest,
    service: UniverseService = Depends(_universe_service),
):
    """Resolve would-be membership WITHOUT persisting anything."""
    _, owner_id = await _authorize(request, "workflows:read")
    preview = _call(
        service.preview_membership,
        owner_id,
        payload.kind,
        dict(payload.source_config or {}),
    )
    return PreviewResponse(
        kind=str(preview["kind"]),
        members=list(preview["members"]),
        rejected=list(preview["rejected"]),
        source_generation=preview.get("source_generation"),
        coverage=dict(preview["coverage"]),
    )


async def list_universe_revisions(
    request: Request,
    name: str,
    limit: int = Query(50, ge=1, le=500),
    service: UniverseService = Depends(_universe_service),
):
    _, owner_id = await _authorize(request, "workflows:read")
    universe = _call(service.get_universe, owner_id, name)
    revisions = _call(service.list_revisions, owner_id, name, limit=limit)
    return UniverseRevisionsResponse(
        universe_id=str(universe["universe_id"]),
        name=str(universe["name"]),
        limit=limit,
        revisions=[UniverseRevisionItem(**revision) for revision in revisions],
    )


def _revision_summary(revision: Optional[dict]) -> Optional[UniverseRevisionSummary]:
    if revision is None:
        return None
    return UniverseRevisionSummary(
        revision=int(revision["revision"]),
        member_count=int(revision.get("member_count") or 0),
        source_generation=revision.get("source_generation"),
        resolved_at=revision.get("resolved_at"),
        created_at=revision.get("created_at"),
    )


router.add_api_route("", create_universe, methods=["POST"], response_model=UniverseMutationResponse, status_code=201)
router.add_api_route("", list_universes, methods=["GET"], response_model=UniverseListResponse)
router.add_api_route("/preview", preview_universe, methods=["POST"], response_model=PreviewResponse)
router.add_api_route("/{name}", get_universe, methods=["GET"], response_model=UniverseDetailResponse)
router.add_api_route("/{name}/resolve", resolve_universe, methods=["POST"], response_model=ResolveResponse)
router.add_api_route("/{name}/revisions", list_universe_revisions, methods=["GET"], response_model=UniverseRevisionsResponse)
