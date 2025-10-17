"""
Position Protection System - API Router
Phase 1: Basic endpoints for index-based monitoring
"""

import logging
import json
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kiteconnect import KiteConnect

from broker_api.broker_api import get_kite
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

router = APIRouter(tags=["index-stoploss"])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_engine(request: Request):
    """Get PositionProtectionEngine from app state"""
    engine = getattr(request.app.state, "protection_engine", None)
    if not engine:
        raise HTTPException(status_code=503, detail="Protection engine not available")
    return engine


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
    
    Phase 1: Index-based monitoring only
    - Supports two-way bracket stoploss (upper and lower boundaries)
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
        
        query = """
            INSERT INTO position_protection_strategies (
                name, strategy_type, monitoring_mode, status,
                index_instrument_token, index_tradingsymbol, index_exchange,
                index_upper_stoploss, index_lower_stoploss,
                stoploss_order_type, stoploss_limit_offset,
                trailing_mode, trailing_distance, trailing_unit,
                trailing_lock_profit,
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
            json.dumps([pos.model_dump() for pos in position_snapshot])
        ))
        
        result = cur.fetchone()
        strategy_id = result[0]
        created_at = result[1]
        updated_at = result[2]
        
        conn.commit()
        
        logger.info(f"Strategy created: {strategy_id}")
        
        # 4. Subscribe to index token in WebSocket
        if req.index_instrument_token:
            engine.ws_manager.subscribe([req.index_instrument_token])
            logger.info(f"Subscribed to index token: {req.index_instrument_token}")
        
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
                index_instrument_token, index_upper_stoploss, index_lower_stoploss,
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
            snapshot = row[7] or []
            total_lots = sum(pos.get('lots', 0) for pos in snapshot)
            
            strategies.append(StrategyListItem(
                strategy_id=row[0],
                name=row[1],
                monitoring_mode=row[2],
                status=row[3],
                total_lots=float(total_lots),
                index_instrument_token=row[4],
                index_upper_stoploss=row[5],
                index_lower_stoploss=row[6],
                last_evaluated_at=row[8],
                created_at=row[9]
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


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: UUID):
    """
    Delete a strategy.
    
    Only allows deleting paused, completed, or triggered strategies.
    Active strategies must be paused first.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check current status
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

