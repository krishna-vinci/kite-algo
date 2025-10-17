# Position Protection System - API Endpoints & Testing

**Version:** 2.0 | **Updated:** 2025-10-16

All endpoints under tag **"index-stoploss"** | Base: `http://localhost:8777/api/strategies`

---

## 📡 Complete Endpoint List

### Position Building (NEW)
1. `GET /mini-chain/{underlying}/{expiry}` - Get 5 strikes with Greeks
2. `POST /suggest-strikes` - Delta-based strike recommendations  
3. `POST /build-position` - Place orders + create protection atomically

### Protection Management
4. `POST /protection` - Create strategy (index/premium/hybrid modes)
5. `GET /` - List all strategies
6. `GET /{id}` - Get strategy details
7. `PUT /{id}` - Update thresholds
8. `PATCH /{id}/status` - Pause/resume
9. `DELETE /{id}` - Delete strategy

### Product Conversion & Monitoring
10. `POST /{id}/convert-to-nrml` - MIS → NRML conversion
11. `POST /{id}/calculate-charges` - Virtual contract charges
12. `GET /{id}/events` - Event history
13. `GET /health` - Engine health check

---

## 🧪 Testing Examples

### 1. Create Premium-Based Protection (NEW)

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "name": "Straddle Protection",
    "monitoring_mode": "premium",
    "premium_thresholds": {
      "12345678": {
        "tradingsymbol": "NIFTY25JAN23500CE",
        "transaction_type": "SELL",
        "entry_price": 150.0,
        "stoploss_price": 200.0,
        "trailing_mode": "continuous",
        "trailing_distance": 20.0
      },
      "87654321": {
        "tradingsymbol": "NIFTY25JAN23500PE",
        "transaction_type": "SELL",
        "entry_price": 145.0,
        "stoploss_price": 195.0,
        "trailing_mode": "continuous",
        "trailing_distance": 20.0
      }
    },
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'
```

**Response:**
```json
{
  "strategy_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "active",
  "monitoring_mode": "premium",
  "positions_captured": 2,
  "total_lots": 8.0,
  "position_snapshot": [
    {
      "instrument_token": 12345678,
      "tradingsymbol": "NIFTY25JAN23500CE",
      "quantity": 200,
      "lots": 4.0,
      "transaction_type": "SELL",
      "current_ltp": 148.25
    }
  ]
}
```

---

### 2. Create Index-Based Protection (Original)

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "name": "NIFTY Index Protection",
    "monitoring_mode": "index",
    "index_instrument_token": 256265,
    "index_tradingsymbol": "NIFTY 50",
    "index_lower_stoploss": 23000.0,
    "trailing_mode": "continuous",
    "trailing_distance": 100.0,
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'
```

---

### 3. Create Hybrid Protection (NEW)

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "name": "Hybrid Protection",
    "monitoring_mode": "hybrid",
    "exit_logic": "any",
    "index_instrument_token": 256265,
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "premium_thresholds": {
      "12345678": {
        "tradingsymbol": "NIFTY25JAN24000CE",
        "transaction_type": "SELL",
        "stoploss_price": 200.0,
        "trailing_mode": "continuous",
        "trailing_distance": 20.0
      }
    },
    "position_filter": {"exchange": "NFO", "product": "MIS"}
  }'
```

---

### 4. Create Combined Premium Protection (NEW - For Straddles/Strangles)

**cURL (Credit Strategy - SELL Straddle):**
```bash
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "name": "NIFTY Straddle - 50pt Target",
    "strategy_type": "straddle",
    "monitoring_mode": "combined_premium",
    "combined_premium_entry_type": "credit",
    "combined_premium_profit_target": 50.0,
    "combined_premium_trailing_enabled": true,
    "combined_premium_trailing_distance": 30.0,
    "combined_premium_trailing_lock_profit": 30.0,
    "index_instrument_token": 256265,
    "index_tradingsymbol": "NIFTY 50",
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "position_filter": {
      "exchange": "NFO",
      "product": "MIS",
      "strategy_positions": [
        {
          "instrument_token": 12345678,
          "tradingsymbol": "NIFTY25JAN24000CE"
        },
        {
          "instrument_token": 87654321,
          "tradingsymbol": "NIFTY25JAN24000PE"
        }
      ]
    }
  }'
