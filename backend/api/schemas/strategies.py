"""Request/response schemas for the hosted-strategy foundation API.

Owner identity is NEVER a field here: the owner is derived server-side from the
authenticated app session. ``account_scope`` is configuration (which account the
strategy would act on), not an owner.
"""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class StrategyCreateKind(str, Enum):
    HOSTED = "hosted"
    EXTERNAL = "external"


class HostedStrategyCreateRequest(BaseModel):
    """The existing hosted create contract, unchanged, plus the optional
    discriminator. Field names are the CURRENT request-schema names — per-run
    ``execution_mode`` and ``job_kind`` (NOT the ``hosted_strategies``
    strategy-level ``default_job_kind`` column name) — with the existing
    defaults and bounds, so the legacy frontend payload keeps validating
    byte-for-byte."""

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
    #: ABSENT in the legacy payload (which means hosted); an explicit "hosted" is
    #: accepted identically.
    kind: Optional[Literal[StrategyCreateKind.HOSTED]] = None


class ExternalStrategyCreateRequest(BaseModel):
    """Discriminated external request.

    Hosted-only fields (``execution_mode``, ``job_kind``, progress deadlines,
    stale-exit policy) are neither required nor accepted: ``extra="forbid"``
    makes supplying them a 422, so the two contracts cannot blur.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[StrategyCreateKind.EXTERNAL]
    name: str = Field(min_length=1, max_length=120)
    account_scope: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    external_config: Dict[str, Any] = Field(default_factory=dict)


StrategyCreateRequest = HostedStrategyCreateRequest


def parse_strategy_create(
    payload: Dict[str, Any],
) -> Union[HostedStrategyCreateRequest, ExternalStrategyCreateRequest]:
    """Validate one create payload against exactly one of the two contracts."""
    if payload.get("kind") == StrategyCreateKind.EXTERNAL.value:
        return ExternalStrategyCreateRequest.model_validate(payload)
    # Legacy payload (no kind, or kind == "hosted") keeps the hosted contract,
    # including its required fields and validation errors.
    return HostedStrategyCreateRequest.model_validate(payload)


class StrategyUpdateRequest(BaseModel):
    """Minimal metadata update. Versions are immutable and never touched here.

    Fields are applied only when provided (PATCH semantics). Omitting a field
    leaves it unchanged; an explicit ``null`` description clears it. ``status``
    here means hosted **scheduling** enablement, never canonical product status.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = None


