from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

_DEFAULT_WORKER_ACTIONS = [
    "gtt:read",
    "gtt:write",
    "runs:create",
    "runs:read",
    "runs:log",
    "intents:submit",
    "risk:update",
    "runs:exit",
    "heartbeat",
    "market:read",
    "market:stream",
    "funds:read",
]

class WorkerTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    account_scope: Optional[str] = None
    allowed_modes: List[str] = Field(default_factory=lambda: ["paper", "dry_run"])
    allowed_actions: List[str] = Field(default_factory=lambda: sorted(_DEFAULT_WORKER_ACTIONS))
    allowed_templates: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_modes", "allowed_actions", "allowed_templates")
    @classmethod
    def _clean_list(cls, value: List[str]) -> List[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("allowed_modes")
    @classmethod
    def _normalize_modes(cls, value: List[str]) -> List[str]:
        return [item.lower() for item in value]

class WorkerTokenCreateResponse(BaseModel):
    token_id: str
    token: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    expires_at: Optional[datetime]

class WorkerTokenView(BaseModel):
    token_id: str
    name: str
    account_scope: Optional[str]
    allowed_modes: List[str]
    allowed_actions: List[str]
    allowed_templates: List[str]
    status: str
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

class WorkerHeartbeatRequest(BaseModel):
    worker_id: Optional[str] = None
    status: str = "healthy"
    metrics: Dict[str, Any] = Field(default_factory=dict)

class WorkerRunCreateRequest(BaseModel):
    strategy_run_id: Optional[str] = None
    template_id: str = Field(min_length=1)
    account_scope: str = Field(min_length=1)
    execution_mode: str = "paper"
    summary_fields: List[Dict[str, Any]] = Field(default_factory=list)
    risk_schema: List[Dict[str, Any]] = Field(default_factory=list)
    allowed_actions: List[str] = Field(default_factory=lambda: ["edit_risk", "exit_strategy"])
    runtime_state: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("execution_mode")
    @classmethod
    def _clean_mode(cls, value: str) -> str:
        return str(value or "paper").strip().lower()


class WorkerRunSummary(BaseModel):
    """Public, token-scoped run listing fields.

    Deliberately excludes worker token identifiers, metadata, and session
    nonces.  The detail route remains the place for the richer run document.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_run_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    account_scope: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    status: str = Field(min_length=1)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class WorkerRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[WorkerRunSummary] = Field(default_factory=list)
    next_cursor: Optional[str] = None

class WorkerRiskPatchRequest(BaseModel):
    patch: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None

class WorkerProtectionPatchRequest(BaseModel):
    backend_protection: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None
    reset_trailing: bool = True

class WorkerIntentRequest(BaseModel):
    intent_type: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=160)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    safety_token: str | None = None

class WorkerExitRequest(BaseModel):
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    dry_run: bool = False

class WorkerOrderActionRequest(BaseModel):
    strategy_run_id: str = Field(min_length=1)
    variety: str = "regular"
    parent_order_id: Optional[str] = None

class WorkerOrderModifyRequest(WorkerOrderActionRequest):
    order_type: Optional[str] = None
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    quantity: Optional[int] = Field(None, gt=0)
    validity: Optional[str] = None
    validity_ttl: Optional[int] = None

    def to_modify_request(self) -> Any:
        from backend.broker_api.orders import ModifyOrderRequest

        return ModifyOrderRequest.model_validate(
            self.model_dump(
                exclude_none=True,
                exclude={"strategy_run_id", "variety", "parent_order_id"},
            )
        )

class WorkerOrderPreviewRequest(BaseModel):
    order: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class WorkerBasketPreviewRequest(BaseModel):
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    all_or_none: bool = False
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class WorkerBracketCreateRequest(BaseModel):
    entry_order: Dict[str, Any] = Field(default_factory=dict)
    stoploss: Dict[str, Any] = Field(default_factory=dict)
    target: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class WorkerDecisionEventRequest(BaseModel):
    decision_type: Literal[
        "signal",
        "entry",
        "exit",
        "risk_update",
        "management",
        "protection_ack",
        "note",
    ]
    action: Literal[
        "enter",
        "exit",
        "hold",
        "skip",
        "modify",
        "cancel",
        "update_protection",
        "arm_bracket",
        "rebalance",
        "observe",
    ]
    summary: str = Field(min_length=1, max_length=500)
    related_resource_type: Optional[Literal["basket_execution", "bracket_intent", "worker_live_execution_link"]] = None
    related_resource_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    basket_execution_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("related_resource_id", "basket_execution_id")
    @classmethod
    def _trim_optional_ids(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        return str(value).strip()

class WorkerRunPnlTotals(BaseModel):
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0

class WorkerRunPnlLeg(BaseModel):
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    net_quantity: int = 0
    side: str = "FLAT"
    average_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    broker_net_quantity: Optional[int] = None
    is_stale: bool = False
    last_reconciled_at: Optional[str] = None

class WorkerRunPnlSnapshot(BaseModel):
    strategy_run_id: str
    execution_mode: str
    status: str
    currency: str = "INR"
    totals: WorkerRunPnlTotals = Field(default_factory=WorkerRunPnlTotals)
    legs: List[WorkerRunPnlLeg] = Field(default_factory=list)
    position_count: int = 0
    is_realtime: bool = False
    is_stale: bool = False
    updated_at: str

class WorkerFundsSegment(BaseModel):
    net: float = 0.0
    available_cash: float = 0.0
    opening_balance: float = 0.0
    live_balance: Optional[float] = None
    collateral: Optional[float] = None
    utilised: float = 0.0
    m2m_realised: float = 0.0
    m2m_unrealised: float = 0.0

class WorkerFundsSnapshot(BaseModel):
    account_scope: str
    mode: str
    currency: str = "INR"
    source: str
    segments: Dict[str, WorkerFundsSegment] = Field(default_factory=dict)
    allocation: Dict[str, Any] = Field(default_factory=dict)
    stale: bool = False
    updated_at: str

def _parse_csv_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]

def _parse_csv_int_values(value: Optional[str], *, field_name: str) -> List[int]:
    parsed: List[int] = []
    for item in _parse_csv_values(value):
        try:
            numeric = int(item)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"{field_name} must contain comma-separated integers") from None
        if numeric <= 0 or numeric > 9_999_999_999:
            raise HTTPException(status_code=422, detail=f"{field_name} contains an out-of-range instrument token")
        parsed.append(numeric)
    return parsed
