# Position Protection System - Implementation Plan

**Version:** 2.0 | **Updated:** 2025-10-16

---

## 🎯 Implementation Strategy

**Approach:** Phased development with incremental feature rollout

**Total Duration:** 4-5 weeks

**Principle:** Each phase is independently testable and deployable

---

## 📋 Phase 1: Core Engine + Index Mode (Week 1)

### Goal
Get basic system working with original index-based monitoring.

### Tasks

#### 1.1 Database Setup
- [ ] Run schema migration for `position_protection_strategies` table
- [ ] Run schema migration for `strategy_events` table
- [ ] Verify indexes created
- [ ] Test with sample data

**File:** `migrations/add_position_protection_tables.sql`
```sql
-- Copy schema from unified-design-core.md
```

#### 1.2 Core Models
- [ ] Create `strategies/models.py` with Pydantic models
  - `ProtectionStrategyBase`
  - `CreateProtectionRequest`
  - `ProtectionStrategyResponse`
  - `PositionSnapshot`
  - `IndexConfig`
  - `TrailingConfig`

**Estimated:** 200 lines

#### 1.3 Engine Skeleton
- [ ] Create `strategies/index_stoploss_algo.py`
- [ ] Implement `PositionProtectionEngine` class
- [ ] Basic evaluation loop (500ms)
- [ ] DB refresh logic (every 5s)
- [ ] WebSocket integration for index prices
- [ ] Simple MARKET order execution

**Key Methods:**
```python
class PositionProtectionEngine:
    def __init__(db, ws_manager, orders_service, app)
    def start()
    def stop()
    async def _evaluation_loop()
    async def _load_active_strategies()
    async def _check_index_triggers(strategy, current_price)
    async def _execute_exit(strategy, positions)
```

**Estimated:** 400 lines

#### 1.4 Basic Router
- [ ] Create `strategies/router.py`
- [ ] Implement endpoints:
  - `POST /protection` (index mode only)
  - `GET /` (list)
  - `GET /{id}` (details)
  - `DELETE /{id}` (delete)
  - `GET /health`

**Estimated:** 300 lines

#### 1.5 Integration
- [ ] Modify `main.py` - add engine initialization
- [ ] Add router with prefix `/api/strategies`
- [ ] Test engine startup/shutdown

**Changes to main.py:**
```python
from strategies.index_stoploss_algo import PositionProtectionEngine
from strategies.router import router as strategies_router

@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    # ... existing startup ...
    
    # Initialize PositionProtectionEngine
    try:
        from broker_api.kite_orders import OrdersService
        orders_service = OrdersService()
        
        protection_engine = PositionProtectionEngine(
            db=async_db,
            ws_manager=ws_manager,
            orders_service=orders_service,
            app=app
        )
        protection_engine.start()
        app.state.protection_engine = protection_engine
        logging.info("PositionProtectionEngine started (500ms interval)")
    except Exception as e:
        logging.error(f"Failed to start PositionProtectionEngine: {e}")
    
    yield
    
    # Shutdown
    try:
        engine = getattr(app.state, "protection_engine", None)
        if engine:
            await engine.stop()
            logging.info("PositionProtectionEngine stopped")
    except Exception:
        pass

# Include router
app.include_router(
    strategies_router, 
    prefix="/api/strategies", 
    tags=["index-stoploss"]
)
```

### Testing Phase 1
```bash
# 1. Create index-based protection (directional - lower boundary only)
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Index Protection - Directional",
    "monitoring_mode": "index",
    "index_instrument_token": 256265,
    "index_lower_stoploss": 23000.0,
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'

# 2. Create bracket protection (market-neutral - both boundaries)
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Bracket Protection - Neutral",
    "monitoring_mode": "index",
    "index_instrument_token": 256265,
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'

# 3. Verify in DB
psql -d your_db -c "SELECT id, name, index_upper_stoploss, index_lower_stoploss FROM position_protection_strategies;"

# 4. Check health
curl "http://localhost:8777/api/strategies/health"

# 5. Monitor logs
tail -f logs/app.log | grep "PositionProtectionEngine"

# 6. Test bracket triggers (simulate index movement)
# Verify that BOTH upper and lower boundaries trigger exits correctly
```

