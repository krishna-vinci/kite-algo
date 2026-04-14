from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrategyFamily(str, Enum):
    OPTIONS = "options_strategy"
    INDICATOR = "indicator_strategy"
    INVESTMENT = "investment_strategy"
    DISCRETIONARY = "discretionary_strategy"


class ExecutionMode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    DRY_RUN = "dry_run"


class JournalRunStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    REVIEWED = "reviewed"


class ReviewState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWED = "reviewed"
    WAIVED = "waived"


class CapitalBasisType(str, Enum):
    CASH_DEPLOYED = "cash_deployed"
    MARGIN_USED = "margin_used"
    NOTIONAL = "notional"
    PORTFOLIO_NAV = "portfolio_nav"


class SourceType(str, Enum):
    LIVE_ORDER = "live_order"
    PAPER_TRADE = "paper_trade"
    PAPER_ORDER = "paper_order"
    OPTION_STRATEGY_RUN = "option_strategy_run"
    ALGO_INSTANCE = "algo_instance"
    INVESTING_STRATEGY = "investing_strategy"


class DecisionType(str, Enum):
    THESIS = "thesis"
    ENTRY = "entry"
    ADJUSTMENT = "adjustment"
    RISK_CHANGE = "risk_change"
    EXIT = "exit"
    ALGO_TRIGGER = "algo_trigger"
    REVIEW = "review"


class DecisionActorType(str, Enum):
    USER = "user"
    SYSTEM = "system"
    ALGO = "algo"


class RuleType(str, Enum):
    UNIVERSAL = "universal"
    STRATEGY_SPECIFIC = "strategy_specific"
    RISK_EXECUTION = "risk_execution"
    PSYCHOLOGICAL = "psychological"


class EnforcementLevel(str, Enum):
    HARD_BLOCK = "hard_block"
    SOFT_WARNING = "soft_warning"
    REVIEW_ONLY = "review_only"


class RuleStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    REINFORCED = "reinforced"
    DECAYING = "decaying"
    RETIRED = "retired"


class RuleEvidenceResult(str, Enum):
    FOLLOWED = "followed"
    VIOLATED = "violated"
    OVERRIDDEN = "overridden"
    NOT_APPLICABLE = "not_applicable"


class JournalBaseModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


class JournalRun(JournalBaseModel):
    id: Optional[str] = None
    strategy_family: StrategyFamily
    strategy_name: Optional[str] = None
    entry_surface: Optional[str] = None
    execution_mode: ExecutionMode
    account_ref: Optional[str] = None
    status: JournalRunStatus = JournalRunStatus.DRAFT
    benchmark_id: str = "NIFTY50"
    capital_basis_type: CapitalBasisType
    capital_committed: Optional[Decimal] = None
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: Optional[datetime] = None
    review_state: ReviewState = ReviewState.PENDING
    source_summary: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JournalRunLeg(JournalBaseModel):
    id: Optional[int] = None
    run_id: Optional[str] = None
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    leg_role: Optional[str] = None
    direction: Optional[str] = None
    opened_quantity: int = 0
    closed_quantity: int = 0
    net_quantity: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JournalSourceLink(JournalBaseModel):
    id: Optional[int] = None
    run_id: str
    source_type: SourceType
    source_key: str
    source_key_2: Optional[str] = None
    linked_at: datetime = Field(default_factory=_utcnow)

    @field_validator("source_key")
    @classmethod
    def _validate_source_key(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("source_key is required")
        return cleaned


class JournalExecutionFact(JournalBaseModel):
    id: Optional[int] = None
    run_id: str
    leg_id: Optional[int] = None
    source_type: SourceType
    source_fact_key: str
    order_id: Optional[str] = None
    trade_id: Optional[str] = None
    fill_timestamp: datetime = Field(default_factory=_utcnow)
    side: str
    quantity: int
    price: Decimal
    gross_cash_flow: Optional[Decimal] = None
    fees_amount: Decimal = Decimal("0")
    taxes_amount: Decimal = Decimal("0")
    slippage_amount: Decimal = Decimal("0")
    payload: Dict[str, Any] = Field(default_factory=dict)


class JournalDecisionEvent(JournalBaseModel):
    id: Optional[int] = None
    run_id: str
    decision_type: DecisionType
    actor_type: DecisionActorType
    occurred_at: datetime = Field(default_factory=_utcnow)
    summary: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)


class JournalRule(JournalBaseModel):
    id: Optional[str] = None
    family_scope: Optional[str] = None
    strategy_scope: Optional[str] = None
    title: str
    rule_type: RuleType
    enforcement_level: EnforcementLevel
    status: RuleStatus = RuleStatus.DRAFT
    version: int = 1
    description: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class JournalRuleEvidence(JournalBaseModel):
    id: Optional[int] = None
    run_id: str
    rule_id: str
    result: RuleEvidenceResult
    notes: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class JournalEquityPoint(JournalBaseModel):
    id: Optional[int] = None
    subject_type: str
    subject_id: str
    interval: str
    as_of: datetime
    starting_equity: Optional[Decimal] = None
    ending_equity: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    cash_flow: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    return_pct: Optional[Decimal] = None
    benchmark_return_pct: Optional[Decimal] = None
    excess_return_pct: Optional[Decimal] = None


class JournalMetricSnapshot(JournalBaseModel):
    id: Optional[int] = None
    subject_type: str
    subject_id: str
    window: str
    calc_version: str
    computed_at: datetime = Field(default_factory=_utcnow)
    metrics: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkDefinition(JournalBaseModel):
    benchmark_id: str
    name: str
    source_list: str = "Nifty50"
    instrument_token: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkDailyPrice(JournalBaseModel):
    benchmark_id: str
    trading_day: date
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Decimal
    daily_return: Optional[Decimal] = None
    source: str


class ProjectionState(JournalBaseModel):
    projector_name: str
    cursor: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_utcnow)
