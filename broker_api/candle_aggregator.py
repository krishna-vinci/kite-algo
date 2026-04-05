"""
Real-time Candle Aggregator - Aggregates live ticks into candles with automatic DB persistence.
Lightweight, resilient, and handles edge cases properly.
"""

import logging
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass
from uuid import uuid4

import httpx
from kiteconnect import KiteTicker
import pytz
from redis.exceptions import ConnectionError as RedisConnectionError

from .candle_storage import CandleStorage, IST
from .redis_events import get_redis
from .market_runtime_client import get_market_runtime_client, market_runtime_enabled
from database import get_db
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Interval definitions in seconds
INTERVAL_SECONDS: Dict[str, int] = {
    'minute': 60,
    '3minute': 180,
    '5minute': 300,
    '10minute': 600,
    '15minute': 900,
    '30minute': 1800,
    '60minute': 3600,
    'day': 86400
}

SUPPORTED_INTERVALS = set(INTERVAL_SECONDS.keys())


@dataclass
class CandleState:
    """State of a forming candle."""
    bucket_start_ts: datetime  # In UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[float] = None
    base_volume: int = 0  # For volume delta calculation
    tick_count: int = 0
    

class CandleAggregator:
    """
    Real-time candle aggregator that:
    1. Subscribes to live ticks via KiteTicker
    2. Aggregates ticks into candles for multiple intervals
    3. Writes forming candles to Redis
    4. Publishes completed candles to Redis Pub/Sub
    5. Automatically persists completed candles to PostgreSQL
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.access_token: Optional[str] = None
        self.redis = None
        self.kws: Optional[KiteTicker] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None  # Store main event loop
        self.owner_id: Optional[str] = None
        self.source: str = "kite_websocket"
        
        # Configuration
        self.intervals: List[str] = []
        self.owner_scope: str = "all"
        self.refresh_seconds: int = 30
        
        # State
        self.running: bool = False
        self.subscribed_tokens: Set[int] = set()
        self.candle_states: Dict[Tuple[int, str], CandleState] = {}  # (token, interval) -> state
        
        # Background tasks
        self.tasks: Dict[str, asyncio.Task] = {}
        
        # Stats
        self.stats = {
            'candles_completed': 0,
            'candles_persisted': 0,
            'persist_errors': 0,
            'last_persist_error': None,
            'last_candle_time': None,
            'last_subscription_refresh': None,
            'last_runtime_tick': None,
        }
    
    async def start(
        self,
        access_token: str,
        intervals: List[str],
        owner_scope: str = "all",
        refresh_seconds: int = 30
    ):
        """Start the aggregator."""
        if self.running:
            logger.warning("Aggregator already running")
            return
        
        self.access_token = access_token
        self.intervals = [i for i in intervals if i in SUPPORTED_INTERVALS]
        self.owner_scope = owner_scope
        self.refresh_seconds = refresh_seconds
        
        if not self.intervals:
            raise ValueError("No valid intervals provided")
        
        # Store the current event loop
        self.loop = asyncio.get_running_loop()
        
        # Initialize Redis
        self.redis = get_redis()

        self.source = "market_runtime" if market_runtime_enabled() else "kite_websocket"
        self.owner_id = f"candles:{self.owner_scope}:{uuid4()}"

        if self.source == "kite_websocket":
            # Initialize KiteTicker
            self.kws = KiteTicker(self.api_key, access_token)
            self.kws.on_ticks = self._on_ticks
            self.kws.on_connect = self._on_connect
            self.kws.on_close = self._on_close
            self.kws.on_error = self._on_error

            # Connect WebSocket (threaded mode)
            self.kws.connect(threaded=True)
        else:
            self.kws = None

        self.running = True
        
        # Give WebSocket time to connect
        await asyncio.sleep(2)
        
        # Start background tasks
        self.tasks['watchlist_refresh'] = asyncio.create_task(self._watchlist_refresh_loop())
        self.tasks['persist_loop'] = asyncio.create_task(self._persist_loop())
        if self.source == "market_runtime":
            self.tasks['runtime_ticks'] = asyncio.create_task(self._market_runtime_tick_loop())
            self.tasks['runtime_lease'] = asyncio.create_task(self._market_runtime_lease_loop())
        
        logger.info(f"Aggregator started for intervals: {self.intervals}")
    
    async def stop(self):
        """Stop the aggregator gracefully."""
        if not self.running:
            return
        
        self.running = False
        
        # Cancel background tasks
        for name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.tasks.clear()
        
        # Disconnect WebSocket
        if self.kws and self.kws.is_connected():
            if self.subscribed_tokens:
                self.kws.unsubscribe(list(self.subscribed_tokens))
            self.kws.stop()
        elif self.source == "market_runtime" and self.owner_id:
            try:
                client = await get_market_runtime_client()
                await client.delete_owner(self.owner_id)
            except Exception:
                logger.warning("Failed to clean up candle aggregator runtime owner %s", self.owner_id, exc_info=True)
        
        # Final persistence of any remaining candles
        await self._persist_pending_candles()
        
        # Clear state
        self.candle_states.clear()
        self.subscribed_tokens.clear()
        
        logger.info("Aggregator stopped")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current aggregator status."""
        return {
            'running': self.running,
            'source': self.source,
            'owner_id': self.owner_id,
            'intervals': self.intervals,
            'subscribed_tokens': len(self.subscribed_tokens),
            'active_candles': len(self.candle_states),
            'stats': self.stats
        }
    
    # ===== WebSocket Callbacks =====
    
    def _on_ticks(self, ws, ticks: List[Dict]):
        """Handle incoming ticks."""
        try:
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._process_ticks(ticks), self.loop)
        except Exception as e:
            logger.error(f"Error scheduling tick processing: {e}", exc_info=True)
    
    def _on_connect(self, ws, response):
        """Handle WebSocket connection."""
        logger.info("Aggregator WebSocket connected")
        
        # Resubscribe to tokens on reconnect
        if self.subscribed_tokens:
            tokens = list(self.subscribed_tokens)
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info(f"Resubscribed to {len(tokens)} tokens on reconnect")
    
    def _on_close(self, ws, code, reason):
        """Handle WebSocket close."""
        logger.warning(f"Aggregator WebSocket closed: {code} - {reason}")
    
    def _on_error(self, ws, code, reason):
        """Handle WebSocket error."""
        logger.error(f"Aggregator WebSocket error: {code} - {reason}")
    
    # ===== Tick Processing =====
    
    async def _process_ticks(self, ticks: List[Dict]):
        """Process a batch of ticks."""
        for tick in ticks:
            try:
                token = tick.get("instrument_token")
                last_price = tick.get("last_price")
                
                if not token or not last_price:
                    continue

                if self.source == "market_runtime":
                    self.stats['last_runtime_tick'] = datetime.now(timezone.utc).isoformat()
                
                # Get timestamp (prefer exchange_timestamp)
                tick_ts = self._normalize_tick_timestamp(tick.get("exchange_timestamp"))
                
                # Process tick for each configured interval
                for interval in self.intervals:
                    await self._update_candle(token, tick, tick_ts, interval)
                    
            except Exception as e:
                logger.error(f"Error processing tick for token {tick.get('instrument_token')}: {e}", exc_info=True)

    def _normalize_tick_timestamp(self, raw_ts: Any) -> datetime:
        """Normalize tick timestamp values from websocket/runtime payloads into UTC datetimes."""
        if raw_ts is None:
            return datetime.now(timezone.utc)
        if isinstance(raw_ts, datetime):
            if raw_ts.tzinfo is None:
                return raw_ts.replace(tzinfo=timezone.utc)
            return raw_ts.astimezone(timezone.utc)
        if isinstance(raw_ts, str):
            try:
                parsed = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                logger.warning("Invalid tick timestamp string %r; falling back to current time", raw_ts)
                return datetime.now(timezone.utc)
        return datetime.now(timezone.utc)

    async def _market_runtime_tick_loop(self):
        """Consume normalized ticks from the market-runtime Redis channel."""
        pubsub = None
        retry_delay = 1.0
        try:
            while self.running:
                try:
                    if pubsub is None:
                        pubsub = self.redis.pubsub()
                        await pubsub.subscribe("market:ticks")
                        retry_delay = 1.0

                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue

                    payload = json.loads(message.get("data"))
                    token = payload.get("instrument_token")
                    if token is None:
                        continue
                    try:
                        token = int(token)
                    except (TypeError, ValueError):
                        continue
                    if token not in self.subscribed_tokens:
                        continue
                    await self._process_ticks([payload])
                except RedisConnectionError:
                    logger.warning("Candle aggregator lost Redis pubsub connection; retrying in %.1fs", retry_delay)
                    if pubsub is not None:
                        try:
                            await pubsub.aclose()
                        except Exception:
                            pass
                        pubsub = None
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 10.0)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Error in market runtime tick loop: %s", e, exc_info=True)
                    await asyncio.sleep(1)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe("market:ticks")
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def _market_runtime_lease_loop(self):
        """Refresh runtime subscription lease while running."""
        while self.running and self.owner_id:
            try:
                await asyncio.sleep(25)
                await self._sync_market_runtime_subscriptions(self.subscribed_tokens)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Candle aggregator runtime lease refresh failed: %s", e, exc_info=True)
    
    async def _update_candle(self, token: int, tick: Dict, tick_ts: datetime, interval: str):
        """Update candle state for a specific token and interval."""
        key = (token, interval)
        bucket_start = self._get_bucket_start(tick_ts, interval)
        current_state = self.candle_states.get(key)
        
        # Check if we need to finalize current candle and start a new one
        if current_state and current_state.bucket_start_ts != bucket_start:
            # Finalize the completed candle
            await self._finalize_candle(token, interval, current_state)
            current_state = None
        
        # Initialize new candle if needed
        if not current_state:
            self.candle_states[key] = CandleState(
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
            current_state.high = max(current_state.high, tick['last_price'])
            current_state.low = min(current_state.low, tick['last_price'])
            current_state.close = tick['last_price']
            
            # Handle volume delta (account for daily reset)
            current_volume = tick.get('volume_traded', 0)
            if current_volume < current_state.base_volume:
                # Volume reset detected (new trading day)
                current_state.base_volume = current_volume
            current_state.volume = max(0, current_volume - current_state.base_volume)
            
            if 'oi' in tick:
                current_state.oi = tick['oi']
            
            current_state.tick_count += 1
        
        # Write forming candle to Redis
        await self._write_forming_candle_to_redis(token, interval, self.candle_states[key])
    
    def _get_bucket_start(self, ts: datetime, interval: str) -> datetime:
        """Calculate the bucket start timestamp for an interval."""
        seconds = INTERVAL_SECONDS[interval]
        
        if interval == 'day':
            # Day candles start at 00:00 UTC
            return ts.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # For intraday intervals, align to interval boundaries
        epoch = int(ts.timestamp())
        bucket_start_epoch = (epoch // seconds) * seconds
        return datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)
    
    # ===== Redis Operations =====
    
    async def _write_forming_candle_to_redis(self, token: int, interval: str, state: CandleState):
        """Write the currently forming candle to Redis."""
        try:
            key = f"candle:{token}:{interval}:current"
            
            candle_data = [
                state.bucket_start_ts.isoformat().replace('+00:00', 'Z'),
                state.open,
                state.high,
                state.low,
                state.close,
                state.volume
            ]
            if state.oi is not None:
                candle_data.append(state.oi)
            
            # Set with TTL (2x interval duration)
            ttl = INTERVAL_SECONDS[interval] * 2
            await self.redis.set(key, json.dumps(candle_data), ex=ttl)
            
        except Exception as e:
            logger.warning(f"Failed to write forming candle to Redis for {token}|{interval}: {e}")
    
    async def _finalize_candle(self, token: int, interval: str, state: CandleState):
        """Finalize a completed candle: write to Redis, publish, and mark for persistence."""
        try:
            candle_data = [
                state.bucket_start_ts.isoformat().replace('+00:00', 'Z'),
                state.open,
                state.high,
                state.low,
                state.close,
                state.volume
            ]
            if state.oi is not None:
                candle_data.append(state.oi)
            
            # Store as latest completed candle
            latest_key = f"candle:{token}:{interval}:latest"
            await self.redis.set(latest_key, json.dumps(candle_data))
            
            # Delete the forming candle
            current_key = f"candle:{token}:{interval}:current"
            await self.redis.delete(current_key)
            
            # Publish to Pub/Sub
            channel = f"realtime_candles:{token}:{interval}"
            payload = {
                "event": "candle",
                "instrument_token": token,
                "interval": interval,
                "candle": candle_data
            }
            await self.redis.publish(channel, json.dumps(payload))
            
            self.stats['candles_completed'] += 1
            self.stats['last_candle_time'] = datetime.now(timezone.utc).isoformat()
            
            logger.info(f"Finalized candle for {token}|{interval} at {state.bucket_start_ts.isoformat()}")
            
        except Exception as e:
            logger.error(f"Failed to finalize candle for {token}|{interval}: {e}", exc_info=True)
    
    # ===== Database Persistence =====
    
    async def _persist_loop(self):
        """Background task to periodically persist completed candles to DB."""
        while self.running:
            try:
                await asyncio.sleep(60)  # Persist every minute
                await self._persist_pending_candles()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in persist loop: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    async def _persist_pending_candles(self):
        """
        Fetch completed candles from Redis and persist to DB.
        This is a resilience mechanism - even if real-time persistence fails,
        we can recover candles from Redis.
        """
        try:
            # For each subscribed token and interval, check for latest candle
            for token in list(self.subscribed_tokens):
                for interval in self.intervals:
                    try:
                        latest_key = f"candle:{token}:{interval}:latest"
                        raw_data = await self.redis.get(latest_key)
                        
                        if not raw_data:
                            continue
                        
                        candle_data = json.loads(raw_data)
                        
                        # Handle both list and dict formats (for backward compatibility)
                        if isinstance(candle_data, dict):
                            # Old dict format: {'type': 'realtime', 'ts': '...', 'o': 1, 'h': 2, ...}
                            if 'ts' in candle_data and 'o' in candle_data:
                                ts_str = candle_data.get('ts')
                                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                                candle_dict = {
                                    'ts': ts,
                                    'open': candle_data.get('o'),
                                    'high': candle_data.get('h'),
                                    'low': candle_data.get('l'),
                                    'close': candle_data.get('c'),
                                    'volume': candle_data.get('v', 0),
                                    'oi': candle_data.get('oi')
                                }
                            else:
                                logger.warning(f"Invalid dict format for {token}|{interval}: {candle_data}")
                                # Clean up invalid data
                                await self.redis.delete(latest_key)
                                continue
                                
                        elif isinstance(candle_data, list) and len(candle_data) >= 6:
                            # New list format: [timestamp, open, high, low, close, volume, oi?]
                            ts_str = candle_data[0]
                            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                            candle_dict = {
                                'ts': ts,
                                'open': candle_data[1],
                                'high': candle_data[2],
                                'low': candle_data[3],
                                'close': candle_data[4],
                                'volume': candle_data[5],
                                'oi': candle_data[6] if len(candle_data) > 6 else None
                            }
                        else:
                            logger.warning(f"Invalid candle data format for {token}|{interval}: {candle_data}")
                            # Clean up invalid data
                            await self.redis.delete(latest_key)
                            continue
                        
                        # Check if this candle is already in DB
                        latest_db_ts = CandleStorage.get_latest_timestamp(token, interval)
                        
                        if latest_db_ts and candle_dict['ts'] <= latest_db_ts:
                            # Already persisted
                            continue
                        
                        CandleStorage.upsert_candles(token, interval, [candle_dict])
                        self.stats['candles_persisted'] += 1
                        
                    except Exception as e:
                        logger.error(f"Failed to persist candle for {token}|{interval}: {e}", exc_info=True)
                        self.stats['persist_errors'] += 1
                        self.stats['last_persist_error'] = str(e)
                        
        except Exception as e:
            logger.error(f"Error in persist_pending_candles: {e}", exc_info=True)
    
    # ===== Watchlist Management =====
    
    async def _watchlist_refresh_loop(self):
        """Periodically refresh subscriptions based on watchlist."""
        while self.running:
            try:
                await self._refresh_subscriptions()
                await asyncio.sleep(self.refresh_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in watchlist refresh loop: {e}", exc_info=True)
                await asyncio.sleep(self.refresh_seconds)
    
    async def _refresh_subscriptions(self):
        """Refresh WebSocket subscriptions based on current watchlist."""
        try:
            # Get desired tokens from watchlist
            desired_tokens = await self._get_watchlist_tokens()

            if self.source == "market_runtime":
                await self._sync_market_runtime_subscriptions(desired_tokens)
                self.subscribed_tokens = desired_tokens
                self.stats['last_subscription_refresh'] = datetime.now(timezone.utc).isoformat()
                return

            if not self.kws or not self.kws.is_connected():
                logger.warning("WebSocket not connected, skipping subscription refresh")
                return

            to_subscribe = list(desired_tokens - self.subscribed_tokens)
            to_unsubscribe = list(self.subscribed_tokens - desired_tokens)

            if to_subscribe:
                self.kws.subscribe(to_subscribe)
                self.kws.set_mode(self.kws.MODE_FULL, to_subscribe)
                logger.info(f"Subscribed to {len(to_subscribe)} new tokens")

            if to_unsubscribe:
                self.kws.unsubscribe(to_unsubscribe)
                logger.info(f"Unsubscribed from {len(to_unsubscribe)} tokens")

            self.subscribed_tokens = desired_tokens
            self.stats['last_subscription_refresh'] = datetime.now(timezone.utc).isoformat()
            
        except Exception as e:
            logger.error(f"Error refreshing subscriptions: {e}", exc_info=True)

    async def _sync_market_runtime_subscriptions(self, desired_tokens: Set[int]):
        """Sync current desired tokens to the market-runtime using full mode."""
        if not self.owner_id:
            return
        client = await get_market_runtime_client()
        payload = {int(token): "full" for token in desired_tokens}
        try:
            await client.set_owner_subscriptions(self.owner_id, payload)
            logger.info("Runtime candle subscriptions synced: %s tokens", len(desired_tokens))
        except httpx.HTTPError as e:
            logger.error("Failed to sync candle subscriptions to market-runtime: %s", e, exc_info=True)
            raise
    
    async def _get_watchlist_tokens(self) -> Set[int]:
        """Fetch instrument tokens from user watchlists."""
        try:
            db_session = next(get_db())
            try:
                if self.owner_scope == "all":
                    stmt = text("SELECT DISTINCT instrument_token FROM public.user_watchlists")
                    results = db_session.execute(stmt).fetchall()
                else:
                    stmt = text("SELECT instrument_token FROM public.user_watchlists WHERE owner_id = :owner_id")
                    results = db_session.execute(stmt, {"owner_id": self.owner_scope}).fetchall()
                
                return {row[0] for row in results}
            finally:
                db_session.close()
        except Exception as e:
            logger.error(f"Failed to fetch watchlist tokens: {e}", exc_info=True)
            return set()


# Global singleton instance
_aggregator_instance: Optional[CandleAggregator] = None


def get_aggregator(api_key: str) -> CandleAggregator:
    """Get or create the global aggregator instance."""
    global _aggregator_instance
    if _aggregator_instance is None:
        _aggregator_instance = CandleAggregator(api_key)
    return _aggregator_instance