```

**Response:**
```json
{
  "strategy_id": "660e8400-e29b-41d4-a716-446655440002",
  "status": "active",
  "monitoring_mode": "combined_premium",
  "combined_premium_entry_type": "credit",
  "positions_captured": 2,
  "total_lots": 8.0,
  "initial_net_premium": 295.0,
  "current_net_premium": 295.0,
  "index_protection": {
    "index_instrument_token": 256265,
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0
  },
  "position_snapshot": [
    {
      "instrument_token": 12345678,
      "tradingsymbol": "NIFTY25JAN24000CE",
      "transaction_type": "SELL",
      "quantity": 200,
      "lots": 4.0,
      "average_price": 150.50,
      "current_ltp": 150.50
    },
    {
      "instrument_token": 87654321,
      "tradingsymbol": "NIFTY25JAN24000PE",
      "transaction_type": "SELL",
      "quantity": 200,
      "lots": 4.0,
      "average_price": 144.50,
      "current_ltp": 144.50
    }
  ],
  "targets": {
    "profit_target": 50.0,
    "profit_trigger_at_premium": 245.0
  },
  "created_at": "2025-01-16T10:30:00Z"
}
```

**cURL (With Partial Exits):**
```bash
curl -X POST "http://localhost:8777/api/strategies/protection" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "name": "NIFTY Strangle - Partial Exits",
    "strategy_type": "strangle",
    "monitoring_mode": "combined_premium",
    "combined_premium_entry_type": "credit",
    "combined_premium_levels": [
      {
        "level_number": 1,
        "profit_points": 30.0,
        "exit_percent": 50
      },
      {
        "level_number": 2,
        "profit_points": 60.0,
        "exit_percent": 100
      }
    ],
    "index_instrument_token": 256265,
    "index_tradingsymbol": "NIFTY 50",
    "index_upper_stoploss": 24000.0,
    "index_lower_stoploss": 23000.0,
    "position_filter": {
      "exchange": "NFO",
      "product": "MIS"
    }
  }'
```

---

### 4. Get Mini Option Chain

**cURL:**
```bash
curl "http://localhost:8777/api/strategies/mini-chain/NIFTY/2025-01-30?center_strike=23500&count=5" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "underlying": "NIFTY",
  "expiry": "2025-01-30",
  "atm_strike": 23500,
  "spot_price": 23487.50,
  "strikes": [
    {
      "strike": 23300,
      "CE": {
        "instrument_token": 12345678,
        "tradingsymbol": "NIFTY25JAN23300CE",
        "ltp": 280.50,
        "delta": 0.6234,
        "iv": 18.5,
        "lot_size": 50
      },
      "PE": {
        "instrument_token": 87654321,
        "tradingsymbol": "NIFTY25JAN23300PE",
        "ltp": 95.25,
        "delta": -0.3766,
        "iv": 17.8,
        "lot_size": 50
      }
    }
  ]
}
```

---

### 5. Suggest Strikes (Delta-based)

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/suggest-strikes" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "underlying": "NIFTY",
    "expiry": "2025-01-30",
    "strategy_type": "strangle",
    "target_delta": 0.30,
    "risk_amount": 50000
  }'
```

**Response:**
```json
{
  "strategy_type": "strangle",
  "target_delta": 0.30,
  "suggestions": {
    "CE": {
      "strike": 23900,
      "tradingsymbol": "NIFTY25JAN23900CE",
      "ltp": 125.50,
      "delta": 0.3012,
      "lots_recommended": 4,
      "quantity": 200,
      "premium_collected": 25100
    },
    "PE": {
      "strike": 23100,
      "tradingsymbol": "NIFTY25JAN23100PE",
      "ltp": 118.25,
      "delta": -0.2987,
      "lots_recommended": 4,
      "quantity": 200,
      "premium_collected": 23650
    }
  },
  "combined": {
    "total_premium": 48750,
    "total_margin_required": 185000,
    "breakeven_range": [22518, 24151]
  }
}
```

---

### 6. Build Position + Auto Protection

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/build-position" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "strategy_type": "strangle",
    "underlying": "NIFTY",
    "expiry": "2025-01-30",
    "target_delta": 0.30,
    "risk_amount": 50000,
    "protection_config": {
      "monitoring_mode": "hybrid",
      "index_upper_stoploss": 24000,
      "index_lower_stoploss": 23000,
      "premium_stoploss_percent": 100,
      "trailing_mode": "continuous",
      "trailing_distance": 100
    },
    "place_orders": true,
    "product": "MIS"
  }'
