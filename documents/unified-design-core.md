# Position Protection System - Core Design

**Version:** 2.0 | **Last Updated:** 2025-10-16 | **Status:** Production Ready

---

## 🎯 System Overview

Unified position protection system combining **four monitoring modes** for automated F&O trading:

1. **Index Mode**: Monitor NIFTY/BANKNIFTY index (original)
2. **Premium Mode**: Monitor option LTP with direction-aware trailing (NEW)
3. **Hybrid Mode**: Monitor BOTH, exit on either/both triggers (NEW)
4. **Combined Premium Mode**: Monitor net strategy P&L for straddles/strangles with index-backed stoploss (NEW)

### Key Capabilities

- ✅ **Real-time monitoring** (500ms evaluation loop)
- ✅ **Direction-aware trailing** (SELL trails up, BUY trails down)
- ✅ **Delta-based strike selection** for position building
- ✅ **Lot-based exits** with partial fill handling
- ✅ **Multiple trailing modes** (continuous, step, ATR)
- ✅ **Idempotent order execution** with retry logic
- ✅ **MIS ↔ NRML conversion**
- ✅ **Virtual contract charge calculation**
- ✅ **Comprehensive event audit trail**

---

## 🏗️ Architecture

### Component Flow

```
┌────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                         │
├────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐   │
│  │    PositionProtectionEngine (Main Controller)          │   │
│  │                                                          │   │
│  │  • 500ms evaluation loop                               │   │
│  │  • Index + Premium monitoring (4 modes)                │   │
│  │  • Direction-aware trailing (SELL up / BUY down)      │   │
│  │  • Lot-based order execution                           │   │
│  │  • Position tracking after partial fills               │   │
│  └──────┬──────────────────────────────┬──────────────────┘   │
│         │                               │                       │
│         ▼                               ▼                       │
│  ┌─────────────┐               ┌─────────────────┐            │
│  │StrikeSelector│               │ OrdersService   │            │
│  │(Delta-based)│               │ (Kite API)      │            │
│  └──────┬──────┘               └────────┬────────┘            │
│         │                                │                       │
│         ▼                                ▼                       │
│  ┌──────────────┐              ┌─────────────────┐            │
│  │OptionsSession│              │  WebSocket Mgr  │            │
│  │Mgr (Greeks)  │              │  (Price Feed)   │            │
│  └──────────────┘              └─────────────────┘            │
│         │                                │                       │
│         └────────────┬───────────────────┘                      │
│                      ▼                                           │
│         ┌───────────────────────────┐                          │
│         │  PostgreSQL + Redis       │                          │
│         │  (Rules/Events/Cache)     │                          │
│         └───────────────────────────┘                          │
└────────────────────────────────────────────────────────────────┘
```

### Engine Pattern

Follows proven `AlertsEngine` architecture:
- **Single-process**: No multi-threading complexity
- **Periodic evaluation**: 500ms loop with in-memory cache
- **DB refresh**: Every 5 seconds
- **Async order monitoring**: Non-blocking
- **Redis idempotency**: Prevents duplicate orders

---

## 📊 Database Schema

### Table: `position_protection_strategies`