### Deliverables Phase 1
- ✅ Index-based monitoring working
- ✅ Basic order execution
- ✅ 4 endpoints functional
- ✅ Engine running in production

---

## 📋 Phase 2: Premium Mode + Direction-Aware Trailing (Week 2)

### Goal
Add premium-based monitoring with SELL/BUY aware trailing.

### Tasks

#### 2.1 Premium Models
- [ ] Extend models.py with:
  - `PremiumThresholdConfig`
  - `PremiumMonitoringState`
  - Direction-aware trailing fields

**Estimated:** 150 lines

#### 2.2 Trailing Logic
- [ ] Create `strategies/trailing.py`
- [ ] Implement trailing modes:
  - `update_index_trailing()` - Continuous/Step/ATR
  - `update_premium_trailing_sell()` - Trail UP
  - `update_premium_trailing_buy()` - Trail DOWN

**Key Logic:**
```python
def update_premium_trailing_sell(config, current_ltp):
    """SELL position: Premium drops → SL trails UP"""
    if current_ltp < config.lowest_premium:
        config.lowest_premium = current_ltp
        new_sl = current_ltp + config.trailing_distance
        config.current_trailing_sl = min(new_sl, config.stoploss_price)
    
    if current_ltp >= config.current_trailing_sl:
        return True  # Trigger exit
    return False

def update_premium_trailing_buy(config, current_ltp):
    """BUY position: Premium rises → SL trails DOWN"""
    if current_ltp > config.highest_premium:
        config.highest_premium = current_ltp
        new_sl = current_ltp - config.trailing_distance
        config.current_trailing_sl = max(new_sl, config.stoploss_price)
    
    if current_ltp <= config.current_trailing_sl:
        return True  # Trigger exit
    return False
```

**Estimated:** 300 lines

#### 2.3 Engine Enhancement
- [ ] Add premium monitoring to evaluation loop
- [ ] Subscribe to option tokens in WebSocket
- [ ] Implement `_check_premium_triggers()`
- [ ] Add direction-aware logic
- [ ] Handle per-position thresholds

**Estimated:** 250 lines (additions)

#### 2.4 Router Enhancement
- [ ] Update `POST /protection` to accept premium mode
- [ ] Add `PUT /{id}` endpoint for updates

**Estimated:** 150 lines (additions)

### Testing Phase 2
```bash
# 1. Create premium-based protection
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Premium Test",
    "monitoring_mode": "premium",
    "premium_thresholds": {
      "12345678": {
        "tradingsymbol": "NIFTY25JAN23500CE",
        "transaction_type": "SELL",
        "entry_price": 150.0,
        "stoploss_price": 200.0,
        "trailing_mode": "continuous",
        "trailing_distance": 20.0
      }
    },
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'

# 2. Verify trailing updates in real-time
curl "http://localhost:8777/api/strategies/{id}" | jq '.premium_thresholds'

# 3. Test SELL trailing
# - Short CE at 150
# - Wait for premium to drop to 140
# - Verify SL moved from 200 to 160 (140 + 20)

# 4. Test BUY trailing
# - Long CE at 150
# - Wait for premium to rise to 180
# - Verify SL moved from 120 to 160 (180 - 20)
```

### Deliverables Phase 2
- ✅ Premium monitoring functional
- ✅ Direction-aware trailing working
- ✅ SELL positions trail UP correctly
- ✅ BUY positions trail DOWN correctly
- ✅ Per-position thresholds supported

---

## 📋 Phase 3: Position Building + Delta Selection (Week 3)

### Goal
Add strike selection and automated position building.

### Tasks

#### 3.1 InstrumentsRepository Enhancement
- [ ] Add `find_strikes_by_delta_range()`
- [ ] Add `get_atm_strike()`
- [ ] Add `get_strikes_around_atm()`

