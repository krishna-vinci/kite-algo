import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Literal, Union, Tuple, Set
from dataclasses import dataclass, field
from kiteconnect import KiteTicker
import pandas as pd
import pytz
import psycopg2
from psycopg2.extras import execute_values
from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks
from starlette.responses import StreamingResponse
import json
import asyncio
from kiteconnect import KiteConnect
from sqlalchemy.orm import Session
from sqlalchemy import text
from redis.asyncio import Redis as RedisClient
from database import get_db_connection, get_db
from .redis_events import get_redis, pubsub_iter, publish_event
from .kite_orders import KiteSession
from broker_api.kite_auth import login_headless, get_kite
from broker_api.kite_auth import API_KEY
from .instruments_repository import InstrumentsRepository
import pytz


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

router = APIRouter()

# --- Constants and Configuration ---
ALLOWED_INTERVALS = {
    'minute', '3minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day'
}
INTERVAL_SECONDS: Dict[str, Union[int, Literal['day']]] = {
    'minute': 60,
    '3minute': 180,
    '5minute': 300,
    '10minute': 600,
    '15minute': 900,
    '30minute': 1800,
    '60minute': 3600,
    'day': 'day'
}

# --- Pydantic Models ---

class IngestionStartRequest(BaseModel):
    intervals: Optional[List[str]] = Field(default=None)
    owner_scope: Optional[str] = "all"
    schedule_seconds: Optional[int] = 900
    from_override: Optional[datetime] = None
    to_override: Optional[datetime] = None

    @field_validator('intervals')
    def validate_intervals(cls, v):
        if v is not None:
            invalid = set(v) - ALLOWED_INTERVALS
            if invalid:
                raise ValueError(f"Invalid intervals: {', '.join(invalid)}")
        return v

class IngestionRunNowRequest(BaseModel):
    intervals: Optional[List[str]] = None
    owner_scope: Optional[str] = "all"
    from_override: Optional[datetime] = None
    to_override: Optional[datetime] = None
    tokens: Optional[List[int]] = None

    @field_validator('intervals')
    def validate_intervals(cls, v):
        if v is not None:
            invalid = set(v) - ALLOWED_INTERVALS
            if invalid:
                raise ValueError(f"Invalid intervals: {', '.join(invalid)}")
        return v

class IngestRequest(BaseModel):
    instrument_token: int
    interval: str
    from_date: Optional[datetime] = Field(None, alias="from")
    to_date: Optional[datetime] = Field(None, alias="to")
    continuous: Optional[bool] = False
    oi: Optional[bool] = False

class DBCandlesResponse(BaseModel):
    status: Literal['success']
    data: Dict[str, Any]

class WatchlistInstrument(BaseModel):
    instrument_token: int
    tradingsymbol: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    instrument_type: Optional[str] = None

class WatchlistUpsertRequest(BaseModel):
    owner_id: Optional[str] = "default"
    instruments: List[WatchlistInstrument]

class UpsertResponse(BaseModel):
    inserted: int
    updated: int
    removed: int

class AggregatorStartRequest(BaseModel):
   intervals: Optional[List[str]] = Field(
       default=["minute", "5minute", "15minute", "60minute", "day"],
       description="List of intervals to aggregate."
   )
   owner_scope: Optional[str] = Field(
       default="all",
       description="Scope of watchlists to use ('all' or a specific owner_id)."
   )
   refresh_seconds: Optional[int] = Field(
       default=30,
       description="How often to refresh the consolidated watchlist."
   )

   @field_validator('intervals')
   def validate_intervals(cls, v):
       if not v:
           return ["minute", "5minute", "15minute", "60minute", "day"]
       invalid = set(v) - ALLOWED_INTERVALS
       if invalid:
           raise ValueError(f"Invalid intervals provided: {', '.join(invalid)}")
       return v

class AggregatorWarmStartRequest(BaseModel):
    intervals: Optional[List[str]] = Field(default=None, description="Default to all configured aggregator intervals")
    owner_scope: Optional[str] = Field(default="all", description="Or a specific owner_id")
    tokens: Optional[List[int]] = Field(default=None, description="Optionally limit to specific tokens")

    @field_validator('intervals')
    def validate_intervals(cls, v):
        if v is not None:
            invalid = set(v) - ALLOWED_INTERVALS
            if invalid:
                raise ValueError(f"Invalid intervals: {', '.join(invalid)}")
        return v

class AggregatorWarmStartRequest(BaseModel):
    intervals: Optional[List[str]] = Field(default=None, description="Default to all configured aggregator intervals")
    owner_scope: Optional[str] = Field(default="all", description="Or a specific owner_id")
    tokens: Optional[List[int]] = Field(default=None, description="Optionally limit to specific tokens")

    @field_validator('intervals')
    def validate_intervals(cls, v):
        if v is not None:
            invalid = set(v) - ALLOWED_INTERVALS
            if invalid:
                raise ValueError(f"Invalid intervals: {', '.join(invalid)}")
        return v

class AggregatorStatusResponse(BaseModel):
   running: bool
   intervals: List[str]
   token_count: int
   last_publish: Dict[str, datetime]
   last_warm_start_utc: Optional[datetime] = None
   warm_start_stats: Dict[str, Any] = {}
   last_warm_start_utc: Optional[datetime] = None
   warm_start_stats: Dict[str, Any] = {}


# --- Real-time Candle Aggregation ---

@dataclass
class CurrentCandleState:
    """Holds the state of a forming candle for a specific (token, interval) pair."""
    bucket_start_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[float] = None
    base_volume: int = 0
    tick_count: int = 0

async def _get_consolidated_watchlist_tokens(owner_scope: str, db: Session) -> Set[int]:
    """Fetches a unique set of instrument tokens from user watchlists."""
    try:
        if owner_scope == "all":
            stmt = text("SELECT DISTINCT instrument_token FROM public.user_watchlists")
            results = db.execute(stmt).fetchall()
        else:
            stmt = text("SELECT instrument_token FROM public.user_watchlists WHERE owner_id = :owner_id")
            results = db.execute(stmt, {"owner_id": owner_scope}).fetchall()
        
        return {row[0] for row in results}
    except Exception as e:
        logger.error(f"DB error fetching consolidated watchlist for scope '{owner_scope}': {e}", exc_info=True)
        return set()

