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


class JournalEnvironmentMode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    DRY_RUN_PREVIEW = "dry_run_preview"


class JournalEpisodeStatus(str, Enum):
    DRAFT = "draft"
    OPENING = "opening"
    OPEN = "open"
    REDUCING = "reducing"
    FLAT_PENDING_CONFIRMATION = "flat_pending_confirmation"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    UNRESOLVED = "unresolved"


class JournalIntentChannel(str, Enum):
    ENTRY = "entry"
    ADJUSTMENT = "adjustment"
    EXIT = "exit"
    PROTECTION = "protection"
    MANUAL = "manual"


class JournalNoteType(str, Enum):
    THESIS = "thesis"
    PRE_ENTRY_CHECKLIST = "pre_entry_checklist"
    RISK_PLAN = "risk_plan"
    MARKET_CONTEXT = "market_context"
    EXECUTION_RATIONALE = "execution_rationale"
    ADJUSTMENT_RATIONALE = "adjustment_rationale"
    EXIT_RATIONALE = "exit_rationale"
    POST_EXIT_REVIEW = "post_exit_review"
    LESSON = "lesson"
    PSYCHOLOGY = "psychology"
    EXPERIMENT = "experiment"
    OPS_BUG = "ops_bug"
    STRATEGY_IMPROVEMENT = "strategy_improvement"
    RULE_CANDIDATE = "rule_candidate"


class JournalTimelineActorType(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ALGO = "algo"


class JournalEpisodeLegDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


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
    LIVE_FILL = "live_fill"
    BROKER_IMPORT = "broker_import"
    PAPER_STRATEGY_RUN = "paper_strategy_run"
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


class JournalExecutionEnvironment(JournalBaseModel):
    id: Optional[str] = None
    mode: JournalEnvironmentMode
    account_scope: str
    broker_user_id: Optional[str] = None
    paper_account_key: Optional[str] = None
    environment_epoch: int = 1
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    retired_at: Optional[datetime] = None

    @field_validator("account_scope")
    @classmethod
    def _validate_account_scope(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("account_scope is required")
        return cleaned

    @field_validator("environment_epoch")
    @classmethod
    def _validate_environment_epoch(cls, value: int) -> int:
        if value < 1:
            raise ValueError("environment_epoch must be >= 1")
        return value


class JournalStrategyTemplate(JournalBaseModel):
    id: Optional[str] = None
    strategy_family: str
    template_key: str
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("template_key")
    @classmethod
    def _validate_template_key(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("template_key is required")
        return cleaned


class JournalStrategyVariant(JournalBaseModel):
    id: Optional[str] = None
    template_id: str
    variant_key: str
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class JournalStrategyDeployment(JournalBaseModel):
    id: Optional[str] = None
    template_id: str
    variant_id: Optional[str] = None
    deployment_key: str
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class JournalExecutionContext(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    source_system: str
    external_run_id: str
    strategy_template_id: Optional[str] = None
    strategy_variant_id: Optional[str] = None
    strategy_deployment_id: Optional[str] = None
    status: str = "active"
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("external_run_id")
    @classmethod
    def _validate_external_run_id(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("external_run_id is required")
        return cleaned


class JournalEpisode(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    execution_context_id: str
    episode_seq: int
    status: JournalEpisodeStatus = JournalEpisodeStatus.DRAFT
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("episode_seq")
    @classmethod
    def _validate_episode_seq(cls, value: int) -> int:
        if value < 1:
            raise ValueError("episode_seq must be >= 1")
        return value


class JournalEpisodeLeg(JournalBaseModel):
    id: Optional[int] = None
    episode_id: str
    leg_seq: int = 1
    instrument_token: Optional[int] = None
    exchange: Optional[str] = None
    tradingsymbol: Optional[str] = None
    product: Optional[str] = None
    direction: Optional[JournalEpisodeLegDirection] = None
    opened_quantity: int = 0
    closed_quantity: int = 0
    net_quantity: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class JournalExecutionIntent(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    execution_context_id: Optional[str] = None
    episode_id: Optional[str] = None
    channel: Optional[JournalIntentChannel] = None
    intent_type: Optional[str] = None
    idempotency_key: Optional[str] = None
    status: str = "pending"
    requested_at: datetime = Field(default_factory=_utcnow)
    resolved_at: Optional[datetime] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("idempotency_key cannot be blank")
        return cleaned


class JournalTimelineEvent(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    episode_id: Optional[str] = None
    execution_context_id: Optional[str] = None
    subject_type: str
    subject_id: str
    channel: Optional[str] = None
    event_type: str
    actor_type: JournalTimelineActorType = JournalTimelineActorType.SYSTEM
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    occurred_at: datetime = Field(default_factory=_utcnow)
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("subject_type", "subject_id", "event_type")
    @classmethod
    def _validate_required_nonblank_fields(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("field cannot be blank")
        return cleaned


class JournalNote(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    subject_type: str
    subject_id: str
    episode_id: Optional[str] = None
    note_type: JournalNoteType
    title: str
    body_markdown: str
    body_text: str = ""
    body_json: Optional[Dict[str, Any]] = None
    effective_at: Optional[datetime] = None
    author_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    archived_at: Optional[datetime] = None

    @field_validator("subject_type", "subject_id", "title", "body_markdown")
    @classmethod
    def _validate_note_required_nonblank_fields(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("field cannot be blank")
        return cleaned


class JournalNoteRevision(JournalBaseModel):
    id: Optional[int] = None
    note_id: str
    revision_no: int
    body_markdown: str
    body_text: str = ""
    editor_id: Optional[str] = None
    edited_at: datetime = Field(default_factory=_utcnow)
    change_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("revision_no")
    @classmethod
    def _validate_revision_no(cls, value: int) -> int:
        if value < 1:
            raise ValueError("revision_no must be >= 1")
        return value

    @field_validator("body_markdown")
    @classmethod
    def _validate_revision_body_markdown(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("body_markdown cannot be blank")
        return cleaned


class JournalAttachment(JournalBaseModel):
    id: Optional[str] = None
    environment_id: str
    subject_type: str
    subject_id: str
    note_id: Optional[str] = None
    storage_key: str
    mime_type: str
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    ocr_text: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("subject_type", "subject_id", "storage_key", "mime_type")
    @classmethod
    def _validate_attachment_required_nonblank_fields(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("field cannot be blank")
        return cleaned


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
    environment_id: Optional[str] = None
    episode_id: Optional[str] = None
    intent_id: Optional[str] = None
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
    position_effect: Optional[str] = None
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
    environment_id: Optional[str] = None
    subject_type: str
    subject_id: str
    window: str
    calc_version: str
    identity_rule_version: str = "v1_legacy"
    grouping_rule_version: str = "v1_legacy"
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
