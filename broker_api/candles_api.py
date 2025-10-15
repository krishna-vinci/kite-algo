"""
Candles API - Clean, lightweight endpoints for historical and real-time candle data.
Separates concerns and provides a simple interface for chart applications.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Literal
import json
import pytz
import psycopg2
from psycopg2.extras import execute_values

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from kiteconnect import KiteConnect
from sqlalchemy.orm import Session
from sqlalchemy import text

from .candle_storage import CandleStorage, IST
from .candle_aggregator import get_aggregator, SUPPORTED_INTERVALS
from .candle_ingestion import CandleIngestion, IngestionScheduler
from .kite_auth import get_kite, API_KEY
from .kite_orders import KiteSession
from .instruments_repository import InstrumentsRepository
from database import get_db, get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candles", tags=["Candles"])

# ===== Pydantic Models =====

class CandleData(BaseModel):
    """Single candle data point."""
    time: int  # Unix timestamp in seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[float] = None


class CandlesMeta(BaseModel):
    """Metadata about the candles response."""
    instrument_token: int
    interval: str
    timezone: str = "Asia/Kolkata"
    from_ts: str = Field(alias="from")
    to_ts: str = Field(alias="to")
    count: int


class IngestionStatus(BaseModel):
    """Status of background ingestion."""
    status: Literal["triggered", "up_to_date", "disabled", "error"]
    message: Optional[str] = None


class CandlesResponse(BaseModel):
    """Standard response for candle queries."""
    status: str = "success"
    meta: CandlesMeta
    ingestion: IngestionStatus
    candles: List[CandleData]


class AggregatorConfig(BaseModel):
    """Configuration for starting the aggregator."""
    intervals: List[str] = Field(default=["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"])
    owner_scope: str = Field(default="all")
    refresh_seconds: int = Field(default=30, ge=10, le=300)



class IngestionConfig(BaseModel):
    """Configuration for starting the ingestion scheduler."""
    intervals: List[str] = Field(default=["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"])
    owner_scope: str = Field(default="all")
    schedule_seconds: int = Field(default=900, ge=60, le=3600)


class WatchlistInstrument(BaseModel):
    """Instrument in a user's watchlist."""
    instrument_token: int
    tradingsymbol: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    instrument_type: Optional[str] = None


class WatchlistUpsertRequest(BaseModel):
    """Request to upsert instruments into a watchlist."""
    owner_id: Optional[str] = "default"
    instruments: List[WatchlistInstrument]


class UpsertResponse(BaseModel):
    """Response for watchlist upsert operations."""
    inserted: int
    updated: int
    removed: int


class IngestRequest(BaseModel):
    """Request to ingest historical data."""
    instrument_token: int
    interval: str
    from_date: Optional[datetime] = Field(None, alias="from")
    to_date: Optional[datetime] = Field(None, alias="to")
    continuous: Optional[bool] = False
    oi: Optional[bool] = False


class DBCandlesResponse(BaseModel):
    """Response for database candle queries."""
    status: Literal['success']
    data: Dict[str, Any]


# ===== Authentication =====

def get_kite_db(db: Session = Depends(get_db)) -> KiteConnect:
    """
    Dependency to get a KiteConnect instance initialized with the system access token from the database.
    """
    system_session = db.query(KiteSession).filter_by(session_id="system").first()

    if not system_session or not system_session.access_token:
        logger.error("System Kite access token not found in kite_sessions table.")
        raise HTTPException(status_code=401, detail="Not authenticated; login first")

    access_token = system_session.access_token

    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        # Test call to verify credentials
        kite.profile()
        return kite
    except Exception as e:
        logger.error(f"Failed to initialize KiteConnect or verify credentials with system token: {e}")
        raise HTTPException(status_code=401, detail=f"Kite authentication failed: {e}")


# ===== Global Service Instances =====

ingestion_scheduler: Optional[IngestionScheduler] = None


def get_ingestion_scheduler(kite: KiteConnect = Depends(get_kite_db)) -> IngestionScheduler:
    """Get or create the ingestion scheduler singleton."""
    global ingestion_scheduler
    if ingestion_scheduler is None:
        ingestion_scheduler = IngestionScheduler(kite)
    return ingestion_scheduler


# ===== Timeframe Normalization =====

TIMEFRAME_ALIASES = {
    "1m": "minute", "min": "minute", "minute": "minute",
    "3m": "3minute", "3minute": "3minute",
    "5m": "5minute", "5minute": "5minute",
    "10m": "10minute", "10minute": "10minute",
    "15m": "15minute", "15minute": "15minute",
    "30m": "30minute", "30minute": "30minute",
    "60m": "60minute", "1h": "60minute", "60minute": "60minute",
    "1d": "day", "day": "day",
}


