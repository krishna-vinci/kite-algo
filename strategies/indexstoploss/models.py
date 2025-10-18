"""
Pydantic models for Position Protection System
Phase 1: Core models with index-based monitoring support
"""

from datetime import datetime
from typing import List, Optional, Dict, Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator
from enum import Enum


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════════════════

class MonitoringMode(str, Enum):
    """Monitoring mode for strategy"""
    INDEX = "index"
    PREMIUM = "premium"
    HYBRID = "hybrid"
    COMBINED_PREMIUM = "combined_premium"


class StrategyType(str, Enum):
    """Type of strategy being protected"""
    MANUAL = "manual"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    IRON_CONDOR = "iron_condor"
    SINGLE_LEG = "single_leg"


class StrategyStatus(str, Enum):
    """Current status of strategy"""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    TRIGGERED = "triggered"
    ERROR = "error"
    PARTIAL = "partial"


class TrailingMode(str, Enum):
    """Trailing stoploss mode"""
    NONE = "none"
    CONTINUOUS = "continuous"
    STEP = "step"
    ATR = "atr"


class OrderTypeEnum(str, Enum):
    """Order type for exits"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL_M = "SL-M"


class ExitLogic(str, Enum):
    """Exit logic for hybrid mode"""
    ANY = "any"  # Exit when either index OR premium triggers
    ALL = "all"  # Exit when both index AND premium trigger


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION SNAPSHOT MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class PositionSnapshot(BaseModel):
    """Single position in the snapshot"""
    instrument_token: int
    tradingsymbol: str
    exchange: str = "NFO"
    product: str = "MIS"
    transaction_type: Literal["BUY", "SELL"]
    quantity: int
    lot_size: int
    lots: float
    average_price: float
    current_ltp: Optional[float] = None  # Runtime, updated from WebSocket


class PositionFilter(BaseModel):
    """Filter to identify positions to protect"""
    exchange: Optional[str] = None
    product: Optional[str] = None
    tradingsymbols: Optional[List[str]] = None
    instrument_tokens: Optional[List[int]] = None


# ═══════════════════════════════════════════════════════════════════════════════
# INDEX MONITORING MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class IndexConfig(BaseModel):
    """Configuration for index-based monitoring"""
    instrument_token: int
    tradingsymbol: str = Field(default="NIFTY 50")
    exchange: str = Field(default="NSE")
    
    # Bracket stoploss (two-way protection)
    upper_stoploss: Optional[float] = Field(default=None, description="Exit if index >= this (protects from rally)")
    lower_stoploss: Optional[float] = Field(default=None, description="Exit if index <= this (protects from crash)")
    
    order_type: OrderTypeEnum = Field(default=OrderTypeEnum.MARKET)
    limit_offset: Optional[float] = None
    
    @model_validator(mode='after')
    def validate_at_least_one_stoploss(self):
        """Ensure at least one stoploss boundary is set"""
        if self.upper_stoploss is None and self.lower_stoploss is None:
            raise ValueError("At least one of upper_stoploss or lower_stoploss must be set")
        return self


class TrailingConfig(BaseModel):
    """Configuration for trailing stoploss"""
    mode: TrailingMode = TrailingMode.NONE
    distance: Optional[float] = None
    unit: Literal["points", "percent"] = "points"
    step_size: Optional[float] = None  # For step mode
    atr_multiplier: Optional[float] = None  # For ATR mode
    atr_period: int = 14
    lock_profit: Optional[float] = None  # Activate trailing after this profit
    
    @model_validator(mode='after')
    def validate_trailing_params(self):
        """Validate trailing parameters based on mode"""
        if self.mode == TrailingMode.CONTINUOUS and self.distance is None:
            raise ValueError("distance required for continuous trailing")
        if self.mode == TrailingMode.STEP and (self.distance is None or self.step_size is None):
            raise ValueError("distance and step_size required for step trailing")
        if self.mode == TrailingMode.ATR and self.atr_multiplier is None:
            raise ValueError("atr_multiplier required for ATR trailing")
        return self


# ═══════════════════════════════════════════════════════════════════════════════
# PREMIUM MONITORING MODELS (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════════

class PremiumThresholdConfig(BaseModel):
    """Per-position premium monitoring configuration"""
    tradingsymbol: str
    transaction_type: Literal["BUY", "SELL"]
    entry_price: float
    
    # Exit thresholds
    stoploss_price: Optional[float] = None
    target_price: Optional[float] = None
    
    # Trailing config (per-position)
    trailing_mode: Optional[TrailingMode] = TrailingMode.NONE
    trailing_distance: Optional[float] = None
    trailing_lock_profit: Optional[float] = None
    
    # Runtime tracking (updated by engine)
    highest_premium: Optional[float] = None  # For BUY trailing
    lowest_premium: Optional[float] = None   # For SELL trailing
    current_trailing_sl: Optional[float] = None
    activated: bool = False
    
    @model_validator(mode='after')
    def validate_thresholds(self):
        """Ensure at least one exit threshold is set"""
        if self.stoploss_price is None and self.target_price is None:
            if self.trailing_mode == TrailingMode.NONE:
                raise ValueError("Must provide stoploss_price, target_price, or enable trailing")
        return self
    
    @model_validator(mode='after')
    def validate_trailing(self):
        """Validate trailing configuration"""
        if self.trailing_mode == TrailingMode.CONTINUOUS:
            if self.trailing_distance is None:
                raise ValueError("trailing_distance required for continuous trailing")
        return self


class PremiumMonitoringState(BaseModel):
    """Runtime state for premium monitoring (response only)"""
    instrument_token: int
    tradingsymbol: str
    transaction_type: Literal["BUY", "SELL"]
    entry_price: float
    current_ltp: Optional[float] = None
    current_pnl: Optional[float] = None
    stoploss_price: Optional[float] = None
    target_price: Optional[float] = None
    trailing_activated: bool = False
    current_trailing_sl: Optional[float] = None
    distance_to_sl: Optional[float] = None
    distance_to_target: Optional[float] = None


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED PREMIUM MODELS (Phase 4)
# ═══════════════════════════════════════════════════════════════════════════════

class CombinedPremiumEntryType(str, Enum):
    """Entry type for combined premium strategies"""
    CREDIT = "credit"  # SELL strategies (collect premium)
    DEBIT = "debit"    # BUY strategies (pay premium)


class CombinedPremiumLevel(BaseModel):
    """Partial exit level for combined premium mode"""
    level_number: int
    profit_points: float  # Exit when net profit reaches this
    exit_percent: int  # Percentage of positions to exit (1-100)
    executed: bool = False
    execution_time: Optional[datetime] = None


class CombinedPremiumState(BaseModel):
    """Runtime state for combined premium monitoring (response only)"""
    entry_type: str  # 'credit' or 'debit'
    initial_net_premium: float
    current_net_premium: float
    net_pnl: float  # Profit/loss in premium points
    net_pnl_rupees: float  # Profit/loss in rupees
    best_net_premium: Optional[float] = None
    profit_target: Optional[float] = None
    trailing_enabled: bool = False
    trailing_sl: Optional[float] = None
    distance_to_sl: Optional[float] = None
    distance_to_target: Optional[float] = None
    levels: List[CombinedPremiumLevel] = []


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class CreateProtectionRequest(BaseModel):
    """Request to create a new protection strategy (Phase 2: Index + Premium modes)"""
    
    # Metadata
    name: Optional[str] = None
    strategy_type: StrategyType = StrategyType.MANUAL
    notes: Optional[str] = None
    
    # Monitoring mode (Phase 2: Support premium mode)
    monitoring_mode: MonitoringMode
    
    # Index monitoring config (required for index/hybrid modes)
    index_instrument_token: Optional[int] = None
    index_tradingsymbol: str = "NIFTY 50"
    index_exchange: str = "NSE"
    
    # Bracket stoploss (at least one required for index mode)
    index_upper_stoploss: Optional[float] = Field(
        default=None, 
        description="Exit if index >= this level (protects from rally)"
    )
    index_lower_stoploss: Optional[float] = Field(
        default=None,
        description="Exit if index <= this level (protects from crash)"
    )
    
    stoploss_order_type: OrderTypeEnum = OrderTypeEnum.MARKET
    stoploss_limit_offset: Optional[float] = None
    
    # Index trailing config (optional)
    trailing_mode: Optional[TrailingMode] = TrailingMode.NONE
    trailing_distance: Optional[float] = None
    trailing_unit: Literal["points", "percent"] = "points"
    trailing_step_size: Optional[float] = None
    trailing_lock_profit: Optional[float] = None
    
    # Premium monitoring config (Phase 2 - NEW)
    premium_thresholds: Optional[Dict[str, PremiumThresholdConfig]] = None
    
    # Hybrid mode config (Phase 2 - NEW)
    exit_logic: Optional[ExitLogic] = ExitLogic.ANY
    
    # Combined Premium config (Phase 4 - NEW)
    combined_premium_entry_type: Optional[CombinedPremiumEntryType] = None
    combined_premium_profit_target: Optional[float] = None
    combined_premium_trailing_enabled: bool = False
    combined_premium_trailing_distance: Optional[float] = None
    combined_premium_trailing_lock_profit: Optional[float] = None
    combined_premium_levels: Optional[List[CombinedPremiumLevel]] = None
    
    # Position identification
    position_filter: PositionFilter
    
    @model_validator(mode='after')
    def validate_mode_config(self):
        """Validate configuration based on monitoring mode"""
        if self.monitoring_mode == MonitoringMode.INDEX:
            if self.index_instrument_token is None:
                raise ValueError("index_instrument_token required for INDEX mode")
            if self.index_upper_stoploss is None and self.index_lower_stoploss is None:
                raise ValueError("At least one of index_upper_stoploss or index_lower_stoploss required for INDEX mode")
        
        elif self.monitoring_mode == MonitoringMode.PREMIUM:
            if not self.premium_thresholds or len(self.premium_thresholds) == 0:
                raise ValueError("premium_thresholds required for PREMIUM mode")
        
        elif self.monitoring_mode == MonitoringMode.HYBRID:
            # Hybrid requires BOTH index and premium config
            if self.index_instrument_token is None:
                raise ValueError("index_instrument_token required for HYBRID mode")
            if self.index_upper_stoploss is None and self.index_lower_stoploss is None:
                raise ValueError("At least one index stoploss required for HYBRID mode")
            if not self.premium_thresholds or len(self.premium_thresholds) == 0:
                raise ValueError("premium_thresholds required for HYBRID mode")
        
        elif self.monitoring_mode == MonitoringMode.COMBINED_PREMIUM:
            # Combined premium requires index bracket stops and entry type
            if self.index_instrument_token is None:
                raise ValueError("index_instrument_token required for COMBINED_PREMIUM mode")
            if self.index_upper_stoploss is None and self.index_lower_stoploss is None:
                raise ValueError("At least one index stoploss required for COMBINED_PREMIUM mode (bracket protection)")
            if self.combined_premium_entry_type is None:
                raise ValueError("combined_premium_entry_type required for COMBINED_PREMIUM mode")
            # At least one exit condition required
            if not (self.combined_premium_profit_target or 
                    self.combined_premium_trailing_enabled or 
                    (self.combined_premium_levels and len(self.combined_premium_levels) > 0)):
                raise ValueError("At least one exit condition required: profit_target, trailing, or levels")
        
        return self


class UpdateProtectionRequest(BaseModel):
    """Request to update an existing strategy (Phase 2: Add premium updates)"""
    
    # Allow updating index stoploss levels
    index_upper_stoploss: Optional[float] = None
    index_lower_stoploss: Optional[float] = None
    
    # Allow updating index trailing config
    trailing_mode: Optional[TrailingMode] = None
    trailing_distance: Optional[float] = None
    trailing_lock_profit: Optional[float] = None
    
    # Allow updating premium thresholds (Phase 2)
    premium_thresholds: Optional[Dict[str, PremiumThresholdConfig]] = None
    
    # Allow updating metadata
    name: Optional[str] = None
    notes: Optional[str] = None


class StatusUpdateRequest(BaseModel):
    """Request to update strategy status"""
    status: Literal["active", "paused"]
    reason: Optional[str] = None


class ProtectionStrategyResponse(BaseModel):
    """Response with strategy details (Phase 2: Add premium monitoring)"""
    
    # Core fields
    strategy_id: UUID
    name: Optional[str]
    strategy_type: str
    monitoring_mode: str
    status: str
    
    # Index config
    index_instrument_token: Optional[int] = None
    index_tradingsymbol: Optional[str] = None
    index_upper_stoploss: Optional[float] = None
    index_lower_stoploss: Optional[float] = None
    
    # Index trailing state
    trailing_mode: Optional[str] = None
    trailing_distance: Optional[float] = None
    trailing_activated: bool = False
    trailing_current_level: Optional[float] = None
    
    # Premium monitoring (Phase 2 - NEW)
    premium_monitoring: Optional[Dict[str, PremiumMonitoringState]] = None
    
    # Combined premium monitoring (Phase 4 - NEW)
    combined_premium_state: Optional[CombinedPremiumState] = None
    
    # Position snapshot
    positions_captured: int
    total_lots: float
    position_snapshot: List[PositionSnapshot]
    
    # Runtime tracking
    remaining_quantities: Dict[str, Any] = {}
    placed_orders: List[Dict[str, Any]] = []
    levels_executed: List[str] = []
    stoploss_executed: bool = False
    
    # Evaluation state
    last_evaluated_price: Optional[float] = None
    last_evaluated_at: Optional[datetime] = None
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class StrategyListItem(BaseModel):
    """Compact strategy info for list view"""
    strategy_id: UUID
    name: Optional[str]
    monitoring_mode: str
    status: str
    total_lots: float
    index_instrument_token: Optional[int] = None
    index_tradingsymbol: Optional[str] = None
    index_upper_stoploss: Optional[float] = None
    index_lower_stoploss: Optional[float] = None
    last_evaluated_at: Optional[datetime] = None
    created_at: datetime


class StrategyListResponse(BaseModel):
    """Response with list of strategies"""
    total: int
    strategies: List[StrategyListItem]


# ═══════════════════════════════════════════════════════════════════════════════
# EVENT MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class StrategyEvent(BaseModel):
    """Event record from strategy_events table"""
    event_id: int
    strategy_id: UUID
    event_type: str
    trigger_price: Optional[float] = None
    trigger_type: Optional[str] = None
    level_name: Optional[str] = None
    quantity_affected: Optional[int] = None
    lots_affected: Optional[float] = None
    order_id: Optional[str] = None
    correlation_id: Optional[str] = None
    order_status: Optional[str] = None
    instrument_token: Optional[int] = None
    error_message: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class EventsResponse(BaseModel):
    """Response with event history"""
    strategy_id: UUID
    total_events: int
    events: List[StrategyEvent]


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class EngineHealthResponse(BaseModel):
    """Engine health status"""
    status: str
    engine_running: bool
    active_strategies: int
    monitoring_modes: Dict[str, int]
    last_evaluation: Optional[datetime] = None
    websocket_status: str
    evaluation_interval_ms: int
    orders_placed_today: int = 0