class AggregatorManager:
   """Singleton to manage the real-time candle aggregation lifecycle."""
   _instance = None

   def __new__(cls, *args, **kwargs):
       if cls._instance is None:
           cls._instance = super(AggregatorManager, cls).__new__(cls)
       return cls._instance

   def __init__(self):
       # Prevent re-initialization
       if hasattr(self, '_initialized') and self._initialized:
           return
       
       self.running: bool = False
       self.config: Optional[AggregatorStartRequest] = None
       self.tasks: Dict[str, asyncio.Task] = {}
       self.kws: Optional[KiteTicker] = None
       self.redis: Optional[RedisClient] = None
       
       self.current_candles: Dict[Tuple[int, str], CurrentCandleState] = {}
       self.last_publish: Dict[Tuple[int, str], datetime] = {}
       self.subscribed_tokens: Set[int] = set()
       self.last_warm_start_utc: Optional[datetime] = None
       self.warm_start_stats: Dict[str, Any] = {}
       
       self._initialized: bool = True
       self._lock = asyncio.Lock()

   def get_status(self) -> AggregatorStatusResponse:
       """Returns the current status of the aggregator."""
       return AggregatorStatusResponse(
           running=self.running,
           intervals=self.config.intervals if self.config else [],
           token_count=len(self.subscribed_tokens),
           last_publish={f"{token}|{interval}": ts for (token, interval), ts in self.last_publish.items()},
           last_warm_start_utc=self.last_warm_start_utc,
           warm_start_stats=self.warm_start_stats
       )

   async def start(self, access_token: str, db: Session):
       """Initializes and starts the aggregator."""
       logger.info("AggregatorManager: Starting...")
       self.kws = KiteTicker(API_KEY, access_token)
       self.kws.on_ticks = self.on_ticks
       self.kws.on_connect = self.on_connect
       self.kws.on_close = self.on_close
       self.kws.on_error = self.on_error

       self.kws.connect(threaded=True)

       # Give the connection a moment to establish before warm-starting
       await asyncio.sleep(2)

       # Perform initial warm-start before processing ticks
       asyncio.create_task(self.warm_start_current_candles(db=db))
       
       # Start background tasks
       self.tasks['watchlist_refresh'] = asyncio.create_task(self._watchlist_refresh_loop(db))
       logger.info("AggregatorManager: Started.")

   async def stop(self):
       """Stops the aggregator and cleans up resources."""
       logger.info("AggregatorManager: Stopping...")
       for task_name, task in self.tasks.items():
           if not task.done():
               task.cancel()
               logger.info(f"Cancelled task: {task_name}")
       self.tasks.clear()

       if self.kws:
           if self.kws.is_connected():
               logger.info(f"Unsubscribing from {len(self.subscribed_tokens)} tokens.")
               self.kws.unsubscribe(list(self.subscribed_tokens))
           self.kws.stop()
       
       # Reset state
       self.running = False
       self.current_candles.clear()
       self.subscribed_tokens.clear()
       self.last_publish.clear()
       self.config = None
       logger.info("AggregatorManager: Stopped.")

   async def _watchlist_refresh_loop(self, db: Session):
       """Periodically refreshes watchlist and updates subscriptions."""
       while self.running:
           try:
               logger.info("Refreshing consolidated watchlist...")
               desired_tokens = await _get_consolidated_watchlist_tokens(self.config.owner_scope, db)
               
               if not self.kws or not self.kws.is_connected():
                   logger.warning("Watchlist refresh: Websocket not connected. Skipping subscription update.")
                   await asyncio.sleep(self.config.refresh_seconds)
                   continue

               to_subscribe = list(desired_tokens - self.subscribed_tokens)
               to_unsubscribe = list(self.subscribed_tokens - desired_tokens)

               if to_subscribe:
                   logger.info(f"Subscribing to {len(to_subscribe)} new tokens.")
                   self.kws.subscribe(to_subscribe)
                   self.kws.set_mode(self.kws.MODE_FULL, to_subscribe)
               
               if to_unsubscribe:
                   logger.info(f"Unsubscribing from {len(to_unsubscribe)} tokens.")
                   self.kws.unsubscribe(to_unsubscribe)

               self.subscribed_tokens = desired_tokens
               logger.info(f"Watchlist refresh complete. Total subscribed tokens: {len(self.subscribed_tokens)}")

           except Exception as e:
               logger.error(f"Error in watchlist refresh loop: {e}", exc_info=True)
           
           await asyncio.sleep(self.config.refresh_seconds)

   def _get_bucket_start_ts(self, ts: datetime, interval: str) -> datetime:
       """Calculates the UTC timestamp for the start of the candle bucket."""
       timeframe = INTERVAL_SECONDS.get(interval)
       if timeframe == 'day':
           return ts.replace(hour=0, minute=0, second=0, microsecond=0)
       
       epoch = ts.timestamp()
       bucket_start_epoch = (epoch // timeframe) * timeframe
       return datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)

   def on_ticks(self, ws, ticks):
       """Main callback to process incoming ticks from the websocket."""
       try:
           loop = asyncio.get_event_loop()
           asyncio.run_coroutine_threadsafe(self._process_ticks_async(ticks), loop)
       except Exception as e:
           logger.error(f"Error scheduling tick processing: {e}", exc_info=True)

   async def _process_ticks_async(self, ticks: List[Dict]):
       """Async handler for processing a batch of ticks."""
       for tick in ticks:
           try:
               token = tick.get("instrument_token")
               last_price = tick.get("last_price")
               tick_ts_raw = tick.get("exchange_timestamp") or datetime.now(timezone.utc)
               
               if not all([token, last_price, tick_ts_raw]):
                   logger.warning(f"Skipping malformed tick: {tick}")
                   continue
               
               tick_ts = tick_ts_raw.replace(tzinfo=timezone.utc) if tick_ts_raw.tzinfo is None else tick_ts_raw.astimezone(timezone.utc)

               for interval in self.config.intervals:
                   await self._update_candle_for_interval(token, tick, tick_ts, interval)
           except Exception as e:
               logger.error(f"Error processing single tick for token {tick.get('instrument_token')}: {e}", exc_info=True)

   async def _update_candle_for_interval(self, token: int, tick: Dict, tick_ts: datetime, interval: str):
       """Updates the candle state for a single token and interval."""
       key = (token, interval)
       current_candle = self.current_candles.get(key)
       bucket_start = self._get_bucket_start_ts(tick_ts, interval)

       if not current_candle or current_candle.bucket_start_ts != bucket_start:
           if current_candle:
               await self._finalize_and_publish_candle(token, interval, current_candle)
           
           # Start new candle
           self.current_candles[key] = CurrentCandleState(
               bucket_start_ts=bucket_start,
               open=tick['last_price'],
               high=tick['last_price'],
               low=tick['last_price'],
               close=tick['last_price'],
               volume=0,
               base_volume=tick.get('volume_traded', 0),
               oi=tick.get('oi'),
               tick_count=1
           )
       else:
           # Update existing candle
           current_candle.high = max(current_candle.high, tick['last_price'])
           current_candle.low = min(current_candle.low, tick['last_price'])
           current_candle.close = tick['last_price']
           
           # Volume delta logic
           current_volume_traded = tick.get('volume_traded', 0)
           if current_volume_traded < current_candle.base_volume: # Reset detected
               current_candle.base_volume = current_volume_traded
           current_candle.volume = max(0, current_volume_traded - current_candle.base_volume)

           if 'oi' in tick:
               current_candle.oi = tick['oi']
           current_candle.tick_count += 1

       # Write current forming candle to Redis (can be frequent)
       await self._write_current_candle_to_redis(token, interval, self.current_candles[key])

   async def _finalize_and_publish_candle(self, token: int, interval: str, candle: CurrentCandleState):
       """Writes the completed candle to Redis and publishes it."""
       logger.info(f"Finalizing candle for {token} ({interval}) at {candle.bucket_start_ts.isoformat()}")
       
       candle_ts_iso = candle.bucket_start_ts.isoformat().replace('+00:00', 'Z')
       
       # Payload for Redis 'latest' and Pub/Sub
       candle_list = [
           candle_ts_iso,
           candle.open,
           candle.high,
           candle.low,
           candle.close,
           candle.volume
       ]
       if candle.oi is not None:
           candle_list.append(candle.oi)

       # Pub/Sub payload
       publish_payload = {
           "event": "candle",
           "instrument_token": token,
           "interval": interval,
           "candle": candle_list
       }
       
       # Redis keys
       latest_key = f"candle:{token}:{interval}:latest"
       current_key = f"candle:{token}:{interval}:current"
       channel = f"realtime_candles:{token}:{interval}"

       try:
           async with self.redis.pipeline(transaction=True) as pipe:
               await pipe.set(latest_key, json.dumps(candle_list))
               await pipe.delete(current_key)
               await pipe.publish(channel, json.dumps(publish_payload))
               await pipe.execute()
           
           self.last_publish[(token, interval)] = datetime.now(timezone.utc)
           logger.info(f"Published candle for {token} ({interval}) to channel {channel}")

       except Exception as e:
           logger.error(f"Redis pipeline failed for finalizing candle {token} ({interval}): {e}", exc_info=True)

   async def _write_current_candle_to_redis(self, token: int, interval: str, candle: CurrentCandleState):
       """Writes the currently forming candle to a Redis key."""
       key = f"candle:{token}:{interval}:current"
       candle_ts_iso = candle.bucket_start_ts.isoformat().replace('+00:00', 'Z')
       
       candle_list = [
           candle_ts_iso,
           candle.open,
           candle.high,
           candle.low,
           candle.close,
           candle.volume
       ]
       if candle.oi is not None:
           candle_list.append(candle.oi)
           
       try:
           await self.redis.set(key, json.dumps(candle_list), ex=int(INTERVAL_SECONDS.get(interval, 60) * 2))
       except Exception as e:
           logger.warning(f"Failed to write current candle to Redis for {token} ({interval}): {e}")

   async def warm_start_current_candles(
       self,
       db: Session,
       owner_scope: Optional[str] = None,
       intervals: Optional[List[str]] = None,
       tokens: Optional[List[int]] = None
   ) -> Dict[str, Any]:
       """
       Reconstructs the 'current' candle state from the DB for all relevant tokens and intervals.
       This is used on startup and reconnect to ensure seamless aggregation.
       """
       logger.info("Starting aggregator warm-start process...")
       start_time = datetime.now(timezone.utc)
       
       # Use configured values if overrides are not provided
       effective_owner_scope = owner_scope or (self.config.owner_scope if self.config else "all")
       effective_intervals = intervals or (self.config.intervals if self.config else [])
       
       if not effective_intervals:
           logger.warning("Warm-start: No intervals configured. Aborting.")
           return {"status": "no_intervals", "updated_keys": 0}

       # 1. Get target tokens
       if tokens:
           target_tokens = set(tokens)
       else:
           target_tokens = await _get_consolidated_watchlist_tokens(effective_owner_scope, db)
       
       if not target_tokens:
           logger.warning(f"Warm-start: No tokens found for owner_scope '{effective_owner_scope}'.")
           return {"status": "no_tokens", "updated_keys": 0}

       logger.info(f"Warm-start for {len(target_tokens)} tokens and intervals: {effective_intervals}")

       summary = {
           "status": "success",
           "updated_count_per_interval": {interval: 0 for interval in effective_intervals},
           "last_updated": {},
           "start_utc": start_time.isoformat(),
           "duration_seconds": 0
       }

       # 2. Loop through each token and interval
       for token in target_tokens:
           for interval in effective_intervals:
               try:
                   now_utc = datetime.now(timezone.utc)
                   
                   # A) Compute current bucket start
                   timeframe_sec = INTERVAL_SECONDS.get(interval)
                   if timeframe_sec == 'day':
                       bucket_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                   else:
                       epoch = now_utc.timestamp()
                       bucket_start_epoch = (epoch // timeframe_sec) * timeframe_sec
                       bucket_start = datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)

                   # B) Fetch minute candles from DB for the current bucket window
                   minute_candles = await _fetch_minute_candles_for_warm_start(token, bucket_start, now_utc)

                   if not minute_candles:
                       logger.info(f"Warm-start: No minute data for {token}|{interval} in window [{bucket_start}, {now_utc}). Skipping.")
                       continue

                   # C) Aggregate minute candles into a single 'current' candle
                   first, last = minute_candles[0], minute_candles[-1]
                   agg_open = first['open']
                   agg_high = max(c['high'] for c in minute_candles)
                   agg_low = min(c['low'] for c in minute_candles)
                   agg_close = last['close']
                   agg_volume = sum(c['volume'] for c in minute_candles)
                   # Find last non-null OI
                   agg_oi = next((c['oi'] for c in reversed(minute_candles) if c['oi'] is not None), None)

                   reconstructed_candle = CurrentCandleState(
                       bucket_start_ts=bucket_start,
                       open=agg_open, high=agg_high, low=agg_low, close=agg_close,
                       volume=agg_volume, oi=agg_oi
                   )

                   # D) Write to Redis :current key, but do not publish
                   await self._write_current_candle_to_redis(token, interval, reconstructed_candle)
                   
                   summary["updated_count_per_interval"][interval] += 1
                   summary["last_updated"][f"{token}|{interval}"] = now_utc.isoformat()

               except Exception as e:
                   logger.error(f"Warm-start failed for {token}|{interval}: {e}", exc_info=True)

       end_time = datetime.now(timezone.utc)
       summary["duration_seconds"] = (end_time - start_time).total_seconds()
       
       self.last_warm_start_utc = end_time
       self.warm_start_stats = summary
       
       logger.info(f"Aggregator warm-start finished in {summary['duration_seconds']:.2f}s. "
                   f"Updated keys: {sum(summary['updated_count_per_interval'].values())}")
       
       return summary

   def on_connect(self, ws, response):
       """Callback on successful connect to KiteTicker."""
       logger.info("Aggregator websocket connected. Resubscribing and triggering warm-start.")
       
       # Resubscribe to existing tokens
       if self.subscribed_tokens:
           logger.info(f"Resubscribing to {len(self.subscribed_tokens)} tokens.")
           ws.subscribe(list(self.subscribed_tokens))
           ws.set_mode(ws.MODE_FULL, list(self.subscribed_tokens))
       
       # Trigger warm-start on reconnect
       # Need to get a DB session to pass to the warm-start function
       try:
           db_session = next(get_db())
           asyncio.create_task(self.warm_start_current_candles(db=db_session))
       except Exception as e:
           logger.error(f"Failed to obtain DB session for reconnect warm-start: {e}")

   def on_close(self, ws, code, reason):
       """Callback on connection close."""
       logger.warning(f"Aggregator websocket closed: {code} - {reason}")

   def on_error(self, ws, code, reason):
       """Callback on connection error."""
       logger.error(f"Aggregator websocket error: {code} - {reason}")


