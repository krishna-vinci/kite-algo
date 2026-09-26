"""Response/request models for the platform control plane (P2 UX).

These mirror the shapes the operator home screen reads (``/api/platform/*`` plus
the cross-strategy approvals inbox). ``extra="forbid"`` on requests keeps a
client from smuggling an un-modelled lane or actor through a body.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PlatformLanes(BaseModel):
    """The four public live lanes, each explicitly open or closed."""

    model_config = ConfigDict(extra="forbid")

    cnc: bool
    mis: bool
    futures: bool
    options: bool


class PlatformAccountView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Masked (``kite:XJJ***``); the full account identity is never sent.
    scope: str
    allowed: bool


class PlatformLiveSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The read-only deployment master switch (``HOSTED_LIVE_ENABLED``).
    live_enabled: bool
    lanes: PlatformLanes
    #: Which source answered for ``lanes``: the persisted row or the env default.
    lanes_source: Literal["db", "env"]
    account: PlatformAccountView
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class PlatformLiveSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: PlatformLanes
    #: Recorded in the audit row. Optional, never a caller identity.
    reason: Optional[str] = Field(default=None, max_length=1000)


class PlatformBrokerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["connected", "expired", "unknown"]
    detail: Optional[str] = None


class PlatformMarketDataStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["ok", "stale", "down"]
    last_tick_age_s: Optional[float] = None


class PlatformStrategyRunnerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["ok", "stale", "unknown"]
    last_seen_age_s: Optional[int] = None


class PlatformLiveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    lanes_open: List[str] = Field(default_factory=list)


class PlatformStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["live", "paper"]
    broker: PlatformBrokerStatus
    market_data: PlatformMarketDataStatus
    strategy_runner: PlatformStrategyRunnerStatus
    live: PlatformLiveStatus


class PendingApprovalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    strategy_name: str
    request_id: str
    plan_id: str
    environment: str
    summary: str
    created_at: Optional[datetime] = None
    #: When the linked reservation lapses. ``None`` while no reservation exists,
    #: because unknown is reported as unknown.
    expires_at: Optional[datetime] = None


class PendingApprovalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PendingApprovalRow] = Field(default_factory=list)
    count: int


__all__ = [
    "PendingApprovalRow",
    "PendingApprovalsResponse",
    "PlatformAccountView",
    "PlatformBrokerStatus",
    "PlatformLanes",
    "PlatformLiveSettingsResponse",
    "PlatformLiveSettingsUpdateRequest",
    "PlatformLiveStatus",
    "PlatformMarketDataStatus",
    "PlatformStatusResponse",
    "PlatformStrategyRunnerStatus",
]
