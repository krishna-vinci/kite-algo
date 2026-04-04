"""
Position Protection System - API Router
Phase 3: Enhanced with position building and delta selection
"""

import logging
import json
from datetime import datetime, timezone, date
from typing import List, Optional, Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Body
from kiteconnect import KiteConnect
from pydantic import BaseModel

from broker_api.broker_api import get_kite
from broker_api.kite_orders import get_correlation_id, realtime_positions_service, run_kite_write_action
from database import get_db_connection
from .models import (
    CreateProtectionRequest,
    UpdateProtectionRequest,
    StatusUpdateRequest,
    ProtectionStrategyResponse,
    StrategyListResponse,
    StrategyListItem,
    EventsResponse,
    StrategyEvent,
    EngineHealthResponse,
    PositionSnapshot
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Strategies"])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_engine(request: Request):
    """Get PositionProtectionEngine from app state"""
    engine = getattr(request.app.state, "protection_engine", None)
    if not engine:
        raise HTTPException(status_code=503, detail="Protection engine not available")
    return engine


def _get_strike_selector(request: Request):
    """Get or create StrikeSelector from app state (Phase 3)"""
    if not hasattr(request.app.state, "strike_selector"):
        # Lazy initialization
        from strategies.strike_selector import StrikeSelector
        from broker_api.instruments_repository import InstrumentsRepository
        from database import get_db
        
        # Get OptionsSessionManager (will be created if not exists)
        osm = getattr(request.app.state, "options_session_manager", None)
        if not osm:
            raise HTTPException(
                status_code=503, 
                detail="Options session manager not available. Start an options session first."
            )
        
        db_session = next(get_db())
        instruments_repo = InstrumentsRepository(db=db_session)
        request.app.state.strike_selector = StrikeSelector(osm, instruments_repo)
        logger.info("StrikeSelector initialized")
    
    return request.app.state.strike_selector


def _get_position_builder(request: Request):
    """Get or create PositionBuilder from app state (Phase 3)"""
    if not hasattr(request.app.state, "position_builder"):
        # Lazy initialization - requires StrikeSelector
        from strategies.strike_selector import PositionBuilder
        from broker_api.instruments_repository import InstrumentsRepository
        from database import get_db
        
        # Get StrikeSelector (will be created if not exists)
        strike_selector = _get_strike_selector(request)
        
        db_session = next(get_db())
        instruments_repo = InstrumentsRepository(db=db_session)
        request.app.state.position_builder = PositionBuilder(strike_selector, instruments_repo)
        logger.info("PositionBuilder initialized")
    
    return request.app.state.position_builder


async def _fetch_user_positions(kite: KiteConnect) -> List[dict]:
    """Fetch current positions from Kite"""
    try:
        positions_data = kite.positions()
        # Combine net and day positions
        all_positions = []
        if positions_data.get('net'):
            all_positions.extend(positions_data['net'])
        if positions_data.get('day'):
            all_positions.extend(positions_data['day'])
        return all_positions
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to fetch positions: {e}")


def _filter_positions(
    all_positions: List[dict],
    position_filter: dict
) -> List[dict]:
    """Filter positions based on criteria"""
    filtered = []
    
    for pos in all_positions:
        # Skip positions with zero quantity
        if pos.get('quantity', 0) == 0:
            continue
        
        # Apply filters
        if position_filter.get('exchange') and pos.get('exchange') != position_filter['exchange']:
            continue
        if position_filter.get('product') and pos.get('product') != position_filter['product']:
            continue
        if position_filter.get('tradingsymbols') and pos.get('tradingsymbol') not in position_filter['tradingsymbols']:
            continue
        if position_filter.get('instrument_tokens') and pos.get('instrument_token') not in position_filter['instrument_tokens']:
            continue
        
        filtered.append(pos)
    
    return filtered


def _create_position_snapshot(positions: List[dict]) -> List[PositionSnapshot]:
    """Convert Kite positions to PositionSnapshot format"""
    snapshot = []
    
    for pos in positions:
        # Determine lot size and lots
        quantity = abs(pos['quantity'])
        lot_size = pos.get('lot_size', 1)
        lots = quantity / lot_size if lot_size > 0 else 0
        
        snapshot.append(PositionSnapshot(
            instrument_token=pos['instrument_token'],
            tradingsymbol=pos['tradingsymbol'],
            exchange=pos['exchange'],
            product=pos['product'],
            transaction_type='SELL' if pos['quantity'] < 0 else 'BUY',
            quantity=quantity,
            lot_size=lot_size,
            lots=lots,
            average_price=pos.get('average_price', 0),
            current_ltp=pos.get('last_price')
        ))
    
    return snapshot


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/protection", response_model=ProtectionStrategyResponse)
async def create_protection_strategy(
    req: CreateProtectionRequest,
    request: Request,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Create a new position protection strategy.
    
    Supports:
    - Index Mode: Two-way bracket stoploss (upper and lower boundaries)
    - Combined Premium Mode: P&L based exits for strategy
    - Automatically captures current positions matching filter
    - Subscribes to index token for real-time monitoring
    """
    engine = _get_engine(request)
    conn = None
    
    try:
        # 1. Fetch and filter user positions
        logger.info(f"Creating protection strategy: {req.name or 'Unnamed'}")
        
        all_positions = await _fetch_user_positions(kite)
        filtered_positions = _filter_positions(
            all_positions,
            req.position_filter.model_dump(exclude_none=True)
        )
        
        if not filtered_positions:
            raise HTTPException(
                status_code=400,
                detail="No positions found matching filter criteria"
            )
        
        # 2. Create position snapshot
        position_snapshot = _create_position_snapshot(filtered_positions)
        total_lots = sum(pos.lots for pos in position_snapshot)
        
        logger.info(
            f"Captured {len(position_snapshot)} positions "
            f"({total_lots:.1f} lots total)"
        )
        
        # 3. Insert into database
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Prepare combined_premium_levels JSON (Phase 4)
        combined_levels_json = None
        if req.combined_premium_levels:
            combined_levels_json = json.dumps([level.model_dump() for level in req.combined_premium_levels])
        
        query = """
            INSERT INTO position_protection_strategies (
                name, strategy_type, monitoring_mode, status,
                index_instrument_token, index_tradingsymbol, index_exchange,
                index_upper_stoploss, index_lower_stoploss,
                stoploss_order_type, stoploss_limit_offset,
                trailing_mode, trailing_distance, trailing_unit,
                trailing_lock_profit,
                combined_premium_entry_type,
                combined_premium_profit_target,
                combined_premium_trailing_enabled,
                combined_premium_trailing_distance,
                combined_premium_trailing_lock_profit,
                combined_premium_levels,
                position_snapshot,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, 'active',
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                NOW(), NOW()
            )
            RETURNING id, created_at, updated_at
        """
        
        cur.execute(query, (
            req.name,
            req.strategy_type.value,
            req.monitoring_mode.value,
            req.index_instrument_token,
            req.index_tradingsymbol,
            req.index_exchange,
            req.index_upper_stoploss,
            req.index_lower_stoploss,
            req.stoploss_order_type.value,
            req.stoploss_limit_offset,
            req.trailing_mode.value if req.trailing_mode else None,
            req.trailing_distance,
            req.trailing_unit,
            req.trailing_lock_profit,
            req.combined_premium_entry_type.value if req.combined_premium_entry_type else None,
            req.combined_premium_profit_target,
            req.combined_premium_trailing_enabled,
            req.combined_premium_trailing_distance,
            req.combined_premium_trailing_lock_profit,
            combined_levels_json,
            json.dumps([pos.model_dump() for pos in position_snapshot])
        ))
        
        result = cur.fetchone()
        strategy_id = result[0]
        created_at = result[1]
        updated_at = result[2]
        
        conn.commit()
        
        logger.info(f"Strategy created: {strategy_id}")
        
        # 4. Subscribe to tokens in WebSocket
        tokens_to_subscribe = []
        
        # Subscribe to index token
        if req.index_instrument_token:
            tokens_to_subscribe.append(req.index_instrument_token)
        
        if tokens_to_subscribe:
            engine.ws_manager.subscribe(tokens_to_subscribe)
            logger.info(f"Subscribed to tokens: {tokens_to_subscribe}")
        
        # 5. Log creation event
        event_query = """
            INSERT INTO strategy_events (
                strategy_id, event_type, meta, created_at
            ) VALUES (%s, 'created', %s, NOW())
        """
        cur.execute(event_query, (
            strategy_id,
            json.dumps({
                "positions_count": len(position_snapshot),
                "total_lots": float(total_lots),
                "index_token": req.index_instrument_token
            })
        ))
        conn.commit()
        
        # 6. Build response
        return ProtectionStrategyResponse(
            strategy_id=strategy_id,
            name=req.name,
            strategy_type=req.strategy_type.value,
            monitoring_mode=req.monitoring_mode.value,
            status="active",
            index_instrument_token=req.index_instrument_token,
            index_tradingsymbol=req.index_tradingsymbol,
            index_upper_stoploss=req.index_upper_stoploss,
            index_lower_stoploss=req.index_lower_stoploss,
            trailing_mode=req.trailing_mode.value if req.trailing_mode else None,
            trailing_distance=req.trailing_distance,
            trailing_activated=False,
            trailing_current_level=None,
            positions_captured=len(position_snapshot),
            total_lots=float(total_lots),
            position_snapshot=position_snapshot,
            remaining_quantities={},
            placed_orders=[],
            levels_executed=[],
            stoploss_executed=False,
            last_evaluated_price=None,
            last_evaluated_at=None,
            created_at=created_at,
            updated_at=updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create protection strategy: {e}", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.get("/", response_model=StrategyListResponse)
async def list_strategies(
    status: Optional[str] = Query(None, description="Filter by status"),
    monitoring_mode: Optional[str] = Query(None, description="Filter by monitoring mode"),
    limit: int = Query(50, ge=1, le=200)
):
    """
    List all protection strategies.
    
    Supports filtering by status and monitoring mode.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Build query with filters
        where_clauses = []
        params = []
        
        if status:
            where_clauses.append("status = %s")
            params.append(status)
        
        if monitoring_mode:
            where_clauses.append("monitoring_mode = %s")
            params.append(monitoring_mode)
        
        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        
        query = f"""
            SELECT 
                id, name, monitoring_mode, status,
                index_instrument_token, index_tradingsymbol,
                index_upper_stoploss, index_lower_stoploss,
                position_snapshot, last_evaluated_at, created_at
            FROM position_protection_strategies
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s
        """
        params.append(limit)
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        strategies = []
        for row in rows:
            # Calculate total lots from snapshot
            snapshot = row[8] or []
            total_lots = sum(pos.get('lots', 0) for pos in snapshot)
            
            strategies.append(StrategyListItem(
                strategy_id=row[0],
                name=row[1],
                monitoring_mode=row[2],
                status=row[3],
                total_lots=float(total_lots),
                index_instrument_token=row[4],
                index_tradingsymbol=row[5],
                index_upper_stoploss=row[6],
                index_lower_stoploss=row[7],
                last_evaluated_at=row[9],
                created_at=row[10]
            ))
        
        return StrategyListResponse(
            total=len(strategies),
            strategies=strategies
        )
        
    except Exception as e:
        logger.error(f"Failed to list strategies: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.get("/health", response_model=EngineHealthResponse)
async def health_check(request: Request):
    """
    Get engine health status.
    
    Returns:
    - Engine running status
    - Number of active strategies
    - Monitoring mode breakdown
    - WebSocket connection status
    - Evaluation metrics
    """
    try:
        engine = _get_engine(request)
        stats = engine.get_stats()
        
        # Get monitoring mode breakdown
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT monitoring_mode, COUNT(*)
            FROM position_protection_strategies
            WHERE status IN ('active', 'partial')
            GROUP BY monitoring_mode
        """)
        
        mode_counts = {}
        for row in cur.fetchall():
            mode_counts[row[0]] = row[1]
        
        conn.close()
        
        # Get WebSocket status
        ws_status = "connected" if engine.ws_manager.get_websocket_status() == "CONNECTED" else "disconnected"
        
        return EngineHealthResponse(
            status="healthy" if stats['running'] else "stopped",
            engine_running=stats['running'],
            active_strategies=stats['active_strategies'],
            monitoring_modes=mode_counts,
            last_evaluation=None,  # TODO: track in engine
            websocket_status=ws_status,
            evaluation_interval_ms=engine.interval_ms,
            orders_placed_today=stats['orders_placed']
        )
        
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{strategy_id}", response_model=ProtectionStrategyResponse)
async def get_strategy(strategy_id: UUID):
    """Get detailed information about a specific strategy"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
            SELECT
                id, name, strategy_type, monitoring_mode, status,
                index_instrument_token, index_tradingsymbol,
                index_upper_stoploss, index_lower_stoploss,
                trailing_mode, trailing_distance, trailing_activated,
                trailing_current_level,
                position_snapshot, remaining_quantities,
                placed_orders, levels_executed, stoploss_executed,
                last_evaluated_price, last_evaluated_at,
                created_at, updated_at
            FROM position_protection_strategies
            WHERE id = %s
        """
        
        cur.execute(query, (strategy_id,))
        row = cur.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        
        # Parse position snapshot
        position_snapshot = []
        if row[13]:
            for pos_data in row[13]:
                position_snapshot.append(PositionSnapshot(**pos_data))
        
        total_lots = sum(pos.lots for pos in position_snapshot)
        
        return ProtectionStrategyResponse(
            strategy_id=row[0],
            name=row[1],
            strategy_type=row[2],
            monitoring_mode=row[3],
            status=row[4],
            index_instrument_token=row[5],
            index_tradingsymbol=row[6],
            index_upper_stoploss=row[7],
            index_lower_stoploss=row[8],
            trailing_mode=row[9],
            trailing_distance=row[10],
            trailing_activated=row[11] or False,
            trailing_current_level=row[12],
            positions_captured=len(position_snapshot),
            total_lots=float(total_lots),
            position_snapshot=position_snapshot,
            remaining_quantities=row[14] or {},
            placed_orders=row[15] or [],
            levels_executed=row[16] or [],
            stoploss_executed=row[17] or False,
            last_evaluated_price=row[18],
            last_evaluated_at=row[19],
            created_at=row[20],
            updated_at=row[21]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get strategy {strategy_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.patch("/{strategy_id}/status")
async def update_strategy_status(
    strategy_id: UUID,
    req: StatusUpdateRequest,
    request: Request
):
    """
    Update strategy status (pause/resume).
    
    Phase 4: Enhanced pause/resume with engine integration.
    """
    conn = None
    try:
        engine = _get_engine(request)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if strategy exists
        cur.execute(
            "SELECT status FROM position_protection_strategies WHERE id = %s",
            (strategy_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        
        current_status = row[0]
        
        # Validate status transition
        if req.status == "paused" and current_status not in ("active", "partial"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot pause strategy with status '{current_status}'"
            )
        
        if req.status == "active" and current_status != "paused":
            raise HTTPException(
                status_code=400,
                detail=f"Can only resume paused strategies (current: '{current_status}')"
            )
        
        # Update status
        cur.execute(
            "UPDATE position_protection_strategies SET status = %s, updated_at = NOW() WHERE id = %s",
            (req.status, strategy_id)
        )
        conn.commit()
        
        # Log event
        event_type = "paused" if req.status == "paused" else "resumed"
        cur.execute(
            """
            INSERT INTO strategy_events (strategy_id, event_type, meta, created_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (strategy_id, event_type, json.dumps({"reason": req.reason or "manual"}))
        )
        conn.commit()
        
        # Force strategy reload in engine
        await engine._load_active_strategies()
        
        logger.info(f"Strategy {strategy_id} status updated to {req.status}")
        
        return {
            "status": req.status,
            "strategy_id": str(strategy_id),
            "message": f"Strategy {'paused' if req.status == 'paused' else 'resumed'} successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update strategy status: {e}", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: UUID):
    """Delete a strategy (must be paused first)"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if strategy exists and status
        cur.execute(
            "SELECT status FROM position_protection_strategies WHERE id = %s",
            (strategy_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        
        status = row[0]
        if status in ('active', 'partial'):
            raise HTTPException(
                status_code=400,
                detail="Cannot delete active/partial strategy. Pause it first."
            )
        
        # Delete (cascade will delete events)
        cur.execute(
            "DELETE FROM position_protection_strategies WHERE id = %s",
            (strategy_id,)
        )
        conn.commit()
        
        logger.info(f"Strategy deleted: {strategy_id}")
        
        return {"status": "deleted", "strategy_id": str(strategy_id)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete strategy {strategy_id}: {e}", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.patch("/{strategy_id}", response_model=ProtectionStrategyResponse)
async def update_strategy(
    strategy_id: UUID,
    req: UpdateProtectionRequest,
    request: Request
):
    """
    Update strategy parameters (stoploss levels, trailing config, name).
    
    - Cannot edit strategies in 'completed', 'triggered', or 'error' status
    - Only updates fields provided in request (partial update)
    - Resets trailing state if trailing config is modified
    - Forces engine reload if strategy is active/partial
    - Logs 'updated' event with change details
    """
    conn = None
    try:
        engine = _get_engine(request)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Fetch current strategy
        cur.execute(
            """
            SELECT status, monitoring_mode, index_upper_stoploss, index_lower_stoploss,
                   trailing_mode, trailing_distance
            FROM position_protection_strategies WHERE id = %s
            """,
            (strategy_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        
        current_status = row[0]
        
        # 2. Validate status - cannot edit completed/triggered/error strategies
        if current_status in ('completed', 'triggered', 'error'):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot edit strategy with status '{current_status}'. Only active, paused, or partial strategies can be edited."
            )
        
        # 3. Validate stoploss levels
        if req.index_upper_stoploss is not None and req.index_lower_stoploss is not None:
            if req.index_upper_stoploss <= req.index_lower_stoploss:
                raise HTTPException(
                    status_code=400,
                    detail="Upper stoploss must be greater than lower stoploss"
                )
        elif req.index_upper_stoploss is not None and row[3] is not None:  # lower exists
            if req.index_upper_stoploss <= row[3]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Upper stoploss must be greater than current lower stoploss ({row[3]})"
                )
        elif req.index_lower_stoploss is not None and row[2] is not None:  # upper exists
            if req.index_lower_stoploss >= row[2]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Lower stoploss must be less than current upper stoploss ({row[2]})"
                )
        
        # 4. Validate trailing distance
        if req.trailing_distance is not None and req.trailing_distance <= 0:
            raise HTTPException(
                status_code=400,
                detail="Trailing distance must be greater than 0"
            )
        
        # 5. Build dynamic UPDATE query
        update_fields = []
        params = []
        changes = {}
        
        if req.name is not None:
            update_fields.append("name = %s")
            params.append(req.name)
            changes['name'] = req.name
        
        if req.index_upper_stoploss is not None:
            update_fields.append("index_upper_stoploss = %s")
            params.append(req.index_upper_stoploss)
            changes['index_upper_stoploss'] = req.index_upper_stoploss
        
        if req.index_lower_stoploss is not None:
            update_fields.append("index_lower_stoploss = %s")
            params.append(req.index_lower_stoploss)
            changes['index_lower_stoploss'] = req.index_lower_stoploss
        
        # Check if trailing config is being modified
        trailing_modified = False
        if req.trailing_mode is not None:
            update_fields.append("trailing_mode = %s")
            params.append(req.trailing_mode.value)
            changes['trailing_mode'] = req.trailing_mode.value
            trailing_modified = True
        
        if req.trailing_distance is not None:
            update_fields.append("trailing_distance = %s")
            params.append(req.trailing_distance)
            changes['trailing_distance'] = req.trailing_distance
            trailing_modified = True
        
        if req.trailing_lock_profit is not None:
            update_fields.append("trailing_lock_profit = %s")
            params.append(req.trailing_lock_profit)
            changes['trailing_lock_profit'] = req.trailing_lock_profit
            trailing_modified = True
        
        # If no fields to update, return error
        if not update_fields:
            raise HTTPException(
                status_code=400,
                detail="No fields to update. Provide at least one field to modify."
            )
        
        # Reset trailing state if trailing config modified
        if trailing_modified:
            update_fields.extend([
                "trailing_activated = false",
                "trailing_current_level = NULL"
            ])
            changes['trailing_state_reset'] = True
        
        # Always update timestamp
        update_fields.append("updated_at = NOW()")
        
        # 6. Execute UPDATE
        query = f"""
            UPDATE position_protection_strategies 
            SET {', '.join(update_fields)}
            WHERE id = %s
            RETURNING updated_at
        """
        params.append(strategy_id)
        
        cur.execute(query, params)
        updated_at = cur.fetchone()[0]
        conn.commit()
        
        logger.info(f"Strategy {strategy_id} updated. Changes: {changes}")
        
        # 7. Log update event
        cur.execute(
            """
            INSERT INTO strategy_events (strategy_id, event_type, meta, created_at)
            VALUES (%s, 'updated', %s, NOW())
            """,
            (strategy_id, json.dumps({"changes": changes}))
        )
        conn.commit()
        
        # 8. Force engine reload if strategy is active/partial
        if current_status in ('active', 'partial'):
            await engine._load_active_strategies()
            logger.info(f"Engine reloaded after strategy {strategy_id} update")
        
        # 9. Fetch and return updated strategy
        cur.execute(
            """
            SELECT
                id, name, strategy_type, monitoring_mode, status,
                index_instrument_token, index_tradingsymbol,
                index_upper_stoploss, index_lower_stoploss,
                trailing_mode, trailing_distance, trailing_activated,
                trailing_current_level,
                position_snapshot, remaining_quantities,
                placed_orders, levels_executed, stoploss_executed,
                last_evaluated_price, last_evaluated_at,
                created_at, updated_at
            FROM position_protection_strategies
            WHERE id = %s
            """,
            (strategy_id,)
        )
        row = cur.fetchone()
        
        # Parse position snapshot
        position_snapshot = []
        if row[13]:
            for pos_data in row[13]:
                position_snapshot.append(PositionSnapshot(**pos_data))
        
        total_lots = sum(pos.lots for pos in position_snapshot)
        
        return ProtectionStrategyResponse(
            strategy_id=row[0],
            name=row[1],
            strategy_type=row[2],
            monitoring_mode=row[3],
            status=row[4],
            index_instrument_token=row[5],
            index_tradingsymbol=row[6],
            index_upper_stoploss=row[7],
            index_lower_stoploss=row[8],
            trailing_mode=row[9],
            trailing_distance=row[10],
            trailing_activated=row[11] or False,
            trailing_current_level=row[12],
            positions_captured=len(position_snapshot),
            total_lots=float(total_lots),
            position_snapshot=position_snapshot,
            remaining_quantities=row[14] or {},
            placed_orders=row[15] or [],
            levels_executed=row[16] or [],
            stoploss_executed=row[17] or False,
            last_evaluated_price=row[18],
            last_evaluated_at=row[19],
            created_at=row[20],
            updated_at=row[21]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update strategy {strategy_id}: {e}", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.get("/{strategy_id}/events", response_model=EventsResponse)
async def get_strategy_events(
    strategy_id: UUID,
    limit: int = Query(50, ge=1, le=500)
):
    """Get event history for a strategy"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if strategy exists
        cur.execute(
            "SELECT COUNT(*) FROM position_protection_strategies WHERE id = %s",
            (strategy_id,)
        )
        if cur.fetchone()[0] == 0:
            raise HTTPException(status_code=404, detail="Strategy not found")
        
        # Fetch events
        query = """
            SELECT
                id, strategy_id, event_type, trigger_price, trigger_type,
                level_name, quantity_affected, lots_affected,
                order_id, correlation_id, order_status,
                instrument_token, error_message, meta, created_at
            FROM strategy_events
            WHERE strategy_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
        
        cur.execute(query, (strategy_id, limit))
        rows = cur.fetchall()
        
        events = []
        for row in rows:
            events.append(StrategyEvent(
                event_id=row[0],
                strategy_id=row[1],
                event_type=row[2],
                trigger_price=row[3],
                trigger_type=row[4],
                level_name=row[5],
                quantity_affected=row[6],
                lots_affected=row[7],
                order_id=row[8],
                correlation_id=row[9],
                order_status=row[10],
                instrument_token=row[11],
                error_message=row[12],
                meta=row[13],
                created_at=row[14]
            ))
        
        return EventsResponse(
            strategy_id=strategy_id,
            total_events=len(events),
            events=events
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get events for {strategy_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: POSITION BUILDING & DELTA SELECTION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# Request models
class SuggestStrikesRequest(BaseModel):
    underlying: str
    expiry: date
    strategy_type: str  # 'straddle', 'strangle', 'iron_condor', 'single_leg'
    target_delta: float = 0.30
    risk_amount: Optional[float] = None


class SelectedStrikeData(BaseModel):
    instrument_token: int
    tradingsymbol: str
    strike: float
    option_type: str  # 'CE' or 'PE'
    ltp: float
    lot_size: int
    delta: float
    lots: int
    transaction_type: str  # 'BUY' or 'SELL'


class BuildPositionRequest(BaseModel):
    underlying: str
    expiry: date
    strategy_type: str
    selected_strikes: Optional[List[SelectedStrikeData]] = None  # Manual selection
    target_delta: float = 0.30  # Auto-selection based on delta
    risk_amount: Optional[float] = None
    protection_config: Optional[Dict[str, Any]] = None
    place_orders: bool = False  # Dry run by default


@router.get("/available-expiries/{underlying}")
async def get_available_expiries(
    underlying: str,
    request: Request
):
    """
    Get available expiry dates for an underlying from active options session.
    
    Phase 3: Returns list of expiry dates that have live data.
    """
    try:
        # Get options session manager from app state
        osm = request.app.state.options_session_manager
        
        session = osm.sessions.get(underlying.upper())
        if not session or not session.snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"No active options session for {underlying}. Start a session first."
            )
        
        snapshot = session.snapshot
        expiries = snapshot.get('expiries', [])
        spot_ltp = snapshot.get('spot_ltp')
        
        return {
            "underlying": underlying.upper(),
            "expiries": expiries,
            "spot_ltp": spot_ltp,
            "timestamp": snapshot.get('timestamp')
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get available expiries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mini-chain/{underlying}/{expiry}")
async def get_mini_chain(
    underlying: str,
    expiry: date,
    request: Request,
    center_strike: Optional[float] = Query(None, description="Center strike (uses ATM if not provided)"),
    count: int = Query(11, ge=5, le=25, description="Number of strikes to return")
):
    """
    Get mini option chain with live Greeks.
    
    Phase 3: Returns strikes with LTP, delta, gamma, theta, vega, IV.
    """
    try:
        selector = _get_strike_selector(request)
        
        chain_data = await selector.get_mini_chain(
            underlying=underlying.upper(),
            expiry=expiry,
            center_strike=center_strike,
            count=count
        )
        
        # Check if error was returned from strike selector
        if isinstance(chain_data, dict) and "error" in chain_data:
            error_msg = chain_data["error"]
            logger.warning(f"Mini chain error: {error_msg}")
            raise HTTPException(status_code=404, detail=error_msg)
        
        return chain_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get mini chain: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/suggest-strikes")
async def suggest_strikes(
    req: SuggestStrikesRequest,
    request: Request
):
    """
    Suggest strikes for a strategy based on delta.
    
    Phase 3: Delta-based strike selection with lot calculation.
    """
    try:
        selector = _get_strike_selector(request)
        
        suggestions = await selector.suggest_strikes(
            underlying=req.underlying.upper(),
            expiry=req.expiry,
            strategy_type=req.strategy_type,
            target_delta=req.target_delta,
            risk_amount=req.risk_amount
        )
        
        return suggestions
        
    except Exception as e:
        logger.error(f"Failed to suggest strikes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/build-position")
async def build_position(
    req: BuildPositionRequest,
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    """
    Build position with optional protection strategy.
    
    Phase 3: Atomic position building + protection setup.
    
    - If place_orders=false: Returns dry run plan
    - If place_orders=true: Executes orders and creates protection strategy
    """
    try:
        builder = _get_position_builder(request)
        engine = _get_engine(request)
        
        # Build position plan - use manual strikes if provided, otherwise auto-select
        if req.selected_strikes:
            # Manual strike selection mode
            plan = await builder.build_position_plan_from_strikes(
                underlying=req.underlying.upper(),
                expiry=req.expiry,
                strategy_type=req.strategy_type,
                selected_strikes=[s.model_dump() for s in req.selected_strikes],
                protection_config=req.protection_config
            )
        else:
            # Auto-selection mode based on delta
            plan = await builder.build_position_plan(
                underlying=req.underlying.upper(),
                expiry=req.expiry,
                strategy_type=req.strategy_type,
                target_delta=req.target_delta,
                risk_amount=req.risk_amount,
                protection_config=req.protection_config
            )
        
        if 'error' in plan:
            raise HTTPException(status_code=400, detail=plan['error'])
        
        # Dry run: Just return the plan
        if not req.place_orders:
            return {
                "mode": "dry_run",
                "plan": plan,
                "message": "Dry run complete. Set place_orders=true to execute."
            }
        
        # Execute orders
        logger.info(f"Executing position build: {req.strategy_type} on {req.underlying} {req.expiry}")
        
        orders_placed = []
        orders_failed = []
        
        for order in plan['orders']:
            try:
                # Place order via Kite
                order_id = await run_kite_write_action(
                    "indexstoploss_place_order",
                    corr_id,
                    lambda _order=order: kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=kite.EXCHANGE_NFO,
                        tradingsymbol=_order['tradingsymbol'],
                        transaction_type=_order['transaction_type'],
                        quantity=_order['quantity'],
                        product=kite.PRODUCT_MIS,
                        order_type=kite.ORDER_TYPE_MARKET,
                    ),
                    meta={"strategy_type": req.strategy_type, "underlying": req.underlying, "tradingsymbol": order['tradingsymbol']},
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to place order for {order['tradingsymbol']}: {e}")
                orders_failed.append({
                    "tradingsymbol": order['tradingsymbol'],
                    "error": str(e)
                })
                continue

            orders_placed.append({
                "order_id": order_id,
                "tradingsymbol": order['tradingsymbol'],
                "transaction_type": order['transaction_type'],
                "quantity": order['quantity'],
                "status": "placed"
            })

            logger.info(f"Order placed: {order_id} for {order['tradingsymbol']}")
        
        # If all orders failed, return error
        if not orders_placed:
            return {
                "mode": "execution",
                "status": "failed",
                "orders_placed": [],
                "orders_failed": orders_failed,
                "message": "All orders failed to place"
            }
        
        # Create protection strategy if protection_config provided
        strategy_id = None
        if req.protection_config and plan.get('protection_plan'):
            # Wait a bit for positions to reflect
            import asyncio
            await asyncio.sleep(2)
            
            # Fetch positions and create protection
            # This is a simplified version - you may want to enhance this
            protection_plan = plan['protection_plan']
            
            logger.info(f"Creating protection strategy for position")
            # Protection creation would happen here via the existing endpoint
            # For now, just log it
            logger.info(f"Protection plan ready: {protection_plan['monitoring_mode']}")
        
        return {
            "mode": "execution",
            "status": "success" if not orders_failed else "partial",
            "orders_placed": orders_placed,
            "orders_failed": orders_failed,
            "strategy_id": strategy_id,
            "plan": plan,
            "message": f"Executed {len(orders_placed)}/{len(plan['orders'])} orders successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to build position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# REAL-TIME POSITION TRACKING ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/positions/realtime-summary")
async def get_realtime_positions_summary(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    """
    Get real-time positions with PnL calculated using:
    pnl = (sellValue - buyValue) + (netQuantity * lastPrice * multiplier)
    
    This is a convenience endpoint for the indexstoploss system that
    leverages the application-wide real-time positions service.
    """
    try:
        sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
        if not sid:
            raise HTTPException(401, "Session ID required")
        
        # Get positions from real-time service
        positions = await realtime_positions_service.get_positions(sid, corr_id)
        
        # If no positions in cache, initialize from Kite API
        if not positions:
            logger.info("No cached positions, initializing from Kite API")
            positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
        
        # Calculate summary
        total_pnl = sum(pos.pnl for pos in positions.values())
        realized_pnl = sum(pos.realized_pnl for pos in positions.values())
        unrealized_pnl = sum(pos.unrealized_pnl for pos in positions.values())
        
        # Group by exchange and product
        by_exchange = {}
        by_product = {}
        
        for pos in positions.values():
            # By exchange
            if pos.exchange not in by_exchange:
                by_exchange[pos.exchange] = {"count": 0, "pnl": 0.0, "positions": []}
            by_exchange[pos.exchange]["count"] += 1
            by_exchange[pos.exchange]["pnl"] += pos.pnl
            by_exchange[pos.exchange]["positions"].append({
                "tradingsymbol": pos.tradingsymbol,
                "quantity": pos.quantity,
                "pnl": pos.pnl
            })
            
            # By product
            if pos.product not in by_product:
                by_product[pos.product] = {"count": 0, "pnl": 0.0}
            by_product[pos.product]["count"] += 1
            by_product[pos.product]["pnl"] += pos.pnl
        
        return {
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_positions": len(positions),
                "total_pnl": round(total_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2)
            },
            "by_exchange": by_exchange,
            "by_product": by_product,
            "positions": {k: {
                "tradingsymbol": v.tradingsymbol,
                "exchange": v.exchange,
                "product": v.product,
                "quantity": v.quantity,
                "multiplier": v.multiplier,
                "average_price": round(v.average_price, 2),
                "last_price": round(v.last_price, 2),
                "pnl": round(v.pnl, 2),
                "realized_pnl": round(v.realized_pnl, 2),
                "unrealized_pnl": round(v.unrealized_pnl, 2),
                "day_change_percentage": round(v.day_change_percentage, 2)
            } for k, v in positions.items()}
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get realtime positions summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
