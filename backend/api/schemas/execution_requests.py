"""Run-bound execution-request and owned-work schemas (Phase 2).

Everything here is derived: the strategy, owner, account and environment come
from the run's persisted binding and its hosting job, never from the payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RunExecutionRequestCreate(BaseModel):
    """Ask the platform to execute one already-frozen plan.

    ``strategy_run_id`` must be the caller's own run (the route verifies the
    token/attempt/session); ``plan_id`` must be a plan of that run's strategy.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=160)


class RunExecutionRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    strategy_id: str
    strategy_run_id: str
    account_id: str
    execution_environment: str
    plan_id: str
    plan_hash: str
    authorization_mode: str
    grant_id: Optional[str] = None
    status: str
    refusal_code: Optional[str] = None
    refusal_detail: Dict[str, Any] = Field(default_factory=dict)
    decision_kind: Optional[str] = None
    decision_at: Optional[datetime] = None
    approval_id: Optional[str] = None
    reservation_id: Optional[str] = None
    execution_detail: Dict[str, Any] = Field(default_factory=dict)
    version_id: str
    attempt: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    #: ``terminal`` means "this request will not be dispatched again", never
    #: "the trade finished": ``outcome_state`` carries the executor's own word.
    terminal: bool = False
    executable: bool = False
    outcome_state: Optional[str] = None
    idempotent: bool = False
    #: What the caller should do next, in words, derived from the state above.
    next_action: str = ""


class RunExecutionRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_run_id: str
    requests: List[RunExecutionRequestResponse] = Field(default_factory=list)


class OwnedPositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_kind: str
    identity_key: str
    product: str
    instrument_token: int
    exchange: str
    tradingsymbol: str
    net_quantity: int
    unresolved_reason: Optional[str] = None


class PendingWorkRow(BaseModel):
    """One unit of work the strategy has already asked for but not settled.

    ``submitted_quantity``, ``filled_quantity`` and ``remaining_quantity`` are
    reported separately so a fill is never counted as pending work twice, and one
    coordinate (``plan_id`` + ``step_no``) appears ONCE: a live step's plan-trail
    event and its live claim are merged, with the contributing records named in
    ``sources``. ``coverage='unknown'`` means the platform has no - or
    contradictory - quantity evidence for this unit; it is never reported as zero
    work.
    """

    model_config = ConfigDict(extra="forbid")

    source: str  # live_submission | plan_execution | execution_request
    sources: List[str] = Field(default_factory=list)
    plan_id: Optional[str] = None
    step_no: Optional[int] = None
    state: str
    execution_environment: str
    submitted_quantity: Optional[float] = None
    filled_quantity: Optional[float] = None
    remaining_quantity: Optional[float] = None
    coverage: str = "known"
    reason: Optional[str] = None
    #: The step's own instrument, product and direction, so a caller can act on
    #: this row without guessing which leg it is.
    instrument_id: Optional[str] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    side: Optional[str] = None
    lane: Optional[str] = None
    depends_on: List[int] = Field(default_factory=list)
    release_rule: Optional[str] = None
    attribution_source: Optional[str] = None
    #: What the merged sources said about each other (both states, and any
    #: disagreement that forced ``coverage='unknown'``).
    confidence: Dict[str, Any] = Field(default_factory=dict)
    detail: Dict[str, Any] = Field(default_factory=dict)


class ProjectionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: bool
    coverage: str  # published | unpublished_unknown
    projection_version: int = 0
    content_sha256: Optional[str] = None
    last_rebuild_at: Optional[datetime] = None
    #: How old the published projection is, and whether it is inside the
    #: freshness bound. A projection whose freshness cannot be proven makes the
    #: overall coverage ``unknown``.
    age_seconds: Optional[float] = None
    fresh: bool = False


class OwnedOptionRunRow(BaseModel):
    """One option run this strategy owns, from its own bound attempts."""

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    plan_ids: List[str] = Field(default_factory=list)
    originating_plan_id: str
    #: The phase of the plan that OPENED this run (``entry``/``exit``). Distinct
    #: from ``phase`` above, which is the phase of the edge that first reported
    #: the run in plan-id order.
    originating_phase: str = ""
    phase: str
    worker_run_id: Optional[str] = None
    underlying: str = ""
    expiry: str = ""
    structure_id: str = ""
    #: The FROZEN structure identity of the plan that opened this run. A strategy
    #: compares it (or, absent it, the leg identities) to tell "the structure I
    #: already hold" from "a different structure".
    structure_digest: str = ""
    expiry_policy: str = ""
    product: str = ""
    status: str = "unknown"
    legs: List[Dict[str, Any]] = Field(default_factory=list)
    completed_legs: List[str] = Field(default_factory=list)
    pending_legs: List[str] = Field(default_factory=list)
    failed_legs: List[str] = Field(default_factory=list)
    #: Whether the run still owns a protective exit stage the platform committed
    #: and has not resolved. It is in-flight work, not a settled outcome.
    protective_exit_unresolved: bool = False
    coverage: str = "known"


class OwnedOptionRunsCoverage(BaseModel):
    """Whether the option-run set above is complete, and why not if it is not."""

    model_config = ConfigDict(extra="forbid")

    coverage: str = "unknown"
    count: int = 0
    truncated: bool = False
    reason: str = ""


class OwnedPositionsResponse(BaseModel):
    """The run-bound view of its canonical strategy's own book and pending work.

    This is an interface onto the existing attributed books and execution links,
    not another ledger: ``positions`` is the strategy's published projection and
    ``pending`` is its outstanding execution work. An unpublished projection is
    reported ``unpublished_unknown`` rather than as a flat book.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_run_id: str
    strategy_id: str
    account_id: str
    execution_environment: str
    projection: ProjectionPublication
    positions: List[OwnedPositionRow] = Field(default_factory=list)
    pending: List[PendingWorkRow] = Field(default_factory=list)
    #: This strategy's own option runs, discovered from its bound attempts. An
    #: unreadable set is reported as unknown coverage rather than as empty.
    option_runs: List["OwnedOptionRunRow"] = Field(default_factory=list)
    option_runs_coverage: "OwnedOptionRunsCoverage" = Field(
        default_factory=lambda: OwnedOptionRunsCoverage()
    )
    coverage: str  # known | unknown
    observed_at: datetime
    notes: List[str] = Field(default_factory=list)