**File:** `broker_api/instruments_repository.py`
```python
def find_strikes_by_delta_range(
    self,
    underlying: str,
    expiry: date,
    target_delta: float,
    option_type: str,  # 'CE' or 'PE'
    tolerance: float = 0.05
) -> List[Dict]:
    """Find strikes within delta range ±tolerance"""
    # Query OptionsSessionManager for option chain
    # Filter by delta range
    pass

def get_atm_strike(
    self,
    underlying: str,
    current_spot: float,
    expiry: date
) -> float:
    """Calculate ATM strike closest to spot"""
    strikes = self.get_distinct_strikes(underlying, expiry)
    return min(strikes, key=lambda x: abs(x - current_spot))

def get_strikes_around_atm(
    self,
    atm_strike: float,
    all_strikes: List[float],
    count: int = 5
) -> List[float]:
    """Get N strikes centered around ATM"""
    atm_index = all_strikes.index(atm_strike)
    half = count // 2
    start = max(0, atm_index - half)
    end = min(len(all_strikes), atm_index + half + 1)
    return all_strikes[start:end]
```

**Estimated:** 200 lines

#### 3.2 Strike Selector
- [ ] Create `strategies/strike_selector.py`
- [ ] Implement `StrikeSelector` class
- [ ] Delta-based selection logic
- [ ] Risk-based lot calculation

**Key Methods:**
```python
class StrikeSelector:
    def __init__(options_session_manager, instruments_repo)
    
    async def get_mini_chain(underlying, expiry, center_strike, count)
    async def suggest_strikes(underlying, expiry, strategy_type, 
                             target_delta, risk_amount)
    async def calculate_lots_for_risk(premium, risk_amount, lot_size)
```

**Estimated:** 350 lines

#### 3.3 Router Enhancement
- [ ] Add `GET /mini-chain/{underlying}/{expiry}`
- [ ] Add `POST /suggest-strikes`
- [ ] Add `POST /build-position`

**Estimated:** 350 lines (additions)

#### 3.4 Engine Integration
- [ ] Pass `options_session_manager` to engine
- [ ] Pass `instruments_repo` to engine
- [ ] Initialize `StrikeSelector` in engine

### Testing Phase 3
```bash
# 1. Get mini chain
curl "http://localhost:8777/api/strategies/mini-chain/NIFTY/2025-01-30?center_strike=23500"

# 2. Suggest strikes
curl -X POST "http://localhost:8777/api/strategies/suggest-strikes" \
  -H "Content-Type: application/json" \
  -d '{
    "underlying": "NIFTY",
    "expiry": "2025-01-30",
    "strategy_type": "strangle",
    "target_delta": 0.30,
    "risk_amount": 50000
  }'

# 3. Build position (dry run)
curl -X POST "http://localhost:8777/api/strategies/build-position" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_type": "strangle",
    "underlying": "NIFTY",
    "expiry": "2025-01-30",
    "target_delta": 0.30,
    "risk_amount": 50000,
    "protection_config": {
      "monitoring_mode": "premium",
      "premium_stoploss_percent": 100
    },
    "place_orders": false
  }'

# 4. Build position (execute)
# Change "place_orders": true
```

### Deliverables Phase 3
- ✅ Mini chain endpoint working
- ✅ Delta-based strike selection accurate
- ✅ Lot calculation correct
- ✅ Atomic position building + protection
- ✅ Dry run mode functional

---

## 📋 Phase 4: Hybrid Mode + Advanced Features (Week 4)

### Goal
Complete all features and polish.

### Tasks

#### 4.1 Hybrid Mode Implementation
- [ ] Add hybrid monitoring to evaluation loop
- [ ] Implement exit_logic ('any' vs 'all')
- [ ] Handle complex trigger scenarios

