"""Proposal and frozen-plan API schemas (G5).

Request models are ``extra="forbid"``: the proposal contract is two-way, and a
typo'd field must fail loudly rather than silently producing a different plan
than the caller believes they submitted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Evaluation kinds a client may assert.
EVALUATION_KINDS = ("scheduled_occurrence", "run_now")


class ProposalSubmitRequest(BaseModel):
    """One evaluation's proposal, as a worker asserts it.

    ``strategy_id`` and ``account_scope`` are asserted *by the caller* and then
    checked against the run's durable binding: they are never identity, only a
    claim the authority check must confirm (D-8).
    """

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(min_length=1, max_length=255)
    evaluation_kind: str = Field(default="run_now", max_length=32)
    strategy_run_id: str = Field(min_length=1, max_length=255)
    strategy_id: str = Field(min_length=1, max_length=255)
    account_scope: str = Field(min_length=1, max_length=64)
    job_id: Optional[str] = Field(default=None, max_length=255)
    target_kind: str = Field(min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    proposal_id: str
    strategy_id: str
    account_id: str
    plan_kind: str
    plan_hash: str
    logical_plan: Dict[str, Any] = Field(default_factory=dict)
    resolved_plan: Dict[str, Any] = Field(default_factory=dict)
    pinned_universe_revision_id: Optional[str] = None
    pinned_member_hash: Optional[str] = None
    pinned_catalog_generation: str
    #: Derived at read time, never stored (D-5).
    invalidation_state: Dict[str, Any] = Field(default_factory=dict)


class ProposalSubmitResponse(BaseModel):
    """Stable submission response (D-9)."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str
    plan: Optional[PlanResponse] = None
    idempotent: bool = False


class ProposalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    strategy_id: str
    account_id: str
    evaluation_id: str
    evaluation_kind: str
    job_id: Optional[str] = None
    strategy_run_id: str
    target_kind: str
    #: The envelope payload verbatim — the owner's own decision record.
    payload: Dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str
    status: str


class ProposalJournalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    evaluation_id: Optional[str] = None
    proposal_id: Optional[str] = None
    reason_code: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ProposalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: List[ProposalRow] = Field(default_factory=list)
    journal: List[ProposalJournalRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan execution (Phase 6 / Project 6, D-7)
# ---------------------------------------------------------------------------


class ExecutionStepOut(BaseModel):
    """One derived step outcome as the executor returned it."""

    model_config = ConfigDict(extra="forbid")

    step_no: int
    event: str
    paper_order_id: Optional[str] = None
    filled_quantity: Optional[int] = None
    refusal_reason: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class ExecutionResponse(BaseModel):
    """The result of one owner-triggered paper execution."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    status: str
    steps: List[ExecutionStepOut] = Field(default_factory=list)
    reservation_id: Optional[str] = None
    paper_order_ids: List[str] = Field(default_factory=list)


class ExecutionEventRow(BaseModel):
    """One append-only trail row (D-3): a fact, never a state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    plan_id: str
    step_no: int
    event: str
    paper_order_id: Optional[str] = None
    filled_quantity: Optional[int] = None
    refusal_reason: Optional[str] = None
    actor_id: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    created_at: Any = None


class ExecutionTrailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    events: List[ExecutionEventRow] = Field(default_factory=list)
