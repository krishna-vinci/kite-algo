"""
Historical Candle Ingestion - Fetches historical data from Kite API and stores in DB.
Handles chunking, rate limiting, and timezone conversion properly.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import pytz
from kiteconnect import KiteConnect

from .candle_storage import CandleStorage, IST

logger = logging.getLogger(__name__)

# Maximum days per API call for each interval (Kite API limits)
MAX_DAYS_PER_INTERVAL = {
    'minute': 60,
    '3minute': 100,
    '5minute': 100,
    '10minute': 100,
    '15minute': 200,
    '30minute': 200,
    '60minute': 400,
    'day': 2000
}

# Default lookback periods for initial ingestion
DEFAULT_LOOKBACK = {
    'minute': timedelta(days=30),
    '3minute': timedelta(days=60),
    '5minute': timedelta(days=120),
    '10minute': timedelta(days=120),
    '15minute': timedelta(days=180),
    '30minute': timedelta(days=200),
    '60minute': timedelta(days=365),
    'day': timedelta(days=365 * 5)
}


class CandleIngestion:
    """Handles fetching and storing historical candle data."""
    
    def __init__(self, kite: KiteConnect):
        self.kite = kite
    
    async def ingest_historical_data(
        self,
        instrument_token: int,
        interval: str,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Ingest historical data for an instrument and interval.
        
        Args:
            instrument_token: The instrument token
            interval: Time interval (minute, 5minute, etc.)
            from_date: Start date (UTC), defaults to smart calculation
            to_date: End date (UTC), defaults to now
            force_refresh: If True, fetch from from_date; if False, only fetch missing data
        
        Returns:
            Dict with status and statistics
        """
        try:
            # Determine effective date range
            to_utc = to_date or datetime.now(timezone.utc)
            if to_utc.tzinfo is None:
                to_utc = to_utc.replace(tzinfo=timezone.utc)
            
            # Determine from_date
            if force_refresh and from_date:
                from_utc = from_date
                if from_utc.tzinfo is None:
                    from_utc = from_utc.replace(tzinfo=timezone.utc)
            else:
                # Smart from_date: check last timestamp in DB
                latest_ts = CandleStorage.get_latest_timestamp(instrument_token, interval)
                
                if latest_ts:
                    # Fetch from one interval after the last stored candle
                    from_utc = latest_ts.astimezone(timezone.utc) + self._get_interval_timedelta(interval)
                else:
                    # No data exists, use default lookback
                    from_utc = to_utc - DEFAULT_LOOKBACK.get(interval, timedelta(days=30))
            
            # Validate date range
            if from_utc >= to_utc:
                return {
                    'status': 'up_to_date',
                    'message': 'Data is already up to date',
                    'from': from_utc.isoformat(),
                    'to': to_utc.isoformat(),
                    'fetched': 0,
                    'inserted': 0,
                    'updated': 0
                }
            
            logger.info(f"Ingesting {instrument_token}|{interval} from {from_utc} to {to_utc}")
            
            # Fetch data in chunks
            records = await self._fetch_historical_chunked(
                instrument_token,
                from_utc,
                to_utc,
                interval
            )
            
            if not records:
                return {
                    'status': 'no_data',
                    'message': 'No data returned from API',
                    'from': from_utc.isoformat(),
                    'to': to_utc.isoformat(),
                    'fetched': 0,
                    'inserted': 0,
                    'updated': 0
                }
            
            # Convert and store
            candles = self._convert_records_to_candles(records)
            inserted, updated = CandleStorage.upsert_candles(instrument_token, interval, candles)
            
            return {
                'status': 'success',
                'message': f'Ingested {len(records)} candles',
                'from': from_utc.isoformat(),
                'to': to_utc.isoformat(),
                'fetched': len(records),
                'inserted': inserted,
                'updated': updated
            }
            
        except Exception as e:
            logger.error(f"Ingestion failed for {instrument_token}|{interval}: {e}", exc_info=True)
            return {
                'status': 'error',
                'message': str(e),
                'error': str(e)
            }
    
    async def fetch_raw_records(
        self,
        instrument_token: int,
        interval: str,
        from_date: datetime,
        to_date: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch historical records without persisting them."""
        if from_date is None or to_date is None:
            raise ValueError("from_date and to_date are required for raw fetch")

        to_utc = to_date if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
        from_utc = from_date if from_date.tzinfo else from_date.replace(tzinfo=timezone.utc)

        if from_utc >= to_utc:
            return []

        return await self._fetch_historical_chunked(
            instrument_token,
            from_utc,
            to_utc,
            interval
        )
    
    async def _fetch_historical_chunked(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical data in chunks to respect API limits.
        Fetches most recent data first to prioritize latest candles.
        
        Args:
            instrument_token: The instrument token
            from_date: Start date in UTC
            to_date: End date in UTC
            interval: Time interval
        
        Returns:
            List of candle records from Kite API
        """
        all_records = []
        max_days = MAX_DAYS_PER_INTERVAL.get(interval, 60)
        
        # Convert UTC to IST for Kite API (expects IST)
        current_to = to_date.astimezone(IST)
        target_from = from_date.astimezone(IST)
        
        # Fetch in reverse chronological order (most recent first)
        while current_to > target_from:
            current_from = max(current_to - timedelta(days=max_days), target_from)
            
            try:
                logger.debug(f"Fetching chunk: {instrument_token}|{interval} from {current_from} to {current_to}")
                
                records = await asyncio.to_thread(
                    self.kite.historical_data,
                    instrument_token,
                    current_from,
                    current_to,
                    interval,
                    continuous=False,
                    oi=True
                )
                
                if records:
                    all_records.extend(records)
                    logger.debug(f"Fetched {len(records)} records in chunk")
                
                # Move to next chunk
                current_to = current_from - timedelta(seconds=1)
                
                # Rate limiting: Kite allows ~3 requests/second
                await asyncio.sleep(0.35)
                
            except Exception as e:
                logger.error(
                    f"Error fetching chunk for {instrument_token}|{interval} "
                    f"from {current_from} to {current_to}: {e}",
                    exc_info=True
                )
                # Continue with next chunk despite error
                current_to = current_from - timedelta(seconds=1)
        
        # Sort by date (ascending) since we fetched in reverse
        all_records.sort(key=lambda x: x['date'])
        
        logger.info(f"Fetched {len(all_records)} total records for {instrument_token}|{interval}")
        return all_records
    
    def _convert_records_to_candles(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert Kite API records to candle dicts for storage.
        Handles timezone conversion properly.
        
        Args:
            records: Raw records from Kite API
        
        Returns:
            List of candle dicts ready for DB insertion
        """
        candles = []
        for record in records:
            ts = record['date']
            
            # Kite API returns naive datetimes in IST
            if ts.tzinfo is None:
                ts_ist = IST.localize(ts)
            else:
                ts_ist = ts.astimezone(IST)
            
            candle = {
                'ts': ts_ist,
                'open': float(record['open']),
                'high': float(record['high']),
                'low': float(record['low']),
                'close': float(record['close']),
                'volume': int(record.get('volume', 0)),
                'oi': float(record.get('oi')) if record.get('oi') is not None else None
            }
            candles.append(candle)
        
        return candles
    
    def _get_interval_timedelta(self, interval: str) -> timedelta:
        """Get timedelta for an interval string."""
        interval_map = {
            'minute': timedelta(minutes=1),
            '3minute': timedelta(minutes=3),
            '5minute': timedelta(minutes=5),
            '10minute': timedelta(minutes=10),
            '15minute': timedelta(minutes=15),
            '30minute': timedelta(minutes=30),
            '60minute': timedelta(minutes=60),
            'day': timedelta(days=1)
        }
        return interval_map.get(interval, timedelta(minutes=1))


class IngestionScheduler:
    """
    Manages scheduled background ingestion for multiple instruments.
    Runs periodically to keep data up-to-date.
    """
    
    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self.ingestion = CandleIngestion(kite)
        self.running = False
        self.task: Optional[asyncio.Task] = None
        
        # Configuration
        self.intervals: List[str] = []
        self.owner_scope: str = "all"
        self.schedule_seconds: int = 900  # 15 minutes
        
        # Stats
        self.stats = {
            'last_run': None,
            'last_success': None,
            'total_ingested': 0,
            'total_errors': 0,
            'last_error': None
        }
    
    async def start(
        self,
        intervals: List[str],
        owner_scope: str = "all",
        schedule_seconds: int = 900
    ):
        """Start the ingestion scheduler."""
        if self.running:
            logger.warning("Ingestion scheduler already running")
            return
        
        self.intervals = intervals
        self.owner_scope = owner_scope
        self.schedule_seconds = schedule_seconds
        self.running = True
        
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info(f"Ingestion scheduler started: intervals={intervals}, schedule={schedule_seconds}s")
    
    async def stop(self):
        """Stop the ingestion scheduler."""
        if not self.running:
            return
        
        self.running = False
        
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        
        logger.info("Ingestion scheduler stopped")
    
    async def _schedule_loop(self):
        """Main scheduling loop."""
        while self.running:
            try:
                await self._run_ingestion_cycle()
                await asyncio.sleep(self.schedule_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in ingestion schedule loop: {e}", exc_info=True)
                self.stats['total_errors'] += 1
                self.stats['last_error'] = str(e)
                await asyncio.sleep(self.schedule_seconds)
    
    async def _run_ingestion_cycle(self):
        """Run one ingestion cycle for all configured instruments."""
        self.stats['last_run'] = datetime.now(timezone.utc).isoformat()
        
        try:
            # Get target tokens from watchlist
            tokens = await self._get_watchlist_tokens()
            
            if not tokens:
                logger.info("No instruments in watchlist, skipping ingestion cycle")
                return
            
            logger.info(f"Starting ingestion cycle for {len(tokens)} tokens and {len(self.intervals)} intervals")
            
            # Ingest each token/interval combination
            for token in tokens:
                for interval in self.intervals:
                    try:
                        result = await self.ingestion.ingest_historical_data(
                            token,
                            interval,
                            force_refresh=False
                        )
                        
                        if result['status'] == 'success':
                            self.stats['total_ingested'] += result['fetched']
                        
                    except Exception as e:
                        logger.error(f"Failed to ingest {token}|{interval}: {e}", exc_info=True)
                        self.stats['total_errors'] += 1
                        self.stats['last_error'] = str(e)
            
            self.stats['last_success'] = datetime.now(timezone.utc).isoformat()
            logger.info("Ingestion cycle completed")
            
        except Exception as e:
            logger.error(f"Error in ingestion cycle: {e}", exc_info=True)
            self.stats['total_errors'] += 1
            self.stats['last_error'] = str(e)
    
    async def _get_watchlist_tokens(self) -> List[int]:
        """Get instrument tokens from watchlist."""
        try:
            from database import get_db
            from sqlalchemy import text
            
            db_session = next(get_db())
            try:
                if self.owner_scope == "all":
                    stmt = text("SELECT DISTINCT instrument_token FROM public.user_watchlists")
                    results = db_session.execute(stmt).fetchall()
                else:
                    stmt = text("SELECT instrument_token FROM public.user_watchlists WHERE owner_id = :owner_id")
                    results = db_session.execute(stmt, {"owner_id": self.owner_scope}).fetchall()
                
                return [row[0] for row in results]
            finally:
                db_session.close()
        except Exception as e:
            logger.error(f"Failed to fetch watchlist tokens: {e}", exc_info=True)
            return []
    
    def get_status(self) -> Dict[str, Any]:
        """Get scheduler status."""
        return {
            'running': self.running,
            'intervals': self.intervals,
            'owner_scope': self.owner_scope,
            'schedule_seconds': self.schedule_seconds,
            'stats': self.stats
        }
