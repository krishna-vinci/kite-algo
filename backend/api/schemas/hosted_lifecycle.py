"""Request/response schemas for the supervisor lifecycle API.

These models carry **authority references only** (lease owner/epoch/attempt) —
never a run id the caller chooses and never any supervisor secret. Job identity
and configuration are derived server-side from the persisted job record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Authority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_owner: str = Field(min_length=1, max_length=120)
    lease_epoch: int = Field(ge=0)
    attempt: int = Field(ge=1)


class ClaimJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_owner: str = Field(min_length=1, max_length=120)
    expected_lease_epoch: int = Field(ge=0)
    expected_attempt: int = Field(ge=1)
    lease_until: datetime


class PrepareLaunchRequest(_Authority):
    pass


class HeartbeatRequest(_Authority):
    lease_until: datetime


class ReleaseRequest(_Authority):
    pass


class FenceRequest(_Authority):
    reason: Optional[str] = Field(default=None, max_length=200)


class ChildLaunchConfigResponse(BaseModel):
    """The one-time child configuration handed to the supervisor.

    ``worker_token`` is a secret shown exactly once. It is the *child* token, not
    the supervisor credential.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    attempt: int
    lease_epoch: int
    run_id: str
    worker_token: str
    session_nonce: str
    template_id: str
    execution_mode: str
    account_scope: str
    params: Dict[str, Any]
    max_duration_s: int
    progress_deadline_s: int
    stale_exit_policy: str
    version_id: Optional[str] = None
    source_sha256: Optional[str] = None


class JobStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    attempt: int
    status: str
    desired_state: str
    lease_owner: Optional[str] = None
    lease_epoch: int
    lease_until: Optional[str] = None
    run_id: Optional[str] = None
    token_id: Optional[str] = None
    handoff_at: Optional[str] = None
    last_progress_at: Optional[str] = None
    progress_deadline_s: Optional[int] = None
    process_cleanup_state: Optional[str] = None
    process_cleanup_at: Optional[str] = None
    run_status: Optional[str] = None


class ProcessCleanupRequest(BaseModel):
    """Supervisor-owned child process-cleanup report, bound to the attempt."""

    model_config = ConfigDict(extra="forbid")

    lease_owner: str = Field(min_length=1, max_length=120)
    lease_epoch: int = Field(ge=0)
    attempt: int = Field(ge=1)
    state: str = Field(pattern="^(confirmed|unresolved)$")
    note: Optional[str] = Field(default=None, max_length=200)


class ProcessCleanupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    attempt: int
    process_cleanup_state: str
    note: Optional[str] = None


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    job_id: str
    reason: Optional[str] = None
    lease_until: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    #: True when the terminal transition leaves the replacement block in place
    #: (open exposure may remain); False when replacement is safe.
    replacement_blocked: Optional[bool] = None


class JobSummaryResponse(BaseModel):
    """Narrow discovery row; no secrets, no strategy configuration."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    attempt: int
    status: str
    execution_mode: str
    lease_epoch: int
    created_at: Optional[str] = None


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: List[JobSummaryResponse] = Field(default_factory=list)


class JobSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    version_id: str
    version: int
    source: str
    source_sha256: str