```sql
CREATE TABLE position_protection_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Metadata
    name TEXT,
    strategy_type TEXT CHECK (strategy_type IN 
        ('manual', 'straddle', 'strangle', 'iron_condor', 'single_leg')),
    notes TEXT,
    
    -- ═══════════════════════════════════════════════════════════
    -- MONITORING MODE (Core Feature)
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- INDEX MONITORING CONFIG (Mode: index, hybrid)
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- PREMIUM MONITORING CONFIG (Mode: premium, hybrid)
    -- Per-position thresholds with direction-aware trailing
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- COMBINED PREMIUM CONFIG (Mode: combined_premium)
    -- Monitor net P&L across all positions (for straddles/strangles)
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- POSITION SNAPSHOT (LOT-BASED, FROZEN at creation)
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- EXIT LEVELS (Flexible - unlimited levels)
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- POSITION BUILDING (NEW - Delta-based strike selection)
    -- ═══════════════════════════════════════════════════════════
    target_delta NUMERIC(5,4),
    /* Example: 0.3000 for 30 delta OTM options
       Used by StrikeSelector to find strikes matching target delta */
    
    risk_amount NUMERIC(18,2),
    /* Maximum risk in rupees
       System calculates lots based on this risk tolerance */
    
    -- ═══════════════════════════════════════════════════════════
    -- RUNTIME TRACKING
    -- ═══════════════════════════════════════════════════════════
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
    
    -- ═══════════════════════════════════════════════════════════
    -- CONSTRAINTS
    -- ═══════════════════════════════════════════════════════════
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
CREATE INDEX idx_strategies_status 
    ON position_protection_strategies(status);
CREATE INDEX idx_strategies_active 
    ON position_protection_strategies(id) 
    WHERE status IN ('active', 'partial');
CREATE INDEX idx_strategies_mode 
    ON position_protection_strategies(monitoring_mode);
CREATE INDEX idx_strategies_index_token 
    ON position_protection_strategies(index_instrument_token);
CREATE INDEX idx_strategies_updated 
    ON position_protection_strategies(updated_at DESC);
```

### Table: `strategy_events`

```sql
CREATE TABLE strategy_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES position_protection_strategies(id) 
        ON DELETE CASCADE,
    
    event_type TEXT NOT NULL CHECK (event_type IN 
        ('created', 'updated', 
         'index_stoploss_triggered', 'premium_stoploss_triggered',
         'level_triggered', 'trailing_activated', 'trailing_updated',
         'product_converted', 'paused', 'resumed', 'completed',
         'order_placed', 'order_filled', 'order_failed',
         'virtual_contract_calculated', 'error')),
    
    -- Trigger details
    trigger_price NUMERIC(18,6),
    trigger_type TEXT CHECK (trigger_type IN ('index', 'premium')),
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
CREATE INDEX idx_strategy_events_strategy_id 
    ON strategy_events(strategy_id);
CREATE INDEX idx_strategy_events_created_at 
    ON strategy_events(created_at DESC);
CREATE INDEX idx_strategy_events_type 
    ON strategy_events(event_type);
CREATE INDEX idx_strategy_events_order_id 
    ON strategy_events(order_id) WHERE order_id IS NOT NULL;
CREATE INDEX idx_strategy_events_correlation_id 
    ON strategy_events(correlation_id) WHERE correlation_id IS NOT NULL;
```

---

## 🔄 Monitoring Modes Explained

### Mode 1: Index-Based

**Use Case**: Protect options when underlying index moves against you

**Supports Two-Way Bracket**: For neutral strategies, set BOTH upper and lower stops

```
Monitor: NIFTY index price
Trigger: When NIFTY breaches either boundary
Action: Exit all positions (regardless of premium)
```

**Example 1 (Directional - Long):**
- Holding: 3 lots NIFTY 24000 CE (bought at ₹150)
- Index Lower SL: 23000
- Current NIFTY: 23500
- If NIFTY drops to 23000 → Exit all 3 lots

**Example 2 (Market Neutral - Short Straddle):**
- Holding: 
  - 4 lots NIFTY 23500 CE (sold at ₹150)
  - 4 lots NIFTY 23500 PE (sold at ₹145)
- Index Upper SL: 24000
- Index Lower SL: 23000
- Current NIFTY: 23500
- If NIFTY rallies to 24000 OR crashes to 23000 → Exit ALL positions
- **CRITICAL**: Both boundaries protect from unlimited loss in either direction

### Mode 2: Premium-Based (NEW)

**Use Case**: Protect individual positions based on their premium movement

```
Monitor: Option LTP for each position
Trigger: When option premium crosses position-specific threshold
Action: Exit that specific position
```

**Direction-Aware Trailing:**

**SELL Position (Short):**
```
Entry: SELL CE at ₹150
Premium drops to ₹100 (profit: ₹50)
SL trails UP to ₹120 (locking ₹30 profit)
If premium rises back to ₹120 → Exit
```

