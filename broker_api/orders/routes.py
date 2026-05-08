import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from kiteconnect import KiteConnect
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from broker_api.orders.models import *
from broker_api.orders.service import *
from broker_api.session.kite_session import get_kite, get_kite_session_id, get_session_account_id

router = APIRouter(tags=["Orders"])
service = OrdersService()


async def place_order(
    req: PlaceOrderRequest,
    request: Request,
    response: Response,
    kite: KiteConnect = Depends(get_kite),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key", description="Client-generated key for idempotent retries."),
    corr_id: str = Depends(get_correlation_id),
):
    sid = get_kite_session_id(request)
    return await service.place_order(kite, req, corr_id, idempotency_key, sid, response)

def get_orders(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.orders(kite, corr_id)

def get_order_snapshot(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_snapshot(kite, order_id, corr_id)

def get_order_history(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_history(kite, order_id, corr_id)

def get_order_trades(order_id: str, kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_trades(kite, order_id, corr_id)

def get_trades(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.trades(kite, corr_id)

def get_positions(kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.positions(kite, corr_id)

async def convert_position(
    req: ConvertPositionRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.convert_position(kite, req, corr_id)

async def modify_order(
    variety: str,
    order_id: str,
    req: ModifyOrderRequest,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if modifying the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.modify_order(kite, variety, order_id, req, corr_id, parent_order_id)

async def cancel_order(
    variety: str,
    order_id: str,
    parent_order_id: Optional[str] = Query(None, description="Required for Cover Orders if cancelling the SL leg."),
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    return await service.cancel_order(kite, variety, order_id, corr_id, parent_order_id)

def get_order_margins(items: List[OrderMarginInput], mode: Optional[str] = Query(None, enum=["compact", "full"]), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.order_margins(kite, items, corr_id, mode)

def get_basket_margins(items: List[OrderMarginInput], consider_positions: bool = Query(True), mode: Optional[str] = Query(None, enum=["compact", "full"]), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.basket_margins(kite, items, consider_positions, corr_id, mode)

def get_charges_orders(items: List[ChargesOrderInput], kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.charges_orders(kite, items, corr_id)

def get_trigger_range(transaction_type: TransactionType, instruments: List[str] = Query(...), kite: KiteConnect = Depends(get_kite), corr_id: str = Depends(get_correlation_id)):
    return service.trigger_range(kite, transaction_type, instruments, corr_id)

async def place_basket_orders(
    req: BasketOrderRequest,
    request: Request,
    response: Response,
    kite: KiteConnect = Depends(get_kite),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key", description="Client-generated key for idempotent basket retries."),
    corr_id: str = Depends(get_correlation_id),
):
    """
    Place a basket of orders sequentially.
    - Set dry_run=true to preview margins without placing orders.
    - Set all_or_none=true to attempt rollback on first failure (best-effort).
    """
    sid = get_kite_session_id(request)
    return await service.place_basket(kite, req, corr_id, sid, idempotency_key, response)

async def initialize_realtime_positions(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Initialize real-time position tracking.
    Fetches current positions from Kite API and sets up tracking state.
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    try:
        positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    
    return {
        "status": "initialized",
        "account_id": get_session_account_id(db, sid),
        "position_count": len(positions),
        "positions": {k: v.model_dump() for k, v in positions.items()}
    }

async def get_realtime_positions(
    request: Request,
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Get current real-time positions with calculated PnL.
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    account_id = get_session_account_id(db, sid)
    if not account_id:
        raise HTTPException(409, "Broker account not initialized for this session. Call /positions/initialize first.")

    positions = await realtime_positions_service.get_positions(account_id, corr_id)
    
    # Calculate summary
    total_pnl = sum(pos.pnl for pos in positions.values())
    realized_pnl = sum(pos.realized_pnl for pos in positions.values())
    unrealized_pnl = sum(pos.unrealized_pnl for pos in positions.values())
    
    return {
        "position_count": len(positions),
        "total_pnl": total_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "positions": {k: v.model_dump() for k, v in positions.items()}
    }

async def stream_realtime_positions(
    request: Request,
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Server-Sent Events (SSE) endpoint for real-time position streaming.
    Updates are sent whenever:
    - LTP changes (from WebSocket)
    - Orders are filled
    - Positions are exited
    """
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    
    account_id = get_session_account_id(db, sid)
    if not account_id:
        raise HTTPException(409, "Broker account not initialized for this session. Call /positions/initialize first.")

    return StreamingResponse(
        realtime_positions_service.subscribe_to_positions(account_id, corr_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

async def reconcile_realtime_positions(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id),
):
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")

    account_id = get_session_account_id(db, sid)
    if not account_id:
        try:
            positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        total_pnl = sum(pos.pnl for pos in positions.values())
        return {
            "status": "ok",
            "account_id": next(iter(positions.values())).account_id if positions else None,
            "position_count": len(positions),
            "total_pnl": total_pnl,
            "mode": "initialized",
        }

    count = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)
    positions = await realtime_positions_service.get_positions(account_id, corr_id)
    total_pnl = sum(pos.pnl for pos in positions.values())
    return {
        "status": "ok",
        "account_id": account_id,
        "position_count": count,
        "cached_positions": len(positions),
        "total_pnl": total_pnl,
        "mode": "reconciled",
    }

async def get_order_runtime_status(
    request: Request,
    db: Session = Depends(get_db),
):
    sid = get_kite_session_id(request)
    account_id = get_session_account_id(db, sid) if sid else None

    counts = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE processing_state = 'pending') AS pending_events,
                COUNT(*) FILTER (WHERE processing_state = 'processing') AS processing_events,
                COUNT(*) FILTER (WHERE processing_state = 'failed') AS failed_events
            FROM canonical_order_events
            """
        )
    ).fetchone()

    dirty_counts = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE dirty_for_trade_sync = TRUE) AS dirty_orders,
                COUNT(*) FILTER (WHERE needs_reconcile = TRUE) AS reconcile_orders
            FROM order_state_projection
            WHERE (:account_id IS NULL OR account_id = :account_id)
            """
        ),
        {"account_id": account_id},
    ).fetchone()

    position_counts = db.execute(
        text(
            """
            SELECT COUNT(*)
            FROM account_positions
            WHERE (:account_id IS NULL OR account_id = :account_id)
              AND net_quantity <> 0
            """
        ),
        {"account_id": account_id},
    ).fetchone()

    return {
        "account_id": account_id,
        "canonical_events": {
            "pending": int(counts[0] or 0),
            "processing": int(counts[1] or 0),
            "failed": int(counts[2] or 0),
        },
        "orders": {
            "dirty_for_trade_sync": int(dirty_counts[0] or 0),
            "needs_reconcile": int(dirty_counts[1] or 0),
        },
        "positions": {
            "open_rows": int(position_counts[0] or 0),
        },
    }

async def process_order_runtime_now(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    db: Session = Depends(get_db),
    corr_id: str = Depends(get_correlation_id),
):
    sid = get_kite_session_id(request)
    if not sid:
        raise HTTPException(401, "Session ID required")
    account_id = get_session_account_id(db, sid)

    processed = await order_event_runtime.process_pending_events(batch_size=100)
    synced = await order_event_runtime.sync_dirty_orders(kite, realtime_positions_service, batch_size=25)
    reconciled = 0
    if account_id:
        reconciled = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)

    return {
        "status": "ok",
        "account_id": account_id,
        "processed_events": processed,
        "synced_orders": synced,
        "reconciled_positions": reconciled,
    }

async def place_gtt_trigger(
    req: PlaceGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Place a GTT trigger.
    
    - **single**: Single trigger value, executes first order when reached
    - **two-leg**: Two trigger values (OCO - One Cancels Other), executes corresponding order
    """
    return await gtt_service.place_gtt(kite, req, corr_id)

def get_gtt_triggers(
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Retrieve all GTT triggers (active and from last 7 days).
    
    Statuses:
    - active: Trigger is active and monitoring
    - triggered: Trigger was activated
    - disabled: Trigger is disabled, user action needed
    - expired: Trigger expired based on expiry date
    - cancelled: Trigger cancelled by system
    - rejected: Trigger rejected by system
    - deleted: Trigger deleted by user
    """
    return gtt_service.get_gtts(kite, corr_id)

def get_gtt_trigger(
    trigger_id: int,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """Retrieve details of a specific GTT trigger by ID."""
    return gtt_service.get_gtt(kite, trigger_id, corr_id)

async def modify_gtt_trigger(
    trigger_id: int,
    req: ModifyGTTRequest,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Modify an existing GTT trigger.
    
    Recommended: Fetch the trigger using GET /gtt/triggers/{id}, modify values, and send to this endpoint.
    """
    return await gtt_service.modify_gtt(kite, trigger_id, req, corr_id)

async def delete_gtt_trigger(
    trigger_id: int,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id)
):
    """Delete an active GTT trigger."""
    return await gtt_service.delete_gtt(kite, trigger_id, corr_id)

async def receive_order_postback(
    request: Request,
    db: Session = Depends(get_db),
    x_test_mode: Optional[str] = Header(None, alias="X-Test-Mode"),
    corr_id: str = Depends(get_correlation_id)
):
    """
    Webhook endpoint for receiving Kite Connect order postback notifications.
    
    This endpoint:
    - Receives POST requests with JSON payload from Kite Connect
    - Validates checksum (SHA-256 of order_id + order_timestamp + api_secret)
    - Stores validated events to database with idempotency
    - Returns 200 OK for both new and duplicate events
    - Returns 401 if checksum validation fails
    - Returns 400 if payload is malformed
    
    Test Mode:
    - Set header `X-Test-Mode: true` to bypass checksum validation
    - Only works if environment variable `ALLOW_WEBHOOK_TEST_MODE=true`
    """
    log_ctx = {"correlation_id": corr_id}
    
    try:
        # Read raw body
        body = await request.body()
        body_str = body.decode('utf-8')
        
        # Parse JSON
        try:
            payload_dict = json.loads(body_str)
        except json.JSONDecodeError as e:
            logger.error(
                "Webhook JSON parsing failed",
                extra={**log_ctx, "error": str(e), "body_preview": body_str[:200]}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON payload: {str(e)}"
            )
        
        # Validate with Pydantic
        try:
            payload = PostbackPayload.model_validate(payload_dict)
        except Exception as e:
            logger.error(
                "Webhook payload validation failed",
                extra={**log_ctx, "error": str(e), "payload": payload_dict}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Payload validation failed: {str(e)}"
            )
        
        # Log received postback
        logger.info(
            "Webhook postback received",
            extra={
                **log_ctx,
                "order_id": payload.order_id,
                "status": payload.status,
                "user_id": payload.user_id,
                "tradingsymbol": payload.tradingsymbol
            }
        )
        
        # Check test mode
        test_mode = False
        if x_test_mode and x_test_mode.lower() == "true":
            if not ALLOW_WEBHOOK_TEST_MODE:
                logger.warning(
                    "Test mode requested but not allowed",
                    extra={**log_ctx, "order_id": payload.order_id}
                )
                raise HTTPException(
                    status_code=403,
                    detail="Test mode not enabled on server"
                )
            test_mode = True
        
        # Validate checksum
        webhook_service._validate_checksum(payload, corr_id, test_mode)
        
        # Store raw + canonical event (with idempotency)
        ingest_result = await order_event_runtime.ingest_webhook_event(payload, corr_id, db)
        db.commit()
        event_id = ingest_result.get("canonical_event_id")

        # Publish SSE event only when inserted (not duplicate)
        if event_id:
            try:
                await publish_event("orders.events", {
                    "source": "webhook",
                    "id": event_id,
                    "order_id": payload.order_id,
                    "user_id": payload.user_id,
                    "status": payload.status,
                    "event_timestamp": payload.get_event_timestamp().isoformat(),
                    "exchange": payload.exchange,
                    "tradingsymbol": payload.tradingsymbol,
                    "instrument_token": payload.instrument_token,
                    "transaction_type": payload.transaction_type,
                    "quantity": payload.quantity,
                    "filled_quantity": payload.filled_quantity,
                    "average_price": payload.average_price,
                    "payload": payload.model_dump()
                })
            except Exception as pe:
                logger.error("Failed to publish webhook order event: %s", pe, exc_info=True)
        
        # Return success (200 OK for both new and duplicate events)
        return {
            "status": "ok",
            "event_id": event_id,
            "duplicate": bool(ingest_result.get("duplicate", event_id is None)),
            "order_id": payload.order_id
        }
        
    except HTTPException:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(
            "Webhook processing failed",
            extra={**log_ctx, "error": str(e)},
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"Webhook processing failed: {str(e)}"
        )

async def query_webhook_events(
    order_id: Optional[str] = Query(None, description="Filter by order ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    status: Optional[str] = Query(None, description="Filter by order status"),
    start_date: Optional[datetime] = Query(None, description="Filter by start date (event_timestamp)"),
    end_date: Optional[datetime] = Query(None, description="Filter by end date (event_timestamp)"),
    limit: int = Query(50, ge=1, le=500, description="Number of events to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db)
):
    """
    Query stored webhook events with filters and pagination.
    
    Returns events ordered by event_timestamp descending (most recent first).
    Each event includes the complete postback payload in the `payload` field.
    
    Use pagination (limit/offset) for large result sets.
    """
    return await webhook_service.query_events(
        db=db,
        order_id=order_id,
        user_id=user_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset
    )

async def sse_order_events(request: Request, source: Optional[str] = Query(None, description="Filter by 'webhook', 'ws' or 'all'")):
    async def event_stream():
        try:
            norm = None
            if source:
                s = source.lower().strip()
                if s == "websocket":
                    s = "ws"
                if s != "all":
                    norm = s
            async for message in pubsub_iter("orders.events"):
                if await request.is_disconnected():
                    break
                if isinstance(message, dict) and message.get("event") == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                if norm and isinstance(message, dict):
                    if message.get("source") != norm:
                        continue
                try:
                    src = message.get("source") if isinstance(message, dict) else None
                    prefix = f"event: {src}\n" if src else ""
                    payload = json.dumps(message)
                    yield f"{prefix}data: {payload}\n\n"
                except Exception:
                    continue
        except asyncio.CancelledError:
            pass
    return StreamingResponse(event_stream(), media_type="text/event-stream")

async def enable_ws_order_updates(request: Request):
    runtime = getattr(request.app.state, "market_data_runtime", None)
    if not runtime:
        raise HTTPException(status_code=503, detail="Market runtime not available")
    runtime.order_updates_enabled = True
    return {"status": "ok", "enabled": True}

async def disable_ws_order_updates(request: Request):
    runtime = getattr(request.app.state, "market_data_runtime", None)
    if not runtime:
        raise HTTPException(status_code=503, detail="Market runtime not available")
    runtime.order_updates_enabled = False
    return {"status": "ok", "enabled": False}

async def ws_order_updates_status(request: Request):
    runtime = getattr(request.app.state, "market_data_runtime", None)
    if not runtime:
        raise HTTPException(status_code=503, detail="Market runtime not available")
    return {
        "enabled": bool(getattr(runtime, "order_updates_enabled", False)),
        "ws_status": runtime.get_websocket_status() if hasattr(runtime, "get_websocket_status") else "unknown",
        "last_order_update_at": getattr(runtime, "last_order_update_at", None),
    }

async def get_ws_order_events(
    db: Session = Depends(get_db),
    order_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        conditions = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if order_id:
            conditions.append("order_id = :order_id")
            params["order_id"] = order_id
        if user_id:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id
        if status:
            conditions.append("status = :status")
            params["status"] = status
        if start_date:
            conditions.append("event_timestamp >= :start_date")
            params["start_date"] = start_date
        if end_date:
            conditions.append("event_timestamp <= :end_date")
            params["end_date"] = end_date

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        query_sql = text(f"""
            SELECT 
                id, order_id, user_id, status, event_timestamp, received_at,
                exchange, tradingsymbol, instrument_token, transaction_type,
                quantity, filled_quantity, average_price, payload_json
            FROM ws_order_events
            WHERE {where_clause}
            ORDER BY event_timestamp DESC, created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        result = db.execute(query_sql, params)
        rows = result.fetchall()

        events: List[OrderEventResponse] = []
        for row in rows:
            events.append(OrderEventResponse(
                id=str(row[0]),
                order_id=row[1] or "",
                user_id=row[2] or "",
                status=row[3] or "",
                event_timestamp=row[4],
                received_at=row[5],
                exchange=row[6],
                tradingsymbol=row[7],
                instrument_token=row[8],
                transaction_type=row[9],
                quantity=row[10],
                filled_quantity=row[11],
                average_price=row[12],
                payload=row[13] or {}
            ))

        return events
    except Exception as e:
        logger.error(f"Failed to query WS order events: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to query WS events: {str(e)}")
