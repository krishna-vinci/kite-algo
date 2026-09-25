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
    #: ``approval_based`` (default) or ``autonomous``. Selecting autonomous
    #: authorises nothing by itself; a grant is a separate owner act.
    authorization_mode: str = "approval_based"
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


class ScheduleEnabledRequest(BaseModel):
    """Explicit enable/disable of the stored schedule."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class HostedScheduleRequest(BaseModel):
    """One operator schedule configuration (create or edit).

    The schedule stores *what* to run (`version_id`, `params`, mode, kind) and
    *when* (`schedule_kind` + its kind-specific field). Account, policy and
    capability snapshots are derived server-side from the strategy and the
    pinned version, exactly like a launch, so an edit is a re-pin rather than a
    caller-asserted identity.
    """

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["paper", "dry_run", "live"]
    job_kind: str = Field(default="finite", max_length=32)
    params: Dict[str, Any] = Field(default_factory=dict)
    schedule_kind: Literal["daily", "weekly", "monthly", "calendar"]
    at_time: str = Field(max_length=5)
    weekday: Optional[int] = Field(default=None, ge=0, le=6)
    day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    calendar_dates: Optional[List[str]] = None
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    window_end: Optional[str] = Field(default=None, max_length=5)
    squareoff_at: Optional[str] = Field(default=None, max_length=5)
    enabled: bool = True


class HostedScheduleOccurrenceResponse(BaseModel):
    """One materialised occurrence (the scheduler's own durable row)."""

    model_config = ConfigDict(extra="forbid")

    occurrence_key: str
    due_at: Optional[str] = None
    #: ``pending`` / ``fired`` / ``skipped`` / ``expired``.
    status: str
    fired_at: Optional[str] = None
    evaluation_id: Optional[str] = None
    skip_reason: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class HostedScheduleResponse(BaseModel):
    """The stored schedule plus what the runtime will actually do with it."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    strategy_id: str
    version_id: str
    version_number: Optional[int] = None
    account_scope: str
    execution_mode: str
    job_kind: str
    params_snapshot: Dict[str, Any] = Field(default_factory=dict)
    schedule_kind: str
    at_time: str
    weekday: Optional[int] = None
    day_of_month: Optional[int] = None
    calendar_dates: List[str] = Field(default_factory=list)
    timezone: str
    window_end: Optional[str] = None
    squareoff_at: Optional[str] = None
    enabled: bool
    #: The stored manual pause, if one was set outside this surface.
    manually_paused: bool = False
    max_duration_s: int
    progress_deadline_s: int
    #: How late a missed occurrence may still fire, and what happens when the
    #: previous occurrence is unresolved. Reported from the live runtime rather
    #: than described in prose.
    misfire_grace_seconds: int
    overlap_policy: str
    next_occurrence_at: Optional[str] = None
    next_occurrence_key: Optional[str] = None
    last_occurrence: Optional["HostedScheduleOccurrenceResponse"] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class HostedStrategyOptionsResponse(BaseModel):
    """Server-authorized choices for configuring a hosted strategy.

    The browser must never hardcode or invent account choices: this returns only
    the account scopes the server will actually authorize, plus the supported
    modes, job kinds and policies.

    ``live`` is advertised ONLY when the deployment has hosted live enabled, and
    ``live_lanes`` names the live lanes that are ACTUALLY wired (empty while live is
    off), so a client can never advertise a mode or a lane the server would refuse.
    """

    model_config = ConfigDict(extra="forbid")

    account_scopes: List[str] = Field(default_factory=list)
    execution_modes: List[str] = Field(default_factory=list)
    job_kinds: List[str] = Field(default_factory=list)
    stale_exit_policies: List[str] = Field(default_factory=list)
    #: The wired live lanes, e.g. ``["cnc", "mis", "futures", "options"]``. Empty
    #: whenever hosted live is disabled.
    live_lanes: List[str] = Field(default_factory=list)
    #: Live execution always requires the owner's own approval. Stated, not
    #: inferred by the client.
    live_requires_owner_approval: bool = True
    hosted_execution_only: bool = True
    #: The documented runner profile (dependencies, Python version, whether
    #: arbitrary runtime installs are supported). Best-effort additive field:
    #: existing UI fields above are unchanged.
    runner_profile: Optional["RunnerProfileResponse"] = None


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
    #: ``manual`` for the owner's own click, ``automatic`` for the server
    #: recording a standing grant's authorisation. The decision is auditable and
    #: the automatic evidence travels with it, so it is never read as a click.
    actor_kind: str = "manual"
    authorization_evidence: Dict[str, Any] = Field(default_factory=dict)
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


# ---------------------------------------------------------------------------
# Governed execution authorization (Phase 2): mode, grants, requests
# ---------------------------------------------------------------------------


class AuthorizationModeRequest(BaseModel):
    """Explicit owner mode change. ``reason`` is recorded, never required."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["approval_based", "autonomous"]
    reason: Optional[str] = Field(default=None, max_length=1000)


class AuthorizationModeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    authorization_mode: str
    previous_mode: str
    changed: bool


class ExecutionGrantRequest(BaseModel):
    """An owner's standing authorisation request.

    The version, its source hash, the account and the policy hash are DERIVED by
    the server; only the environment is chosen (and validated). ``idempotency_key``
    is owner-provided and hidden from the UI.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=160)
    version_id: str = Field(min_length=1, max_length=128)
    execution_environment: Literal["paper", "dry_run", "live"]
    expires_at: Optional[datetime] = None


class ExecutionGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str
    owner_id: str
    strategy_id: str
    canonical_strategy_id: str
    version_id: str
    version_number: int
    source_sha256: str
    account_id: str
    execution_environment: str
    policy_hash: str
    policy_snapshot: Dict[str, Any] = Field(default_factory=dict)
    issued_by: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    status: str
    revoked_by: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revocation_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    superseded_at: Optional[datetime] = None
    supersession_reason: Optional[str] = None
    #: The owner's idempotency key (hidden from the UI) and the canonical hash
    #: of the request content that key is bound to.
    request_key: str
    content_sha256: str
    created_at: Optional[datetime] = None
    idempotent: bool = False


class ExecutionGrantRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=1000)


class ExecutionGrantRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant: ExecutionGrantResponse
    revoked_at: Optional[datetime] = None


class AuthorizationStatusResponse(BaseModel):
    """The operator's authorization view. ``grant_usable`` is a statement about
    the current record; execution re-derives it under the strategy lock."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    authorization_mode: str
    active_grant: Optional[ExecutionGrantResponse] = None
    policy_snapshot: Dict[str, Any] = Field(default_factory=dict)
    policy_hash: str
    policy_concrete: bool
    grant_usable: bool
    blocking_reasons: List[str] = Field(default_factory=list)
    evaluated_at: Optional[datetime] = None


class ExecutionRequestRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    owner_id: str
    strategy_id: str
    canonical_strategy_id: str
    account_id: str
    execution_environment: str
    strategy_run_id: str
    job_id: Optional[str] = None
    #: The child credential the request was created under (an identifier, never a
    #: secret). A rotated token is a different attempt and refuses the dispatch.
    token_id: Optional[str] = None
    attempt: Optional[int] = None
    lease_epoch: Optional[int] = None
    version_id: str
    version_number: Optional[int] = None
    source_sha256: str
    policy_hash: str
    evaluation_id: Optional[str] = None
    plan_id: str
    plan_hash: str
    authorization_mode: str
    grant_id: Optional[str] = None
    status: str
    refusal_code: Optional[str] = None
    refusal_detail: Dict[str, Any] = Field(default_factory=dict)
    decision_kind: Optional[str] = None
    decision_actor: Optional[str] = None
    decision_at: Optional[datetime] = None
    decision_evidence: Dict[str, Any] = Field(default_factory=dict)
    approval_id: Optional[str] = None
    reservation_id: Optional[str] = None
    execution_detail: Dict[str, Any] = Field(default_factory=dict)
    #: The executor's own outcome word (submitted / filled / rejected / failed /
    #: uncertain / no_op). ``terminal`` means "not dispatched again", not "done".
    outcome_state: Optional[str] = None
    dispatch_claim_id: Optional[str] = None
    dispatch_claimed_at: Optional[datetime] = None
    dispatch_started_at: Optional[datetime] = None
    dispatch_finished_at: Optional[datetime] = None
    idempotency_key: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    terminal: bool = False
    executable: bool = False


class ExecutionRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    requests: List[ExecutionRequestRow] = Field(default_factory=list)


class ExecutionRequestDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=1000)


class ExecutionRequestDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: ExecutionRequestRow
    approved: bool = False
    rejected: bool = False


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


class RollCreateRequest(BaseModel):
    """Open a roll. The plan id is recorded, never trusted for authority."""

    model_config = ConfigDict(extra="forbid")

    old_instrument_id: str
    new_instrument_id: str
    required_replacement_quantity: int
    old_coordinate: Dict[str, Any] = Field(default_factory=dict)
    new_coordinate: Dict[str, Any] = Field(default_factory=dict)
    plan_id: Optional[str] = None
    peak_margin_evidence: Dict[str, Any] = Field(default_factory=dict)


class RollStallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class OptionSettlementEvidenceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    account_id: str
    option_run_id: str
    structure_digest: str
    settlement_kind: str
    evidence_source: str
    evidence_ref: Dict[str, Any] = Field(default_factory=dict)
    recorded_by: str
    adjustment_id: Optional[str] = None


class OptionSettlementResponse(BaseModel):
    """A run's settlement state, and the evidence that justifies it."""

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    #: Derived, never stored: settled exactly when authoritative evidence exists.
    settled: bool = False
    evidence: List[OptionSettlementEvidenceRow] = Field(default_factory=list)


class OptionRunRepairPlanLeg(BaseModel):
    """One bounded, risk-reducing close action the repair would submit."""

    model_config = ConfigDict(extra="forbid")

    tradingsymbol: str
    transaction_type: str
    quantity: int
    exchange: Optional[str] = None
    product: Optional[str] = None
    order_type: Optional[str] = None


