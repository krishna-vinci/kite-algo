from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    strategy_run_id: str | None = None
    strategy_name: str
    product: Literal["MIS", "NRML"]
    legs: list["OptionExecutionLeg"]
    protection: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _apply_run_level_leg_defaults(self) -> "OptionRunCreateRequest":
        normalized: list[OptionExecutionLeg] = []
        for leg in self.legs:
            payload = leg.model_dump()
            if payload.get("product") is None:
                payload["product"] = self.product
            normalized.append(OptionExecutionLeg.model_validate(payload))
        self.legs = normalized
        self.metadata = dict(self.metadata or {})
        if self.protection is not None:
            self.protection = dict(self.protection)
        return self


class OptionExecutionLeg(BaseModel):
    model_config = ConfigDict(extra="allow")

    leg_id: str | None = None
    tradingsymbol: str
    transaction_type: str
    quantity: int
    exchange: str = "NFO"
    product: str | None = None
    instrument_token: int | None = None
    strike: float | None = None
    option_type: str | None = None
    expiry_key: str | None = None
    ltp: float | None = None
    lot_size: int | None = None
    lots: int | None = None
    order_type: str = "MARKET"
    price: float | None = None
    trigger_price: float | None = None
    market_protection: int | None = None
    exit_order_type: str | None = None
    exit_price: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize(self) -> "OptionExecutionLeg":
        self.leg_id = str(self.leg_id or f"leg_{uuid4().hex[:8]}")
        self.tradingsymbol = str(self.tradingsymbol).strip().upper()
        self.transaction_type = str(self.transaction_type).strip().upper()
        self.exchange = str(self.exchange or "NFO").strip().upper()
        self.product = None if self.product is None else str(self.product).strip().upper()
        self.order_type = str(self.order_type or "MARKET").strip().upper()
        self.exit_order_type = None if self.exit_order_type is None else str(self.exit_order_type).strip().upper()
        self.metadata = dict(self.metadata or {})
        return self


class OptionRunActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str | None = None
    execution_mode: str | None = None
    account_scope: str | None = None
    all_or_none: bool = False
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    order_results: list[dict[str, Any]] | None = None
    trade_results: list[dict[str, Any]] | None = None
    safety_token: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "OptionRunActionRequest":
        self.mode = None if self.mode is None else str(self.mode).strip()
        self.execution_mode = None if self.execution_mode is None else str(self.execution_mode).strip()
        self.account_scope = None if self.account_scope is None else str(self.account_scope).strip()
        self.idempotency_key = None if self.idempotency_key is None else str(self.idempotency_key).strip()
        self.metadata = dict(self.metadata or {})
        return self


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
            legs=[
                leg.model_dump(exclude_none=True)
                if isinstance(leg, OptionExecutionLeg)
                else deepcopy(dict(leg))
                for leg in request.legs
            ],
            protection=deepcopy(request.protection),
            metadata=deepcopy(request.metadata),
            status=status,
        )

    def __post_init__(self) -> None:
        raw_status = self.status.value if isinstance(self.status, OptionRunStatus) else str(self.status)
        canonical_status = _LEGACY_STATUS_ALIASES.get(raw_status, raw_status)
        self.status = OptionRunStatus(canonical_status).value
