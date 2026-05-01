from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth_service import require_app_user
from journaling.models import (
    CapitalBasisType,
    DecisionActorType,
    DecisionType,
    ExecutionMode,
    JournalDecisionEvent,
    JournalRule,
    JournalRun,
    JournalRunStatus,
    JournalSourceLink,
    EnforcementLevel,
    RuleStatus,
    RuleType,
    ReviewState,
    SourceType,
    StrategyFamily,
)
from journaling.service import JournalService


router = APIRouter(tags=["Trading Journal"])


def get_journal_service(request: Request) -> JournalService:
    service = getattr(request.app.state, "journal_service", None)
    if service is None:
        service = JournalService()
        request.app.state.journal_service = service
    return service


def validate_run_id(run_id: str) -> str:
    try:
        return str(UUID(str(run_id)))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid run_id") from exc


def _resolve_v2_environment_or_raise(
    service: JournalService,
    *,
    environment_id: str | None,
    mode: str | None,
    account_scope: str | None,
) -> str:
    try:
        return service.resolve_v2_environment_id(
            environment_id=environment_id,
            mode=mode,
            account_scope=account_scope,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class JournalRunCreateRequest(BaseModel):
    strategy_family: StrategyFamily
    strategy_name: Optional[str] = None
    entry_surface: Optional[str] = None
    execution_mode: ExecutionMode
    account_ref: Optional[str] = None
    status: JournalRunStatus = JournalRunStatus.DRAFT
    benchmark_id: str = "NIFTY50"
    capital_basis_type: CapitalBasisType
    capital_committed: Optional[float] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    review_state: ReviewState = ReviewState.PENDING
    source_summary: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JournalRunUpdateRequest(BaseModel):
    status: Optional[JournalRunStatus] = None
    review_state: Optional[ReviewState] = None
    ended_at: Optional[datetime] = None
    source_summary: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class JournalDecisionEventRequest(BaseModel):
    decision_type: DecisionType
    actor_type: DecisionActorType
    occurred_at: Optional[datetime] = None
    summary: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)


class JournalSourceLinkRequest(BaseModel):
    source_type: SourceType
    source_key: str = Field(min_length=1)
    source_key_2: Optional[str] = None
    linked_at: Optional[datetime] = None


class JournalRuleCreateRequest(BaseModel):
    family_scope: Optional[str] = None
    strategy_scope: Optional[str] = None
    title: str = Field(min_length=1)
    rule_type: RuleType
    enforcement_level: EnforcementLevel
    status: RuleStatus = RuleStatus.DRAFT
    version: int = Field(default=1, ge=1)
    description: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JournalRuleUpdateRequest(BaseModel):
    family_scope: Optional[str] = None
    strategy_scope: Optional[str] = None
    title: Optional[str] = None
    rule_type: Optional[RuleType] = None
    enforcement_level: Optional[EnforcementLevel] = None
    status: Optional[RuleStatus] = None
    version: Optional[int] = Field(default=None, ge=1)
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class JournalReviewUpdateRequest(BaseModel):
    review_status: Literal["pending", "in_progress", "completed", "reviewed", "skipped", "waived"]
    notes: Optional[str] = None


class JournalV2NoteCreateRequest(BaseModel):
    environment_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    note_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body_markdown: str = Field(min_length=1)
    episode_id: Optional[str] = None
    body_json: Dict[str, Any] = Field(default_factory=dict)
    effective_at: Optional[datetime] = None
    author_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JournalV2NoteUpdateRequest(BaseModel):
    environment_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    title: Optional[str] = None
    body_markdown: Optional[str] = None
    body_json: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    editor_id: Optional[str] = None
    change_reason: Optional[str] = None


class JournalV2AttachmentRequest(BaseModel):
    environment_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    storage_key: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    note_id: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    ocr_text: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.post("/journal/runs")
