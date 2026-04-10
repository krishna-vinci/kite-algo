from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrategyExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class StrategyFamily(str, Enum):
    DIRECTIONAL = "directional"
    NEUTRAL_SHORT_PREMIUM = "neutral-short-premium"
    LONG_VOL = "long-vol"
    PREMIUM_MANAGED_STRUCTURE = "premium-managed-structure"


class MetricKind(str, Enum):
    INDEX_PRICE = "index_price"
    COMBINED_PREMIUM_POINTS = "combined_premium_points"
    BASKET_MTM_RUPEES = "basket_mtm_rupees"


class RuleRole(str, Enum):
    EMERGENCY_GUARD = "emergency_guard"
    HARD_STOP = "hard_stop"
    PROFIT_TARGET = "profit_target"
    TRAILING_STOP = "trailing_stop"


class InputGroup(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    EMERGENCY = "emergency"


class SelectedOptionLeg(BaseModel):
    model_config = ConfigDict(extra="allow")

    instrument_token: int
    tradingsymbol: str
    strike: float
    option_type: Literal["CE", "PE"]
    transaction_type: Literal["BUY", "SELL"]
    ltp: float = Field(ge=0)
    lot_size: int = Field(default=1, ge=1)
    lots: int = Field(default=1, ge=1)
    quantity: Optional[int] = Field(default=None, ge=1)
    expiry_key: Optional[str] = None

    @field_validator("option_type", mode="before")
    @classmethod
    def _normalize_option_type(cls, value: Any) -> str:
        cleaned = str(value or "").strip().upper()
        if cleaned in {"CALL", "C"}:
            return "CE"
        if cleaned in {"PUT", "P"}:
            return "PE"
        if cleaned not in {"CE", "PE"}:
            raise ValueError("option_type must be CE or PE")
        return cleaned

    @field_validator("transaction_type", mode="before")
    @classmethod
    def _normalize_transaction_type(cls, value: Any) -> str:
        cleaned = str(value or "").strip().upper()
        if cleaned not in {"BUY", "SELL"}:
            raise ValueError("transaction_type must be BUY or SELL")
        return cleaned

    @property
    def effective_quantity(self) -> int:
        return int(self.quantity or (self.lot_size * self.lots))


class StrategyProtectionPreferences(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    index_lower_boundary: Optional[float] = None
    index_upper_boundary: Optional[float] = None
    combined_premium_target: Optional[float] = None
    combined_premium_stoploss: Optional[float] = None
    basket_mtm_target: Optional[float] = None
    basket_mtm_stoploss: Optional[float] = None

    @classmethod
    def from_payload(cls, payload: Optional[Dict[str, Any]]) -> "StrategyProtectionPreferences":
        raw = dict(payload or {})
        if "index_lower_stoploss" in raw and "index_lower_boundary" not in raw:
            raw["index_lower_boundary"] = raw.get("index_lower_stoploss")
        if "index_upper_stoploss" in raw and "index_upper_boundary" not in raw:
            raw["index_upper_boundary"] = raw.get("index_upper_stoploss")
        if "combined_premium_profit_target" in raw and "combined_premium_target" not in raw:
            raw["combined_premium_target"] = raw.get("combined_premium_profit_target")
        if "combined_premium_trailing_distance" in raw and "combined_premium_stoploss" not in raw:
            raw["combined_premium_stoploss"] = raw.get("combined_premium_trailing_distance")
        return cls.model_validate(raw)


class RuleInputDescriptor(BaseModel):
    key: str
    label: str
    unit: str
    group: InputGroup
    visible: bool = True
    required: bool = False
    recommended: bool = False
    value: Optional[float] = None
    source: Literal["backend_default", "backend_required", "user_input", "empty_optional"]
    help_text: Optional[str] = None


class NormalizedRule(BaseModel):
    key: str
    metric: MetricKind
    role: RuleRole
    label: str
    operator: Literal["lte", "gte"]
    threshold: float
    required: bool = False
    source: Literal["backend_default", "backend_required", "user_input"]


class RuntimeManagedOptionStrategyConfig(BaseModel):
    account_scope: str
    selected_legs: List[SelectedOptionLeg]
    rules: List[NormalizedRule]
    precedence: List[RuleRole] = Field(default_factory=list)
    spot_token: Optional[int] = None
    session_id: Optional[str] = None
    underlying: Optional[str] = None
    exit_order_type: Literal["MARKET"] = "MARKET"
    order_variety: Literal["regular"] = "regular"
    product_override: Optional[Literal["CNC", "MIS", "NRML", "MTF"]] = None
    all_or_none: bool = True
    dry_run: bool = False
    auto_disable_after_trigger: bool = True
    skip_if_exit_order_open: bool = True


class CanonicalOptionStrategyPreview(BaseModel):
    user_intent: str
    inferred_structure: str
    inferred_family: StrategyFamily
    direction_bias: Literal["bullish", "bearish", "neutral", "long_volatility", "structure"]
    classification_confidence: float = Field(ge=0, le=1)
    classification_reason: str
    description: str
    primary_metric: MetricKind
    emergency_metric: Optional[MetricKind] = None
    combined_premium_entry_type: Optional[Literal["credit", "debit"]] = None
    entry_combined_premium_points: float = 0.0
    estimated_entry_cost_rupees: float = 0.0
    warnings: List[str] = Field(default_factory=list)
    precedence: List[RuleRole] = Field(
        default_factory=lambda: [
            RuleRole.EMERGENCY_GUARD,
            RuleRole.HARD_STOP,
            RuleRole.PROFIT_TARGET,
            RuleRole.TRAILING_STOP,
        ]
    )
    inputs: Dict[str, RuleInputDescriptor] = Field(default_factory=dict)
    rules: List[NormalizedRule] = Field(default_factory=list)
