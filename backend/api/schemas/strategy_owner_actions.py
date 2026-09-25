"""Owner-action schemas for one hosted strategy (B2.6b S1 and S2).

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

from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# owner exit of ONE option run (B2.6b S2, §5)
# ---------------------------------------------------------------------------


class OptionRunExitPlanLeg(BaseModel):
    """One bounded, risk-reducing close the staged structure exit would submit.

    Shorts come first; a hedge leg appears only once every short the run holds is
    PROVEN closed by confirmed fills.
    """

    model_config = ConfigDict(extra="forbid")

    tradingsymbol: str
    transaction_type: str
    quantity: int
    exchange: Optional[str] = None
    product: Optional[str] = None
    order_type: Optional[str] = None


class OptionRunExitResponse(BaseModel):
    """``GET .../option-runs/{option_run_id}/exit``.

    ``state`` is DERIVED from the run's own confirmed fills: ``flat`` (nothing
    held - the exit is complete), ``residual`` (some leg is still open - the
    ``close_plan`` is the next stage), ``ambiguous`` (the platform cannot explain
    the run from its own evidence) or ``not_repairable`` (a status an exit may not
    act on). ``evidence_digest`` is what a POST must still match.
    """

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    #: The run's own durable status (``entered``, ``exiting``, ...).
    status: str
    state: str
    reason_code: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    #: The adjust takeover rule's word for a run an adjust owns; ``finished``
    #: when no adjust can be in flight.
    adjust_owner_state: str = "unknown"
    adjust_owner_reason: Optional[str] = None
    #: ``resolved``, or the unresolved stage claim's own state.
    protective_stage_state: str = "resolved"
    close_plan: List[OptionRunExitPlanLeg] = Field(default_factory=list)
    #: Whether every short the run holds is proven closed by its own fills.
    shorts_proven_closed: bool = False
    #: The short quantity that is still uncovered - why hedges stay withheld.
    naked_short_quantity: int = 0
    withheld_hedges: List[Dict[str, Any]] = Field(default_factory=list)
    #: ``orders_outstanding`` | ``shorts_not_proven_closed`` |
    #: ``no_permitted_action`` when a residual run has nothing to submit YET,
    #: else ``None``. Waiting is expected; it is not ambiguity.
    waiting_reason: Optional[str] = None
    evidence_digest: str


class OptionRunExitRequest(BaseModel):
    """The digest the owner actually read, plus their reason."""

    model_config = ConfigDict(extra="forbid")

    evidence_digest: str
    reason: str


class OptionRunExitItemResponse(BaseModel):
    """One leg of the stage an exit POST actually submitted."""

    model_config = ConfigDict(extra="forbid")

    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_id: Optional[str] = None
    client_order_ref: Optional[str] = None
    stage_digest: Optional[str] = None
    #: ``submitted`` | ``unknown`` - a leg without a broker reference is never
    #: reported as submitted.
    state: str
    reason_code: Optional[str] = None


class OptionRunExitActionResponse(BaseModel):
    """``POST .../option-runs/{option_run_id}/exit`` (§5).

    ``status`` is ``accepted`` for one submitted stage, ``complete`` when the
    run's own fills are flat (and the run is ``exited``), and ``blocked`` when a
    stage could not be submitted - ``refusal`` then names why, and the run is
    left in the durable state the refusal describes.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    action_id: str
    option_run_id: str
    run_status: str
    state: str
    evidence_digest: str
    items: List[OptionRunExitItemResponse] = Field(default_factory=list)
    refusal: Optional[str] = None
    audit_id: Optional[str] = None
    submission: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# flatten of the whole strategy (B2.6b S3, sections 3 and 5)
# ---------------------------------------------------------------------------


class FlattenRequest(BaseModel):
    """``POST .../owner-actions/flatten``.

    The owner supplies a reason and whether flatten may stop the evaluator itself.
    Never an account, an environment, a quantity or a plan: what flatten does is
    derived from the strategy's own durable evidence.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    #: Stop the evaluator first (section 3 step 1). The default is the safe one;
    #: flatten refuses ``FLATTEN_EVALUATION_ACTIVE`` when an active evaluation
    #: cannot be PROVEN stopped either way.
    stop_evaluator: bool = True


class FlattenItemResponse(BaseModel):
    """One manifest item: what flatten did, or still has to do, for one unit.

    ``kind`` is ``cancel_pending`` (one plan step's pending entry work),
    ``option_exit`` (ONE option run's staged exit) or ``nonoption_reduction``
    (ONE instrument+product book). ``state`` is ``done``, ``in_progress`` (a stage
    is working or waiting on fills), ``blocked`` (``reason_code`` says why) or
    ``pending``. Nothing here is optimistic: an item is ``done`` only when its own
    evidence is terminal.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    key: str
    state: str
    reason_code: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class FlattenStopResponse(BaseModel):
    """The evaluator-stop evidence the flatten was gated on (section 3 step 1)."""

    model_config = ConfigDict(extra="forbid")

    requested: bool = False
    #: ``confirmed`` | ``unproven`` | ``none``.
    state: str = "none"
    jobs: List[Dict[str, Any]] = Field(default_factory=list)
    approvals: List[str] = Field(default_factory=list)
    requested_by: Optional[str] = None
    reason: str = ""


class FlattenResponse(BaseModel):
    """``POST``/``GET .../owner-actions/flatten``.

    ``status`` is the OPERATION's verdict: ``complete`` only while every section 3
    done condition holds, otherwise ``in_progress`` (waiting on fills) or
    ``blocked`` (``refusal`` names the item that stopped). ``operation_id`` is the
    durable handle: posting again resumes it, and the outcomes already recorded
    are preserved. ``missing`` lists the done conditions that are not satisfied
    yet, by name.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    action_id: str
    operation_id: str
    evidence_digest: str
    stop: FlattenStopResponse = Field(default_factory=FlattenStopResponse)
    items: List[FlattenItemResponse] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    done_conditions: Dict[str, bool] = Field(default_factory=dict)
    refusal: Optional[str] = None
    audit_id: Optional[str] = None
