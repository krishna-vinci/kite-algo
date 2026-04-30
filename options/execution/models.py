from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Literal

from pydantic import BaseModel, ConfigDict, Field


class OptionRunStatus(str, Enum):
    CREATED = "created"
    ENTRY_PREVIEWED = "entry_previewed"
    ENTERING = "entering"
    ENTERED = "entered"
    PARTIAL_ENTRY = "partial_entry"
    CLEANUP_REQUIRED = "cleanup_required"
    EXIT_PREVIEWED = "exit_previewed"
    EXITING = "exiting"
    PARTIAL_EXIT = "partial_exit"
    EXITED = "exited"


class OptionRunCreateRequest(BaseModel):
    """Canonical run creation contract for option strategy execution."""

    model_config = ConfigDict(extra="allow")

    strategy_name: str
    product: Literal["MIS", "NRML"]
    legs: list[dict[str, Any]]
    protection: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


_LEGACY_STATUS_ALIASES: dict[str, str] = {
    "not_started": OptionRunStatus.CREATED.value,
    "partially_entered": OptionRunStatus.PARTIAL_ENTRY.value,
    "exit_pending": OptionRunStatus.EXITING.value,
    "partially_exited": OptionRunStatus.PARTIAL_EXIT.value,
    "closed": OptionRunStatus.EXITED.value,
}


@dataclass
class OptionRunState:
    status: str = OptionRunStatus.CREATED.value

    strategy_run_id: str = ""
    strategy_name: str = ""
    product: Literal["MIS", "NRML"] | None = None
    legs: List[dict[str, Any]] = field(default_factory=list)
    protection: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    completed_legs: List[str] = field(default_factory=list)
    failed_legs: List[str] = field(default_factory=list)
    pending_legs: List[str] = field(default_factory=list)
    orders: List[dict[str, Any]] = field(default_factory=list)
    trades: List[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_create_request(
        cls,
        request: OptionRunCreateRequest,
        *,
        strategy_run_id: str,
        status: OptionRunStatus | str = OptionRunStatus.CREATED,
    ) -> "OptionRunState":
        return cls(
            strategy_run_id=strategy_run_id,
            strategy_name=request.strategy_name,
            product=request.product,
            legs=deepcopy(request.legs),
            protection=deepcopy(request.protection),
            metadata=deepcopy(request.metadata),
            status=status,
        )

    def __post_init__(self) -> None:
        raw_status = self.status.value if isinstance(self.status, OptionRunStatus) else str(self.status)
        canonical_status = _LEGACY_STATUS_ALIASES.get(raw_status, raw_status)
        self.status = OptionRunStatus(canonical_status).value