**BUY Position (Long):**
```
Entry: BUY CE at ₹150
Premium rises to ₹200 (profit: ₹50)
SL trails DOWN to ₹180 (locking ₹30 profit)
If premium drops to ₹180 → Exit
```

**Example:**
- Holding: 
  - NIFTY 24000 CE (SELL @ ₹150, SL @ ₹200)
  - NIFTY 23500 PE (SELL @ ₹145, SL @ ₹195)
- Each position has independent SL and trailing
- Exit individually when premium threshold hit

### Mode 3: Hybrid (NEW)

**Use Case**: Protect positions with BOTH index AND premium safeguards

```
Monitor: Index price + Option LTP
Trigger: Based on exit_logic ('any' or 'all')
Action: Exit when condition(s) met
```

**Exit Logic:**

**'any' (default):**
```
Exit when EITHER condition triggers:
- Index crosses 22000, OR
- Premium crosses ₹200
→ Whichever happens first
```

**'all':**
```
Exit when BOTH conditions trigger:
- Index crosses 22000, AND
- Premium crosses ₹200
→ Must satisfy both
```

### Mode 4: Combined Premium (NEW)

**Use Case**: Protect multi-leg strategies (straddles/strangles) based on **net P&L** rather than individual legs

```
Monitor: Net premium across ALL positions
Trigger: When net profit/loss reaches target
Action: Exit all positions together
```

**Credit Strategies (SELL):**
```
Entry: SELL Straddle (CE @ ₹150 + PE @ ₹145) = ₹295 collected at NIFTY 23500
Current: CE @ ₹120 + PE @ ₹110 = ₹230 current premium
Net P&L: ₹295 - ₹230 = ₹65 profit

Profit Target: ₹50 → Trigger exit (take profit)
Index Upper SL: 24000 → Exit if NIFTY rallies (protect from unlimited loss)
Index Lower SL: 23000 → Exit if NIFTY crashes (protect from unlimited loss)

CRITICAL: Premium profit/trailing alone is NOT sufficient stoploss.
Index bracket MUST be set for catastrophic move protection.
```

**Debit Strategies (BUY):**
```
Entry: BUY Straddle (CE @ ₹150 + PE @ ₹145) = ₹295 paid at NIFTY 23500
Current: CE @ ₹200 + PE @ ₹180 = ₹380 current premium
Net P&L: ₹380 - ₹295 = ₹85 profit

Profit Target: ₹50 → Trigger exit (take profit)
Index Upper SL: 24000 → Exit if NIFTY rallies beyond expectation
Index Lower SL: 23000 → Exit if NIFTY drops too far
```

**Combined Premium Trailing:**
```
Credit Strategy (SELL straddle @ ₹300):
- Premium decays to ₹250 (₹50 profit)
- Trailing activates after ₹30 profit lock
- SL trails UP: If premium rises back to ₹270, exit with ₹30 profit locked

Debit Strategy (BUY straddle @ ₹300):
- Premium rises to ₹350 (₹50 profit)
- Trailing activates after ₹30 profit lock
- SL trails DOWN: If premium drops to ₹330, exit with ₹30 profit locked
```

**Partial Exits:**
```
Strategy: SELL Straddle @ ₹300 total premium at NIFTY 23500

Level 1: Exit 50% at ₹30 profit (when premium = ₹270)
Level 2: Exit remaining 50% at ₹50 profit (when premium = ₹250)
Index Upper SL: 24000 → Hard stop if index rallies
Index Lower SL: 23000 → Hard stop if index crashes
```

