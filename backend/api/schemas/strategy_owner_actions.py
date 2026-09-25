"""Owner-action schemas for one hosted strategy (B2.6b S1).

Field names and shapes here are the binding API contract (design note
``documents/hosted-owner-actions-b2-6b-design-2026-09-25.md`` §5): the options UI
is built against them, so a rename is a breaking change. Everything is derived
server-side - the client sends a digest, a reason and (for a dead submission) a
disposition, never an account, an environment or a quantity.

Nothing here reports a value the platform cannot prove: an unproven order is
``ineligible`` with a named ``reason_code`` rather than a zero, and a route that
cannot read its evidence refuses by name.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class PendingWorkItemResponse(BaseModel):
    """One previewed pending-entry candidate, exactly as the UI lists it."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    step_no: int
    order_id: Optional[str] = None
    remaining_quantity: int = 0
    #: ``eligible`` | ``ineligible``.
    eligibility: str
    #: The named reason this candidate is not cancellable, or ``None``.
    reason_code: Optional[str] = None


class PendingWorkResponse(BaseModel):
    """``GET .../owner-actions/pending-work``.

    ``coverage`` is the read's completeness verdict: ``unknown`` means the list is
    NOT the complete set of the strategy's pending entry work, so the UI must say
    so rather than render "nothing to cancel".
    """

    model_config = ConfigDict(extra="forbid")

    coverage: str = "unknown"
    evidence_digest: str
    items: List[PendingWorkItemResponse] = Field(default_factory=list)


class CancelPendingRequest(BaseModel):
    """The digest the owner actually read, plus their reason."""

    model_config = ConfigDict(extra="forbid")

    evidence_digest: str
    reason: str


class OwnerActionItemResponse(BaseModel):
    """What one item of an owner action actually did."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    step_no: int
    order_id: Optional[str] = None
    #: The preview's own verdict for this item (``eligible`` | ``ineligible``),
    #: echoed so a POST result reads with the same vocabulary as the preview it
    #: came from - ``eligible`` + ``blocked`` is a real, explainable pair.
    eligibility: str = "eligible"
    #: ``cancelled`` | ``already_cancelled`` | ``skipped`` | ``blocked``.
    outcome: str
    filled_quantity: int = 0
    remaining_quantity: int = 0
    #: ``owner_cancelled`` for a cancel; the disposition for a dead submission.
    disposition: Optional[str] = None
    #: The option run's status after the action, when a run was involved.
    run_status: Optional[str] = None
    reason_code: Optional[str] = None


class OwnerActionResponse(BaseModel):
    """The shared shape every owner-action POST answers with (§5)."""

    model_config = ConfigDict(extra="forbid")

    #: ``complete`` | ``accepted`` | ``blocked``.
    status: str
    action_id: str
    evidence_digest: str
    items: List[OwnerActionItemResponse] = Field(default_factory=list)
    refusal: Optional[str] = None
    audit_id: Optional[str] = None


class DeadSubmissionResponse(BaseModel):
    """``GET .../plans/{plan_id}/steps/{step_no}/dead-submission``.

    The evidence is read from the paper order / progress row or the broker order
    projection; an owner cannot type an outcome into existence.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    step_no: int
    execution_environment: str = ""
    #: ``submitted`` | ``partially_filled`` | ``failed`` | ... - the trail's word.
    trail_state: str
    #: ``paper_order`` | ``broker_order`` | ``plan_trail``.
    source: str
    #: The platform status of the linked order, or the trail word when none.
    status: str
    order_id: Optional[str] = None
    requested_quantity: Optional[int] = None
    filled_quantity: int = 0
    remaining_quantity: int = 0
    allowed_dispositions: List[str] = Field(default_factory=list)
    evidence_digest: str


class DeadSubmissionDispositionRequest(BaseModel):
    """The disposition the owner chose, pinned to the evidence they read."""

    model_config = ConfigDict(extra="forbid")

    evidence_digest: str
    disposition: str
    reason: str