class ProductStatusUpdateRequest(BaseModel):
    """Canonical PRODUCT status (``active`` | ``disabled`` | ``archived``).

    Archiving preserves every binding and projection row; there is no delete.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled", "archived"]


class ExternalAdapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: Dict[str, Any] = Field(default_factory=dict)


class ExternalAdapterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    strategy_id: str
    status: str
    config: Dict[str, Any] = Field(default_factory=dict)


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str = Field(min_length=1, max_length=128)


class GrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    token_id: str
    granted_by: str
    revoked: bool = False


class PositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_kind: str
    identity_key: str
    product: str
    instrument_token: int
    exchange: str
    tradingsymbol: str
    net_quantity: int
    unresolved_reason: Optional[str] = None


class PositionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    environment: str
    positions: List[PositionRow] = Field(default_factory=list)


class AdjustmentLineRequest(BaseModel):
    """One signed quantity move between the manual residual and this strategy.

    ``quantity_delta`` is the quantity **credited to the strategy**: claiming a
    ``-10`` unattributed fill is ``-10``, which lowers the strategy's book by 10
    and raises the manual residual by 10 toward zero.
    """

    model_config = ConfigDict(extra="forbid")

    trade_ref: Optional[str] = Field(default=None, max_length=255)
    instrument_token: int
    exchange: str = Field(min_length=1, max_length=32)
    tradingsymbol: str = Field(min_length=1, max_length=255)
    product: str = Field(min_length=1, max_length=32)
    quantity_delta: int
    #: Omitted defaults to the adjustment's creation time — never the fill's.
    effective_at: Optional[datetime] = None


class AdjustmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(min_length=1, max_length=120)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    lines: List[AdjustmentLineRequest] = Field(min_length=1)


class AdjustmentLineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int
    instrument_token: int
    exchange: str
    tradingsymbol: str
    product: str
    quantity_delta: int
    effective_at: Optional[str] = None


class AdjustmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustment_id: str
    strategy_id: str
    account_id: str
    adjustment_kind: str
    reason_code: str
    created_by: str
    evidence: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    lines: List[AdjustmentLineResponse] = Field(default_factory=list)


class RebuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    execution_environment: str
    projection_version: int
    unchanged: bool
    folded_facts: int = 0
    unresolved: List[Dict[str, Any]] = Field(default_factory=list)
    anomalies: List[Dict[str, Any]] = Field(default_factory=list)


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
    #: Hosted SCHEDULING enablement (the existing field, unchanged meaning).
    status: str
    #: Canonical PRODUCT status — additive; never written by PATCH /{id}.
    product_status: str = "active"
    #: Which compute adapters this one product has ("hosted" / "external").
    adapter_kinds: List[str] = Field(default_factory=list)
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
    stop_requested_at: Optional[str] = None
    stop_requested_by: Optional[str] = None
    stop: Dict[str, Any] = Field(default_factory=dict)
    logs_discarded: bool = False
    logs_source: Optional[str] = None


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


# ---------------------------------------------------------------------------
# operator controls: run now, stop, logs, notification history
# ---------------------------------------------------------------------------


class RunNowRequest(BaseModel):
    """Explicit operator launch request against an immutable version."""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=128)
    params: Dict[str, Any] = Field(default_factory=dict)
    execution_mode: Optional[str] = None
    job_kind: Optional[str] = None
    #: Request idempotency: a retry with the same key returns the same job.
    idempotency_key: str = Field(min_length=8, max_length=160)


class JobStopView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested: bool
    state: str  # none | requested | stopping | confirmed | cleanup_unresolved
    requested_at: Optional[str] = None
    requested_by: Optional[str] = None
    replacement_blocked: bool = False
    note: str = ""


class RunNowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotent: bool = False
    job: "JobDetailResponse"


class StopJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    lease_epoch: Optional[int] = Field(default=None, ge=0)


class StopJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    attempt: int
    idempotent: bool = False
    stop: JobStopView


class DeliveryAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_no: int
    outcome: str
    detail: str = ""
    provider_id: Optional[str] = None
    created_at: Optional[str] = None


class DeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    channel_id: str
    channel_name: Optional[str] = None
    status: str
    attempts: int
    last_error: Optional[str] = None
    delivered_at: Optional[str] = None
    attempt_history: List[DeliveryAttemptResponse] = Field(default_factory=list)


class RunNotificationEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: str
    fired_at: Optional[str] = None
    text: str = ""
    subject: Optional[str] = None
    deliveries: List[DeliveryResponse] = Field(default_factory=list)
    delivery_status_counts: Dict[str, int] = Field(default_factory=dict)


class RunNotificationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    run_id: Optional[str] = None
    events: List[RunNotificationEventResponse] = Field(default_factory=list)


class JobLogEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    content: str
    created_at: Optional[str] = None


class JobLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    available: bool
    #: True means output was actually discarded (or the stored cap was reached).
    truncated: bool = False
    #: How logs were collected: `post_termination` in v1 (live collection is not
    #: implemented); `null` when nothing was collected.
    source: Optional[str] = None
    next_seq: int = 0
    entries: List[JobLogEntryResponse] = Field(default_factory=list)
    notice: str = ""


class HostedStrategyOptionsResponse(BaseModel):
    """Server-authorized choices for configuring a hosted strategy.

    The browser must never hardcode or invent account choices: this returns only
    the account scopes the server will actually authorize, plus the supported
    modes, job kinds and policies.
    """

    model_config = ConfigDict(extra="forbid")

    account_scopes: List[str] = Field(default_factory=list)
    execution_modes: List[str] = Field(default_factory=list)
    job_kinds: List[str] = Field(default_factory=list)
    stale_exit_policies: List[str] = Field(default_factory=list)
    hosted_execution_only: bool = True


class AdmissionPolicyRequest(BaseModel):
    """The recorded basis for admission. ``extra="forbid"`` at the boundary."""

    model_config = ConfigDict(extra="forbid")

    allocation_inr: Optional[float] = Field(default=None, ge=0)
    per_instrument_notional_inr: Optional[float] = Field(default=None, ge=0)
    gross_notional_inr: Optional[float] = Field(default=None, ge=0)
    max_open_instruments: Optional[int] = Field(default=None, ge=0)
    admissions_per_window: Optional[int] = Field(default=None, gt=0)
    admission_window_seconds: Optional[int] = Field(default=None, gt=0)
    daily_loss_budget_inr: Optional[float] = Field(default=None, ge=0)


class AdmissionPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    account_id: str
    allocation_inr: Optional[float] = None
    per_instrument_notional_inr: Optional[float] = None
    gross_notional_inr: Optional[float] = None
    max_open_instruments: Optional[int] = None
    admissions_per_window: Optional[int] = None
    admission_window_seconds: Optional[int] = None
    daily_loss_budget_inr: Optional[float] = None
    updated_by: str = ""


class AdmissionVerdictResponse(BaseModel):
    """A preview verdict. A preview is NOT a reservation (R3 §8)."""

    model_config = ConfigDict(extra="forbid")

    admitted: bool
    rejection_reason: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ReservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str
    plan_id: str
    strategy_id: str
    account_id: str
    evaluation_id: str
    execution_environment: str
    status: str
    reserved_notional_inr: float
    margin_evidence: Dict[str, Any] = Field(default_factory=dict)
    margin_as_of: Optional[str] = None
    valid_until: Optional[str] = None
    renewed_at: Optional[str] = None
    released_at: Optional[str] = None
    release_reason: Optional[str] = None


class ReservationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservations: List[ReservationResponse] = Field(default_factory=list)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    plan_id: str
    strategy_id: str
    account_id: str
    reservation_id: str
    plan_hash: str
    exposure_snapshot_version: int
    exposure_snapshot_hash: Optional[str] = None
    reconciliation_version: int
    catalog_generation: str
    session_product_snapshot: Dict[str, Any] = Field(default_factory=dict)
    actor_id: str
    status: str
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    #: Derived at read time, never stored.
    structural_validity: Dict[str, Any] = Field(default_factory=dict)


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approvals: List[ApprovalResponse] = Field(default_factory=list)


class ApprovalRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str = Field(min_length=1, max_length=64)
    validity_seconds: int = Field(default=900, gt=0, le=86400)


# ---------------------------------------------------------------------------
# Settlement evidence surfaces (G7): owner read + assess trigger
# ---------------------------------------------------------------------------


class SettlementAxisResponse(BaseModel):
    """One settlement axis (R3 §16): satisfied/failed/unknown plus its digest."""

    model_config = ConfigDict(extra="forbid")

    satisfied: bool
    state: str
    evidence_digest: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class SettlementAssessmentResponse(BaseModel):
    """An append-only assessment SNAPSHOT (D-5), never a settlement state.

    ``stale`` is derived at read time by comparing the snapshot's
    ``barrier_version`` with the barrier's current version: a later work event
    (a late fill) makes a ``settled`` snapshot detectably stale.
    """

    model_config = ConfigDict(extra="forbid")

    assessment_id: str
    strategy_id: str
    account_id: str
    execution_environment: str
    overall: str
    barrier_version: int
    axes: Dict[str, SettlementAxisResponse] = Field(default_factory=dict)
    evidence_digest: str
    created_at: Optional[str] = None
    stale: bool = False


class SettlementAssessRequest(BaseModel):
    """Owner trigger for one assessment. Nothing else is configurable here:
    the axes, the barrier and the rollup are the platform's, not the caller's."""

    model_config = ConfigDict(extra="forbid")

    environment: Optional[str] = None


class SquareoffEvidenceRow(BaseModel):
    """One square-off outcome. Append-only evidence, never edited."""

    model_config = ConfigDict(extra="forbid")

    id: str
    account_id: str
    strategy_id: str
    strategy_run_id: str
    product: str
    session_date: Optional[str] = None
    exchange: str
    scheduled_at: Optional[str] = None
    exit_claim_id: Optional[str] = None
    outcome: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class SquareoffEvidenceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    squareoffs: List[SquareoffEvidenceRow] = Field(default_factory=list)


class RollEventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class RollResponse(BaseModel):
    """A roll carries BOTH instrument identities through the whole transition."""

    model_config = ConfigDict(extra="forbid")

    roll_id: str
    strategy_id: str
    account_id: str
    old_instrument_id: str
    new_instrument_id: str
    old_coordinate: Dict[str, Any] = Field(default_factory=dict)
    new_coordinate: Dict[str, Any] = Field(default_factory=dict)
    required_replacement_quantity: int
    proven_filled_quantity: int
    state: str
    action_reason: Optional[str] = None
    peak_margin_evidence: Dict[str, Any] = Field(default_factory=dict)
    plan_id: Optional[str] = None
    #: The append-only trail, ordered. Populated on the single-roll read.
    events: List[RollEventRow] = Field(default_factory=list)
    #: Derived at read time: whether the close step is reachable yet.
    close_release_permitted: bool = False


class RollListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rolls: List[RollResponse] = Field(default_factory=list)
