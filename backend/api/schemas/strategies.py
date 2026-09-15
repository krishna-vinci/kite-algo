"""Request/response schemas for the hosted-strategy foundation API.

Owner identity is NEVER a field here: the owner is derived server-side from the
authenticated app session. ``account_scope`` is configuration (which account the
strategy would act on), not an owner.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrategyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    execution_mode: str = Field(default="paper")
    job_kind: str = Field(default="finite")
    account_scope: str = Field(min_length=1, max_length=255)
    #: Required explicitly until defaults are agreed (coordinator constraint).
    max_duration_s: int = Field(gt=0, le=7 * 24 * 3600)
    progress_deadline_s: int = Field(gt=0, le=7 * 24 * 3600)
    stale_exit_policy: str = Field(min_length=1)


class StrategyUpdateRequest(BaseModel):
    """Minimal metadata update. Versions are immutable and never touched here.

    Fields are applied only when provided (PATCH semantics). Omitting a field
    leaves it unchanged; an explicit ``null`` description clears it.
    """

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = None


class VersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    parameters_schema: Optional[Dict[str, Any]] = None
    #: Explicit capability declaration for this version. Omitted/empty means
    #: data-only (no trading rights). Unknown keys or non-boolean values are 422.
    capabilities: Optional[Dict[str, Any]] = None


class StrategyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    owner_id: str
    name: str
    template_id: str
    description: Optional[str]
    default_execution_mode: str
    default_job_kind: str
    default_account_scope: str
    max_duration_s: int
    progress_deadline_s: int
    stale_exit_policy: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class StrategyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategies: List[StrategyResponse] = Field(default_factory=list)


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    strategy_id: str
    version: int
    source: str
    source_sha256: str
    parameters_schema: Dict[str, Any]
    capabilities_snapshot: Dict[str, Any]
    created_by: str
    created_at: Optional[str] = None


class VersionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: List[VersionResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# jobs + operator reconciliation
# ---------------------------------------------------------------------------


class JobSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    owner_id: str
    attempt: int
    status: str
    desired_state: str
    execution_mode: str
    account_scope: str
    run_id: Optional[str] = None
    replacement_blocked: bool = False
    recovery_required_at: Optional[str] = None
    reconciled_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: List[JobSummaryResponse] = Field(default_factory=list)


class JobDetailResponse(JobSummaryResponse):
    handoff_at: Optional[str] = None
    process_cleanup_state: Optional[str] = None
    process_cleanup_at: Optional[str] = None
    process_cleanup_actor: Optional[str] = None
    last_progress_at: Optional[str] = None
    version_id: str
    token_present: bool = False


class ReconciliationAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    attempt: int
    outcome: str
    reason_code: str
    actor_id: str
    run_id: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class ReconciliationAssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    case: str
    reason_code: str
    blocking_reasons: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class ReconciliationInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    strategy_id: str
    attempt: int
    replacement_blocked: bool
    assessment: ReconciliationAssessmentResponse
    evidence: Dict[str, Any]
    history: List[ReconciliationAuditResponse] = Field(default_factory=list)


class ReconciliationActionRequest(BaseModel):
    """Explicit reconciliation against immutable identity.

    Deliberately carries **no** ``flat``/``reconciled`` assertion: the server
    decides from persisted evidence. ``attempt`` (and optionally ``lease_epoch``)
    pin the request to the exact attempt so a stale request is refused.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    lease_epoch: Optional[int] = Field(default=None, ge=0)


class ReconciliationActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    job_id: str
    attempt: int
    case: str
    reason_code: str
    replacement_blocked: bool
    blocking_reasons: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    audit_id: str