**Complete Example:**
```
SELL NIFTY Straddle at 23500:
- 24000 CE @ ₹150 (4 lots)
- 24000 PE @ ₹145 (4 lots)
- Total collected: ₹295 per set

Protection (Multi-Layer):
1. Index Bracket (MANDATORY - Hard stops):
   - Upper SL: 24000 (exit if index >= 24000)
   - Lower SL: 23000 (exit if index <= 23000)
   
2. Premium Targets (Profit booking):
   - Profit target: ₹50 (exit when premium drops to ₹245)
   - Trailing: 30 points after ₹30 profit
   
3. Partial Exits (Optional):
   - Exit 50% at ₹30 profit
   - Exit remaining at ₹50 profit

Result: Index bracket protects from catastrophic moves in EITHER direction,
        while premium targets optimize profit booking.
```

**Key Advantages:**
- ✅ Manage straddle/strangle as single unit
- ✅ One leg can gain while other loses - net P&L matters
- ✅ Avoid premature exits on individual leg movements
- ✅ Cleaner P&L tracking
- ✅ Partial profit booking at multiple levels

---

## ⚙️ Engine Evaluation Logic

### Main Loop (Every 500ms)

```python
async def evaluation_loop():
    while running:
        # 1. Get latest prices
        index_prices = ws_manager.latest_ticks  # Index LTP
        option_prices = ws_manager.latest_ticks  # Option LTP
        
        # 2. Refresh rules from DB (every 5s)
        if time_to_refresh():
            strategies = load_active_strategies()
        
        # 3. Evaluate each strategy
        for strategy in strategies:
            if strategy.monitoring_mode == 'index':
                check_index_triggers(strategy, index_prices)
            
            elif strategy.monitoring_mode == 'premium':
                check_premium_triggers(strategy, option_prices)
            
            elif strategy.monitoring_mode == 'combined_premium':
                check_combined_premium_triggers(strategy, option_prices)
            
            elif strategy.monitoring_mode == 'hybrid':
                check_index_triggers(strategy, index_prices)
                check_premium_triggers(strategy, option_prices)
                apply_exit_logic(strategy)
            
            # 4. Update trailing levels
            update_trailing_stoplosses(strategy)
        
        await asyncio.sleep(0.5)  # 500ms
```

### Index Bracket Stoploss Logic (Two-Way Protection)

```python
async def check_index_triggers(strategy, index_prices):
    """
    Check index-based bracket stoploss.
    Supports TWO-WAY protection for market-neutral strategies.
    """
    if not strategy.index_instrument_token:
        return
    
    index_token = strategy.index_instrument_token
    if index_token not in index_prices:
        logging.warning(f"Strategy {strategy.id}: Missing index price for token {index_token}")
        return
    
    current_index = index_prices[index_token]['last_price']
    
    # Check UPPER boundary (protects from upward rally)
    if strategy.index_upper_stoploss is not None:
        if current_index >= strategy.index_upper_stoploss:
            logging.info(
                f"Strategy {strategy.id}: Index UPPER stoploss triggered! "
                f"Index={current_index:.2f} >= {strategy.index_upper_stoploss}"
            )
            await execute_exit(strategy, "index_upper_stoploss_triggered")
            return
    
    # Check LOWER boundary (protects from downward crash)
    if strategy.index_lower_stoploss is not None:
        if current_index <= strategy.index_lower_stoploss:
            logging.info(
                f"Strategy {strategy.id}: Index LOWER stoploss triggered! "
                f"Index={current_index:.2f} <= {strategy.index_lower_stoploss}"
            )
            await execute_exit(strategy, "index_lower_stoploss_triggered")
            return
    
    # CRITICAL: For market-neutral strategies (straddles/strangles),
    # BOTH boundaries should be set. For directional strategies, 
    # set only the relevant boundary.
```

### Direction-Aware Trailing Logic

```python
def update_premium_trailing(position, current_ltp):
    config = position.premium_config
    
    if config.transaction_type == "SELL":
        # SELL: Premium drops → Trail UP (lock profit)
        if current_ltp < config.lowest_premium:
            config.lowest_premium = current_ltp
            new_sl = current_ltp + config.trailing_distance
            config.current_trailing_sl = min(new_sl, config.stoploss_price)
        
        # Trigger if premium rises above SL
        if current_ltp >= config.current_trailing_sl:
            execute_exit(position, "premium_stoploss_triggered")
    
    elif config.transaction_type == "BUY":
        # BUY: Premium rises → Trail DOWN (lock profit)
        if current_ltp > config.highest_premium:
            config.highest_premium = current_ltp
            new_sl = current_ltp - config.trailing_distance
            config.current_trailing_sl = max(new_sl, config.stoploss_price)
        
        # Trigger if premium drops below SL
        if current_ltp <= config.current_trailing_sl:
            execute_exit(position, "premium_stoploss_triggered")
```