def normalize_timeframe(timeframe: str) -> str:
    """Normalize timeframe alias to canonical interval name."""
    canonical = TIMEFRAME_ALIASES.get(timeframe.lower())
    if not canonical:
        raise HTTPException(status_code=400, detail=f"Invalid timeframe '{timeframe}'")
    return canonical


async def resolve_identifier(identifier: str, db: Session) -> int:
    """Resolve identifier (token or EXCHANGE:SYMBOL) to instrument token."""
    if identifier.isdigit():
        return int(identifier)
    
    if ":" not in identifier:
        raise HTTPException(
            status_code=400,
            detail="Invalid identifier format. Use token number or 'EXCHANGE:SYMBOL'"
        )
    
    from sqlalchemy import text
    exchange, symbol = identifier.split(":", 1)
    
    stmt = text("""
        SELECT instrument_token FROM public.kite_instruments
        WHERE exchange = :exchange AND tradingsymbol = :symbol
        LIMIT 1
    """)
    result = db.execute(stmt, {"exchange": exchange.upper(), "symbol": symbol.upper()}).scalar_one_or_none()
    
    if result is None:
        raise HTTPException(status_code=404, detail=f"Instrument '{identifier}' not found")
    
    return result


# ===== Core API Endpoints =====

@router.get("/{identifier}", response_model=CandlesResponse, summary="Get historical candles with auto-ingestion")
async def get_candles(
    identifier: str,
    background_tasks: BackgroundTasks,
    timeframe: str = Query(..., description="e.g., 1m, 5m, 15m, 1h, 1d"),
    from_ts: Optional[datetime] = Query(None, alias="from", description="Start time (ISO 8601)"),
    to_ts: Optional[datetime] = Query(None, alias="to", description="End time (ISO 8601), defaults to now"),
    ingest: bool = Query(True, description="Trigger background ingestion for missing data"),
    db: Session = Depends(get_db),
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Get historical candles for an instrument.
    
    - **identifier**: Instrument token (number) or EXCHANGE:SYMBOL (e.g., NSE:RELIANCE)
    - **timeframe**: Time interval (1m, 5m, 15m, 30m, 1h, 1d)
    - **from**: Optional start time (defaults to reasonable lookback)
    - **to**: Optional end time (defaults to now)
    - **ingest**: If true, triggers background ingestion for missing data
    
    Returns candles in UTC epoch seconds. Frontend should add IST offset (+19800s) for display.
    """
    # Resolve identifier and normalize timeframe
    instrument_token = await resolve_identifier(identifier, db)
    interval = normalize_timeframe(timeframe)
    
    # Determine time range
    to_utc = to_ts or datetime.now(timezone.utc)
    if to_utc.tzinfo is None:
        to_utc = to_utc.replace(tzinfo=timezone.utc)
    
    # Calculate reasonable from_ts if not provided
    if from_ts is None:
        lookback_days = {
            'minute': 7, '3minute': 14, '5minute': 30, '10minute': 45,
            '15minute': 60, '30minute': 90, '60minute': 180, 'day': 365
        }
        from_utc = to_utc - timedelta(days=lookback_days.get(interval, 30))
    else:
        from_utc = from_ts if from_ts.tzinfo else from_ts.replace(tzinfo=timezone.utc)
    
    # Trigger background ingestion if enabled
    ingestion_status = IngestionStatus(status="disabled")
    if ingest:
        try:
            ingestion_service = CandleIngestion(kite)
            background_tasks.add_task(
                ingestion_service.ingest_historical_data,
                instrument_token,
                interval,
                from_utc,
                to_utc,
                force_refresh=False
            )
            ingestion_status = IngestionStatus(status="triggered", message="Background ingestion started")
        except Exception as e:
            logger.error(f"Failed to trigger ingestion: {e}")
            ingestion_status = IngestionStatus(status="error", message=str(e))
    
    # Query existing data from database
    candles = CandleStorage.query_candles(
        instrument_token,
        interval,
        from_utc,
        to_utc,
        include_oi=True
    )
    
    # Check if data is reasonably up-to-date
    if candles and ingestion_status.status == "triggered":
        latest_candle_ts = candles[-1]['ts']
        time_since_latest = (to_utc - latest_candle_ts.astimezone(timezone.utc)).total_seconds()
        
        # If latest candle is within 2x the interval, consider it up-to-date
        interval_seconds = {'minute': 60, '3minute': 180, '5minute': 300, '10minute': 600,
                          '15minute': 900, '30minute': 1800, '60minute': 3600, 'day': 86400}
        if time_since_latest < interval_seconds.get(interval, 300) * 2:
            ingestion_status = IngestionStatus(status="up_to_date", message="Data is current")
    
    # Convert candles to response format
    candle_data = [
        CandleData(
            time=int(c['ts'].timestamp()),  # UTC timestamp
            open=c['open'],
            high=c['high'],
            low=c['low'],
            close=c['close'],
            volume=c['volume'],
            oi=c.get('oi')
        )
        for c in candles
    ]
    
    meta = CandlesMeta(
        instrument_token=instrument_token,
        interval=interval,
        **{"from": from_utc.astimezone(IST).isoformat(), "to": to_utc.astimezone(IST).isoformat()},
        count=len(candle_data)
    )
    
    return CandlesResponse(
        meta=meta,
        ingestion=ingestion_status,
        candles=candle_data
    )


@router.get("/stream/{identifier}", summary="Stream real-time candles via SSE")
async def stream_candles(
    identifier: str,
    timeframe: str = Query(..., description="e.g., 1m, 5m, 15m, 1h"),
    db: Session = Depends(get_db)
):
    """
    Server-Sent Events stream for real-time candles.
    
    Combines historical snapshot with live updates from Redis Pub/Sub.
    Client can reconnect using Last-Event-ID for seamless recovery.
    """
    from fastapi.responses import StreamingResponse
    from .redis_events import get_redis, pubsub_iter
    
    instrument_token = await resolve_identifier(identifier, db)
    interval = normalize_timeframe(timeframe)
    
    async def event_generator():
        redis = get_redis()
        
        # Send initial snapshot from database
        now_utc = datetime.now(timezone.utc)
        lookback = timedelta(days=7 if interval == 'minute' else 30)
        
        candles = CandleStorage.query_candles(
            instrument_token,
            interval,
            now_utc - lookback,
            now_utc
        )
        
        # Check for latest completed candle in Redis
        latest_key = f"candle:{instrument_token}:{interval}:latest"
        latest_redis = await redis.get(latest_key)
        
        snapshot_data = {
            "instrument_token": instrument_token,
            "interval": interval,
            "candles": [
                [
                    c['ts'].isoformat().replace('+00:00', 'Z'),
                    c['open'], c['high'], c['low'], c['close'], c['volume']
                ]
                for c in candles
            ]
        }
        
        # Add Redis candle if newer than DB (handle both formats)
        if latest_redis:
            try:
                redis_candle_raw = json.loads(latest_redis)
                
                # Convert to list format if needed
                if isinstance(redis_candle_raw, dict):
                    # Old dict format
                    if 'ts' in redis_candle_raw and 'o' in redis_candle_raw:
                        redis_candle = [
                            redis_candle_raw['ts'],
                            redis_candle_raw['o'],
                            redis_candle_raw['h'],
                            redis_candle_raw['l'],
                            redis_candle_raw['c'],
                            redis_candle_raw.get('v', 0)
                        ]
                        if redis_candle_raw.get('oi') is not None:
                            redis_candle.append(redis_candle_raw['oi'])
                    else:
                        logger.warning(f"Invalid Redis dict candle: {redis_candle_raw}")
                        redis_candle = None
                elif isinstance(redis_candle_raw, list) and len(redis_candle_raw) >= 6:
                    redis_candle = redis_candle_raw
                else:
                    logger.warning(f"Invalid Redis candle format: {redis_candle_raw}")
                    redis_candle = None
                
                # Add to snapshot if valid and newer
                if redis_candle:
                    redis_ts = datetime.fromisoformat(redis_candle[0].replace('Z', '+00:00'))
                    if not candles or redis_ts > candles[-1]['ts']:
                        snapshot_data['candles'].append(redis_candle)
            except Exception as e:
                logger.warning(f"Failed to parse Redis candle: {e}")
        
        yield f"event: snapshot\ndata: {json.dumps(snapshot_data)}\n\n"
        
        # Stream live updates: completed candles from Pub/Sub + forming candle ticks
        channel = f"realtime_candles:{instrument_token}:{interval}"
        current_key = f"candle:{instrument_token}:{interval}:current"
        
        import asyncio
        from asyncio import Queue
        
        # Queue to merge both streams
        event_queue: Queue = Queue()
        running = True
        
        async def poll_current_candle():
            """Continuously poll and send forming candle updates"""
            last_candle_str = None
            while running:
                try:
                    await asyncio.sleep(0.5)  # Poll every 500ms
                    current_data = await redis.get(current_key)
                    if current_data and current_data != last_candle_str:
                        last_candle_str = current_data
                        try:
                            candle = json.loads(current_data)
                            if isinstance(candle, list) and len(candle) >= 6:
                                event_data = {
                                    "event": "tick",
                                    "instrument_token": instrument_token,
                                    "interval": interval,
                                    "candle": candle
                                }
                                await event_queue.put(('tick', event_data))
                        except Exception as e:
                            logger.debug(f"Failed to parse current candle: {e}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error polling current candle: {e}")
        
        async def listen_pubsub():
            """Listen for completed candle events from Pub/Sub"""
            try:
                async for message in pubsub_iter(channel):
                    if message.get("event") == "candle":
                        await event_queue.put(('candle', message))
            except Exception as e:
                logger.error(f"Pub/Sub error: {e}")
        
        # Start both tasks
        poll_task = asyncio.create_task(poll_current_candle())
        pubsub_task = asyncio.create_task(listen_pubsub())
        
        try:
            # Stream events from queue
            while True:
                try:
                    # Wait for next event with timeout
                    event_type, event_data = await asyncio.wait_for(
                        event_queue.get(), 
                        timeout=1.0
                    )
                    
                    if event_type == 'tick':
                        yield f"event: tick\ndata: {json.dumps(event_data)}\n\n"
                    elif event_type == 'candle':
                        yield f"id: {event_data['candle'][0]}\nevent: candle\ndata: {json.dumps(event_data)}\n\n"
                        
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f": heartbeat\n\n"
                    continue
                except asyncio.CancelledError:
                    break
        except Exception as e:
            logger.error(f"Error in SSE stream: {e}")
        finally:
            running = False
            poll_task.cancel()
            pubsub_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            try:
                await pubsub_task
            except asyncio.CancelledError:
                pass
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.delete("/{identifier}/cache", summary="Clear cached candle data")
async def clear_cache(
    identifier: str,
    db: Session = Depends(get_db)
):
    """Delete all cached candle data for an instrument."""
    instrument_token = await resolve_identifier(identifier, db)
    deleted = CandleStorage.clear_instrument_cache(instrument_token)
    
    return {
        "status": "success",
        "instrument_token": instrument_token,
        "deleted_rows": deleted
    }


@router.get("/{identifier}/coverage", summary="Get data coverage statistics")
async def get_coverage(
    identifier: str,
    timeframe: str = Query(..., description="e.g., 5m, 1h, 1d"),
    db: Session = Depends(get_db)
):
    """Get coverage statistics for an instrument's candle data."""
    instrument_token = await resolve_identifier(identifier, db)
    interval = normalize_timeframe(timeframe)
    
    coverage = CandleStorage.get_data_coverage(instrument_token, interval)
    
    return {
        "status": "success",
        "instrument_token": instrument_token,
        "interval": interval,
        "coverage": coverage
    }


# ===== Service Management Endpoints =====

@router.post("/aggregator/start", summary="Start real-time candle aggregator")
async def start_aggregator(
    config: AggregatorConfig,
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Start the real-time candle aggregator service.
    
    This service:
    - Subscribes to live ticks via WebSocket
    - Aggregates ticks into candles for configured intervals
    - Writes forming candles to Redis
    - Publishes completed candles to Redis Pub/Sub
    - Automatically persists completed candles to database
    """
    try:
        aggregator = get_aggregator(API_KEY)
        
        if aggregator.running:
            return {
                "status": "already_running",
                "config": aggregator.get_status()
            }
        
        # Validate intervals
        invalid_intervals = [i for i in config.intervals if i not in SUPPORTED_INTERVALS]
        if invalid_intervals:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid intervals: {invalid_intervals}. Supported: {list(SUPPORTED_INTERVALS)}"
            )
        
        await aggregator.start(
            kite.access_token,
            config.intervals,
            config.owner_scope,
            config.refresh_seconds
        )
        
        return {
            "status": "started",
            "config": aggregator.get_status()
        }
        
    except Exception as e:
        logger.error(f"Failed to start aggregator: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/aggregator/stop", summary="Stop real-time candle aggregator")
async def stop_aggregator():
    """Stop the real-time candle aggregator service."""
    try:
        aggregator = get_aggregator(API_KEY)
        
        if not aggregator.running:
            return {"status": "already_stopped"}
        
        await aggregator.stop()
        
        return {"status": "stopped"}
        
    except Exception as e:
        logger.error(f"Failed to stop aggregator: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/aggregator/status", summary="Get aggregator status")
async def get_aggregator_status():
    """Get the current status of the candle aggregator."""
    aggregator = get_aggregator(API_KEY)
    return aggregator.get_status()


@router.post("/ingestion/start", summary="Start historical data ingestion scheduler")
async def start_ingestion(
    config: IngestionConfig,
    scheduler: IngestionScheduler = Depends(get_ingestion_scheduler)
):
    """
    Start the historical data ingestion scheduler.
    
    This service periodically fetches missing historical data from Kite API
    to keep the database up-to-date.
    """
    try:
        if scheduler.running:
            return {
                "status": "already_running",
                "config": scheduler.get_status()
            }
        
        await scheduler.start(
            config.intervals,
            config.owner_scope,
            config.schedule_seconds
        )
        
        return {
            "status": "started",
            "config": scheduler.get_status()
        }
        
    except Exception as e:
        logger.error(f"Failed to start ingestion scheduler: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingestion/stop", summary="Stop historical data ingestion scheduler")
async def stop_ingestion(
    scheduler: IngestionScheduler = Depends(get_ingestion_scheduler)
):
    """Stop the historical data ingestion scheduler."""
    try:
        if not scheduler.running:
            return {"status": "already_stopped"}
        
        await scheduler.stop()
        
        return {"status": "stopped"}
        
    except Exception as e:
        logger.error(f"Failed to stop ingestion scheduler: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ingestion/status", summary="Get ingestion scheduler status")
async def get_ingestion_status(
    scheduler: IngestionScheduler = Depends(get_ingestion_scheduler)
):
    """Get the current status of the ingestion scheduler."""
    return scheduler.get_status()


@router.post("/ingestion/run-now", summary="Trigger immediate ingestion run")
async def run_ingestion_now(
    intervals: Optional[List[str]] = Query(None),
    tokens: Optional[List[int]] = Query(None),
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Trigger an immediate ingestion run for specified intervals and tokens.
    If not specified, uses all watchlist tokens and configured intervals.
    """
    try:
        ingestion = CandleIngestion(kite)
        
        # Get tokens from watchlist if not specified
        if not tokens:
            from database import get_db
            from sqlalchemy import text
            
            db_session = next(get_db())
            try:
                stmt = text("SELECT DISTINCT instrument_token FROM public.user_watchlists")
                results = db_session.execute(stmt).fetchall()
                tokens = [row[0] for row in results]
            finally:
                db_session.close()
        
        if not tokens:
            return {
                "status": "no_tokens",
                "message": "No tokens found in watchlist"
            }
        
        intervals = intervals or ["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"]

        
        results = []
        for token in tokens:
            for interval in intervals:
                try:
                    result = await ingestion.ingest_historical_data(token, interval)
                    results.append({
                        "token": token,
                        "interval": interval,
                        **result
                    })
                except Exception as e:
                    logger.error(f"Failed to ingest {token}|{interval}: {e}")
                    results.append({
                        "token": token,
                        "interval": interval,
                        "status": "error",
                        "error": str(e)
                    })
        
        return {
            "status": "completed",
            "results": results
        }
        
    except Exception as e:
        logger.error(f"Failed to run immediate ingestion: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===== Helper Functions for Historical Data =====

def get_max_days_for_interval(interval: str) -> int:
    """Returns the maximum number of days for which historical data can be fetched in a single API call."""
    interval_days_map = {
        'minute': 60,
        '3minute': 100,
        '5minute': 100,
        '10minute': 100,
        '15minute': 200,
        '30minute': 200,
        '60minute': 400,
        'day': 2000
    }
    return interval_days_map.get(interval, 1)


async def fetch_historical_data_chunked(
    kite: KiteConnect,
    instrument_token: int,
    from_date: datetime,
    to_date: datetime,
    interval: str,
    continuous: bool,
    oi: bool
) -> List[Dict[str, Any]]:
    """Fetches historical data in chunks, prioritizing the most recent data first."""
    all_records = []
    max_days = get_max_days_for_interval(interval)
    
    current_to = to_date
    while current_to > from_date:
        current_from = max(current_to - timedelta(days=max_days), from_date)
        
        try:
            records = kite.historical_data(
                instrument_token,
                current_from,
                current_to,
                interval,
                continuous=continuous,
                oi=oi
            )
            if records:
                all_records.extend(records)
            
            current_to = current_from - timedelta(seconds=1)
            
            # Rate limit: wait before the next call to stay within 3 requests per second
            await asyncio.sleep(1/3)

        except Exception as e:
            logger.error(f"Error fetching historical data for token {instrument_token} from {current_from} to {current_to}: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream API error: {e}")
            
    # The records are fetched in reverse chronological order, so we must sort them back
    all_records.sort(key=lambda x: x['date'])
    return all_records


def convert_kite_record_to_ist_tuple(record: Dict[str, Any]) -> tuple:
    """Converts a Kite API record to an IST-aware tuple for DB insertion."""
    ts = record['date']
    ist = pytz.timezone('Asia/Kolkata')
    
    # Kiteconnect can return naive datetimes which are in IST
    if ts.tzinfo is None:
        ts_ist = ist.localize(ts)
    else:
        ts_ist = ts.astimezone(ist)
        
    return (
        ts_ist,
        record['open'],
        record['high'],
        record['low'],
        record['close'],
        record.get('volume'),
        record.get('oi')
    )


def upsert_candles_batch(instrument_token: int, interval: str, records: List[tuple]) -> tuple:
    """UPSERTS a batch of candle records into the historical_candles table. Returns (inserted_count, updated_count)."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            query = """
                INSERT INTO public.historical_candles (instrument_token, interval, ts, open, high, low, close, volume, oi)
                VALUES %s
                ON CONFLICT (instrument_token, interval, ts) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    oi = EXCLUDED.oi,
                    updated_at = NOW()
                RETURNING (xmax = 0) AS inserted;
            """
            
            template = f"({instrument_token}, '{interval}', %s, %s, %s, %s, %s, %s, %s)"
            results = execute_values(cur, query, records, template=template, fetch=True)
            conn.commit()

            inserted_count = sum(1 for r in results if r[0])
            updated_count = len(results) - inserted_count
            return inserted_count, updated_count

    except Exception as e:
        logger.error(f"DB error during batch upsert for {instrument_token} ({interval}): {e}", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during upsert: {e}")
    finally:
        if conn:
            conn.close()
    return 0, 0


def get_latest_timestamp_from_db(instrument_token: int, interval: str) -> Optional[datetime]:
    """Fetches the latest timestamp for a given instrument and interval from the DB."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(ts) FROM public.historical_candles
                WHERE instrument_token = %s AND interval = %s
                """,
                (instrument_token, interval)
            )
            result = cur.fetchone()
            if result and result[0]:
                return result[0]
    except Exception as e:
        logger.error(f"DB error fetching latest timestamp for {instrument_token} ({interval}): {e}")
    finally:
        if conn:
            conn.close()
    return None


def get_default_from_date(interval: str) -> datetime:
    """Provides a sensible default 'from' date based on the interval."""
    now = datetime.now(timezone.utc)
    if interval == 'day':
        return now - timedelta(days=365)
    return now - timedelta(days=30)


# ===== Additional Historical Data Endpoints =====

ALLOWED_INTERVALS = {
    'minute', '3minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day'
}


@router.post("/historical/ingest", summary="Ingest historical data from Kite API into DB")
async def ingest_historical_data(
    req: IngestRequest,
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Fetches historical data from the upstream Kite API and upserts it into the local database.
    This is a direct ingestion endpoint for manual data loading.
    """
    if req.interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Allowed values: {', '.join(ALLOWED_INTERVALS)}")

    # Determine effective from/to dates
    to_date = req.to_date or datetime.now(timezone.utc)
    if to_date.tzinfo is None:
        to_date = to_date.replace(tzinfo=timezone.utc)

    from_date = req.from_date
    if not from_date:
        latest_ts = get_latest_timestamp_from_db(req.instrument_token, req.interval)
        if latest_ts:
            from_date = latest_ts + timedelta(seconds=1)
        else:
            from_date = get_default_from_date(req.interval)
    
    if from_date.tzinfo is None:
        from_date = from_date.replace(tzinfo=timezone.utc)

    if from_date >= to_date:
        return {
            "status": "success",
            "data": {
                "message": "Data is already up to date.",
                "instrument_token": req.instrument_token,
                "interval": req.interval,
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "received": 0, "inserted": 0, "updated": 0
            }
        }

    logger.info(f"Ingesting data for {req.instrument_token} ({req.interval}) from {from_date} to {to_date}")

    # Fetch data from API
    records = await fetch_historical_data_chunked(
        kite, req.instrument_token, from_date, to_date, req.interval,
        req.continuous, req.oi
    )

    if not records:
        return {"status": "success", "data": {"received": 0, "inserted": 0, "updated": 0}}

    # Transform and UPSERT into DB
    db_rows = [convert_kite_record_to_ist_tuple(r) for r in records]
    inserted, updated = upsert_candles_batch(req.instrument_token, req.interval, db_rows)

    return {
        "status": "success",
        "data": {
            "instrument_token": req.instrument_token,
            "interval": req.interval,
            "from": from_date.isoformat().replace('+00:00', 'Z'),
            "to": to_date.isoformat().replace('+00:00', 'Z'),
            "received": len(records),
            "inserted": inserted,
            "updated": updated
        }
    }


@router.get("/historical/query", response_model=DBCandlesResponse, summary="Query historical data from DB")
async def query_historical_data(
    instrument_token: int,
    interval: str,
    from_date: Optional[datetime] = Query(None, alias="from"),
    to_date: Optional[datetime] = Query(None, alias="to"),
    include_oi: int = Query(0, ge=0, le=1)
):
    """
    Queries the local database for historical candle data.
    This is a direct DB query endpoint without any ingestion logic.
    """
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Allowed values: {', '.join(ALLOWED_INTERVALS)}")

    # Default to_date to now if not provided
    if to_date is None:
        to_date = datetime.now(timezone.utc)

    # Default from_date based on a lookback from to_date if not provided
    if from_date is None:
        from_date = to_date - timedelta(days=30)

    # Ensure timezone-aware UTC
    from_utc = from_date.astimezone(timezone.utc) if from_date.tzinfo else from_date.replace(tzinfo=timezone.utc)
    to_utc = to_date.astimezone(timezone.utc) if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            query = """
                SELECT ts, open, high, low, close, volume, oi
                FROM public.historical_candles
                WHERE instrument_token = %s AND interval = %s AND ts BETWEEN %s AND %s
                ORDER BY ts ASC;
            """
            cur.execute(query, (instrument_token, interval, from_utc, to_utc))
            rows = cur.fetchall()

        candles = []
        for row in rows:
            ts, open_val, high_val, low_val, close_val, volume, oi = row
            candle = [
                ts.isoformat().replace('+00:00', 'Z'),
                float(open_val), float(high_val), float(low_val), float(close_val),
                volume
            ]
            if include_oi and oi is not None:
                candle.append(oi)
            candles.append(candle)

        return {"status": "success", "data": {"candles": candles}}

    except Exception as e:
        logger.error(f"DB query error for {instrument_token} ({interval}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database query error: {e}")
    finally:
        if conn:
            conn.close()


@router.get("/instruments/historical/{instrument_token}/{interval}", summary="Fetch historical data directly from Kite API")
async def get_historical_instrument_data(
    instrument_token: int,
    interval: str,
    from_date: datetime = Query(..., alias="from", description="Start date in yyyy-mm-dd hh:mm:ss format"),
    to_date: datetime = Query(..., alias="to", description="End date in yyyy-mm-dd hh:mm:ss format"),
    continuous: int = Query(0, description="0 or 1 for continuous data"),
    oi: int = Query(0, description="0 or 1 for OI data"),
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Retrieves historical candle records directly from Kite API (bypasses DB).
    Handles chunking for large date ranges automatically.
    """
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail="Invalid interval specified.")

    continuous_bool = bool(continuous)
    oi_bool = bool(oi)

    try:
        records = await fetch_historical_data_chunked(
            kite,
            instrument_token,
            from_date,
            to_date,
            interval,
            continuous_bool,
            oi_bool
        )

        candles = []
        for record in records:
            if isinstance(record['date'], datetime):
                timestamp = record['date']
                ist_tz = pytz.timezone('Asia/Kolkata')

                if timestamp.tzinfo is None:
                    timestamp = ist_tz.localize(timestamp)
                else:
                    timestamp = timestamp.astimezone(ist_tz)
                
                formatted_timestamp = timestamp.strftime('%Y-%m-%dT%H:%M:%S%z')
            else:
                formatted_timestamp = record['date']

            candle = [
                formatted_timestamp,
                record['open'],
                record['high'],
                record['low'],
                record['close'],
                record['volume']
            ]
            if oi_bool and 'oi' in record:
                candle.append(record['oi'])
            candles.append(candle)

        return {
            "status": "success",
            "data": {
                "candles": candles
            }
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching historical data for token {instrument_token}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


# ===== User Watchlist Management =====

@router.post("/user/watchlist", response_model=UpsertResponse, summary="Upsert instruments into a user's watchlist")
def upsert_user_watchlist(
    req: WatchlistUpsertRequest,
    replace: bool = Query(False, description="If true, remove existing instruments not in the request"),
    db: Session = Depends(get_db)
):
    """
    Performs a batch UPSERT of instruments for a given owner_id.
    - If `replace=true`, instruments for that owner not in the list will be deleted.
    - Returns counts of inserted, updated, and removed instruments.
    """
    owner_id = req.owner_id
    instrument_data = [i.model_dump() for i in req.instruments]
    
    if not instrument_data:
        if not replace:
            return {"inserted": 0, "updated": 0, "removed": 0}
        # Handle deletion if replace is true and instrument list is empty
        try:
            delete_stmt = text("DELETE FROM public.user_watchlists WHERE owner_id = :owner_id")
            result = db.execute(delete_stmt, {"owner_id": owner_id})
            db.commit()
            return {"inserted": 0, "updated": 0, "removed": result.rowcount}
        except Exception as e:
            db.rollback()
            logger.error(f"DB error deleting watchlist for owner {owner_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {e}")

    removed_count = 0
    if replace:
        try:
            provided_tokens = {i['instrument_token'] for i in instrument_data}
            delete_stmt = text(
                "DELETE FROM public.user_watchlists WHERE owner_id = :owner_id AND instrument_token NOT IN :tokens"
            )
            result = db.execute(delete_stmt, {"owner_id": owner_id, "tokens": tuple(provided_tokens)})
            removed_count = result.rowcount
        except Exception as e:
            db.rollback()
            logger.error(f"DB error during replace operation for owner {owner_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error during replace: {e}")

    try:
        upsert_stmt = text("""
            INSERT INTO public.user_watchlists (owner_id, instrument_token, tradingsymbol, name, exchange, instrument_type)
            VALUES (:owner_id, :instrument_token, :tradingsymbol, :name, :exchange, :instrument_type)
            ON CONFLICT (owner_id, instrument_token) DO UPDATE SET
                tradingsymbol = EXCLUDED.tradingsymbol,
                name = EXCLUDED.name,
                exchange = EXCLUDED.exchange,
                instrument_type = EXCLUDED.instrument_type
            RETURNING (xmax = 0) AS inserted;
        """)
        
        results = []
        for instrument in instrument_data:
            params = {"owner_id": owner_id, **instrument}
            result = db.execute(upsert_stmt, params).fetchone()
            if result:
                results.append(result[0])

        db.commit()

        inserted_count = sum(1 for r in results if r)
        updated_count = len(results) - inserted_count
        
        return {"inserted": inserted_count, "updated": updated_count, "removed": removed_count}

    except Exception as e:
        db.rollback()
        logger.error(f"DB error during watchlist upsert for owner {owner_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error during upsert: {e}")


@router.get("/user/watchlist", response_model=List[WatchlistInstrument], summary="Get a user's watchlist")
def get_user_watchlist(
    owner_id: str = Query("default", description="The owner ID of the watchlist"),
    db: Session = Depends(get_db)
):
    """
    Retrieves the list of instruments for a given owner_id.
    """
    try:
        stmt = text("""
            SELECT instrument_token, tradingsymbol, name, exchange, instrument_type
            FROM public.user_watchlists
            WHERE owner_id = :owner_id
            ORDER BY tradingsymbol;
        """)
        results = db.execute(stmt, {"owner_id": owner_id}).fetchall()
        return [WatchlistInstrument.model_validate(row, from_attributes=True) for row in results]
    except Exception as e:
        logger.error(f"DB error fetching watchlist for owner {owner_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# ===== Debug Endpoints =====

@router.get("/debug/timezone/{instrument_token}/{interval}", summary="Debug timezone handling")
async def debug_timezone(
    instrument_token: int,
    interval: str,
    db: Session = Depends(get_db)
):
    """Debug endpoint to check timezone handling for candle data."""
    ist = pytz.timezone("Asia/Kolkata")
    
    now_utc = datetime.now(timezone.utc)
    now_ist = datetime.now(ist)
    
    last_ts = get_latest_timestamp_from_db(instrument_token, interval)
    
    result = {
        "now_utc": now_utc.isoformat(),
        "now_ist": now_ist.isoformat(),
        "last_ts_in_db": last_ts.isoformat() if last_ts else None,
        "last_ts_timezone": str(last_ts.tzinfo) if last_ts else None,
    }
    
    if last_ts:
        last_ts_utc = last_ts.astimezone(timezone.utc) if last_ts.tzinfo else ist.localize(last_ts).astimezone(timezone.utc)
        result["last_ts_converted_to_utc"] = last_ts_utc.isoformat()
        result["comparison"] = {
            "last_ts_utc": last_ts_utc.isoformat(),
            "now_utc": now_utc.isoformat(),
            "diff_seconds": (now_utc - last_ts_utc).total_seconds()
        }
    
    return result