**Logic:**
```python
async def _check_hybrid_triggers(strategy):
    index_triggered = await _check_index_triggers(strategy)
    premium_triggered = await _check_premium_triggers(strategy)
    
    if strategy.exit_logic == 'any':
        if index_triggered or premium_triggered:
            await _execute_exit(strategy, "hybrid_trigger")
    
    elif strategy.exit_logic == 'all':
        if index_triggered and premium_triggered:
            await _execute_exit(strategy, "hybrid_trigger")
```

**Estimated:** 150 lines

#### 4.2 Order Execution Enhancement
- [ ] Create `strategies/orders.py`
- [ ] Implement idempotency with Redis
- [ ] Add retry logic (exponential backoff)
- [ ] Async order monitoring
- [ ] Handle partial fills

**Estimated:** 400 lines

#### 4.3 Product Conversion
- [ ] Create `strategies/conversion.py`
- [ ] Implement MIS → NRML conversion
- [ ] Add `POST /{id}/convert-to-nrml` endpoint

**Estimated:** 200 lines

#### 4.4 Virtual Contract Charges
- [ ] Create `strategies/charges.py`
- [ ] Implement charge calculation
- [ ] Add `POST /{id}/calculate-charges` endpoint

**Estimated:** 250 lines

#### 4.5 Event System
- [ ] Add comprehensive event logging
- [ ] Implement `GET /{id}/events` endpoint
- [ ] Add Redis pub/sub for real-time updates
- [ ] Add ntfy notifications

**Estimated:** 200 lines

#### 4.6 Final Router Endpoints
- [ ] Complete `PATCH /{id}/status` (pause/resume)
- [ ] Enhance health check with detailed stats

**Estimated:** 100 lines

#### 4.7 Combined Premium Mode (NEW)
- [ ] Add `combined_premium_entry_type` to models
- [ ] Add combined premium config fields to schema
- [ ] Implement `_check_combined_premium_triggers()` in engine
- [ ] Implement `update_combined_premium_trailing()`
- [ ] Handle CREDIT vs DEBIT strategies
- [ ] Support partial exits at profit levels
- [ ] Update router to accept combined_premium mode

**Key Implementation:**
```python
# In engine evaluation loop
elif strategy.monitoring_mode == 'combined_premium':
    await self._check_combined_premium_triggers(strategy, option_prices)

# Calculate net P&L
if strategy.combined_premium_entry_type == 'credit':
    net_pnl = initial_premium - current_premium  # SELL: profit when decays
else:  # 'debit'
    net_pnl = current_premium - initial_premium  # BUY: profit when rises

# Check targets
if net_pnl >= profit_target:
    execute_all_positions()
elif net_pnl <= -stoploss_target:
    execute_all_positions()
```

**Validation in Router:**
```python
if create_request.monitoring_mode == 'combined_premium':
    if not create_request.combined_premium_entry_type:
        raise ValueError("combined_premium_entry_type required")
    if not (create_request.combined_premium_profit_target or 
            create_request.combined_premium_stoploss_target):
        raise ValueError("At least one target required")
```

**Estimated:** 300 lines (engine + models + router)

### Testing Phase 4
```bash
# 1. Test hybrid mode
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -d '{
    "monitoring_mode": "hybrid",
    "exit_logic": "any",
    ...
  }'

# 2. Test idempotency
# Place same order twice, verify only one execution

# 3. Test partial fills
# Use LIMIT orders, verify quantity tracking

# 4. Test MIS → NRML conversion
curl -X POST "http://localhost:8777/api/strategies/{id}/convert-to-nrml" \
  -d '{"instrument_tokens": [], "reason": "manual"}'

# 5. Test charges calculation
curl -X POST "http://localhost:8777/api/strategies/{id}/calculate-charges"

# 6. Test event history
curl "http://localhost:8777/api/strategies/{id}/events?limit=50"

# 7. Test combined premium mode (CREDIT - SELL straddle with BRACKET protection)
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "SELL Straddle Protection",
    "monitoring_mode": "combined_premium",
    "combined_premium_entry_type": "credit",
    "combined_premium_profit_target": 50.0,
    "combined_premium_trailing_enabled": true,
    "combined_premium_trailing_distance": 30.0,
    "index_instrument_token": 256265,
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'

# 8. Test combined premium mode (DEBIT - BUY straddle with BRACKET protection)
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BUY Straddle Protection",
    "monitoring_mode": "combined_premium",
    "combined_premium_entry_type": "debit",
    "combined_premium_profit_target": 50.0,
    "index_instrument_token": 256265,
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'

# 9. Verify combined premium tracking + bracket protection
curl "http://localhost:8777/api/strategies/{id}" | jq '{
  initial_net_premium,
  current_net_premium,
  net_pnl: (.initial_net_premium - .current_net_premium),
  profit_target,
  index_upper_stoploss,
  index_lower_stoploss
}'

# 10. Test bracket triggers (CRITICAL for neutral strategies)
# Simulate index hitting upper boundary (24000) - should exit immediately
# Simulate index hitting lower boundary (23000) - should exit immediately
# Verify BOTH boundaries work independently

# 10. Load testing
# Create 50 active strategies across all 4 modes, monitor engine performance
```

