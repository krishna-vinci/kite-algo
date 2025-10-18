"""
Candle Storage Module - Handles all database operations for historical candles.
Clean, lightweight, and focused on storage operations only.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
import psycopg2
from psycopg2.extras import execute_values, DictCursor
import pytz

from database import get_db_connection

logger = logging.getLogger(__name__)

# Timezone constants
IST = pytz.timezone("Asia/Kolkata")
UTC = timezone.utc


class CandleStorage:
    """Handles all database operations for candles with proper timezone handling."""
    
    @staticmethod
    def upsert_candles(
        instrument_token: int,
        interval: str,
        candles: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """
        Upsert a batch of candles into the database.
        
        Args:
            instrument_token: The instrument token
            interval: Time interval (minute, 5minute, etc.)
            candles: List of candle dicts with keys: ts, open, high, low, close, volume, oi
        
        Returns:
            (inserted_count, updated_count)
        """
        if not candles:
            return 0, 0
        
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                # Prepare data tuples ensuring timezone-aware timestamps
                data_tuples = []
                for candle in candles:
                    ts = candle['ts']
                    # Ensure timestamp is timezone-aware (IST)
                    if ts.tzinfo is None:
                        ts = IST.localize(ts)
                    elif ts.tzinfo != IST:
                        ts = ts.astimezone(IST)
                    
                    data_tuples.append((
                        ts,
                        float(candle['open']),
                        float(candle['high']),
                        float(candle['low']),
                        float(candle['close']),
                        int(candle.get('volume', 0)),
                        int(candle.get('oi', 0)) if candle.get('oi') is not None else None
                    ))
                
                query = """
                    INSERT INTO public.historical_candles 
                    (instrument_token, interval, ts, open, high, low, close, volume, oi)
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
                results = execute_values(cur, query, data_tuples, template=template, fetch=True)
                conn.commit()
                
                inserted_count = sum(1 for r in results if r[0])
                updated_count = len(results) - inserted_count
                
                logger.info(
                    f"Upserted {len(candles)} candles for {instrument_token}|{interval}: "
                    f"inserted={inserted_count}, updated={updated_count}"
                )
                
                return inserted_count, updated_count
                
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Failed to upsert candles for {instrument_token}|{interval}: {e}", exc_info=True)
            raise
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_latest_timestamp(instrument_token: int, interval: str) -> Optional[datetime]:
        """
        Get the latest candle timestamp for an instrument and interval.
        
        Returns:
            datetime in IST timezone or None if no data exists
        """
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
                    return result[0]  # Should already be timezone-aware from DB
        except Exception as e:
            logger.error(f"Failed to get latest timestamp for {instrument_token}|{interval}: {e}")
        finally:
            if conn:
                conn.close()
        return None
    
    @staticmethod
    def query_candles(
        instrument_token: int,
        interval: str,
        from_ts: datetime,
        to_ts: datetime,
        include_oi: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Query candles from database for a given time range.
        
        Args:
            instrument_token: The instrument token
            interval: Time interval
            from_ts: Start timestamp (timezone-aware)
            to_ts: End timestamp (timezone-aware)
            include_oi: Whether to include OI in results
        
        Returns:
            List of candle dicts with keys: ts, open, high, low, close, volume, oi (optional)
        """
        conn = None
        try:
            # Ensure timestamps are in IST for DB query
            from_ist = from_ts.astimezone(IST) if from_ts.tzinfo else IST.localize(from_ts)
            to_ist = to_ts.astimezone(IST) if to_ts.tzinfo else IST.localize(to_ts)
            
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                query = """
                    SELECT ts, open, high, low, close, volume, oi
                    FROM public.historical_candles
                    WHERE instrument_token = %s AND interval = %s AND ts BETWEEN %s AND %s
                    ORDER BY ts ASC;
                """
                cur.execute(query, (instrument_token, interval, from_ist, to_ist))
                rows = cur.fetchall()
                
                candles = []
                for row in rows:
                    candle = {
                        'ts': row['ts'],
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': int(row['volume'])
                    }
                    if include_oi and row['oi'] is not None:
                        candle['oi'] = float(row['oi'])
                    candles.append(candle)
                
                logger.debug(f"Queried {len(candles)} candles for {instrument_token}|{interval}")
                return candles
                
        except Exception as e:
            logger.error(f"Failed to query candles for {instrument_token}|{interval}: {e}", exc_info=True)
            return []
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_minute_candles_for_aggregation(
        instrument_token: int,
        from_ts: datetime,
        to_ts: datetime
    ) -> List[Dict[str, Any]]:
        """
        Fetch minute candles for aggregating into higher timeframes.
        Used by warm-start and aggregation processes.
        
        Args:
            instrument_token: The instrument token
            from_ts: Start timestamp (inclusive)
            to_ts: End timestamp (exclusive)
        
        Returns:
            List of minute candle dicts
        """
        conn = None
        try:
            # Convert to IST for DB query
            from_ist = from_ts.astimezone(IST) if from_ts.tzinfo else IST.localize(from_ts)
            to_ist = to_ts.astimezone(IST) if to_ts.tzinfo else IST.localize(to_ts)
            
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                query = """
                    SELECT ts, open, high, low, close, volume, oi
                    FROM public.historical_candles
                    WHERE instrument_token = %s AND interval = 'minute' 
                    AND ts >= %s AND ts < %s
                    ORDER BY ts ASC;
                """
                cur.execute(query, (instrument_token, from_ist, to_ist))
                rows = cur.fetchall()
                
                return [dict(row) for row in rows]
                
        except Exception as e:
            logger.error(
                f"Failed to fetch minute candles for aggregation "
                f"{instrument_token} [{from_ts} to {to_ts}]: {e}",
                exc_info=True
            )
            return []
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def clear_instrument_cache(instrument_token: int) -> int:
        """
        Delete all cached candle data for an instrument.
        
        Args:
            instrument_token: The instrument token to clear
        
        Returns:
            Number of rows deleted
        """
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.historical_candles WHERE instrument_token = %s",
                    (instrument_token,)
                )
                deleted_count = cur.rowcount
                conn.commit()
                logger.info(f"Cleared cache for instrument {instrument_token}: {deleted_count} rows deleted")
                return deleted_count
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Failed to clear cache for {instrument_token}: {e}", exc_info=True)
            raise
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_data_coverage(
        instrument_token: int,
        interval: str
    ) -> Dict[str, Any]:
        """
        Get coverage statistics for an instrument's candle data.
        
        Returns:
            Dict with keys: earliest_ts, latest_ts, count, gaps (if any)
        """
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                query = """
                    SELECT 
                        MIN(ts) as earliest_ts,
                        MAX(ts) as latest_ts,
                        COUNT(*) as count
                    FROM public.historical_candles
                    WHERE instrument_token = %s AND interval = %s;
                """
                cur.execute(query, (instrument_token, interval))
                row = cur.fetchone()
                
                if row and row['count'] > 0:
                    return {
                        'earliest_ts': row['earliest_ts'],
                        'latest_ts': row['latest_ts'],
                        'count': row['count'],
                        'has_data': True
                    }
                else:
                    return {
                        'has_data': False,
                        'count': 0
                    }
        except Exception as e:
            logger.error(f"Failed to get data coverage for {instrument_token}|{interval}: {e}")
            return {'has_data': False, 'count': 0, 'error': str(e)}
        finally:
            if conn:
                conn.close()