# Singleton instance
aggregator_manager = AggregatorManager()


# --- Authentication ---
def get_kite_db(db: Session = Depends(get_db)) -> KiteConnect:
    """
    Dependency to get a KiteConnect instance initialized with the system access token from the database.
    """
    system_session = db.query(KiteSession).filter_by(session_id="system").first()

    if not system_session or not system_session.access_token:
        logger.error("System Kite access token not found in kite_sessions table.")
        raise HTTPException(status_code=401, detail="System Kite credentials not configured in the database.")

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


# --- Helper Functions ---

def get_max_days_for_interval(interval: str) -> int:
    """Returns the maximum number of days for which historical data can be fetched in a single API call for a given interval."""
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
    """
    Fetches historical data in chunks to respect API limits and returns a consolidated list of candles.
    """
    all_records = []
    max_days = get_max_days_for_interval(interval)
    
    current_from = from_date
    while current_from < to_date:
        current_to = min(current_from + timedelta(days=max_days), to_date)
        
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
            
            current_from = current_to + timedelta(seconds=1) # Move to the next second to avoid overlap
            
            # Rate limit: wait before the next call to stay within 3 requests per second
            await asyncio.sleep(1/3)

        except Exception as e:
            logger.error(f"Error fetching historical data for token {instrument_token} from {current_from} to {current_to}: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream API error: {e}")
            
    return all_records