```

**Response:**
```json
{
  "status": "success",
  "strategy_id": "550e8400-e29b-41d4-a716-446655440000",
  "orders_placed": [
    {
      "order_id": "240116000123456",
      "tradingsymbol": "NIFTY25JAN23900CE",
      "quantity": 200,
      "status": "COMPLETE",
      "average_price": 125.25
    },
    {
      "order_id": "240116000123457",
      "tradingsymbol": "NIFTY25JAN23100PE",
      "quantity": 200,
      "status": "COMPLETE",
      "average_price": 118.10
    }
  ],
  "protection_created": {
    "monitoring_mode": "hybrid",
    "positions_protected": 2,
    "total_lots": 8
  },
  "summary": {
    "total_premium_collected": 48670,
    "protection_active": true
  }
}
```

---

### 7. List Strategies

**cURL:**
```bash
curl "http://localhost:8777/api/strategies?status=active&monitoring_mode=premium" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "total": 5,
  "strategies": [
    {
      "strategy_id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "Straddle Protection",
      "monitoring_mode": "premium",
      "status": "active",
      "total_lots": 8.0,
      "pnl": 3250.50,
      "pnl_percent": 6.54,
      "created_at": "2025-01-16T10:30:00Z"
    }
  ]
}
```

---

### 8. Get Strategy Details

**cURL:**
```bash
curl "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "strategy_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Straddle Protection",
  "monitoring_mode": "premium",
  "status": "active",
  "premium_thresholds": {
    "12345678": {
      "tradingsymbol": "NIFTY25JAN23500CE",
      "entry_price": 150.0,
      "stoploss_price": 200.0,
      "current_ltp": 148.25,
      "current_trailing_sl": 178.25,
      "trailing_activated": true,
      "pnl": 450.00,
      "distance_to_sl": 30.00
    }
  },
  "remaining_quantities": {...},
  "placed_orders": [],
  "levels_executed": []
}
```

---

### 9. Update Strategy

**cURL:**
```bash
curl -X PUT "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "premium_thresholds": {
      "12345678": {
        "stoploss_price": 210.0,
        "trailing_distance": 25.0
      }
    }
  }'
```

---

### 10. Pause/Resume Strategy

**cURL:**
```bash
# Pause
curl -X PATCH "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000/status" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{"status": "paused"}'

# Resume
curl -X PATCH "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000/status" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{"status": "active"}'
```

---

### 11. Convert to NRML

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000/convert-to-nrml" \
  -H "Content-Type: application/json" \
  -H "Cookie: kite_session_id=YOUR_SESSION" \
  -d '{
    "instrument_tokens": [12345678],
    "reason": "manual_conversion"
  }'
```

---

### 12. Calculate Charges

**cURL:**
```bash
curl -X POST "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000/calculate-charges" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "status": "success",
  "total_charges": 245.50,
  "breakdown": {
    "brokerage": 20.00,
    "stt": 100.00,
    "gst": 15.00
  },
  "per_position": {
    "12345678": {"estimated_charges": 122.75}
  }
}
```

---

### 13. Get Event History

**cURL:**
```bash
curl "http://localhost:8777/api/strategies/550e8400-e29b-41d4-a716-446655440000/events?limit=50" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "strategy_id": "550e8400-e29b-41d4-a716-446655440000",
  "total_events": 127,
  "events": [
    {
      "event_id": 1001,
      "event_type": "premium_stoploss_triggered",
      "trigger_price": 201.25,
      "instrument_token": 12345678,
      "order_id": "240116000123458",
      "created_at": "2025-01-16T14:35:22Z"
    }
  ]
}
```

---

### 14. Health Check

**cURL:**
```bash
curl "http://localhost:8777/api/strategies/health" \
  -H "Cookie: kite_session_id=YOUR_SESSION"
```

**Response:**
```json
{
  "status": "healthy",
  "engine_running": true,
  "active_strategies": 15,
  "monitoring_modes": {
    "index": 4,
    "premium": 6,
    "combined_premium": 3,
    "hybrid": 2
  },
  "last_evaluation": "2025-01-16T14:35:42.123Z",
  "websocket_status": "connected",
  "evaluation_interval_ms": 500,
  "orders_placed_today": 47
}
```

