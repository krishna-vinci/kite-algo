"""Request/response schemas for the screener worker API (Phase 3 F9).

Style mirrors ``backend/api/schemas/universes.py`` / ``workflows.py``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ScreenerPreviewRequest(BaseModel):
    """Pure screener dry-run: evaluation over stored data, no persistence,
    no scheduled work, no subscriptions, no outbox rows, no provider calls."""

    yaml_text: Optional[str] = None
    document: Optional[Dict[str, Any]] = None
    as_of: Optional[str] = None  # ISO-8601; default: now
    member_limit: int = 50


class RunMemberOut(BaseModel):
    instrument_key: str
    passed: bool = False
    exclusion_reason: Optional[str] = None
    rank: Optional[int] = None
    score: Optional[float] = None
    values: Dict[str, Any] = {}


class RunOut(BaseModel):
    run_id: str
    workflow_id: str
    workflow_revision_id: str
    occurrence_key: str
    scheduled_for: Optional[str] = None
    triggered_by: str = "schedule"
    status: str = "running"
    universe_revision: Optional[int] = None
    as_of: Optional[str] = None
    coverage: Dict[str, Any] = {}
    data_freshness: Dict[str, Any] = {}
    failure_reason: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class RunDetailResponse(BaseModel):
    ok: bool = True
    run: RunOut
    members: List[RunMemberOut] = []
    member_count: int = 0
    offset: int = 0
    limit: int = 100


class RunListResponse(BaseModel):
    ok: bool = True
    runs: List[RunOut] = []
    total_count: int = 0
    offset: int = 0
    limit: int = 20


class RunTriggerResponse(BaseModel):
    ok: bool = True
    run_id: Optional[str] = None
    status: str = "accepted"
    detail: Optional[str] = None


class ScreenerEventOut(BaseModel):
    event_id: str
    fired_at: Optional[str] = None
    evidence: Dict[str, Any] = {}


class ScreenerEventsResponse(BaseModel):
    ok: bool = True
    events: List[ScreenerEventOut] = []
    offset: int = 0
    limit: int = 50


class ScreenerPreviewResponse(BaseModel):
    ok: bool = True
    evaluation: str = "dry_run"
    status: str = "partial"
    coverage: Dict[str, Any] = {}
    data_freshness: Dict[str, Any] = {}
    members: List[RunMemberOut] = []