def get_default_from_date(interval: str) -> datetime:
    """Provides a sensible default 'from' date based on the interval."""
    now = datetime.now(timezone.utc)
    if interval == 'day':
        return now - timedelta(days=365)
    return now - timedelta(days=30)

def convert_kite_record_to_utc_tuple(record: Dict[str, Any]) -> Tuple:
    """Converts a Kite API record to a UTC-normalized tuple for DB insertion."""
    # Kiteconnect timestamps are timezone-aware (IST). Convert to UTC.
    ts_utc = record['date'].astimezone(timezone.utc)
    return (
        ts_utc,
        record['open'],
        record['high'],
        record['low'],
        record['close'],
        record.get('volume'),
        record.get('oi')
    )

# --- Database Helpers ---

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

async def _fetch_minute_candles_for_warm_start(instrument_token: int, from_utc: datetime, to_utc: datetime) -> List[Dict]:
    """Fetches minute-level candles from the DB for warm-start reconstruction."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            query = """
                SELECT ts, open, high, low, close, volume, oi
                FROM public.historical_candles
                WHERE instrument_token = %s AND interval = 'minute' AND ts >= %s AND ts < %s
                ORDER BY ts ASC;
            """
            cur.execute(query, (instrument_token, from_utc, to_utc))
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"DB error fetching minute candles for warm-start ({instrument_token}): {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

def upsert_candles_batch(instrument_token: int, interval: str, records: List[Tuple]) -> Tuple[int, int]:
    """
    UPSERTS a batch of candle records into the historical_candles table.
    Returns (inserted_count, updated_count).
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # The query uses ON CONFLICT to update existing records.
            # It returns the xmax pseudo-column, which is 0 for an insert and non-zero for an update.
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
            
            # Prepare data for execute_values
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


# --- Historical Data Ingestion Service ---

def _get_default_lookback_for_ingestion(interval: str) -> timedelta:
    """Returns the default lookback period for initial ingestion."""
    return {
        'minute': timedelta(days=30),
        '3minute': timedelta(days=60),
        '5minute': timedelta(days=120),
        '10minute': timedelta(days=120),
        '15minute': timedelta(days=180),
        '30minute': timedelta(days=200),
        '60minute': timedelta(days=365),
        'day': timedelta(days=365 * 5) # 5 years for daily
    }.get(interval, timedelta(days=30))