### Combined Premium Logic (NEW)

```python
async def check_combined_premium_triggers(strategy, option_prices, index_prices):
    """
    Monitor net P&L across all positions in a strategy.
    Handles both CREDIT (SELL) and DEBIT (BUY) strategies.
    """
    if not strategy.position_snapshot:
        return
    
    # 1. Calculate initial net premium (one-time, on first evaluation)
    if strategy.initial_net_premium is None:
        initial_premium = 0
        for pos in strategy.position_snapshot:
            initial_premium += pos.get('average_price', 0)
        
        strategy.initial_net_premium = initial_premium
        await db.update_strategy(strategy.id, {
            "initial_net_premium": initial_premium
        })
        logging.info(f"Strategy {strategy.id}: Initial net premium = {initial_premium}")
    
    # 2. Calculate current net premium (every evaluation)
    current_premium = 0
    all_legs_have_price = True
    
    for pos in strategy.position_snapshot:
        token = pos['instrument_token']
        if token in option_prices:
            current_premium += option_prices[token]['last_price']
        else:
            all_legs_have_price = False
            logging.warning(f"Missing price for token {token}")
            break
    
    if not all_legs_have_price:
        return  # Skip evaluation if any leg missing price
    
    strategy.current_net_premium = current_premium
    
    # 3. Calculate net P&L based on entry type
    if strategy.combined_premium_entry_type == 'credit':
        # CREDIT (SELL): Profit when premium decays
        net_pnl = strategy.initial_net_premium - strategy.current_net_premium
        # Positive = profit (premium dropped)
        # Negative = loss (premium rose)
    else:  # 'debit'
        # DEBIT (BUY): Profit when premium rises
        net_pnl = strategy.current_net_premium - strategy.initial_net_premium
        # Positive = profit (premium rose)
        # Negative = loss (premium dropped)
    
    logging.debug(f"Strategy {strategy.id}: Net P&L = {net_pnl:.2f}")
    
    # 4. Check profit target
    if strategy.combined_premium_profit_target:
        if net_pnl >= strategy.combined_premium_profit_target:
            logging.info(
                f"Strategy {strategy.id}: Profit target hit! "
                f"P&L={net_pnl:.2f} >= Target={strategy.combined_premium_profit_target}"
            )
            await execute_combined_exit(
                strategy, 
                "combined_premium_profit_target",
                net_pnl
            )
            return

    # 5. Check index-based bracket stoploss (TWO-WAY protection)
    if strategy.index_instrument_token:
        index_token = strategy.index_instrument_token
        if index_token in index_prices:
            current_index = index_prices[index_token]['last_price']
            
            # Check UPPER boundary (protects from upward rally)
            if strategy.index_upper_stoploss is not None:
                if current_index >= strategy.index_upper_stoploss:
                    logging.info(
                        f"Strategy {strategy.id}: Index UPPER stoploss hit! "
                        f"Index={current_index:.2f} >= {strategy.index_upper_stoploss}"
                    )
                    await execute_combined_exit(
                        strategy,
                        "combined_premium_index_upper_stoploss",
                        net_pnl
                    )
                    return
            
            # Check LOWER boundary (protects from downward crash)
            if strategy.index_lower_stoploss is not None:
                if current_index <= strategy.index_lower_stoploss:
                    logging.info(
                        f"Strategy {strategy.id}: Index LOWER stoploss hit! "
                        f"Index={current_index:.2f} <= {strategy.index_lower_stoploss}"
                    )
                    await execute_combined_exit(
                        strategy,
                        "combined_premium_index_lower_stoploss",
                        net_pnl
                    )
                    return
        else:
            logging.warning(
                f"Strategy {strategy.id}: Missing index price for token {index_token}, "
                "skipping bracket stoploss evaluation"
            )

    # 6. Check partial exit levels
    if strategy.combined_premium_levels:
        for level in strategy.combined_premium_levels:
            if level.get('executed'):
                continue
            
            profit_target = level['profit_points']
            if net_pnl >= profit_target:
                logging.info(
                    f"Strategy {strategy.id}: Level {level['level_number']} hit! "
                    f"P&L={net_pnl:.2f} >= {profit_target}"
                )
                await execute_partial_exit(
                    strategy,
                    level,
                    net_pnl
                )
    
    # 7. Update trailing stoploss
    if strategy.combined_premium_trailing_enabled:
        await update_combined_premium_trailing(strategy, current_premium, net_pnl)


async def update_combined_premium_trailing(strategy, current_premium, net_pnl):
    """
    Update trailing stoploss for combined premium mode.
    Direction-aware: CREDIT trails up, DEBIT trails down.
    """
    # Check if trailing should activate
    if (not strategy.trailing_activated and 
        strategy.combined_premium_trailing_lock_profit and
        net_pnl >= strategy.combined_premium_trailing_lock_profit):
        
        strategy.trailing_activated = True
        strategy.best_net_premium = current_premium
        
        logging.info(
            f"Strategy {strategy.id}: Combined premium trailing activated "
            f"at P&L={net_pnl:.2f}"
        )
    
    if not strategy.trailing_activated:
        return
    
    # Update best premium and trailing SL
    if strategy.combined_premium_entry_type == 'credit':
        # CREDIT (SELL): Trail UP as premium decays (profit)
        if current_premium < strategy.best_net_premium:
            strategy.best_net_premium = current_premium
            new_sl = current_premium + strategy.combined_premium_trailing_distance
            strategy.combined_premium_trailing_sl = new_sl
            
            logging.debug(
                f"Strategy {strategy.id}: Trailing SL updated to {new_sl:.2f}"
            )
        
        # Trigger if premium rises above trailing SL
        if current_premium >= strategy.combined_premium_trailing_sl:
            locked_profit = strategy.initial_net_premium - strategy.combined_premium_trailing_sl
            logging.info(
                f"Strategy {strategy.id}: Combined premium trailing SL hit! "
                f"Locking profit={locked_profit:.2f}"
            )
            await execute_combined_exit(
                strategy,
                "combined_premium_trailing_sl",
                locked_profit
            )
    
    else:  # 'debit'
        # DEBIT (BUY): Trail DOWN as premium rises (profit)
        if current_premium > strategy.best_net_premium:
            strategy.best_net_premium = current_premium
            new_sl = current_premium - strategy.combined_premium_trailing_distance
            strategy.combined_premium_trailing_sl = new_sl
            
            logging.debug(
                f"Strategy {strategy.id}: Trailing SL updated to {new_sl:.2f}"
            )
        
        # Trigger if premium drops below trailing SL
        if current_premium <= strategy.combined_premium_trailing_sl:
            locked_profit = strategy.combined_premium_trailing_sl - strategy.initial_net_premium
            logging.info(
                f"Strategy {strategy.id}: Combined premium trailing SL hit! "
                f"Locking profit={locked_profit:.2f}"
            )
            await execute_combined_exit(
                strategy,
                "combined_premium_trailing_sl",
                locked_profit
            )


async def execute_combined_exit(strategy, trigger_reason, net_pnl):
    """
    Exit all positions in the strategy together.
    """
    logging.info(
        f"Executing combined exit for strategy {strategy.id}: "
        f"Reason={trigger_reason}, P&L={net_pnl:.2f}"
    )
    
    # Exit all positions
    await execute_exit(strategy, trigger_reason, strategy.position_snapshot)
    
    # Log event
    await log_event(strategy, "combined_premium_triggered", {
        "trigger_reason": trigger_reason,
        "net_pnl": net_pnl,
        "initial_premium": strategy.initial_net_premium,
        "current_premium": strategy.current_net_premium
    })
    
    # Update strategy status
    strategy.status = 'triggered'
    await db.update_strategy(strategy.id, {"status": "triggered"})
```

