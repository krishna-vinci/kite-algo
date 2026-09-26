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
    #: The account-wide day-loss cap in INR, or ``None`` for "no cap configured".
    account_daily_loss_cap_inr: Optional[float] = None
    account: PlatformAccountView
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class PlatformLiveSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: PlatformLanes
    #: The account-wide day-loss cap. An ABSENT field preserves the stored value;
    #: an explicit ``null`` clears it.
    account_daily_loss_cap_inr: Optional[float] = Field(default=None, ge=0)
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


class PlatformRiskStatus(BaseModel):
    """The account-wide day-loss cap and the broker day P&L it is tested against."""

    model_config = ConfigDict(extra="forbid")

    #: The broker's own day P&L for the platform account, or ``None`` when the
    #: reconciled positions book could not be read (unknown, never guessed).
    day_pnl_inr: Optional[float] = None
    #: The configured cap, or ``None`` when no cap is configured.
    cap_inr: Optional[float] = None
    #: True when a cap is configured and the day P&L is at or below it - or could
    #: not be read at all, since an unreadable cap evidence fails closed.
    cap_reached: bool = False


class PlatformStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["live", "paper"]
    broker: PlatformBrokerStatus
    market_data: PlatformMarketDataStatus
    strategy_runner: PlatformStrategyRunnerStatus
    live: PlatformLiveStatus
    risk: PlatformRiskStatus


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
    "PlatformRiskStatus",
    "PlatformStatusResponse",
    "PlatformStrategyRunnerStatus",
]
