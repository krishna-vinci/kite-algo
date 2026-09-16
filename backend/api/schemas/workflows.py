"""Request/response schemas for the alerts-platform worker APIs (Task 8).

Consumed by ``backend.api.routers.worker_workflows`` and
``worker_notifications``. The issue envelope (``ValidationIssueModel``) mirrors
``backend.workflows.compiler.ValidationIssue`` so parse and validation
problems from the workflows layer reach API clients in one shape.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ValidationIssueModel(BaseModel):
    where: str
    code: str
    message: str
    # "error" blocks the document; "warning" is advisory. Defaults to "error" so
    # existing clients keep their interpretation (Phase 6 6A.0).
    severity: str = "error"


class IssueEnvelope(BaseModel):
    """Generic parse/validation result: 200 with ok=False when invalid."""

    ok: bool = True
    issues: List[ValidationIssueModel] = []


def issue(
    where: str, code: str, message: str, severity: str = "error"
) -> ValidationIssueModel:
    return ValidationIssueModel(
        where=where, code=code, message=message, severity=severity
    )


class WorkflowValidateRequest(BaseModel):
    yaml_text: Optional[str] = None
    document: Optional[Dict[str, Any]] = None
    # Optional recent samples for the read-only preview.  The API deliberately
    # does not fetch live data or mutate evaluation state; callers may supply
    # exchange-timestamped samples from their market-data snapshot.
    observations: List[Dict[str, Any]] = []


class WorkflowCreateRequest(BaseModel):
    name: Optional[str] = None  # defaults to the document's own name
    yaml_text: Optional[str] = None
    document: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None


class WorkflowImportRequest(BaseModel):
    yaml_text: str
    idempotency_key: Optional[str] = None


class WorkflowPatchRequest(BaseModel):
    yaml_text: Optional[str] = None
    document: Optional[Dict[str, Any]] = None
    expected_revision: int


class WorkflowActivateRequest(BaseModel):
    """Optional activate body: ``{"revision": N}`` activates that revision
    (rollback path); absent/null revision activates the latest one."""

    revision: Optional[int] = None


class PreviewResponse(IssueEnvelope):
    instruments: List[str] = []
    stages: List[str] = []
    alerts: List[str] = []
    evaluation: str = "dry_run_no_data"
    warmup_bars: int = 0
    evaluated_observations: int = 0
    would_fire: List[Dict[str, Any]] = []
    unknown_reasons: List[str] = []
    note: str = ""


class RevisionSummary(BaseModel):
    revision_id: str
    revision: int
    status: str
    canonical_hash: str
    created_at: Optional[str] = None
    activated_at: Optional[str] = None


class WorkflowSummary(BaseModel):
    workflow_id: str
    name: str
    idempotency_key: Optional[str] = None
    archived: bool = False
    archived_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    latest_revision: Optional[RevisionSummary] = None
    active_revision: Optional[RevisionSummary] = None


class WorkflowListResponse(BaseModel):
    workflows: List[WorkflowSummary]


class WorkflowMutationResponse(BaseModel):
    ok: bool = True
    workflow_id: str
    name: Optional[str] = None
    idempotency_key: Optional[str] = None
    created: Optional[bool] = None  # create/import: False on idempotent replay
    changed: Optional[bool] = None  # patch: False when the document hash is unchanged
    revision: Optional[int] = None
    revision_id: Optional[str] = None
    revision_status: Optional[str] = None
    canonical_hash: Optional[str] = None
    subscriptions_created: Optional[int] = None  # activate
    state: Optional[str] = None  # pause/resume
    updated: Optional[int] = None  # pause/resume rowcount
    archived: Optional[bool] = None  # archive
    archived_at: Optional[str] = None
    revisions_archived: Optional[int] = None  # archive: revisions flipped to archived


class WorkflowExportResponse(BaseModel):
    workflow_id: str
    revision: int
    canonical_hash: str
    document: Dict[str, Any]


class SignalEventItem(BaseModel):
    id: str
    subscription_id: str
    alert_id: Optional[str] = None
    occurrence_key: str
    fired_at: Optional[str] = None
    evidence: Dict[str, Any] = {}


class EventPage(BaseModel):
    workflow_id: str
    limit: int
    offset: int
    total: int
    events: List[SignalEventItem]


class SubscriptionHealth(BaseModel):
    alert_id: str
    instrument_key: str
    state: str
    # max(evaluation_checkpoints.updated_at) for this subscription; None when
    # the subscription has never been evaluated.
    last_evaluated_at: Optional[str] = None


class HealthResponse(BaseModel):
    workflow_id: str
    active_revision: Optional[int] = None
    subscriptions: List[SubscriptionHealth] = []
    last_event_at: Optional[str] = None
    delivery_counts: Dict[str, int] = {}
    # Constant so clients can compute staleness:
    # now - last_evaluated_at > stale_after_seconds  =>  treat as stale.
    stale_after_seconds: int = 300


class ChannelCreateRequest(BaseModel):
    name: str
    provider: str
    destination: Dict[str, Any]
    secret_env: Optional[str] = None
    enabled: bool = True


class ChannelResponse(BaseModel):
    channel_id: str
    name: str
    provider: str
    destination: Dict[str, Any] = {}
    secret_env: Optional[str] = None
    enabled: bool = True
    created_at: Optional[str] = None


class ChannelListResponse(BaseModel):
    channels: List[ChannelResponse]


class ChannelTestRequest(BaseModel):
    message: Optional[str] = None


class ChannelTestResponse(BaseModel):
    status: str
    provider_id: Optional[str] = None
    detail: str = ""


class WorkflowFrequencyRequest(BaseModel):
    """Change how often an EXISTING alert notifies.

    Deliberately narrow: the caller names a frequency, never a document. The
    server merges the trigger into the stored revision, so the change cannot
    silently drop the rest of a definition the client does not hold.
    """

    frequency: str
    reminder_interval_s: Optional[int] = None
    expected_revision: Optional[int] = None