class IngestionManager:
    """Singleton to manage the historical data ingestion scheduler."""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(IngestionManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self.running: bool = False
        self.schedule_task: Optional[asyncio.Task] = None
        self.schedule_seconds: int = 900
        self.intervals: List[str] = ["minute", "5minute", "15minute", "60minute", "day"]
        self.owner_scope: str = "all"
        
        self.stats: Dict[str, Any] = {
            "last_run_utc": None,
            "last_success_utc": None,
            "last_error": None,
            "totals": {"fetched": 0, "inserted": 0, "updated": 0},
            "last_ingested": {}
        }
        self._initialized: bool = True

    def status(self) -> Dict[str, Any]:
        """Returns the operational status of the manager."""
        return {
            "running": self.running,
            "intervals": self.intervals,
            "owner_scope": self.owner_scope,
            "schedule_seconds": self.schedule_seconds,
            **self.stats
        }

    async def start(self, config: IngestionStartRequest, db: Session):
        """Starts the periodic ingestion scheduler."""
        if self.running:
            logger.warning("IngestionManager.start() called but it is already running.")
            return

        self.intervals = config.intervals or ["minute", "5minute", "15minute", "60minute", "day"]
        self.owner_scope = config.owner_scope
        self.schedule_seconds = config.schedule_seconds
        self.running = True

        self.schedule_task = asyncio.create_task(self._schedule_loop(db, config.from_override, config.to_override))
        logger.info(f"Ingestion scheduler started. Running every {self.schedule_seconds}s for intervals: {self.intervals}")

    async def _schedule_loop(self, db: Session, from_override: Optional[datetime], to_override: Optional[datetime]):
        """The main async loop for the scheduler."""
        while self.running:
            try:
                kite = get_kite_db(db)
                await self.run_once(kite, from_override=from_override, to_override=to_override)
                await asyncio.sleep(self.schedule_seconds)
            except asyncio.CancelledError:
                logger.info("Ingestion schedule loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in ingestion schedule loop: {e}", exc_info=True)
                self.stats['last_error'] = f"{datetime.now(timezone.utc).isoformat()}: {e}"
                # Avoid rapid failure loops
                await asyncio.sleep(self.schedule_seconds)


    async def stop(self):
        """Stops the ingestion scheduler."""
        if not self.running or not self.schedule_task:
            logger.warning("IngestionManager.stop() called but it is not running.")
            return

        self.running = False
        if self.schedule_task and not self.schedule_task.done():
            self.schedule_task.cancel()
        self.schedule_task = None
        logger.info("Ingestion scheduler stopped.")

    async def _get_targets(self, db: Session, tokens_override: Optional[List[int]] = None) -> Set[Tuple[int, str]]:
        """Builds the set of (instrument_token, interval) pairs to ingest."""
        if tokens_override:
            target_tokens = set(tokens_override)
        else:
            target_tokens = await _get_consolidated_watchlist_tokens(self.owner_scope, db)

        if not target_tokens:
            logger.warning("Consolidated watchlist is empty. Ingestion run will be a no-op.")
            return set()

        valid_intervals = set(self.intervals).intersection(ALLOWED_INTERVALS)
        return {(token, interval) for token in target_tokens for interval in valid_intervals}

    async def run_once(self, kite: KiteConnect, db: Session, from_override: Optional[datetime] = None, to_override: Optional[datetime] = None, tokens_override: Optional[List[int]] = None, intervals_override: Optional[List[str]] = None) -> Dict[str, Any]:
        """Runs a single ingestion cycle for all target pairs."""
        self.stats['last_run_utc'] = datetime.now(timezone.utc).isoformat()
        
        original_intervals = self.intervals
        if intervals_override:
            self.intervals = intervals_override

        try:
            target_pairs = await self._get_targets(db, tokens_override)
            if not target_pairs:
                self.stats['last_success_utc'] = datetime.now(timezone.utc).isoformat()
                return self.status()

            for token, interval in target_pairs:
                max_retries = 3
                retry_delay = 1.0
                for attempt in range(max_retries):
                    try:
                        # 1. Determine effective from/to
                        to_date = to_override or datetime.now(timezone.utc)
                        if to_date.tzinfo is None: to_date = to_date.replace(tzinfo=timezone.utc)

                        from_date = from_override
                        if not from_date:
                            latest_ts = get_latest_timestamp_from_db(token, interval)
                            if latest_ts:
                                from_date = latest_ts + timedelta(seconds=1)
                            else:
                                from_date = datetime.now(timezone.utc) - _get_default_lookback_for_ingestion(interval)
                        if from_date.tzinfo is None: from_date = from_date.replace(tzinfo=timezone.utc)

                        if from_date >= to_date:
                            logger.info(f"Data for {token}|{interval} is up to date. Skipping.")
                            break

                        # 2. Fetch data
                        records = await fetch_historical_data_chunked(kite, token, from_date, to_date, interval, False, False)
                        
                        if not records:
                            break

                        # 3. Transform and UPSERT
                        db_rows = [convert_kite_record_to_utc_tuple(r) for r in records]
                        inserted, updated = upsert_candles_batch(token, interval, db_rows)

                        # 4. Update stats
                        self.stats['totals']['fetched'] += len(records)
                        self.stats['totals']['inserted'] += inserted
                        self.stats['totals']['updated'] += updated
                        last_ts = records[-1]['date'].astimezone(timezone.utc).isoformat()
                        self.stats['last_ingested'][f"{token}|{interval}"] = last_ts
                        
                        logger.info(f"Ingested {token}|{interval}: Fetched={len(records)}, Inserted={inserted}, Updated={updated}, LastTS={last_ts}")
                        break # Success, exit retry loop

                    except Exception as e:
                        logger.error(f"Attempt {attempt+1}/{max_retries} failed for {token}|{interval}: {e}")
                        if attempt + 1 == max_retries:
                            self.stats['last_error'] = f"{datetime.now(timezone.utc).isoformat()}: Failed {token}|{interval} after {max_retries} attempts. Last error: {e}"
                            break # Move to next pair
                        
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 8) # Exponential backoff capped at 8s

            self.stats['last_success_utc'] = datetime.now(timezone.utc).isoformat()
        
        except Exception as e:
            logger.error(f"Critical error during ingestion run_once: {e}", exc_info=True)
            self.stats['last_error'] = f"{datetime.now(timezone.utc).isoformat()}: {e}"
        
        finally:
            if intervals_override:
                self.intervals = original_intervals # Restore original intervals
        
        return self.status()


ingestion_manager = IngestionManager()


# --- API Endpoints ---

@router.post("/ingestion/start", summary="Start the historical data ingestion scheduler")
async def start_ingestion(req: IngestionStartRequest, db: Session = Depends(get_db)):
    if ingestion_manager.running:
        return {"status": "ok", "already_running": True, "config": ingestion_manager.status()}
    
    await ingestion_manager.start(req, db)
    return {"status": "ok", "started": True, "config": ingestion_manager.status()}

@router.post("/ingestion/stop", summary="Stop the historical data ingestion scheduler")
async def stop_ingestion():
    if not ingestion_manager.running:
        return {"status": "ok", "already_stopped": True}
    
    await ingestion_manager.stop()
    return {"status": "ok", "stopped": True}

@router.get("/ingestion/status", summary="Get the status of the ingestion service")
async def get_ingestion_status():
    return ingestion_manager.status()

@router.post("/ingestion/run-now", summary="Trigger a one-time ingestion run")
async def run_ingestion_now(req: IngestionRunNowRequest, kite: KiteConnect = Depends(get_kite_db), db: Session = Depends(get_db)):
    # Temporarily set intervals for this run if provided
    intervals_override = req.intervals or ingestion_manager.intervals
    
    # Reset stats for this run to provide a clean summary
    ingestion_manager.stats['totals'] = {"fetched": 0, "inserted": 0, "updated": 0}
    ingestion_manager.stats['last_ingested'] = {}

    summary = await ingestion_manager.run_once(
        kite,
        db,
        from_override=req.from_override,
        to_override=req.to_override,
        tokens_override=req.tokens,
        intervals_override=intervals_override
    )
    return {"status": "ok", "execution_summary": summary}


@router.post("/historical/ingest", summary="Ingest historical data from API into DB")
async def ingest_historical_data(
    req: IngestRequest,
    kite: KiteConnect = Depends(get_kite_db)
):
    """
    Fetches historical data from the upstream API and upserts it into the local database.
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
    db_rows = [convert_kite_record_to_utc_tuple(r) for r in records]
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
    """
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Allowed values: {', '.join(ALLOWED_INTERVALS)}")

    # Default to_date to now if not provided
    if to_date is None:
        to_date = datetime.now(timezone.utc)

    # Default from_date based on a lookback from to_date if not provided
    if from_date is None:
        from_date = to_date - _get_default_lookback(interval)

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


