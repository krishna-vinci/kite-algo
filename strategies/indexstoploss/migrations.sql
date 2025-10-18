-- ═══════════════════════════════════════════════════════════════════════════════
-- Position Protection System - Database Schema
-- Version: 2.0 | Created: 2025-10-16
-- ═══════════════════════════════════════════════════════════════════════════════

-- Table: position_protection_strategies
-- Core table for storing protection strategy configurations and runtime state

CREATE TABLE IF NOT EXISTS position_protection_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- METADATA
    -- ═══════════════════════════════════════════════════════════════════════════
    name TEXT,
    strategy_type TEXT CHECK (strategy_type IN 
        ('manual', 'straddle', 'strangle', 'iron_condor', 'single_leg')),
    notes TEXT,
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- MONITORING MODE (Core Feature)
    -- ═══════════════════════════════════════════════════════════════════════════
    monitoring_mode TEXT NOT NULL CHECK (monitoring_mode IN 
        ('index', 'premium', 'hybrid', 'combined_premium')),
    exit_logic TEXT DEFAULT 'any' CHECK (exit_logic IN ('any', 'all')),
    /*
    MODES:
    • 'index': Monitor index price only (original)
    • 'premium': Monitor option LTP per-position (for naked selling)
    • 'hybrid': Monitor BOTH index AND premium, exit when either triggers
    • 'combined_premium': Monitor net P&L across all positions (NEW - for straddles/strangles)
    
    EXIT LOGIC (for hybrid mode):
    • 'any': Exit on first trigger (index OR premium)
    • 'all': Exit only when both trigger (index AND premium)
    */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- INDEX MONITORING CONFIG (Mode: index, hybrid, combined_premium)
    -- ═══════════════════════════════════════════════════════════════════════════
    index_instrument_token BIGINT,
    index_tradingsymbol TEXT,
    index_exchange TEXT DEFAULT 'NSE',
    
    -- Index stoploss (Two-way bracket for neutral strategies)
    index_upper_stoploss NUMERIC(18,6),
    /* Exit if index >= this level (protects from upward rally) */
    
    index_lower_stoploss NUMERIC(18,6),
    /* Exit if index <= this level (protects from downward crash) */
    
    stoploss_order_type TEXT DEFAULT 'MARKET' 
        CHECK (stoploss_order_type IN ('MARKET', 'LIMIT', 'SL-M')),
    stoploss_limit_offset NUMERIC(18,6),
    
    /* CRITICAL: For market-neutral strategies (straddles/strangles),
       BOTH upper and lower stops should be set to create a bracket.
       For directional strategies, set only one boundary.
       
       Example (Short Straddle at NIFTY 23500):
       - index_upper_stoploss: 24000 (exit if index rallies)
       - index_lower_stoploss: 23000 (exit if index crashes)
    */
    
    -- Index trailing
    trailing_mode TEXT CHECK (trailing_mode IN 
        ('continuous', 'step', 'atr', 'none')),
    trailing_distance NUMERIC(18,6),
    trailing_unit TEXT CHECK (trailing_unit IN ('points', 'percent')) 
        DEFAULT 'points',
    trailing_step_size NUMERIC(18,6),  -- For step mode
    trailing_atr_multiplier NUMERIC(5,2),  -- For ATR mode
    trailing_atr_period INT DEFAULT 14,
    trailing_lock_profit NUMERIC(18,6),  -- Activate after profit
    trailing_highest_price NUMERIC(18,6),  -- Runtime
    trailing_current_level NUMERIC(18,6),  -- Runtime
    trailing_activated BOOLEAN DEFAULT FALSE,  -- Runtime
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- PREMIUM MONITORING CONFIG (Mode: premium, hybrid)
    -- Per-position thresholds with direction-aware trailing
    -- ═══════════════════════════════════════════════════════════════════════════
    premium_thresholds JSONB DEFAULT '{}'::JSONB,
    /* Structure:
    {
      "12345678": {  // instrument_token
        "tradingsymbol": "NIFTY25JAN24000CE",
        "transaction_type": "SELL",  // CRITICAL for trailing direction
        "entry_price": 150.0,
        "stoploss_price": 200.0,     // SELL: trails UP
        "target_price": 50.0,         // Exit at profit
        "trailing_mode": "continuous",
        "trailing_distance": 20.0,    // Points
        "trailing_lock_profit": 30.0, // Activate after 30pt profit
        "highest_premium": 150.0,     // Runtime: for BUY
        "lowest_premium": 150.0,      // Runtime: for SELL
        "current_trailing_sl": 200.0, // Runtime
        "activated": false            // Runtime
      }
    }
    
    DIRECTION-AWARE TRAILING:
    • SELL position: Premium drops → SL trails UP (lock profit)
    • BUY position: Premium rises → SL trails DOWN (lock profit)
    */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- COMBINED PREMIUM CONFIG (Mode: combined_premium)
    -- Monitor net P&L across all positions (for straddles/strangles)
    -- ═══════════════════════════════════════════════════════════════════════════
    combined_premium_entry_type TEXT CHECK (combined_premium_entry_type IN ('credit', 'debit')),
    /* 
    • 'credit': Short strategies (SELL straddle/strangle) - collect premium
    • 'debit': Long strategies (BUY straddle/strangle) - pay premium
    */
    
    combined_premium_profit_target NUMERIC(18,6),
    /* Exit when net profit reaches this (in premium points)
       Example: Sold straddle for 300, exit when profit = 50 points (premium decays to 250)
    */
    
    /* STOPLOSS: Always derived from index bracket stops (above)
       Combined premium mode MUST provide index_instrument_token + at least one bracket boundary.
       For neutral strategies, BOTH upper and lower stops are required.
       Premium metrics are NOT used for stoploss exits. */
    
    combined_premium_trailing_enabled BOOLEAN DEFAULT FALSE,
    combined_premium_trailing_distance NUMERIC(18,6),
    combined_premium_trailing_lock_profit NUMERIC(18,6),
    /* Trailing for combined premium:
       Credit strategy: As premium decays (profit), trail stoploss UP
       Debit strategy: As premium rises (profit), trail stoploss DOWN
    */
    
    -- Runtime tracking (calculated in evaluation loop)
    initial_net_premium NUMERIC(18,6),
    /* Calculated once on creation:
       - Credit (SELL): SUM(entry_price) for all positions
       - Debit (BUY): SUM(entry_price) for all positions
    */
    
    current_net_premium NUMERIC(18,6),
    /* Updated every 500ms:
       - SUM(current_ltp) for all positions in snapshot
    */
    
    best_net_premium NUMERIC(18,6),
    /* For trailing:
       - Credit: Lowest premium reached (best profit)
       - Debit: Highest premium reached (best profit)
    */
    
    combined_premium_trailing_sl NUMERIC(18,6),
    /* Current trailing stoploss level (in net premium points) */
    
    combined_premium_levels JSONB DEFAULT '[]'::JSONB,
    /* Partial exits at different profit levels:
    [
      {
        "level_number": 1,
        "profit_points": 30,    // Exit when 30 points profit
        "exit_percent": 50,     // Exit 50% of positions
        "executed": false,
        "execution_time": null
      },
      {
        "level_number": 2,
        "profit_points": 50,
        "exit_percent": 100,    // Exit remaining 50%
        "executed": false
      }
    ]
    */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- POSITION SNAPSHOT (LOT-BASED, FROZEN at creation)
    -- ═══════════════════════════════════════════════════════════════════════════
    position_snapshot JSONB NOT NULL,
    /* Structure:
    [
      {
        "instrument_token": 12345678,
        "tradingsymbol": "NIFTY25JAN24000CE",
        "exchange": "NFO",
        "product": "MIS",
        "transaction_type": "SELL",
        "quantity": 150,
        "lot_size": 50,
        "lots": 3.0,
        "average_price": 150.50,
        "current_ltp": 145.30  // Runtime: updated from WebSocket
      }
    ]
    */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- EXIT LEVELS (Flexible - unlimited levels)
    -- ═══════════════════════════════════════════════════════════════════════════
    exit_levels JSONB DEFAULT '[]'::JSONB,
    takeprofit_levels JSONB DEFAULT '[]'::JSONB,
    /* Structure:
    [
      {
        "level_number": 1,
        "trigger_price": 22800,  // Index or premium based on mode
        "trigger_type": "index",  // 'index' or 'premium'
        "instrument_token": 12345678,  // Required for premium mode
        "quantity": 100,
        "lots": 2.0,
        "order_type": "LIMIT",
        "limit_offset": 5.0,
        "executed": false,
        "execution_time": null,
        "order_id": null
      }
    ]
    */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- POSITION BUILDING (Delta-based strike selection)
    -- ═══════════════════════════════════════════════════════════════════════════
    target_delta NUMERIC(5,4),
    /* Example: 0.3000 for 30 delta OTM options
       Used by StrikeSelector to find strikes matching target delta */
    
    risk_amount NUMERIC(18,2),
    /* Maximum risk in rupees
       System calculates lots based on this risk tolerance */
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- RUNTIME TRACKING
    -- ═══════════════════════════════════════════════════════════════════════════
    remaining_quantities JSONB DEFAULT '{}'::JSONB,
    /* Tracks remaining positions after partial exits:
    {
      "12345678": {
        "quantity": 50,
        "lots": 1.0,
        "original_quantity": 150,
        "exited_quantity": 100
      }
    }
    */
    
    placed_orders JSONB DEFAULT '[]'::JSONB,
    /* Full order audit trail:
    [
      {
        "order_id": "240116000123456",
        "level": "tp1",
        "instrument_token": 12345678,
        "quantity": 100,
        "lots": 2.0,
        "order_type": "LIMIT",
        "price": 155.50,
        "status": "COMPLETE",
        "filled_quantity": 100,
        "average_price": 155.25,
        "correlation_id": "uuid",
        "idempotency_key": "strategy_xxx_tp1_12345678",
        "timestamp": "2025-01-16T10:35:22Z"
      }
    ]
    */
    
    execution_errors JSONB DEFAULT '[]'::JSONB,
    levels_executed JSONB DEFAULT '[]'::JSONB,
    stoploss_executed BOOLEAN DEFAULT FALSE,
    
    -- Product conversion
    product_conversion_enabled BOOLEAN DEFAULT FALSE,
    convert_to_nrml_at TEXT CHECK (convert_to_nrml_at IN 
        ('never', 'tp1', 'tp2', 'manual')),
    nrml_conversion_done BOOLEAN DEFAULT FALSE,
    
    -- Virtual contract
    virtual_contract_calculated BOOLEAN DEFAULT FALSE,
    virtual_contract_data JSONB,
    
    -- Status & evaluation
    status TEXT NOT NULL DEFAULT 'active' 
        CHECK (status IN ('active', 'paused', 'completed', 
                          'triggered', 'error', 'partial')),
    last_evaluated_price NUMERIC(18,6),
    last_evaluated_at TIMESTAMP WITH TIME ZONE,
    last_health_check TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    -- ═══════════════════════════════════════════════════════════════════════════
    -- CONSTRAINTS
    -- ═══════════════════════════════════════════════════════════════════════════
    CONSTRAINT valid_mode_config CHECK (
        (monitoring_mode = 'index' AND index_instrument_token IS NOT NULL 
            AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL)) OR
        (monitoring_mode = 'premium' AND premium_thresholds IS NOT NULL) OR
        (monitoring_mode = 'hybrid' AND index_instrument_token IS NOT NULL 
            AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL)
            AND premium_thresholds IS NOT NULL) OR
        (monitoring_mode = 'combined_premium' 
            AND combined_premium_entry_type IS NOT NULL
            AND index_instrument_token IS NOT NULL
            AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL)
            AND (
                combined_premium_profit_target IS NOT NULL OR
                combined_premium_trailing_enabled IS TRUE OR
                jsonb_array_length(coalesce(combined_premium_levels, '[]'::jsonb)) > 0
            ))
    ),
    CONSTRAINT valid_trailing CHECK (
        (trailing_mode IS NULL OR trailing_mode = 'none') OR 
        (trailing_mode = 'continuous' AND trailing_distance IS NOT NULL) OR
        (trailing_mode = 'step' AND trailing_distance IS NOT NULL 
            AND trailing_step_size IS NOT NULL) OR
        (trailing_mode = 'atr' AND trailing_atr_multiplier IS NOT NULL)
    )
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_strategies_status 
    ON position_protection_strategies(status);
