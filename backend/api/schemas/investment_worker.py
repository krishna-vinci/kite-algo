"""Versioned public schemas for investment-oriented Worker HTTP routes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class V1Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1]
    source: str
    source_as_of: str | None
    retrieved_at: str


class PortfolioSnapshotResponse(V1Envelope):
    account_scope: str
    component_times: dict[str, str]
    coherent: bool
    coherence_skew_ms: int


class IndexSnapshotResponse(V1Envelope):
    source_list: str
    complete: bool
    members: list[dict[str, Any]]


class IndexStatusResponse(V1Envelope):
    source_list: str
    complete: bool


class CalendarResponse(V1Envelope):
    exchange: str
    segment: str
    calendar_version: int
    sessions: list[dict[str, Any]]