### Deliverables Phase 4
- ✅ Hybrid mode working
- ✅ Combined premium mode working (CREDIT & DEBIT)
- ✅ Combined premium trailing functional
- ✅ Bracket stop-loss (two-way protection) working
- ✅ BOTH upper and lower boundaries trigger correctly
- ✅ Partial exits at profit levels
- ✅ Idempotent order execution
- ✅ Product conversion functional
- ✅ Charge calculation accurate
- ✅ Event logging comprehensive
- ✅ All 13 endpoints complete

---

## 📋 Phase 5: Testing, Documentation & Deployment (Week 5)

### Goal
Production readiness.

### Tasks

#### 5.1 Integration Testing
- [ ] Test all 4 monitoring modes (index, premium, combined_premium, hybrid)
- [ ] Test all trailing modes (continuous, step, ATR, combined premium trailing)
- [ ] **Test bracket stop-loss (CRITICAL)**:
  - [ ] Verify upper boundary triggers exit (index rallies)
  - [ ] Verify lower boundary triggers exit (index crashes)
  - [ ] Verify BOTH boundaries work for market-neutral strategies
  - [ ] Verify directional strategies work with single boundary
- [ ] Test CREDIT vs DEBIT strategies (combined_premium)
- [ ] Test partial exits and profit levels
- [ ] Test order execution scenarios
- [ ] Test error recovery
- [ ] Load test (100+ active strategies across all modes)

#### 5.2 Documentation
- [ ] API documentation (Swagger complete)
- [ ] User guide with examples
- [ ] Troubleshooting guide
- [ ] Configuration reference

#### 5.3 Monitoring Setup
- [ ] Add Prometheus metrics
- [ ] Setup Grafana dashboards
- [ ] Configure alerting rules
- [ ] Test notification channels

#### 5.4 Production Deployment
- [ ] Database migration in production
- [ ] Gradual rollout strategy
- [ ] Monitoring during initial deployment
- [ ] Gather user feedback

---

## 📁 Final File Structure

```
strategies/
├── __init__.py                      # Package init
├── index_stoploss_algo.py          # Main PositionProtectionEngine (800 lines)
├── strike_selector.py              # Delta-based selection (350 lines)
├── models.py                       # Pydantic models (500 lines)
├── router.py                       # API endpoints (700 lines)
├── trailing.py                     # Trailing logic (300 lines)
├── orders.py                       # Order execution (400 lines)
├── charges.py                      # Charge calculation (250 lines)
└── conversion.py                   # Product conversion (200 lines)

documents/
├── unified-design-core.md          # Architecture & schema (this file)
├── unified-design-endpoints.md     # API documentation
└── unified-design-implementation.md # This implementation plan

migrations/
└── add_position_protection_tables.sql  # Database schema
```

**Total New Code:** ~3,500 lines (clean, well-organized)

---

## 🧪 Testing Checklist

### Unit Tests
- [ ] Models validation
- [ ] Trailing calculations
- [ ] Delta selection logic
- [ ] Lot calculations
- [ ] Idempotency key generation