### Order Execution with Idempotency

```python
async def execute_exit(strategy, level, positions):
    correlation_id = uuid4()
    
    for position in positions:
        # Generate idempotency key
        idem_key = f"strategy_{strategy.id[:8]}_{level}_{position.token}"
        
        # Check Redis cache
        cached_order_id = redis.get(idem_key)
        if cached_order_id:
            logging.info(f"Idempotent replay: {cached_order_id}")
            continue
        
        # Place order with retry (max 3 attempts)
        for attempt in range(3):
            try:
                order_id = await orders_service.place_order(
                    tradingsymbol=position.tradingsymbol,
                    quantity=position.quantity_to_exit,
                    transaction_type=reverse_type(position.transaction_type),
                    order_type="MARKET",
                    product=position.product,
                    tag=f"stoploss_{strategy.id[:8]}"
                )
                
                # Cache for 120 seconds
                redis.setex(idem_key, 120, order_id)
                
                # Log event
                log_event(strategy, "order_placed", {
                    "order_id": order_id,
                    "correlation_id": correlation_id,
                    "idempotency_key": idem_key,
                    "level": level
                })
                
                # Start async order monitoring
                asyncio.create_task(monitor_order(order_id, strategy))
                break
                
            except Exception as e:
                if attempt == 2:
                    log_event(strategy, "order_failed", {
                        "error": str(e),
                        "retry_count": attempt + 1
                    })
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
```

