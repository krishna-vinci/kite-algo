from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from backend.app.auth import require_app_user
from backend.journaling.models import (
    JournalV2DailyResponse,
    JournalV2EpisodeDetailResponse,
    JournalV2PeriodResponse,
    JournalV2StrategyListResponse,
    MetricPeriod,
)
from backend.journaling.service import JournalService


router = APIRouter(tags=["Trading Journal"])


def get_journal_service(request: Request) -> JournalService:
    service = getattr(request.app.state, "journal_service", None)
    if service is None:
        service = JournalService()
        request.app.state.journal_service = service
    return service


def _resolve_v2_environment_or_raise(
    service: JournalService,
    *,
    environment_id: str | None,
    mode: str | None,
    account_scope: str | None,
    create_if_missing: bool = True,
) -> str:
    try:
        return service.resolve_v2_environment_id(
            environment_id=environment_id,
            mode=mode,
            account_scope=account_scope,
            create_if_missing=create_if_missing,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class JournalV2EpisodeNotesPatchRequest(BaseModel):
    environment_id: Optional[str] = None
    notes: str = ""


def _dump_json_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


@router.get("/journal/v2/environments")
def list_v2_environments(
    request: Request,
    mode: Optional[str] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    return {"items": service.list_v2_environments(mode=mode)}


@router.get("/journal/v2/daily", response_model=JournalV2DailyResponse)
def get_v2_daily(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
    date: Optional[date] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
        create_if_missing=False,
    )
    try:
        return _dump_json_model(service.get_v2_daily(environment_id=resolved_environment_id, trading_date=date))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/period", response_model=JournalV2PeriodResponse)
def get_v2_period(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
    from_param: Optional[date] = Query(None, alias="from"),
    to_param: Optional[date] = Query(None, alias="to"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    granularity: str = Query("day"),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_from_date = from_param or from_date
    resolved_to_date = to_param or to_date
    if resolved_from_date is None or resolved_to_date is None:
        raise HTTPException(status_code=422, detail="from/to date range is required")
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
        create_if_missing=False,
    )
    try:
        return _dump_json_model(
            service.get_v2_period(
                environment_id=resolved_environment_id,
                from_date=resolved_from_date,
                to_date=resolved_to_date,
                granularity=granularity,
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/episodes")
def list_v2_episodes(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
    execution_context_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    strategy: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
        create_if_missing=False,
    )
    try:
        items = service.list_v2_episodes(
            environment_id=resolved_environment_id,
            execution_context_id=execution_context_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            strategy=strategy,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": items,
        "count": len(items),
    }


@router.get("/journal/v2/episodes/{episode_id}", response_model=JournalV2EpisodeDetailResponse)
def get_v2_episode_detail(episode_id: str, request: Request, environment_id: str = Query(...)):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        episode = service.get_v2_episode_detail(episode_id, environment_id=environment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Unknown episode_id: {episode_id}")
    return _dump_json_model(episode)


@router.patch("/journal/v2/episodes/{episode_id}", response_model=JournalV2EpisodeDetailResponse)
def patch_v2_episode(
    episode_id: str,
    payload: JournalV2EpisodeNotesPatchRequest,
    request: Request,
    environment_id: Optional[str] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = environment_id or payload.environment_id
    if not resolved_environment_id:
        raise HTTPException(status_code=422, detail="environment_id is required")
    try:
        return _dump_json_model(service.patch_v2_episode_notes(episode_id, environment_id=resolved_environment_id, notes=payload.notes))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/strategies", response_model=JournalV2StrategyListResponse)
def list_v2_strategies(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: Optional[date] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
        create_if_missing=False,
    )
    return service.list_v2_strategies(environment_id=resolved_environment_id, period=period, anchor_date=anchor_date)