class OptionRunRepairAssessmentResponse(BaseModel):
    """The read-only repair verdict for one option run.

    ``state`` is DERIVED from the run's own confirmed fills, never asserted by a
    caller: ``flat`` (nothing held - close it), ``residual`` (some leg still open
    - the close plan is the repair), ``ambiguous`` (the platform cannot explain
    the run - escalate by name) or ``not_repairable`` (wrong status).
    """

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    status: str
    state: str
    reason_code: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    evidence_digest: str
    close_plan: List[OptionRunRepairPlanLeg] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    detail: Dict[str, Any] = Field(default_factory=dict)


class OptionRunRepairActionRequest(BaseModel):
    """A pinned repair action: the digest must still describe the run."""

    model_config = ConfigDict(extra="forbid")

    action: str
    evidence_digest: str


class OptionRunRepairActionResponse(BaseModel):
    """What the repair actually did, and the evidence digest it acted on."""

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    action: str
    state: str
    run_status: str
    evidence_digest: str
    audit_id: Optional[str] = None
    submission: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# first-run source readiness (Slice: hosted data foundation)
# ---------------------------------------------------------------------------


class SourceReadinessRequest(BaseModel):
    """A source file the user is about to run, checked BEFORE anything is stored.

    ``extra="forbid"`` at the boundary, and the source is bounded exactly like a
    stored version. The source is parsed (``ast``) and never imported, compiled
    into a module or executed.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=256 * 1024)


class RunnerPackageResponse(BaseModel):
    """One package the documented runner profile provides."""

    model_config = ConfigDict(extra="forbid")

    import_name: str
    distribution: str
    extra: Optional[str] = None


class RunnerProfileResponse(BaseModel):
    """The single documented runner profile a hosted strategy runs under."""

    model_config = ConfigDict(extra="forbid")

    id: str
    python: str
    base_image: str
    packages: List[RunnerPackageResponse] = Field(default_factory=list)
    #: Server-side indicators need no local numerical stack.
    server_side_indicators: bool = True
    #: Arbitrary runtime ``pip install`` is not supported; stated, not implied.
    runtime_pip_install: bool = False
    notes: Optional[str] = None


class ReadinessCheckResponse(BaseModel):
    """One named check: ``ok``, ``blocked`` or ``unknown`` (never a false pass)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["ok", "blocked", "unknown"]
    detail: str
    remediation: Optional[str] = None


class SourceEntrypointResponse(BaseModel):
    """The ``main(ctx)`` entrypoint as far as static parsing can tell."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    compatible: bool
    name: Optional[str] = None
    is_async: Optional[bool] = None
    detail: str
    remediation: Optional[str] = None


class SourceImportResponse(BaseModel):
    """Statically visible imports, resolved against the runner profile."""

    model_config = ConfigDict(extra="forbid")

    available: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    #: Imports guarded by ``except ImportError`` that the profile does provide.
    optional_available: List[str] = Field(default_factory=list)
    #: Imports guarded by ``except ImportError``: reported, not treated as fatal.
    optional_missing: List[str] = Field(default_factory=list)
    providers: Dict[str, str] = Field(default_factory=dict)
    #: ``importlib``/``__import__``/``exec``/``eval`` seen: cannot be certified.
    dynamic: bool = False


class SourceReadinessResponse(BaseModel):
    """Reusable first-run readiness result consumed by the operator UI."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    status: Literal["ready", "blocked"]
    profile: RunnerProfileResponse
    checks: List[ReadinessCheckResponse] = Field(default_factory=list)
    entrypoint: SourceEntrypointResponse
    imports: SourceImportResponse
    messages: List[str] = Field(default_factory=list)