---

## 🎯 Testing in FastAPI Swagger UI

### Access Swagger
**URL:** `http://localhost:8777/docs`

### Locate Endpoints
Scroll to section: **"index-stoploss"**

### Complete Test Workflow

#### Step 1: Health Check
```
GET /api/strategies/health
```
Verify engine is running

#### Step 2: Get Mini Chain
```
GET /api/strategies/mini-chain/NIFTY/2025-01-30
Query: center_strike=23500, count=5
```
Browse available strikes

#### Step 3: Suggest Strikes
```
POST /api/strategies/suggest-strikes
Body:
{
  "underlying": "NIFTY",
  "expiry": "2025-01-30",
  "strategy_type": "strangle",
  "target_delta": 0.30,
  "risk_amount": 50000
}
```
Get delta-based recommendations

#### Step 4: Build Position (Dry Run)
```
POST /api/strategies/build-position
Body: {...from step 3..., "place_orders": false}
```
Test without placing real orders

#### Step 5: Build Position (Execute)
```
POST /api/strategies/build-position
Body: {...from step 3..., "place_orders": true}
```
Place real orders + create protection

#### Step 6: Monitor Strategy
```
GET /api/strategies/{strategy_id}
```
Check positions and thresholds

#### Step 7: View Events
```
GET /api/strategies/{strategy_id}/events
```
Review event history

#### Step 8: Update (Optional)
```
PUT /api/strategies/{strategy_id}
Body: {"premium_thresholds": {...}}
```
Adjust thresholds if needed

#### Step 9: Pause (Optional)
```
PATCH /api/strategies/{strategy_id}/status
Body: {"status": "paused"}
```
Temporarily disable monitoring

---

## 📊 Event Types Reference

| Event Type | Description | Trigger |
|------------|-------------|---------|
| `created` | Strategy created | API call |
| `index_upper_stoploss_triggered` | Index UPPER boundary crossed | Index >= upper SL (NEW) |
| `index_lower_stoploss_triggered` | Index LOWER boundary crossed | Index <= lower SL (NEW) |
| `premium_stoploss_triggered` | Premium threshold crossed | Option LTP |
| `combined_premium_triggered` | Net P&L target hit | Net premium (NEW) |
| `combined_premium_profit_target` | Profit target hit | Net P&L >= target (NEW) |
| `combined_premium_index_upper_stoploss` | Index upper bracket hit | Index >= upper (NEW) |
| `combined_premium_index_lower_stoploss` | Index lower bracket hit | Index <= lower (NEW) |
| `combined_premium_trailing_sl` | Trailing SL hit | Net premium (NEW) |
| `trailing_activated` | Trailing started | Profit lock reached |
| `trailing_updated` | Trailing level moved | Price movement |
| `level_triggered` | TP/Exit level hit | Price threshold |
| `order_placed` | Order placed | System |
| `order_filled` | Order filled | Kite |
| `order_failed` | Order failed | Kite error |
| `product_converted` | MIS → NRML | Manual/Auto |
| `paused` | Strategy paused | API call |
| `resumed` | Strategy resumed | API call |
| `completed` | All positions exited | All orders filled |
| `error` | Error occurred | System error |

---

## 🔑 Key Testing Scenarios

### Scenario 1: Premium-Only Protection (NEW)
Monitor option premiums with direction-aware trailing for naked selling.

### Scenario 2: Index-Only Protection (Original)
Monitor index price for options when underlying moves.

### Scenario 3: Combined Premium Protection (NEW)
Monitor net P&L across all strategy legs (straddle/strangle). Exit when net profit/loss targets hit.

**Use Cases:**
- SELL straddle: Exit when 50 points profit collected (premium decays)
- BUY straddle: Exit when 50 points profit reached (premium rises)
- Partial exits: Book 50% at 30pt profit, remaining at 60pt profit
- Trailing: Lock profit as net premium moves favorably

### Scenario 4: Hybrid Protection (NEW)
Monitor both index AND premium, exit on first trigger.

### Scenario 5: Delta-Based Position Building (NEW)
Find strikes by delta, calculate lots, place orders, and auto-protect.

---

**Next:** See `unified-design-implementation.md` for phased implementation plan
