"""Worker API for screeners (Phase 3 F9).

Mounted under ``/api/worker/screeners`` (inside the ``/api/worker/``
bearer-token boundary, like every worker router). Auth reuses the existing
worker-token scopes — ``workflows:read`` for inspection/preview,
``workflows:write`` for manual runs; no new permission is introduced.

Preview is a PURE dry-run over stored data: no run rows, no attachment
state, no subscriptions, no outbox rows, no provider calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.routers.worker_shared import require_worker_token
from backend.api.routers.worker_workflows import _parse_issues, _require_action
from backend.api.schemas.screeners import (
    RunDetailResponse,
    RunListResponse,
    RunMemberOut,
    RunOut,
    RunTriggerResponse,
    ScreenerEventOut,
    ScreenerEventsResponse,
    ScreenerPreviewRequest,
    ScreenerPreviewResponse,
)
from backend.workflows.screener_repository import (
    ScreenerRunRepository,
    list_workflow_events,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worker/screeners", tags=["Worker Screeners"])

__all__ = ["router"]

_MAX_MEMBER_PAGE = 200


def _owner_id_for_token(token: Any) -> str:
    scope = str(getattr(token, "account_scope", "") or "").strip()
    return scope or f"worker:{getattr(token, 'token_id', '')}"


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _run_out(run) -> RunOut:
    return RunOut(
        run_id=str(run.id),
        workflow_id=str(run.workflow_id),
        workflow_revision_id=str(run.workflow_revision_id),
        occurrence_key=str(run.occurrence_key),
        scheduled_for=_iso(run.scheduled_for),
        triggered_by=str(run.triggered_by or "schedule"),
        status=str(run.status),
        universe_revision=run.universe_revision,
        as_of=_iso(run.as_of),
        coverage=dict(run.coverage or {}),
        data_freshness=dict(run.data_freshness or {}),
        failure_reason=run.failure_reason,
        created_at=_iso(run.created_at),
        completed_at=_iso(run.completed_at),
    )


async def _authorize(request: Request, action: str) -> Tuple[Any, str]:
    token = await require_worker_token(request)
    _require_action(token, action)
    return token, _owner_id_for_token(token)


def _run_repo(request: Request) -> ScreenerRunRepository:
    factory = getattr(request.app.state, "alerts_session_factory", None)
    if factory is None:
        from backend.app.database import SessionLocal

        factory = SessionLocal
    return ScreenerRunRepository(factory)


def _session_factory(request: Request) -> Callable[[], Any]:
    factory = getattr(request.app.state, "alerts_session_factory", None)
    if factory is None:
        from backend.app.database import SessionLocal

        factory = SessionLocal
    return factory


def _owned_workflow(request: Request, owner_id: str, workflow_id: str):
    """The owner's screener workflow, or 404 (never leak other owners')."""
    from sqlalchemy import select

    from backend.workflows.repository import Workflow

    factory = _session_factory(request)
    session = factory()
    try:
        workflow = session.execute(
            select(Workflow).where(Workflow.id == str(workflow_id))
        ).scalar_one_or_none()
        if workflow is None or workflow.owner_id != owner_id:
            raise HTTPException(status_code=404, detail="screener workflow not found")
        return workflow
    finally:
        session.close()


def _owned_screener_revision(request: Request, owner_id: str, workflow_id: str):
    """The ACTIVE revision of the owner's screener workflow (404 when the
    workflow does not exist for this owner, 409 when it is not a screener or
    has no active revision)."""
    from sqlalchemy import select

    from backend.workflows.repository import WorkflowRevision

    workflow = _owned_workflow(request, owner_id, workflow_id)
    factory = _session_factory(request)
    session = factory()
    try:
        revision = session.execute(
            select(WorkflowRevision)
            .where(
                WorkflowRevision.workflow_id == workflow.id,
                WorkflowRevision.status == "active",
            )
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()
    if revision is None:
        raise HTTPException(status_code=409, detail="workflow has no active revision")
    document = revision.document or {}
    if not isinstance(document, dict) or not document.get("screener"):
        raise HTTPException(status_code=409, detail="workflow is not a screener")
    return workflow, revision


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/{workflow_id}/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    workflow_id: str,
    limit: int = 20,
    offset: int = 0,
):
    token, owner_id = await _authorize(request, "workflows:read")
    _ = token
    _owned_screener_revision(request, owner_id, workflow_id)
    repo = _run_repo(request)
    runs = repo.list_runs(owner_id, str(workflow_id), limit=limit, offset=offset)
    return RunListResponse(
        runs=[_run_out(run) for run in runs],
        total_count=len(runs),
        offset=max(0, int(offset)),
        limit=max(1, min(int(limit), 200)),
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_run(
    request: Request,
    run_id: str,
    limit: int = 100,
    offset: int = 0,
):
    token, owner_id = await _authorize(request, "workflows:read")
    _ = token
    repo = _run_repo(request)
    run = repo.get_run(str(run_id))
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="screener run not found")
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), _MAX_MEMBER_PAGE))
    members = repo.run_members(run.id)
    page = members[offset : offset + limit]
    return RunDetailResponse(
        run=_run_out(run),
        members=[
            RunMemberOut(
                instrument_key=member.instrument_key,
                passed=bool(member.passed),
                exclusion_reason=member.exclusion_reason,
                rank=member.rank,
                score=member.score,
                values=dict(member.values or {}),
            )
            for member in page
        ],
        member_count=len(members),
        offset=offset,
        limit=limit,
    )


@router.post("/{workflow_id}/runs", response_model=RunTriggerResponse)
async def trigger_run(
    request: Request,
    workflow_id: str,
    idempotency_key: Optional[str] = None,
):
    """Manual run. With the same ``idempotency_key`` the original run is
    returned (no duplicate execution); without one each call is a new run."""
    token, owner_id = await _authorize(request, "workflows:write")
    _ = token
    workflow, revision = _owned_screener_revision(request, owner_id, workflow_id)
    scheduler = _scheduler(request)
    # Off the event loop: a manual run may warm bounded candle history first.
    run = await asyncio.to_thread(
        scheduler.execute_manual, workflow, revision, idempotency_key=idempotency_key
    )
    if run is None:
        existing = scheduler.run_repo.get_run_by_occurrence(
            owner_id, f"{workflow.id}:manual:{idempotency_key}"
        )
        return RunTriggerResponse(
            ok=True,
            run_id=str(existing.id) if existing is not None else None,
            status="already_finalized",
            detail="the run for this idempotency key already exists",
        )
    return RunTriggerResponse(run_id=str(run.id), status=str(run.status))


@router.get("/{workflow_id}/events", response_model=ScreenerEventsResponse)
async def list_events(
    request: Request,
    workflow_id: str,
    limit: int = 50,
    offset: int = 0,
):
    token, owner_id = await _authorize(request, "workflows:read")
    _ = token
    _owned_screener_revision(request, owner_id, workflow_id)
    events = list_workflow_events(
        _session_factory(request),
        str(workflow_id),
        limit=limit,
        offset=offset,
    )
    return ScreenerEventsResponse(
        events=[
            ScreenerEventOut(
                event_id=str(event.id),
                fired_at=_iso(event.fired_at),
                evidence=dict(event.evidence or {}),
            )
            for event in events
        ],
        offset=max(0, int(offset)),
        limit=max(1, min(int(limit), 200)),
    )


@router.post("/preview", response_model=ScreenerPreviewResponse)
async def preview_screener(request: Request, payload: ScreenerPreviewRequest):
    """Pure screener dry-run over stored data (bounded member count).

    Reuses the production evaluation pipeline but persists nothing: no run
    rows, no attachment state, no subscriptions, no outbox entries, no
    provider calls, no scheduled work.
    """
    token, owner_id = await _authorize(request, "workflows:read")
    _ = token
    return preview_screener_for(request, owner_id, payload)


def preview_screener_for(request: Request, owner_id: str, payload: ScreenerPreviewRequest):
    """The dry-run itself, with the owner supplied by the caller.

    Split from the route so the app-cookie operator surface can run the SAME
    preview for its authorized scope instead of a second implementation that
    could disagree with this one. The worker route passes the owner derived
    from its token; the operator route passes the owner the server authorized.
    Taking the owner as an argument (rather than reading it from the request)
    is what keeps the two from being able to differ.
    """
    if (payload.yaml_text is None) == (payload.document is None):
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of yaml_text or document",
        )
    from backend.workflows.compiler import (
        WorkflowValidationError,
        compile_document,
    )
    from backend.workflows.parser import (
        WorkflowParseError,
        parse_workflow_dict,
        parse_workflow_yaml,
    )

    def _issues(exc) -> List[dict]:
        items = getattr(exc, "issues", None)
        if items is None:
            return [{"where": "document", "code": "parse_error", "message": str(exc)}]
        return [
            {"where": i.where, "code": i.code, "message": i.message} for i in items
        ]

    try:
        if payload.yaml_text is not None:
            document = parse_workflow_yaml(payload.yaml_text)
        else:
            document = parse_workflow_dict(payload.document)
        compiled = compile_document(document)
    except (WorkflowParseError, WorkflowValidationError) as exc:
        return ScreenerPreviewResponse(
            ok=False, evaluation="dry_run_invalid", coverage={"issues": _issues(exc)}
        )
    if compiled.document.screener is None:
        raise HTTPException(status_code=422, detail="document is not a screener")

    from backend.screeners.runner import ScreenerPipeline

    pipeline = ScreenerPipeline(
        candle_history=_candle_history(request),
        window_bars=120,
    )
    as_of = (
        datetime.fromisoformat(payload.as_of)
        if payload.as_of
        else datetime.now(timezone.utc)
    )
    context_loader = None
    loader = _fundamentals_loader(request)
    if loader is not None:
        context_loader = loader.context_for
    universe_service = _universe_service(request)
    members: List[str] = [str(inst.key()) for inst in compiled.document.instruments]
    universe_revision = None
    universe_expr = compiled.document.universe
    if universe_expr is not None and universe_service is not None:
        members, universe_revision = _resolve_universe_for_preview(
            universe_service, owner_id, universe_expr
        )
    outcome = pipeline.evaluate(
        compiled.document,
        members,
        as_of=as_of,
        context_loader=context_loader,
        member_limit=max(1, min(int(payload.member_limit or 50), 200)),
    )
    return ScreenerPreviewResponse(
        status=outcome["status"],
        coverage={**outcome["coverage"], "universe_revision": universe_revision},
        data_freshness=outcome["data_freshness"],
        members=[
            RunMemberOut(
                instrument_key=member.instrument_key,
                passed=member.passed,
                exclusion_reason=member.exclusion_reason,
                rank=member.rank,
                score=member.score,
                values=member.values,
            )
            for member in outcome["members"]
        ],
    )


# ---------------------------------------------------------------------------
# lazy collaborators (API process)
# ---------------------------------------------------------------------------


def _universe_service(request: Request):
    service = getattr(request.app.state, "universe_service", None)
    if service is not None:
        return service
    try:
        from backend.workflows.universes import UniverseService

        return UniverseService(_session_factory(request))
    except Exception:
        return None


def _fundamentals_loader(request: Request):
    try:
        from backend.workflows.fundamentals_context import FundamentalsLoader

        return FundamentalsLoader(_session_factory(request))
    except Exception:
        return None


class _CatalogTokenMap:
    """Lazy EXCHANGE:SYMBOL -> broker token map for the API process.

    Resolves through the published catalog on first use and caches. Purpose
    a plain mapping (no ``snapshot``) so PgCandleHistory treats it as a
    static token map.
    """

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._sessions = session_factory
        self._cache: dict = {}

    def get(self, key: str) -> Optional[int]:
        if key in self._cache:
            return self._cache[key]
        try:
            from backend.workflows.worker_entry import resolve_catalog_instrument_tokens

            resolved, _rejected = resolve_catalog_instrument_tokens(
                {key}, self._sessions, fallback_tokens={}
            )
            token = resolved.get(key)
        except Exception:
            logger.warning("preview token resolution failed for %s", key, exc_info=True)
            token = None
        self._cache[key] = token
        return token


def _candle_history(request: Request):
    from backend.workflows.runtime import PgCandleHistory

    return PgCandleHistory(_engine(request), _CatalogTokenMap(_session_factory(request)))


def _engine(request: Request):
    engine = getattr(request.app.state, "alerts_engine", None)
    if engine is not None:
        return engine
    from backend.app.database import engine as global_engine

    return global_engine


def _scheduler(request: Request):
    from backend.screeners.candle_warming import build_screener_warmer
    from backend.screeners.runner import ScreenerPipeline
    from backend.screeners.scheduler import ScreenerScheduler

    existing = getattr(request.app.state, "screener_scheduler", None)
    if existing is not None:
        return existing
    factory = _session_factory(request)
    history = _candle_history(request)
    window_bars = int(os.environ.get("ALERTS_SCREENER_WINDOW_BARS", "30"))
    return ScreenerScheduler(
        session_factory=factory,
        workflow_repo=None,
        run_repo=ScreenerRunRepository(factory),
        pipeline=ScreenerPipeline(
            candle_history=history,
            window_bars=window_bars,
            warmer=build_screener_warmer(history, required_bars=window_bars),
        ),
        universe_service=_universe_service(request),
        fundamentals_loader=_fundamentals_loader(request),
        owner_id="api-manual",
    )


def _resolve_universe_for_preview(universe_service, owner_id: str, universe_expr):
    """Best-effort universe resolution for preview (union/intersect/exclude;
    index + saved universes). Resolution failures shrink to what resolved —
    the run coverage discloses incompleteness."""
    members: set = set()
    revision = None
    for ref in universe_expr.refs:
        resolved = _resolve_ref_for_preview(universe_service, owner_id, ref)
        if resolved is None:
            continue
        rev, keys = resolved
        revision = max(revision or 0, rev or 0) or revision
        members |= keys
    for ref in universe_expr.intersect:
        resolved = _resolve_ref_for_preview(universe_service, owner_id, ref)
        if resolved is None:
            continue
        rev, keys = resolved
        revision = max(revision or 0, rev or 0) or revision
        members &= keys
    for ref in universe_expr.exclude:
        resolved = _resolve_ref_for_preview(universe_service, owner_id, ref)
        if resolved is None:
            continue
        _rev, keys = resolved
        members -= keys
    return sorted(members), revision


def _resolve_ref_for_preview(universe_service, owner_id: str, ref):
    try:
        if ref.kind in ("universe", "watchlist"):
            latest = universe_service.latest_revision(owner_id, ref.name)
            if latest is None:
                return None
            return int(latest.get("revision") or 0) or None, set(latest.get("members") or ())
        preview = universe_service.preview_membership(owner_id, "index", {"source_list": ref.name})
        return None, set(preview.get("members") or ())
    except Exception:
        return None
