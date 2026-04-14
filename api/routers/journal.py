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
    status: Optional[JournalRunStatus] = Query(None),
    review_state: Optional[ReviewState] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return service.list_runs_page(
        strategy_family=strategy_family,
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
    limit: int = Query(100, ge=1, le=500),
):
    require_app_user(request)
    service = get_journal_service(request)
    return {
        "items": service.list_strategies(strategy_family=strategy_family, limit=limit)
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