### Integration Tests
- [ ] Engine startup/shutdown
- [ ] WebSocket integration
- [ ] Database operations
- [ ] Order placement
- [ ] Event logging

### End-to-End Tests
- [ ] Index mode workflow
- [ ] Premium mode workflow
- [ ] Hybrid mode workflow
- [ ] Position building workflow
- [ ] Product conversion workflow

### Load Tests
- [ ] 100 active strategies
- [ ] 500ms evaluation cycle maintained
- [ ] Memory usage stable
- [ ] No database bottlenecks

### Error Scenarios
- [ ] WebSocket disconnect
- [ ] Database unavailable
- [ ] Order placement failure
- [ ] Partial fill handling
- [ ] Invalid configuration

---

## 🚨 Critical Success Factors

### Performance
- ✅ 500ms evaluation loop maintained under load
- ✅ Sub-second trigger detection
- ✅ Memory usage < 500MB for 100 strategies

### Reliability
- ✅ Zero duplicate orders (idempotency working)
- ✅ Graceful WebSocket recovery
- ✅ No data loss during crashes

### Accuracy
- ✅ Direction-aware trailing correct (SELL up, BUY down)
- ✅ Delta selection within ±0.02 tolerance
- ✅ Lot calculations precise

### Usability
- ✅ API intuitive and well-documented
- ✅ Swagger UI complete with examples
- ✅ Error messages helpful

---

## 📊 Code Complexity Estimate

| Component | Lines | Complexity |
|-----------|-------|------------|
| Engine | 800 | High |
| Strike Selector | 350 | Medium |
| Models | 500 | Low |
| Router | 700 | Medium |
| Trailing | 300 | Medium |
| Orders | 400 | High |
| Charges | 250 | Medium |
| Conversion | 200 | Low |
| **Total** | **3,500** | **Medium** |

---

## 🎯 Success Metrics

### Week 1
- [ ] Engine running in production
- [ ] Index mode protecting real positions
- [ ] 4 endpoints functional

### Week 2
- [ ] Premium mode active
- [ ] Direction-aware trailing verified
- [ ] 10+ strategies running in production

### Week 3
- [ ] Delta selection accurate
- [ ] 5+ positions built via API
- [ ] Mini chain used by traders

### Week 4
- [ ] All 13 endpoints complete
- [ ] Hybrid mode tested
- [ ] 50+ active strategies

### Week 5
- [ ] Production stable
- [ ] User satisfaction high
- [ ] Zero critical bugs

---

## 🔧 Environment Configuration

```bash
# Engine settings
PROTECTION_ENGINE_INTERVAL_MS=500
PROTECTION_ENGINE_REFRESH_SEC=5
PROTECTION_PERSIST_THROTTLE_SEC=1.0

# Order execution
PROTECTION_ORDER_TIMEOUT_SEC=10
PROTECTION_MAX_RETRIES=3
PROTECTION_ORDER_MONITOR_MAX_SEC=60

# Idempotency
PROTECTION_IDEMPOTENCY_TTL_SEC=120

# Notifications
PROTECTION_NTFY_URL=https://ntfy.example.com/stoploss

# Feature flags
PROTECTION_ENABLE_PREMIUM_MODE=true
PROTECTION_ENABLE_HYBRID_MODE=true
PROTECTION_ENABLE_POSITION_BUILDING=true
PROTECTION_ENABLE_PRODUCT_CONVERSION=true
```

---

## 📞 Support & Rollback

### Rollback Strategy
If critical issues found:
1. Set feature flags to disable new modes
2. Keep engine running for existing strategies
3. Fix issues in separate branch
4. Re-deploy with fixes

### Gradual Rollout
1. **Day 1-3:** Internal testing only
2. **Day 4-7:** Beta users (5-10 traders)
3. **Day 8-14:** Early adopters (20-30 traders)
4. **Day 15+:** General availability

---

**Ready to implement! Each phase is independently testable and deployable.**