def create_run(payload: JournalRunCreateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    run = JournalRun(
        strategy_family=payload.strategy_family,
        strategy_name=payload.strategy_name,
        entry_surface=payload.entry_surface,
        execution_mode=payload.execution_mode,
        account_ref=payload.account_ref,
        status=payload.status,
        benchmark_id=payload.benchmark_id,
        capital_basis_type=payload.capital_basis_type,
        capital_committed=payload.capital_committed,
        started_at=payload.started_at or datetime.utcnow(),
        ended_at=payload.ended_at,
        review_state=payload.review_state,
        source_summary=payload.source_summary,
        metadata=payload.metadata,
    )
    return service.create_run(run)


@router.patch("/journal/runs/{run_id}")
def update_run(run_id: str, payload: JournalRunUpdateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.update_run(
            validated_run_id,
            status=payload.status,
            review_state=payload.review_state,
            ended_at=payload.ended_at,
            source_summary=payload.source_summary,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/journal/runs/{run_id}/decision-events")
def append_decision_event(run_id: str, payload: JournalDecisionEventRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.append_decision_event(
            validated_run_id,
            JournalDecisionEvent(
                run_id=validated_run_id,
                decision_type=payload.decision_type,
                actor_type=payload.actor_type,
                occurred_at=payload.occurred_at or datetime.utcnow(),
                summary=payload.summary,
                context=payload.context,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/journal/runs/{run_id}/sources")
def link_source(run_id: str, payload: JournalSourceLinkRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.link_source(
            validated_run_id,
            JournalSourceLink(
                run_id=validated_run_id,
                source_type=payload.source_type,
                source_key=payload.source_key,
                source_key_2=payload.source_key_2,
                linked_at=payload.linked_at or datetime.utcnow(),
            ),
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/journal/runs/{run_id}")
def get_run_detail(run_id: str, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.get_run_detail(validated_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/journal/runs")
def list_runs(
    request: Request,
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
    status: Optional[JournalRunStatus] = Query(None),
    review_state: Optional[ReviewState] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return service.list_runs_page(
        strategy_family=strategy_family,
        execution_mode=execution_mode,
        status=status,
        review_state=review_state,
        page=page,
        page_size=page_size,
    )


@router.get("/journal/runs/{run_id}/summary")
def get_run_summary(run_id: str, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.get_run_summary(validated_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/journal/runs/{run_id}/review")
def update_run_review(run_id: str, payload: JournalReviewUpdateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id)
    try:
        return service.update_run_review(validated_run_id, review_status=payload.review_status, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/journal/summary")
def get_summary(
    request: Request,
    period: str = Query("month"),
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.get_summary(period=period, strategy_family=strategy_family, execution_mode=execution_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/benchmark")
def get_benchmark(
    request: Request,
    period: str = Query("month"),
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
    benchmark_id: str = Query("NIFTY50"),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.get_benchmark_comparison(
            period=period,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            benchmark_id=benchmark_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/aggregate-summaries")
def get_aggregate_summaries(
    request: Request,
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    return service.get_aggregate_summaries(
        strategy_family=strategy_family,
        execution_mode=execution_mode,
    )


@router.get("/journal/calendar")
def get_calendar_summary(
    request: Request,
    start_day: Optional[date] = Query(None),
    end_day: Optional[date] = Query(None),
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
    limit: int = Query(366, ge=1, le=1000),
):
    require_app_user(request)
    service = get_journal_service(request)
    return service.get_calendar_summary(
        start_day=start_day,
        end_day=end_day,
        strategy_family=strategy_family,
        execution_mode=execution_mode,
        limit=limit,
    )


@router.get("/journal/trades")
def list_trades(
    request: Request,
    run_id: Optional[str] = Query(None),
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
    source_type: Optional[SourceType] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=1000),
):
    require_app_user(request)
    service = get_journal_service(request)
    validated_run_id = validate_run_id(run_id) if run_id is not None else None
    return service.list_trades_page(
        run_id=validated_run_id,
        strategy_family=strategy_family,
        execution_mode=execution_mode,
        source_type=source_type,
        page=page,
        page_size=page_size,
    )


@router.get("/journal/strategies")
def list_strategies(
    request: Request,
    strategy_family: Optional[StrategyFamily] = Query(None),
    execution_mode: Optional[ExecutionMode] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return {
        "items": service.list_strategies(strategy_family=strategy_family, execution_mode=execution_mode, limit=limit)
    }


@router.get("/journal/review-queue")
def get_review_queue(
    request: Request,
    review_state: Optional[ReviewState] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return service.get_review_queue(limit=limit, review_state=review_state)


@router.get("/journal/rules")
def list_rules(
    request: Request,
    family_scope: Optional[str] = Query(None),
    strategy_scope: Optional[str] = Query(None),
    status: Optional[RuleStatus] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return {
        "items": service.list_rules(
            family_scope=family_scope,
            strategy_scope=strategy_scope,
            status=status,
            limit=limit,
        )
    }


@router.post("/journal/rules")
def create_rule(payload: JournalRuleCreateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    return service.create_rule(
        JournalRule(
            family_scope=payload.family_scope,
            strategy_scope=payload.strategy_scope,
            title=payload.title,
            rule_type=payload.rule_type,
            enforcement_level=payload.enforcement_level,
            status=payload.status,
            version=payload.version,
            description=payload.description,
            metadata=payload.metadata,
        )
    )


@router.patch("/journal/rules/{rule_id}")
def update_rule(rule_id: str, payload: JournalRuleUpdateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.update_rule(rule_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/journal/insights")
def get_insights(request: Request, limit: int = Query(20, ge=1, le=100)):
    require_app_user(request)
    service = get_journal_service(request)
    return service.get_insights_feed(limit=limit)


@router.get("/journal/v2/environments")
def list_v2_environments(
    request: Request,
    mode: Optional[str] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    return {"items": service.list_v2_environments(mode=mode)}


@router.get("/journal/v2/episodes")
def list_v2_episodes(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
    execution_context_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
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
    )
    try:
        items = service.list_v2_episodes(
            environment_id=resolved_environment_id,
            execution_context_id=execution_context_id,
            status=status,
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


@router.get("/journal/v2/episodes/{episode_id}")
def get_v2_episode_detail(episode_id: str, request: Request, environment_id: str = Query(...)):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        episode = service.get_v2_episode_detail(episode_id, environment_id=environment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Unknown episode_id: {episode_id}")
    return episode


@router.get("/journal/v2/strategies")
def list_v2_strategies(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
    )
    return service.list_v2_strategies(environment_id=resolved_environment_id)


@router.get("/journal/v2/unresolved")
def list_v2_unresolved(
    request: Request,
    environment_id: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    account_scope: Optional[str] = Query(None),
):
    require_app_user(request)
    service = get_journal_service(request)
    resolved_environment_id = _resolve_v2_environment_or_raise(
        service,
        environment_id=environment_id,
        mode=mode,
        account_scope=account_scope,
    )
    return service.list_v2_unresolved(environment_id=resolved_environment_id)


@router.get("/journal/v2/analytics/summary")
def get_v2_analytics_summary(
    request: Request,
    environment_id: str = Query(...),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.compute_v2_environment_metrics(environment_id=environment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/analytics/strategies")
def get_v2_analytics_strategies(
    request: Request,
    environment_id: str = Query(...),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.compute_v2_environment_strategy_metrics(environment_id=environment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/analytics/compare-paper-live")
def get_v2_compare_paper_live(
    request: Request,
    template_id: str = Query(...),
    paper_environment_id: str = Query(...),
    live_environment_id: str = Query(...),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        return service.compare_v2_paper_live_for_template(
            template_id=template_id,
            paper_environment_id=paper_environment_id,
            live_environment_id=live_environment_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/journal/v2/episodes/{episode_id}/timeline")
def list_v2_episode_timeline(
    episode_id: str,
    request: Request,
    environment_id: str = Query(...),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        items = service.list_v2_timeline(episode_id=episode_id, environment_id=environment_id, limit=limit, offset=offset)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "count": len(items)}


@router.get("/journal/v2/notes")
def list_v2_notes(
    request: Request,
    environment_id: str = Query(...),
    subject_type: Optional[str] = Query(None),
    subject_id: Optional[str] = Query(None),
    episode_id: Optional[str] = Query(None),
    note_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        items = service.list_v2_notes(
            environment_id=environment_id,
            subject_type=subject_type,
            subject_id=subject_id,
            episode_id=episode_id,
            note_type=note_type,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "count": len(items)}


@router.post("/journal/v2/notes")
def create_v2_note(payload: JournalV2NoteCreateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        note_id = service.create_v2_note(
            environment_id=payload.environment_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            note_type=payload.note_type,
            title=payload.title,
            body_markdown=payload.body_markdown,
            episode_id=payload.episode_id,
            body_json=payload.body_json,
            effective_at=payload.effective_at,
            author_id=payload.author_id,
            tags=payload.tags,
            metadata=payload.metadata,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    note = service.get_v2_note(note_id, environment_id=payload.environment_id)
    return note or {"id": note_id}


@router.get("/journal/v2/notes/{note_id}")
def get_v2_note(note_id: str, request: Request, environment_id: str = Query(...)):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        note = service.get_v2_note(note_id, environment_id=environment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if note is None:
        raise HTTPException(status_code=404, detail=f"Unknown note_id: {note_id}")
    return note


@router.patch("/journal/v2/notes/{note_id}")
def update_v2_note(note_id: str, payload: JournalV2NoteUpdateRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        service.update_v2_note(
            note_id,
            environment_id=payload.environment_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            title=payload.title,
            body_markdown=payload.body_markdown,
            body_json=payload.body_json,
            tags=payload.tags,
            metadata=payload.metadata,
            editor_id=payload.editor_id,
            change_reason=payload.change_reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    note = service.get_v2_note(note_id, environment_id=payload.environment_id)
    if note is None:
        raise HTTPException(status_code=404, detail=f"Unknown note_id: {note_id}")
    return note


@router.get("/journal/v2/notes/{note_id}/revisions")
def list_v2_note_revisions(note_id: str, request: Request, environment_id: str = Query(...)):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        items = service.list_v2_note_revisions(note_id, environment_id=environment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "count": len(items)}


@router.post("/journal/v2/attachments")
def create_v2_attachment(payload: JournalV2AttachmentRequest, request: Request):
    require_app_user(request)
    service = get_journal_service(request)
    try:
        attachment_id = service.attach_v2_file_metadata(
            environment_id=payload.environment_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            storage_key=payload.storage_key,
            mime_type=payload.mime_type,
            note_id=payload.note_id,
            sha256=payload.sha256,
            size_bytes=payload.size_bytes,
            ocr_text=payload.ocr_text,
            metadata=payload.metadata,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": attachment_id}
