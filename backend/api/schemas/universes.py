"""Request/response schemas for the universe worker API (alerts platform).

Consumed by ``backend.api.routers.worker_universes``. Style mirrors
``backend/api/schemas/workflows.py`` (Pydantic BaseModel, v1/v2-compatible
Optional/List/Dict typing; no pydantic v2-only APIs).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class UniverseCreateRequest(BaseModel):
    name: str
    kind: str  # explicit | index | portfolio
    source_config: Dict[str, Any] = {}


class UniversePreviewRequest(BaseModel):
    """Preview body: resolve would-be membership without persisting."""

    kind: str
    source_config: Dict[str, Any] = {}


class UniverseRevisionSummary(BaseModel):
    revision: int
    member_count: int = 0
    source_generation: Optional[str] = None
    resolved_at: Optional[str] = None
    created_at: Optional[str] = None


class UniverseSummary(BaseModel):
    universe_id: str
    name: str
    kind: str
    source_config: Dict[str, Any] = {}
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    latest_revision: Optional[UniverseRevisionSummary] = None


class UniverseMutationResponse(BaseModel):
    ok: bool = True
    universe_id: str
    name: str
    kind: str
    source_config: Dict[str, Any] = {}
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UniverseDetailResponse(UniverseSummary):
    latest_revision: Optional[UniverseRevisionSummary] = None
    latest_members: Optional[List[str]] = None
    latest_coverage: Optional[Dict[str, Any]] = None


class UniverseListResponse(BaseModel):
    universes: List[UniverseSummary]


class ResolveResponse(BaseModel):
    """resolve_membership payload: persisted revision + coverage."""

    ok: bool = True
    universe_id: str
    name: str
    kind: str
    revision: int
    members: List[str] = []
    rejected: List[Dict[str, Any]] = []
    source_generation: Optional[str] = None
    coverage: Dict[str, Any] = {}


class PreviewResponse(BaseModel):
    """Preview payload — computed in memory, never persisted."""

    ok: bool = True
    kind: str
    members: List[str] = []
    rejected: List[Dict[str, Any]] = []
    source_generation: Optional[str] = None
    coverage: Dict[str, Any] = {}
    note: str = (
        "preview only: resolved in memory; nothing is persisted "
        "(no universes, no universe_revisions)"
    )


class UniverseRevisionItem(BaseModel):
    revision_id: str
    revision: int
    members: List[str] = []
    member_count: int = 0
    source_generation: Optional[str] = None
    coverage: Dict[str, Any] = {}
    resolved_at: Optional[str] = None
    created_at: Optional[str] = None


class UniverseRevisionsResponse(BaseModel):
    universe_id: str
    name: str
    limit: int
    revisions: List[UniverseRevisionItem]