CREATE INDEX IF NOT EXISTS idx_strategies_active 
    ON position_protection_strategies(id) 
    WHERE status IN ('active', 'partial');
CREATE INDEX IF NOT EXISTS idx_strategies_mode 
    ON position_protection_strategies(monitoring_mode);
CREATE INDEX IF NOT EXISTS idx_strategies_index_token 
    ON position_protection_strategies(index_instrument_token);
CREATE INDEX IF NOT EXISTS idx_strategies_updated 
    ON position_protection_strategies(updated_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Table: strategy_events
-- Event audit trail for all strategy-related activities
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS strategy_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES position_protection_strategies(id) 
        ON DELETE CASCADE,
    
    event_type TEXT NOT NULL CHECK (event_type IN 
        ('created', 'updated', 
         'index_stoploss_triggered', 'premium_stoploss_triggered',
         'index_upper_stoploss_triggered', 'index_lower_stoploss_triggered',
         'combined_premium_triggered', 'combined_premium_profit_target',
         'combined_premium_index_upper_stoploss', 'combined_premium_index_lower_stoploss',
         'combined_premium_trailing_sl',
         'level_triggered', 'trailing_activated', 'trailing_updated',
         'product_converted', 'paused', 'resumed', 'completed',
         'order_placed', 'order_filled', 'order_failed',
         'virtual_contract_calculated', 'error')),
    
    -- Trigger details
    trigger_price NUMERIC(18,6),
    trigger_type TEXT CHECK (trigger_type IN ('index', 'premium', 'combined_premium')),
    level_name TEXT,  -- 'stoploss', 'tp1', 'exit1', etc.
    quantity_affected INT,
    lots_affected NUMERIC(10,2),
    
    -- Order details
    order_id TEXT,
    correlation_id TEXT,
    idempotency_key TEXT,
    order_type TEXT,
    order_status TEXT,
    filled_quantity INT,
    average_fill_price NUMERIC(18,6),
    
    -- Position affected
    instrument_token BIGINT,
    positions_affected JSONB,
    
    -- Trailing specific
    highest_price_at_event NUMERIC(18,6),
    trailing_level_at_event NUMERIC(18,6),
    trailing_mode TEXT,
    
    -- Product conversion
    product_before TEXT,
    product_after TEXT,
    
    -- Error tracking
    error_message TEXT,
    error_details JSONB,
    retry_count INT DEFAULT 0,
    
    -- Metadata
    meta JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_strategy_events_strategy_id 
    ON strategy_events(strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_events_created_at 
    ON strategy_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_events_type 
    ON strategy_events(event_type);
CREATE INDEX IF NOT EXISTS idx_strategy_events_order_id 
    ON strategy_events(order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_strategy_events_correlation_id 
    ON strategy_events(correlation_id) WHERE correlation_id IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════════════════════
-- END OF SCHEMA
-- ═══════════════════════════════════════════════════════════════════════════════