@router.get("/instruments/historical/{instrument_token}/{interval}", summary="Get historical candle data for an instrument")
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
    Retrieves historical candle records for a given instrument, handling chunking for large date ranges.
    """
    # Validate interval
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail="Invalid interval specified.")

    # Convert boolean flags
    continuous_bool = bool(continuous)
    oi_bool = bool(oi)

    try:
        # Fetch data using the chunked helper
        records = await fetch_historical_data_chunked(
            kite,
            instrument_token,
            from_date,
            to_date,
            interval,
            continuous_bool,
            oi_bool
        )

        # Format the response as per the documentation
        candles = []
        for record in records:
            # Convert timestamp to the required format if it's a datetime object
            if isinstance(record['date'], datetime):
                # Ensure the timestamp is timezone-aware (assuming UTC from API) and convert to IST
                timestamp = record['date'].replace(tzinfo=pytz.utc).astimezone(pytz.timezone('Asia/Kolkata'))
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
        # Re-raise HTTP exceptions to be handled by FastAPI
        raise e
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching historical data for token {instrument_token}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")
# --- Unified On-Demand Candle Endpoint ---

# --- Timezone and Lookback Configuration ---
IST = pytz.timezone("Asia/Kolkata")

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

TIMEFRAME_LOOKBACK_DAYS = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}

TIMEFRAME_TIMEDELTA = {
    "minute": timedelta(minutes=1), "3minute": timedelta(minutes=3),
    "5minute": timedelta(minutes=5), "10minute": timedelta(minutes=10),
    "15minute": timedelta(minutes=15), "30minute": timedelta(minutes=30),
    "60minute": timedelta(minutes=60), "day": timedelta(days=1),
}

# --- Pydantic Models for the new endpoint ---
class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[float] = None

from pydantic import ConfigDict

class ResponseMeta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    instrument_token: int
    timeframe: str
    timezone: str
    from_: str = Field(..., alias="from")
    to: str

class IngestionMeta(BaseModel):
    status: Literal["triggered", "up_to_date", "disabled"]

class CandleResponse(BaseModel):
    status: str = "success"
    meta: ResponseMeta
    ingestion: IngestionMeta
    candles: List[Candle]


# --- Helper Functions for the new endpoint ---

def _normalize_timeframe(timeframe: str) -> str:
    """Normalizes a timeframe alias to its canonical name."""
    canonical = TIMEFRAME_ALIASES.get(timeframe.lower())
    if not canonical:
        raise HTTPException(status_code=400, detail=f"Invalid timeframe '{timeframe}'.")
    return canonical

async def _resolve_identifier(identifier: str, db: Session) -> int:
    """Resolves an identifier (token or EXCHANGE:SYMBOL) to an instrument token."""
    if identifier.isdigit():
        return int(identifier)
    
    if ":" not in identifier:
        raise HTTPException(status_code=400, detail="Invalid identifier format. Use integer token or 'EXCHANGE:SYMBOL'.")
        
    exchange, symbol = identifier.split(":", 1)
    repo = InstrumentsRepository(db)
    
    # This is a simplified lookup. A more robust version would query the DB.
    # Assuming InstrumentsRepository has a method like `find_by_symbol`.
    # For now, we'll query the DB directly.
    stmt = text("""
        SELECT instrument_token FROM public.kite_instruments
        WHERE exchange = :exchange AND tradingsymbol = :symbol
    """)
    result = db.execute(stmt, {"exchange": exchange.upper(), "symbol": symbol.upper()}).scalar_one_or_none()

    if result is None:
        raise HTTPException(status_code=404, detail=f"Instrument '{identifier}' not found.")
    return result

async def trigger_historical_ingestion(
    kite: KiteConnect,
    instrument_token: int,
    interval: str,
    from_date: datetime,
    to_date: datetime
):
    """Background task to fetch and store historical data."""
    logger.info(f"BG Ingestion: Starting for {instrument_token}|{interval} from {from_date} to {to_date}")
    try:
        records = await fetch_historical_data_chunked(
            kite, instrument_token, from_date, to_date, interval, False, False
        )
        if records:
            db_rows = [convert_kite_record_to_utc_tuple(r) for r in records]
            inserted, updated = upsert_candles_batch(instrument_token, interval, db_rows)
            logger.info(f"BG Ingestion: Finished for {instrument_token}|{interval}. Fetched={len(records)}, Inserted={inserted}, Updated={updated}")
        else:
            logger.info(f"BG Ingestion: No new records found for {instrument_token}|{interval}.")
    except Exception as e:
        logger.error(f"BG Ingestion Error for {instrument_token}|{interval}: {e}", exc_info=True)


@router.get("/candles/{identifier}", response_model=CandleResponse, summary="Fetch historical candles with on-demand ingestion")
async def get_candles(
    identifier: str,
    background_tasks: BackgroundTasks,
    timeframe: str = Query(..., description="e.g., 5m, 1d, 60minute"),
    from_date: Optional[datetime] = Query(None, alias="from", description="ISO 8601 format"),
    to_date: Optional[datetime] = Query(None, alias="to", description="ISO 8601 format, defaults to now"),
    ingest: bool = Query(True, description="Trigger background ingestion for missing ranges"),
    db: Session = Depends(get_db),
    kite: KiteConnect = Depends(get_kite_db)
):
    # 1. Resolve identifier and normalize timeframe
    instrument_token = await _resolve_identifier(identifier, db)
    interval = _normalize_timeframe(timeframe)

    # 2. Validate and normalize date range
    # Default 'to' to current time in IST if not provided, then convert to UTC for processing.
    if to_date:
        to_utc = to_date.astimezone(timezone.utc) if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
    else:
        to_utc = datetime.now(IST).astimezone(timezone.utc)

    max_lookback_days = TIMEFRAME_LOOKBACK_DAYS[interval]
    allowed_from_utc = to_utc - timedelta(days=max_lookback_days)

    from_utc = from_date
    if from_utc is None:
        from_utc = allowed_from_utc
    else:
        if from_utc.tzinfo is None: from_utc = from_utc.replace(tzinfo=timezone.utc)
        if from_utc < allowed_from_utc:
            logger.warning(f"Requested 'from' date {from_utc} is beyond lookback limit. Clamping to {allowed_from_utc}.")
            from_utc = allowed_from_utc

    if from_utc > to_utc:
        raise HTTPException(status_code=400, detail="'from' date cannot be after 'to' date.")

    # 3. Determine missing range and trigger ingestion
    ingestion_status: Literal["triggered", "up_to_date", "disabled"] = "disabled"
    if ingest:
        last_ts = get_latest_timestamp_from_db(instrument_token, interval)
        
        missing_from = from_utc
        if last_ts:
            # Start check from the candle *after* the last stored one
            missing_from = max(from_utc, last_ts + TIMEFRAME_TIMEDELTA[interval])

        if missing_from < to_utc:
            background_tasks.add_task(
                trigger_historical_ingestion, kite, instrument_token, interval, missing_from, to_utc
            )
            ingestion_status = "triggered"
        else:
            ingestion_status = "up_to_date"

    # 4. Query DB for the requested range and return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            query = """
                SELECT ts, open, high, low, close, volume, oi
                FROM public.historical_candles
                WHERE instrument_token = %s AND interval = %s AND ts BETWEEN %s AND %s
                ORDER BY ts ASC;
            """
            cur.execute(query, (instrument_token, interval, from_utc, to_utc))
            rows = cur.fetchall()

        candles_data = [
            Candle(
                time=int(row['ts'].timestamp()),
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=row['volume'],
                oi=float(row['oi']) if row['oi'] is not None else None
            ) for row in rows
        ]
        
        from_ist = from_utc.astimezone(IST)
        to_ist = to_utc.astimezone(IST)
        
        meta_obj = ResponseMeta.model_validate({
            "instrument_token": instrument_token,
            "timeframe": interval,
            "timezone": "Asia/Kolkata",
            "from": from_ist.isoformat(),
            "to": to_ist.isoformat()
        })
        return CandleResponse(
            meta=meta_obj,
            ingestion=IngestionMeta(status=ingestion_status),
            candles=candles_data
        )
    except Exception as e:
        logger.error(f"Candle query failed for {instrument_token}|{interval}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database query failed.")
    finally:
        if conn:
            conn.close()


# --- SSE Endpoint for Real-time Candles ---

def _get_default_lookback(interval: str) -> timedelta:
    """Returns a sensible default lookback timedelta based on the interval."""
    return {
        'minute': timedelta(days=2),
        '3minute': timedelta(days=5),
        '5minute': timedelta(days=10),
        '10minute': timedelta(days=15),
        '15minute': timedelta(days=20),
        '30minute': timedelta(days=30),
        '60minute': timedelta(days=60),
        'day': timedelta(days=365)
    }.get(interval, timedelta(days=7))

async def _fetch_db_candles(instrument_token: int, interval: str, from_utc: datetime, to_utc: datetime) -> List[list]:
    """Helper to fetch and format candles from the database."""
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
            # Format: [ISO8601 UTC string ending with 'Z', open, high, low, close, volume]
            candles.append([
                ts.isoformat().replace('+00:00', 'Z'),
                float(open_val), float(high_val), float(low_val), float(close_val),
                volume
            ])
        return candles
    except Exception as e:
        logger.error(f"SSE DB query error for {instrument_token} ({interval}): {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

async def _fetch_db_candles_since(instrument_token: int, interval: str, from_exclusive_ts: datetime, to_inclusive_ts: datetime) -> List[list]:
    """Helper to fetch candles from the DB strictly after a given timestamp."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            query = """
                SELECT ts, open, high, low, close, volume, oi
                FROM public.historical_candles
                WHERE instrument_token = %s AND interval = %s AND ts > %s AND ts <= %s
                ORDER BY ts ASC;
            """
            cur.execute(query, (instrument_token, interval, from_exclusive_ts, to_inclusive_ts))
            rows = cur.fetchall()

        candles = []
        for row in rows:
            ts, open_val, high_val, low_val, close_val, volume, oi = row
            candles.append([
                ts.isoformat().replace('+00:00', 'Z'),
                float(open_val), float(high_val), float(low_val), float(close_val),
                volume
            ])
        return candles
    except Exception as e:
        logger.error(f"SSE DB replay query error for {instrument_token} ({interval}): {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

def _parse_last_event_id(last_event_id_str: Optional[str]) -> Optional[datetime]:
    """Parses Last-Event-ID from string (ISO8601 or epoch) to a UTC datetime object."""
    if not last_event_id_str:
        return None
    try:
        # Try parsing as epoch seconds first
        if last_event_id_str.replace('.', '', 1).isdigit():
            return datetime.fromtimestamp(float(last_event_id_str), tz=timezone.utc)
        
        # Try parsing as ISO8601 UTC string
        ts = datetime.fromisoformat(last_event_id_str.replace('Z', '+00:00'))
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        logger.warning(f"Failed to parse Last-Event-ID '{last_event_id_str}': {e}")
        return None

@router.get("/sse/candles/{instrument_token}/{interval}")
async def sse_candles(
    request: Request,
    instrument_token: int,
    interval: str,
    from_date: Optional[datetime] = Query(None, alias="from"),
    to_date: Optional[datetime] = Query(None, alias="to"),
    last_event_id: Optional[str] = Query(None)
):
    """
    Provides a Server-Sent Events stream for historical and real-time candle data.
    Supports resume using 'Last-Event-ID' header or 'last_event_id' query parameter.
    """
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Allowed: {ALLOWED_INTERVALS}")

    # Prioritize header, fallback to query param
    last_event_id_header = request.headers.get("last-event-id")
    last_event_ts = _parse_last_event_id(last_event_id_header or last_event_id)

    async def event_generator():
        last_sent_ts: Optional[datetime] = None

        # --- Resume/Replay Mode ---
        if last_event_ts:
            logger.info(f"[SSE-{instrument_token}-{interval}] Resume request detected. Last-Event-ID: {last_event_ts.isoformat()}")
            now_utc = datetime.now(timezone.utc)
            
            # 1. Backfill missing candles from DB
            replayed_candles = await _fetch_db_candles_since(instrument_token, interval, last_event_ts, now_utc)
            logger.info(f"[SSE-{instrument_token}-{interval}] Replaying {len(replayed_candles)} candles from DB.")
            for candle in replayed_candles:
                ts_str = candle[0]
                ts_dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                
                candle_payload = {"instrument_token": instrument_token, "interval": interval, "candle": candle}
                yield f"id: {ts_str}\nevent: candle\ndata: {json.dumps(candle_payload)}\n\n"
                last_sent_ts = ts_dt
            
            # 2. Check Redis for a newer candle
            try:
                redis = get_redis()
                redis_key = f"candle:{instrument_token}:{interval}:latest"
                raw_redis_candle = await redis.get(redis_key)
                
                if raw_redis_candle:
                    candle_data = None
                    try:
                        candle_data = json.loads(raw_redis_candle)
                    except json.JSONDecodeError:
                        logger.warning(f"[SSE-{instrument_token}-{interval}] Failed to decode Redis candle JSON for replay: {raw_redis_candle.decode(errors='ignore')}")

                    if candle_data:
                        if not isinstance(candle_data, list) or len(candle_data) < 6:
                            logger.warning(f"[SSE-{instrument_token}-{interval}] Invalid candle structure from Redis for replay: {candle_data}")
                        else:
                            redis_ts = datetime.fromisoformat(candle_data[0].replace('Z', '+00:00'))
                            
                            if not last_sent_ts or redis_ts > last_sent_ts:
                                ts_str = candle_data[0]
                                candle_payload = {"instrument_token": instrument_token, "interval": interval, "candle": candle_data}
                                yield f"id: {ts_str}\nevent: candle\ndata: {json.dumps(candle_payload)}\n\n"
                                last_sent_ts = redis_ts
                                logger.info(f"[SSE-{instrument_token}-{interval}] Replayed latest candle from Redis.")

            except Exception as e:
                logger.warning(f"[SSE-{instrument_token}-{interval}] Failed to get/parse Redis latest for replay: {e}")

        # --- Snapshot Mode ---
        else:
            to_utc = to_date or datetime.now(timezone.utc)
            if to_utc.tzinfo is None: to_utc = to_utc.replace(tzinfo=timezone.utc)
            from_utc = from_date or (to_utc - _get_default_lookback(interval))
            if from_utc.tzinfo is None: from_utc = from_utc.replace(tzinfo=timezone.utc)

            logger.info(f"[SSE-{instrument_token}-{interval}] New stream opened. Snapshot: {from_utc.isoformat()} to {to_utc.isoformat()}")
            
            db_candles = await _fetch_db_candles(instrument_token, interval, from_utc, to_utc)
            
            latest_db_ts = datetime.fromisoformat(db_candles[-1][0].replace('Z', '+00:00')) if db_candles else None
            redis_candle = None
            try:
                redis = get_redis()
                redis_key = f"candle:{instrument_token}:{interval}:latest"
                raw_redis_candle = await redis.get(redis_key)
                if raw_redis_candle:
                    candle_data = json.loads(raw_redis_candle)
                    redis_ts = datetime.fromisoformat(candle_data[0].replace('Z', '+00:00'))
                    if not latest_db_ts or redis_ts > latest_db_ts:
                        redis_candle = candle_data
            except Exception as e:
                logger.warning(f"[SSE-{instrument_token}-{interval}] Failed to get Redis latest for snapshot: {e}")

            initial_candles = db_candles
            if redis_candle:
                initial_candles.append(redis_candle)

            snapshot_payload = {
                "instrument_token": instrument_token, "interval": interval,
                "from": from_utc.isoformat().replace('+00:00', 'Z'),
                "to": to_utc.isoformat().replace('+00:00', 'Z'),
                "candles": initial_candles
            }
            
            sse_id = ""
            if initial_candles:
                last_candle_ts_str = initial_candles[-1][0]
                last_sent_ts = datetime.fromisoformat(last_candle_ts_str.replace('Z', '+00:00'))
                sse_id = f"id: {last_candle_ts_str}\n"

            yield f"{sse_id}event: snapshot\ndata: {json.dumps(snapshot_payload)}\n\n"

        # --- Live Pub/Sub Loop ---
        pubsub_channel = f"realtime_candles:{instrument_token}:{interval}"
        logger.info(f"[SSE-{instrument_token}-{interval}] Handing off to live Pub/Sub channel: {pubsub_channel}")
        
        try:
            async for message in pubsub_iter(pubsub_channel):
                if await request.is_disconnected():
                    logger.info(f"[SSE-{instrument_token}-{interval}] Client disconnected.")
                    break

                if message.get("event") == "heartbeat":
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                if message.get("event") == "candle":
                    candle_data = message.get("candle")
                    if not (isinstance(candle_data, list) and len(candle_data) >= 6):
                        logger.warning(f"[SSE-{instrument_token}-{interval}] Malformed live candle data: {candle_data}")
                        continue
                    
                    ts_str = candle_data[0]
                    ts_dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))

                    if last_sent_ts and ts_dt <= last_sent_ts:
                        logger.info(f"[SSE-{instrument_token}-{interval}] Skipping duplicate/stale live candle (ts: {ts_str})")
                        continue
                    
                    yield f"id: {ts_str}\nevent: candle\ndata: {json.dumps(message)}\n\n"
                    last_sent_ts = ts_dt
                else:
                    logger.warning(f"[SSE-{instrument_token}-{interval}] Received unknown message structure: {message}")

        except asyncio.CancelledError:
            logger.info(f"[SSE-{instrument_token}-{interval}] Stream cancelled by client.")
        finally:
            logger.info(f"[SSE-{instrument_token}-{interval}] Closing stream.")

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)

# --- User Watchlist Persistence ---

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

@router.post("/aggregator/start", summary="Start the real-time candle aggregator")
async def start_aggregator(
    req: AggregatorStartRequest,
    background_tasks: BackgroundTasks,
    kite: KiteConnect = Depends(get_kite_db),
    db: Session = Depends(get_db)
):
    async with aggregator_manager._lock:
        if aggregator_manager.running:
            return {"status": "already_running", "config": aggregator_manager.config}
        
        logger.info(f"Starting aggregator with config: {req.model_dump_json()}")
        
        # Initialize manager state
        aggregator_manager.config = req
        aggregator_manager.redis = get_redis()
        
        # Start the main aggregation task in the background
        background_tasks.add_task(aggregator_manager.start, kite.access_token, db)

        aggregator_manager.running = True
        
        return {"status": "started", "config": req}


@router.post("/aggregator/stop", summary="Stop the real-time candle aggregator")
async def stop_aggregator():
    async with aggregator_manager._lock:
        if not aggregator_manager.running:
            return {"status": "already_stopped"}
        
        logger.info("Stopping aggregator...")
        await aggregator_manager.stop()
        
        return {"status": "stopped"}


@router.get("/aggregator/status", response_model=AggregatorStatusResponse, summary="Get the status of the candle aggregator")
async def get_aggregator_status():
    return aggregator_manager.get_status()

@router.post("/aggregator/warm-start", summary="Manually trigger aggregator warm-start")
async def manual_warm_start(req: AggregatorWarmStartRequest, db: Session = Depends(get_db)):
    """
    Manually triggers the warm-start process to reconstruct current candles from the database.
    """
    if not aggregator_manager.running:
        raise HTTPException(status_code=400, detail="Aggregator is not running.")

    summary = await aggregator_manager.warm_start_current_candles(
        db=db,
        owner_scope=req.owner_scope,
        intervals=req.intervals,
        tokens=req.tokens
    )
    return {"status": "ok", "summary": summary}