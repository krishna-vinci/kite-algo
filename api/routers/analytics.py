from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from auth_service import require_app_user
from journaling.analytics_service import AnalyticsService
from journaling.models import (
    AnalyticsSummaryResponse,
    CostAnalysisResponse,
    EquityCurveResponse,
    MetricPeriod,
    PaperLiveComparisonResponse,
    StrategyDeepDiveResponse,
)


router = APIRouter(tags=["Analytics"])


def get_analytics_service(request: Request) -> AnalyticsService:
    service = getattr(request.app.state, "analytics_service", None)
    if service is None:
        service = AnalyticsService()
        request.app.state.analytics_service = service
    return service


@router.get("/analytics/v1/summary", response_model=AnalyticsSummaryResponse)
def get_analytics_summary(
    request: Request,
    environment_id: str = Query(...),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: date | None = Query(None, alias="date"),
):
    require_app_user(request)
    service = get_analytics_service(request)
    try:
        return service.compute_analytics_summary(environment_id=environment_id, period=period, anchor_date=anchor_date)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/v1/strategy/{template_id}", response_model=StrategyDeepDiveResponse)
def get_strategy_deep_dive(
    template_id: str,
    request: Request,
    environment_id: str = Query(...),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: date | None = Query(None, alias="date"),
):
    require_app_user(request)
    service = get_analytics_service(request)
    try:
        return service.compute_strategy_deep_dive(
            environment_id=environment_id,
            template_id=template_id,
            period=period,
            anchor_date=anchor_date,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/v1/equity-curve", response_model=EquityCurveResponse)
def get_equity_curve(
    request: Request,
    environment_id: str = Query(...),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: date | None = Query(None, alias="date"),
    template_id: str | None = Query(None),
):
    require_app_user(request)
    service = get_analytics_service(request)
    try:
        return service.compute_equity_curve(
            environment_id=environment_id,
            period=period,
            anchor_date=anchor_date,
            template_id=template_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/v1/cost-analysis", response_model=CostAnalysisResponse)
def get_cost_analysis(
    request: Request,
    environment_id: str = Query(...),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: date | None = Query(None, alias="date"),
):
    require_app_user(request)
    service = get_analytics_service(request)
    try:
        return service.compute_cost_analysis(environment_id=environment_id, period=period, anchor_date=anchor_date)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/v1/compare", response_model=PaperLiveComparisonResponse)
def get_paper_live_comparison(
    request: Request,
    template_id: str = Query(...),
    paper_environment_id: str = Query(...),
    live_environment_id: str = Query(...),
    period: MetricPeriod = Query(MetricPeriod.SINCE_INCEPTION),
    anchor_date: date | None = Query(None, alias="date"),
):
    require_app_user(request)
    service = get_analytics_service(request)
    try:
        return service.compute_paper_live_comparison(
            template_id=template_id,
            paper_environment_id=paper_environment_id,
            live_environment_id=live_environment_id,
            period=period,
            anchor_date=anchor_date,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