---

## 🚦 Status Lifecycle

```
active → Monitoring positions, evaluating triggers
   ↓
partial → Some positions exited, others still active
   ↓
triggered → All stoplosses hit, all positions exited
   ↓
completed → Successfully closed all positions

paused → Temporarily disabled (manual)
error → Error occurred during execution
```

---

## 🔒 Production Considerations

### Security
- ✅ System-level Kite access token (not user tokens)
- ✅ Idempotency keys prevent duplicate orders
- ✅ Correlation IDs for full audit trail
- ✅ Order tags: `stoploss_{strategy_id[:8]}`

### Reliability
- ✅ WebSocket disconnect recovery
- ✅ Database unavailable fallback (Redis cache)
- ✅ Order placement retry (3 attempts, exponential backoff)
- ✅ Partial fill handling
- ✅ Concurrent strategy evaluation (no locks needed)

### Performance
- ✅ 500ms evaluation cycle (sub-second detection)
- ✅ In-memory strategy cache (refresh every 5s)
- ✅ Minimal database writes (only on triggers)
- ✅ Async order monitoring (non-blocking)
- ✅ Indexed queries for active strategies

### Monitoring
- ✅ Health check endpoint
- ✅ Event audit trail with correlation IDs
- ✅ Error tracking per strategy
- ✅ Real-time notifications (ntfy)
- ✅ Redis pub/sub for frontend updates

---

## 📁 File Structure

```
strategies/
├── __init__.py
├── index_stoploss_algo.py    # Main PositionProtectionEngine
├── strike_selector.py         # Delta-based strike selection
├── models.py                  # Pydantic models for all 3 modes
├── router.py                  # FastAPI endpoints
├── trailing.py                # Trailing logic (all modes + direction-aware)
├── orders.py                  # Order execution with idempotency
├── charges.py                 # Virtual contract charge calculation
└── conversion.py              # MIS ↔ NRML product conversion

documents/
├── unified-design-core.md             # This file
├── unified-design-endpoints.md        # API endpoints + testing
└── unified-design-implementation.md   # Integration steps + phases
```

---

**Next Steps:**
1. See `unified-design-endpoints.md` for complete API documentation with curl/swagger examples
2. See `unified-design-implementation.md` for phased implementation plan
