"""Read-only owner schemas for a strategy's option runs (B2.6a).

Field names and shapes here are the binding API contract: the options UI is
built against them, so a rename is a breaking change. Everything is derived
server-side - the client never sends the owning account or environment, and no
field here is an input.

Missing data is reported by NAME (``coverage`` / ``available`` + ``reason``)
rather than as a zero, an empty list or a fabricated number.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OptionRunLegResponse(BaseModel):
    """One leg of an option structure.

    ``quantity`` is the frozen TARGET quantity the run converges to;
    ``own_open_quantity`` is the signed quantity the run's OWN confirmed fills
    currently hold (``None`` when that evidence could not be read - never ``0``,
    which would claim the leg is flat).
    """

    model_config = ConfigDict(extra="forbid")

    leg_id: str
    tradingsymbol: str
    #: ``BUY`` | ``SELL`` - the side stays the authority; ``role`` is coverage.
    side: str
    #: ``hedge`` | ``short`` | ``naked`` | ``None`` (frozen on the plan leg).
    role: Optional[str] = None
    ratio: int = 1
    quantity: int = 0
    own_open_quantity: Optional[int] = None
    #: ``open`` | ``pending`` | ``failed`` | ``flat``.
    state: str


class OptionRunResponse(BaseModel):
    """One option run as the options UI shows it."""

    model_config = ConfigDict(extra="forbid")

    option_run_id: str
    status: str
    structure_generation: int = 1
    structure_digest: str = ""
    underlying: str = ""
    expiry: str = ""
    product: str = ""
    protective_exit_unresolved: bool = False
    #: Per-run coverage. ``unknown`` means the run's own state could not be read;
    #: the UI must say so rather than render an empty structure.
    coverage: str = "unknown"
    legs: List[OptionRunLegResponse] = Field(default_factory=list)
    #: ``status in partial_entry|partial_exit|cleanup_required|adjusting`` -
    #: DERIVED so the UI never restates the rule and drifts from the backend.
    repairable: bool = False
    #: Placeholder until B2.4 lands protection ownership; the UI shows
    #: "protection owner: not yet available" while this is ``None``.
    protection_owner: Optional[str] = None


class OptionRunListResponse(BaseModel):
    """Every option run this strategy owns for its derived scope."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    #: ``known`` | ``unknown`` - unknown means this list is NOT complete.
    coverage: str
    coverage_reason: str = ""
    runs: List[OptionRunResponse] = Field(default_factory=list)


class OptionRunEdgeResponse(BaseModel):
    """One ``strategy_plan_option_runs`` edge, oldest first."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    #: ``entry`` | ``exit`` | ``adjust``.
    phase: str
    created_at: Optional[str] = None


class OptionRunFrozenResponse(BaseModel):
    """The policies and limits frozen on the run's plan - values may be null."""

    model_config = ConfigDict(extra="forbid")

    protection_policy: Optional[Dict[str, Any]] = None
    max_loss: Optional[Dict[str, Any]] = None
    expiry_policy: Optional[str] = None


class OptionRunRefusalResponse(BaseModel):
    """One named refusal of an option-structure plan for this strategy."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    plan_id: str
    refusal_code: str
    #: ``request`` | ``approval`` | ``dispatch_claim`` | ``dispatch_boundary`` |
    #: ``preparation`` | ``execution`` (``None`` when the record carries none).
    stage: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    at: Optional[str] = None


class OptionRunGreeksResponse(BaseModel):
    """Run-level greeks, or the named reason they are unavailable.

    No existing read derives greeks for an option RUN (the chain service derives
    them per contract for a live session), so this stays ``available: false``
    with nulls rather than aggregating something the platform cannot prove.
    """

    model_config = ConfigDict(extra="forbid")

    available: bool = False
    reason: str = "no_reusable_read"
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


class OptionRunPnlResponse(BaseModel):
    """Run premium / mark-to-market, or the named reason they are unavailable."""

    model_config = ConfigDict(extra="forbid")

    available: bool = False
    reason: str = "no_reusable_read"
    premium: Optional[float] = None
    mtm: Optional[float] = None


class OptionRunDetailResponse(BaseModel):
    """One option run, plus the evidence around it."""

    model_config = ConfigDict(extra="forbid")

    run: OptionRunResponse
    edges: List[OptionRunEdgeResponse] = Field(default_factory=list)
    frozen: OptionRunFrozenResponse = Field(default_factory=OptionRunFrozenResponse)
    #: Newest first, capped at 20.
    refusals: List[OptionRunRefusalResponse] = Field(default_factory=list)
    greeks: OptionRunGreeksResponse = Field(default_factory=OptionRunGreeksResponse)
    pnl: OptionRunPnlResponse = Field(default_factory=OptionRunPnlResponse)
