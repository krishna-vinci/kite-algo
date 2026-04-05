import logging
import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional, Tuple, Set
from kiteconnect import KiteTicker
from fastapi import WebSocket
from asyncio import AbstractEventLoop
from datetime import datetime, timezone
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from broker_api.redis_events import get_redis, publish_event
import uuid
from database import SessionLocal
from sqlalchemy import text
from broker_api.order_runtime import order_event_runtime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Mode precedence: full > quote > ltp
MODE_ORDER = {"ltp": 0, "quote": 1, "full": 2}
VALID_MODES = set(MODE_ORDER.keys())

def normalize_mode(mode: Optional[str]) -> str:
    if not mode:
        return "quote"
    m = mode.lower().strip()
    if m not in VALID_MODES:
        return "quote"
    return m

def higher_mode(a: str, b: str) -> str:
    return a if MODE_ORDER[a] >= MODE_ORDER[b] else b


class ClientConnection:
    """
    Holds per-client subscription state.
    subscriptions: Dict[instrument_token, mode]
    """
    def __init__(self, websocket: WebSocket):
        self.websocket: WebSocket = websocket
        self.subscriptions: Dict[int, str] = {}

    def desired_mode(self, token: int) -> Optional[str]:
        return self.subscriptions.get(token)


class WebSocketManager:
    """
    Manages a single KiteTicker and multiple frontend client WebSocket connections
    with per-client subscriptions, aggregated token modes, and targeted tick delivery.
    """

    def __init__(self, api_key: str, access_token: str, main_event_loop: AbstractEventLoop):
        # Store credentials
        self.api_key = api_key
        self.access_token = access_token

        # Kite Ticker
        self.kws = KiteTicker(self.api_key, self.access_token)

        # Per-client connection map
        self.clients: Dict[WebSocket, ClientConnection] = {}

        # Aggregation maps
        self.token_refcount: Dict[int, int] = {}         # number of clients per token
        self.token_mode_agg: Dict[int, str] = {}         # highest mode requested per token

        # Ticks and status
        self.latest_ticks: Dict[int, Dict[str, Any]] = {}   # last tick per token
        self.websocket_status: str = "DISCONNECTED"
        self.last_order_update_at: Optional[datetime] = None

        # Event loop and batching
        self.main_event_loop = main_event_loop
        self.flush_interval_ms: int = int(os.getenv("KITE_TICK_FLUSH_MS", "100"))
        self._pending_ticks: Dict[int, Dict[str, Any]] = {}
        self._flush_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Alert events queue for text-mode alert messages
        self.alert_event_queue: asyncio.Queue = asyncio.Queue()
        # Assign Kite callbacks
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.on_close = self.on_close
        self.kws.on_error = self.on_error
        self.kws.on_noreconnect = self.on_noreconnect
        self.kws.on_reconnect = self.on_reconnect
        # Raw text/binary messages (alerts and order updates)
        try:
            self.kws.on_message = self.on_message
        except Exception:
            # Fallback for kiteconnect versions without on_message
            pass
        # Dedicated order updates callback (captures all orders for this login)
        try:
            self.kws.on_order_update = self.on_order_update
        except Exception:
            pass

        # Feature flag to allow toggling from API
        self.order_updates_enabled: bool = True
        self._desired_tokens_union: Set[int] = set()
        self._last_converge_ts: float = 0.0

    def get_websocket_status(self) -> str:
        """Returns current KiteTicker/WebSocketManager connection status."""
        return self.websocket_status
    # ---------------------------
    # Lifecycle
    # ---------------------------
    def start(self):
        """Starts the KiteTicker connection in a separate thread and the flush loop on the main loop."""
        logger.info("Starting KiteTicker connection...")
        self.websocket_status = "CONNECTING"
        self.kws.connect(threaded=True)

        # Start flush loop on main loop
        if not self._flush_task:
            self._running = True
            def start_task():
                self._flush_task = asyncio.create_task(self._flush_loop())
            self.main_event_loop.call_soon_threadsafe(start_task)

    def stop(self):
        """Stops the KiteTicker connection and background tasks."""
        logger.info("Stopping KiteTicker connection...")
        self.websocket_status = "DISCONNECTED"
        try:
            self.kws.stop()
        except Exception:
            pass

        # Stop flush loop
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    def reinit_with_token(self, new_token: str) -> None:
        """
        Rotate the access token at runtime without tearing down the flush loop.
        - No-op if token is unchanged.
        - Stops current KiteTicker, rebuilds with new token, reassigns callbacks, and connects again.
        Resubscriptions happen in on_connect().
        """
        try:
            if not isinstance(new_token, str) or not new_token:
                return
            # If equal to current, no-op
            if hasattr(self, "access_token") and new_token == self.access_token:
                return

            old_fp = (self.access_token[-6:] if isinstance(getattr(self, "access_token", None), str) else "")
            new_fp = (new_token[-6:] if isinstance(new_token, str) else "")
            logger.info("Reinitializing KiteTicker with new token (..%s -> ..%s)", old_fp, new_fp)

            # Stop only the KiteTicker; keep flush loop running
            try:
                self.kws.stop()
            except Exception:
                pass

            # Update token and rebuild KiteTicker
            self.access_token = new_token
            self.kws = KiteTicker(self.api_key, self.access_token)

            # Reassign callbacks
            self.kws.on_ticks = self.on_ticks
            self.kws.on_connect = self.on_connect
            self.kws.on_close = self.on_close
            self.kws.on_error = self.on_error
            self.kws.on_noreconnect = self.on_noreconnect
            self.kws.on_reconnect = self.on_reconnect
            try:
                self.kws.on_message = self.on_message
            except Exception:
                pass
            try:
                self.kws.on_order_update = self.on_order_update
            except Exception:
                pass

            # Connect again
            self.websocket_status = "CONNECTING"
            self.kws.connect(threaded=True)
        except Exception as e:
            logger.error("Failed to rotate KiteTicker token: %s", e, exc_info=True)

    # ---------------------------
    # Client connection management
    # ---------------------------
    async def connect(self, websocket: WebSocket):
        """Accept a new client WebSocket connection."""
        await websocket.accept()
        self.clients[websocket] = ClientConnection(websocket)
        logger.info("Client connected. Total clients: %d", len(self.clients))
        # Send initial status
        await self._send_json_safe(websocket, {"type": "status", "state": self.websocket_status})

    def disconnect(self, websocket: WebSocket):
        """Close and cleanup a client WebSocket connection."""
        client = self.clients.pop(websocket, None)
        if not client:
            return

        # Recompute aggregates and unsubscribe where needed
        affected_tokens = list(client.subscriptions.keys())
        for token in affected_tokens:
            # Decrement refcount
            prev_ref = self.token_refcount.get(token, 0)
            new_ref = max(prev_ref - 1, 0)
            if new_ref == 0:
                external_needed = token in self._desired_tokens_union
                # Unsubscribe from Kite if connected and no external subscriber needs it.
                if self.kws.is_connected() and not external_needed:
                    try:
                        self.kws.unsubscribe([token])
                        logger.info("KiteTicker.unsubscribe: %s", [token])
                    except Exception as e:
                        logger.error("Error in Kite unsubscribe on disconnect: %s", e)
                # Cleanup maps
                self.token_refcount.pop(token, None)
                if external_needed:
                    self.token_mode_agg[token] = self.kws.MODE_FULL
                else:
                    self.token_mode_agg.pop(token, None)
            else:
                self.token_refcount[token] = new_ref
                # Recompute aggregate mode for remaining clients
                new_agg = self._compute_aggregate_mode(token)
                prev_agg = self.token_mode_agg.get(token)
                self.token_mode_agg[token] = new_agg
                if prev_agg and new_agg != prev_agg and self.kws.is_connected():
                    # Adjust mode on Kite if aggregate lowered
                    try:
                        self.kws.set_mode(new_agg, [token])
                        logger.info("KiteTicker.set_mode: %s -> %s for %s", prev_agg, new_agg, [token])
                    except Exception as e:
                        logger.error("Error in Kite set_mode on disconnect: %s", e)

        logger.info("Client disconnected. Total clients: %d", len(self.clients))

    # ---------------------------
    # Public API for endpoint
    # ---------------------------
    async def subscribe(self, websocket: WebSocket, instrument_tokens: List[int], mode: Optional[str] = None):
        """Subscribe a client to a list of tokens with requested mode (default quote)."""
        mode = normalize_mode(mode)
        client = self.clients.get(websocket)
        if not client:
            await self._send_error(websocket, "Client not registered")
            return

        if not instrument_tokens:
            await self._send_error(websocket, "No tokens provided")
            return

        # Acknowledge first
        await self._send_json_safe(websocket, {"type": "ack", "action": "subscribe", "tokens": instrument_tokens, "mode": mode})

        new_sub_tokens: List[int] = []
        mode_raise_tokens: List[int] = []

        for token in instrument_tokens:
            prev_client_mode = client.subscriptions.get(token)
            prev_ref = self.token_refcount.get(token, 0)
            prev_agg = self.token_mode_agg.get(token)

            # Update client desired mode
            if prev_client_mode is None:
                client.subscriptions[token] = mode
                self.token_refcount[token] = prev_ref + 1
                if prev_ref == 0 and token not in self._desired_tokens_union:
                    new_sub_tokens.append(token)
            else:
                # Upgrade client's desired mode if higher requested
                if MODE_ORDER[mode] > MODE_ORDER[prev_client_mode]:
                    client.subscriptions[token] = mode

            # Recompute aggregate mode
            new_agg = self._compute_aggregate_mode(token)
            self.token_mode_agg[token] = new_agg
            if prev_agg is None or MODE_ORDER[new_agg] > MODE_ORDER.get(prev_agg, "ltp"):
                # Aggregate increased
                mode_raise_tokens.append(token)

        # Apply Kite deltas
        if new_sub_tokens and self.kws.is_connected():
            try:
                self.kws.subscribe(new_sub_tokens)
                logger.info("KiteTicker.subscribe: %s", new_sub_tokens)
            except Exception as e:
                logger.error("Error in Kite subscribe: %s", e)

        if mode_raise_tokens and self.kws.is_connected():
            # Group by new aggregate mode; compute fresh agg and set
            buckets: Dict[str, List[int]] = {}
            for t in mode_raise_tokens:
                agg = self.token_mode_agg.get(t, mode)
                buckets.setdefault(agg, []).append(t)
            for agg, toks in buckets.items():
                try:
                    self.kws.set_mode(agg, toks)
                    logger.info("KiteTicker.set_mode (raise): %s -> %s", toks, agg)
                except Exception as e:
                    logger.error("Error in Kite set_mode (raise): %s", e)

        # Send initial snapshot for tokens with known latest ticks (filtered to client's mode)
        await self._send_initial_snapshot(websocket, client, instrument_tokens)

    async def unsubscribe(self, websocket: WebSocket, instrument_tokens: List[int]):
        """Unsubscribe a client from a list of tokens."""
        client = self.clients.get(websocket)
        if not client:
            await self._send_error(websocket, "Client not registered")
            return

        if not instrument_tokens:
            await self._send_error(websocket, "No tokens provided")
            return

        await self._send_json_safe(websocket, {"type": "ack", "action": "unsubscribe", "tokens": instrument_tokens})

        unsub_tokens_for_kite: List[int] = []
        mode_lower_tokens: List[Tuple[int, str, str]] = []  # (token, prev_agg, new_agg)

        for token in instrument_tokens:
            if token not in client.subscriptions:
                continue

            # Remove from client
            del client.subscriptions[token]

            # Update refcount and aggregates
            prev_ref = self.token_refcount.get(token, 0)
            prev_agg = self.token_mode_agg.get(token)
            new_ref = max(prev_ref - 1, 0)

            if new_ref == 0:
                self.token_refcount.pop(token, None)
                if token in self._desired_tokens_union:
                    self.token_mode_agg[token] = self.kws.MODE_FULL
                    if prev_agg and self.kws.MODE_FULL != prev_agg:
                        mode_lower_tokens.append((token, prev_agg, self.kws.MODE_FULL))
                else:
                    self.token_mode_agg.pop(token, None)
                if self.kws.is_connected() and token not in self._desired_tokens_union:
                    unsub_tokens_for_kite.append(token)
            else:
                self.token_refcount[token] = new_ref
                new_agg = self._compute_aggregate_mode(token)
                self.token_mode_agg[token] = new_agg
                if prev_agg and new_agg != prev_agg and MODE_ORDER[new_agg] < MODE_ORDER[prev_agg]:
                    mode_lower_tokens.append((token, prev_agg, new_agg))

        if unsub_tokens_for_kite and self.kws.is_connected():
            try:
                self.kws.unsubscribe(unsub_tokens_for_kite)
                logger.info("KiteTicker.unsubscribe: %s", unsub_tokens_for_kite)
            except Exception as e:
                logger.error("Error in Kite unsubscribe: %s", e)

        # Apply lower mode where needed
        if mode_lower_tokens and self.kws.is_connected():
            buckets: Dict[str, List[int]] = {}
            for token, _prev, new_agg in mode_lower_tokens:
                buckets.setdefault(new_agg, []).append(token)
            for agg, toks in buckets.items():
                try:
                    self.kws.set_mode(agg, toks)
                    logger.info("KiteTicker.set_mode (lower): %s -> %s", toks, agg)
                except Exception as e:
                    logger.error("Error in Kite set_mode (lower): %s", e)

    async def set_mode(self, websocket: WebSocket, instrument_tokens: List[int], mode: str):
        """Set desired mode for a list of tokens for this client and adjust aggregates."""
        mode = normalize_mode(mode)
        client = self.clients.get(websocket)
        if not client:
            await self._send_error(websocket, "Client not registered")
            return

        if not instrument_tokens:
            await self._send_error(websocket, "No tokens provided")
            return

        await self._send_json_safe(websocket, {"type": "ack", "action": "set_mode", "tokens": instrument_tokens, "mode": mode})

        mode_raise: List[int] = []
        mode_lower: List[int] = []

        for token in instrument_tokens:
            if token not in client.subscriptions:
                # Setting mode implicitly subscribes? Keep strict: require subscribe first.
                # Alternatively uncomment to allow implicit subscribe:
                # client.subscriptions[token] = mode
                # self.token_refcount[token] = self.token_refcount.get(token, 0) + 1
                # continue
                continue

            prev_client_mode = client.subscriptions[token]
            if prev_client_mode == mode:
                continue

            client.subscriptions[token] = mode
            prev_agg = self.token_mode_agg.get(token)
            new_agg = self._compute_aggregate_mode(token)
            self.token_mode_agg[token] = new_agg

            if prev_agg and new_agg != prev_agg:
                if MODE_ORDER[new_agg] > MODE_ORDER[prev_agg]:
                    mode_raise.append(token)
                else:
                    mode_lower.append(token)

        # Apply changes to Kite
        if self.kws.is_connected():
            if mode_raise:
                buckets: Dict[str, List[int]] = {}
                for t in mode_raise:
                    agg = self.token_mode_agg.get(t, mode)
                    buckets.setdefault(agg, []).append(t)
                for agg, toks in buckets.items():
                    try:
                        self.kws.set_mode(agg, toks)
                        logger.info("KiteTicker.set_mode (raise): %s -> %s", toks, agg)
                    except Exception as e:
                        logger.error("Error in Kite set_mode (raise): %s", e)

            if mode_lower:
                buckets: Dict[str, List[int]] = {}
                for t in mode_lower:
                    agg = self.token_mode_agg.get(t, mode)
                    buckets.setdefault(agg, []).append(t)
                for agg, toks in buckets.items():
                    try:
                        self.kws.set_mode(agg, toks)
                        logger.info("KiteTicker.set_mode (lower): %s -> %s", toks, agg)
                    except Exception as e:
                        logger.error("Error in Kite set_mode (lower): %s", e)

    async def send_latest_ticks_to_client(self, websocket: WebSocket):
        """Send latest known ticks for the client's current subscriptions."""
        client = self.clients.get(websocket)
        if not client:
            return
        data: List[Dict[str, Any]] = []
        for token, mode in client.subscriptions.items():
            tick = self.latest_ticks.get(token)
            if tick:
                data.append(self._downcast_tick(tick, mode))
        if data:
            await self._send_json_safe(websocket, {"type": "ticks", "data": data})

    async def send_latest_ticks_to_all_clients(self):
        """Send latest known ticks to all clients (filtered to their subscriptions)."""
        for ws, _client in list(self.clients.items()):
            await self.send_latest_ticks_to_client(ws)

    # ---------------------------
    # Kite callbacks (from thread)
    # ---------------------------
    def on_ticks(self, ws, ticks):
        """Callback for receiving ticks (from KiteTicker thread)."""
        try:
            # Update latest ticks
            for tick in ticks:
                token = tick.get("instrument_token")
                if token is not None:
                    self.latest_ticks[token] = tick
            
            # Write ticks to Redis overlay
            try:
                # Fire and forget; run in the main event loop to avoid blocking the KiteTicker thread
                self.main_event_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(write_ticks_to_redis_overlay(ticks))
                )
            except Exception as e:
                logger.error("Error scheduling Redis overlay write: %s", e, exc_info=True)

            # Update database with new tick data
            def schedule_overlay_db_update(ticks_snapshot):
                task = asyncio.create_task(update_ticker_data_in_db(ticks_snapshot))
                task.add_done_callback(self._log_background_task_error)

            self.main_event_loop.call_soon_threadsafe(
                schedule_overlay_db_update,
                list(ticks),
            )

            # Pass ticks to OptionsSessionManager if it exists
            if hasattr(self, 'options_session_manager'):
                self.options_session_manager.on_ticks(ticks)

            # Update real-time positions with new LTP
            if hasattr(self, 'realtime_positions_service'):
                def update_positions():
                    task = asyncio.create_task(self._update_realtime_positions(list(ticks)))
                    task.add_done_callback(self._log_background_task_error)
                self.main_event_loop.call_soon_threadsafe(update_positions)

            # Merge into pending and let flush loop deliver
            def enqueue():
                # This runs in main loop
                for tick in ticks:
                    token = tick.get("instrument_token")
                    if token is not None:
                        self._pending_ticks[token] = tick
            self.main_event_loop.call_soon_threadsafe(enqueue)
        except Exception as e:
            logger.error("Error in on_ticks: %s", e, exc_info=True)
    
    async def _update_realtime_positions(self, ticks):
        """Update real-time positions with new LTP from WebSocket ticks"""
        try:
            if not hasattr(self, 'realtime_positions_service'):
                return
            await self.realtime_positions_service.process_ticks(ticks, corr_id="websocket_tick")
        except Exception as e:
            logger.error(f"Error updating realtime positions: {e}", exc_info=True)

    def on_message(self, ws, payload, is_binary):
        """Callback for raw messages; capture alert text messages and enqueue for dispatcher."""
        try:
            if is_binary:
                return
            # payload may be bytes or str
            if isinstance(payload, (bytes, bytearray)):
                try:
                    text = payload.decode("utf-8", errors="ignore")
                except Exception:
                    return
            else:
                text = payload
            data = None
            try:
                data = json.loads(text)
            except Exception:
                return
            if not isinstance(data, dict):
                return
            msg_type = data.get("type")
            if msg_type == "alert":
                # Normalize event shape; leave full payload for dispatcher
                alert_event = {
                    "type": "alert",
                    "raw": data,
                    "received_at": time.monotonic(),
                }
                # Enqueue to main loop to avoid cross-thread issues
                def put_event():
                    try:
                        self.alert_event_queue.put_nowait(alert_event)
                    except Exception as qe:
                        logger.error("Failed to enqueue alert_event: %s", qe)
                self.main_event_loop.call_soon_threadsafe(put_event)
        except Exception as e:
            logger.error("Error in on_message: %s", e, exc_info=True)

    def on_order_update(self, ws, data):
        """Callback for order updates from KiteTicker thread; persist via store_event."""
        try:
            if not getattr(self, "order_updates_enabled", True):
                return

            # Build PostbackPayload-like dict with safe defaults
            def fmt_ts(v) -> str:
                if isinstance(v, str) and len(v) >= 19:
                    return v[:19]
                try:
                    # v could be epoch seconds or datetime
                    if isinstance(v, (int, float)):
                        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(v, datetime):
                        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                        return dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
                return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            payload_dict = {
                "user_id": data.get("user_id") or data.get("placed_by") or "unknown",
                "app_id": int(data.get("app_id") or 0),
                "checksum": "",  # not used for WS path
                "source": "websocket",
                "placed_by": data.get("placed_by") or data.get("user_id") or "unknown",
                "order_id": data.get("order_id"),
                "exchange_order_id": data.get("exchange_order_id"),
                "parent_order_id": data.get("parent_order_id"),
                "status": data.get("status") or "UPDATE",
                "status_message": data.get("status_message"),
                "status_message_raw": data.get("status_message_raw"),
                "order_timestamp": fmt_ts(data.get("order_timestamp") or data.get("exchange_timestamp") or datetime.now(timezone.utc)),
                "exchange_update_timestamp": (fmt_ts(data.get("exchange_update_timestamp")) if data.get("exchange_update_timestamp") else None),
                "exchange_timestamp": (fmt_ts(data.get("exchange_timestamp")) if data.get("exchange_timestamp") else None),
                "variety": data.get("variety") or "regular",
                "exchange": data.get("exchange"),
                "tradingsymbol": data.get("tradingsymbol"),
                "instrument_token": int(data.get("instrument_token") or 0),
                "order_type": data.get("order_type") or "MARKET",
                "transaction_type": data.get("transaction_type") or None,
                "validity": data.get("validity") or "DAY",
                "validity_ttl": data.get("validity_ttl"),
                "product": data.get("product") or None,
                "quantity": int(data.get("quantity") or data.get("filled_quantity") or 0),
                "disclosed_quantity": int(data.get("disclosed_quantity") or 0),
                "price": float(data.get("price") or 0.0),
                "trigger_price": float(data.get("trigger_price") or 0.0),
                "average_price": float(data.get("average_price") or 0.0),
                "filled_quantity": int(data.get("filled_quantity") or 0),
                "pending_quantity": int(data.get("pending_quantity") or 0),
                "cancelled_quantity": int(data.get("cancelled_quantity") or 0),
                "unfilled_quantity": int(data.get("unfilled_quantity") or 0),
                "market_protection": int(data.get("market_protection") or 0),
                "meta": data.get("meta") or {},
                "tag": data.get("tag"),
                "tags": data.get("tags"),
                "guid": data.get("guid"),
            }

            # Persist on main loop to avoid thread-crossing issues
            async def persist():
                try:
                    ingest_result = await order_event_runtime.ingest_ws_event(payload_dict, corr_id="ws_order_update")
                    if ingest_result.get("duplicate"):
                        return
                    evt_ts = payload_dict.get("exchange_update_timestamp") or payload_dict.get("order_timestamp") or datetime.now(timezone.utc).isoformat()
                    try:
                        await publish_event("orders.events", {
                            "source": "ws",
                            "id": ingest_result.get("canonical_event_id"),
                            "order_id": payload_dict.get("order_id"),
                            "user_id": payload_dict.get("user_id"),
                            "status": payload_dict.get("status"),
                            "event_timestamp": evt_ts,
                            "exchange": payload_dict.get("exchange"),
                            "tradingsymbol": payload_dict.get("tradingsymbol"),
                            "instrument_token": payload_dict.get("instrument_token"),
                            "transaction_type": payload_dict.get("transaction_type"),
                            "quantity": payload_dict.get("quantity"),
                            "filled_quantity": payload_dict.get("filled_quantity"),
                            "average_price": payload_dict.get("average_price"),
                            "payload": payload_dict
                        })
                    except Exception as pe:
                        logger.error("Failed to publish WS order event: %s", pe, exc_info=True)
                except Exception as pe:
                    logger.error("Failed to persist WS order_update: %s", pe, exc_info=True)

            try:
                # Update last seen timestamp immediately
                self.last_order_update_at = datetime.now(timezone.utc)
                self.main_event_loop.call_soon_threadsafe(lambda: asyncio.create_task(persist()))
            except Exception as se:
                logger.error("Failed to schedule WS order_update persist: %s", se, exc_info=True)
        except Exception as e:
            logger.error("Error in on_order_update: %s", e, exc_info=True)

    def on_connect(self, ws, response):
        """Callback on successful connect to KiteTicker."""
        logger.info("KiteTicker connection established.")
        self.websocket_status = "CONNECTED"

        # Resubscribe and reapply modes based on aggregate state
        def do_resubscribe():
            try:
                tokens = sorted(set([t for t, rc in self.token_refcount.items() if rc > 0]) | set(self._desired_tokens_union))
                if tokens:
                    self.kws.subscribe(tokens)
                    logger.info("KiteTicker.resubscribe: %s", tokens)

                    # Group tokens by aggregate mode
                    buckets: Dict[str, List[int]] = {}
                    for t in tokens:
                        agg = self.token_mode_agg.get(t, "quote")
                        buckets.setdefault(agg, []).append(t)

                    for agg, toks in buckets.items():
                        self.kws.set_mode(agg, toks)
                        logger.info("KiteTicker.set_mode on reconnect: %s -> %s", toks, agg)
            except Exception as e:
                logger.error("Error in resubscribe on connect: %s", e)
        do_resubscribe()

        # Broadcast status
        self._broadcast_status_async("CONNECTED")

        # Optionally push initial ticks to all clients
        def schedule_initial():
            asyncio.create_task(self.send_latest_ticks_to_all_clients())
        self.main_event_loop.call_soon_threadsafe(schedule_initial)

    def on_close(self, ws, code, reason):
        """Callback on connection close."""
        logger.warning("KiteTicker connection closed: %s - %s", code, reason)
        self.websocket_status = "DISCONNECTED"
        self._broadcast_status_async("DISCONNECTED")

    def on_error(self, ws, code, reason):
        """Callback on connection error."""
        logger.error("KiteTicker error: %s - %s", code, reason)
        self.websocket_status = "ERROR"
        self._broadcast_status_async("ERROR")

    def on_reconnect(self, ws, attempts_count):
        """Callback on auto reconnection attempt."""
        logger.info("KiteTicker attempting to reconnect. Attempt: %s", attempts_count)
        self.websocket_status = "RECONNECTING"
        self._broadcast_status_async("RECONNECTING")

    def on_noreconnect(self, ws):
        """Callback when auto reconnection attempts exceed reconnect_tries."""
        logger.error("KiteTicker auto reconnection failed after multiple attempts.")
        self.websocket_status = "DISCONNECTED"
        self._broadcast_status_async("DISCONNECTED")

    # ---------------------------
    # Internal helpers
    # ---------------------------
    def _compute_aggregate_mode(self, token: int, *, include_external: bool = True) -> str:
        """Compute highest requested mode across all clients for a token."""
        if include_external and token in self._desired_tokens_union:
            return self.kws.MODE_FULL
        agg_mode: Optional[str] = None
        for client in self.clients.values():
            m = client.subscriptions.get(token)
            if not m:
                continue
            agg_mode = m if agg_mode is None else higher_mode(agg_mode, m)
            if agg_mode == "full":
                # Can't go higher than full
                break
        return agg_mode or "quote"

    async def _flush_loop(self):
        """Periodic flush loop to send pending ticks to clients, coalesced and filtered."""
        try:
            while self._running:
                await asyncio.sleep(self.flush_interval_ms / 1000.0)

                # Snapshot pending
                if not self._pending_ticks:
                    continue

                pending = self._pending_ticks
                self._pending_ticks = {}

                # Prepare per-client payloads
                for ws, client in list(self.clients.items()):
                    try:
                        data: List[Dict[str, Any]] = []
                        for token, mode in client.subscriptions.items():
                            tick = pending.get(token)
                            if not tick:
                                continue
                            data.append(self._downcast_tick(tick, mode))
                        if data:
                            await self._send_json_safe(ws, {"type": "ticks", "data": data})
                    except Exception as e:
                        logger.error("Error sending ticks to client: %s", e)
                        # Cleanup that client
                        try:
                            self.disconnect(ws)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            # Normal shutdown
            pass
        except Exception as e:
            logger.error("Error in flush loop: %s", e, exc_info=True)

    def _downcast_tick(self, tick: Dict[str, Any], mode: str) -> Dict[str, Any]:
        """Return a tick dict filtered to the requested mode."""
        token = tick.get("instrument_token")
        last_price = tick.get("last_price")
        change = tick.get("change")
        exchange_timestamp = tick.get("exchange_timestamp")

        base = {
            "instrument_token": token,
            "last_price": last_price,
            "change": change,
        }
        if exchange_timestamp is not None:
            base["exchange_timestamp"] = exchange_timestamp

        if mode == "ltp":
            return base

        # QUOTE adds OHLC and quantities
        quote = dict(base)
        ohlc = tick.get("ohlc")
        if isinstance(ohlc, dict):
            quote["ohlc"] = {
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),
            }
        # Volume and totals
        if "volume_traded" in tick:
            quote["volume_traded"] = tick.get("volume_traded")
        if "total_buy_quantity" in tick:
            quote["total_buy_quantity"] = tick.get("total_buy_quantity")
        if "total_sell_quantity" in tick:
            quote["total_sell_quantity"] = tick.get("total_sell_quantity")

        if mode == "quote":
            return quote

        # FULL adds depth and OI
        full = dict(quote)
        if "depth" in tick:
            full["depth"] = tick.get("depth")
        if "oi" in tick:
            full["oi"] = tick.get("oi")
        if "oi_day_high" in tick:
            full["oi_day_high"] = tick.get("oi_day_high")
        if "oi_day_low" in tick:
            full["oi_day_low"] = tick.get("oi_day_low")
        if "last_trade_time" in tick:
            full["last_trade_time"] = tick.get("last_trade_time")

        return full

    async def _send_initial_snapshot(self, websocket: WebSocket, client: ClientConnection, tokens: List[int]):
        """Send initial snapshot for newly subscribed tokens if available."""
        data: List[Dict[str, Any]] = []
        for token in tokens:
            tick = self.latest_ticks.get(token)
            if not tick:
                continue
            mode = client.subscriptions.get(token, "quote")
            data.append(self._downcast_tick(tick, mode))
        if data:
            await self._send_json_safe(websocket, {"type": "ticks", "data": data})

    def _broadcast_status_async(self, state: str):
        """Schedule broadcasting a status message to all clients."""
        async def _broadcast():
            msg = {"type": "status", "state": state}
            for ws in list(self.clients.keys()):
                await self._send_json_safe(ws, msg)
        try:
            self.main_event_loop.call_soon_threadsafe(lambda: asyncio.create_task(_broadcast()))
        except Exception:
            pass

    async def _send_error(self, websocket: WebSocket, message: str):
        await self._send_json_safe(websocket, {"type": "error", "message": message})

    async def _send_json_safe(self, websocket: WebSocket, payload: Dict[str, Any]):
        """Send JSON to a client, suppressing errors (client may have disconnected)."""
        try:
            await websocket.send_text(json.dumps(payload, default=str))
        except Exception as e:
            logger.debug("Send failed to client (ignored): %s", e)

    @staticmethod
    def _log_background_task_error(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
            if exc:
                logger.error("Background WebSocket task failed: %s", exc, exc_info=True)
        except asyncio.CancelledError:
            pass
    # ---------------------------
    # External subscription management for Options Sessions
    # ---------------------------
    async def set_desired_tokens_union(self, desired: set[int]):
        """
        Accepts a desired set of tokens from an external manager (e.g., OptionsSessionManager),
        computes the diff, and converges the subscriptions. Rate-limited to avoid churn.
        """
        now = asyncio.get_running_loop().time()
        if now - self._last_converge_ts < 5.0:  # Rate limit to once every 5s
            return

        self._last_converge_ts = now
        
        current_external = set(self._desired_tokens_union)
        to_subscribe = desired - current_external
        to_unsubscribe = current_external - desired

        if to_subscribe:
            # For simplicity, subscribe with default 'full' mode to get OI data.
            # The options session manager is LTP-driven, so this is sufficient.
            # A more advanced implementation could accept modes from the manager.
            actual_subscribe = [token for token in to_subscribe if self.token_refcount.get(token, 0) == 0 and token not in self.token_mode_agg]
            if actual_subscribe and self.kws.is_connected():
                self.kws.subscribe(list(actual_subscribe))
                self.kws.set_mode(self.kws.MODE_FULL, list(actual_subscribe))
            mode_raise = [token for token in to_subscribe if token not in actual_subscribe]
            if mode_raise and self.kws.is_connected():
                self.kws.set_mode(self.kws.MODE_FULL, list(mode_raise))
            for token in to_subscribe:
                self.token_mode_agg[token] = self.kws.MODE_FULL
            logger.info(f"[OptionsSession] Converge: Subscribed to {len(to_subscribe)} new tokens.")

        if to_unsubscribe:
            actual_unsubscribe = [token for token in to_unsubscribe if self.token_refcount.get(token, 0) == 0]
            if actual_unsubscribe and self.kws.is_connected():
                self.kws.unsubscribe(list(actual_unsubscribe))
            mode_lower: Dict[str, List[int]] = {}
            for token in to_unsubscribe:
                if self.token_refcount.get(token, 0) > 0:
                    new_mode = self._compute_aggregate_mode(token, include_external=False)
                    self.token_mode_agg[token] = new_mode
                    mode_lower.setdefault(new_mode, []).append(token)
                else:
                    self.token_mode_agg.pop(token, None)
            for new_mode, tokens in mode_lower.items():
                if not self.kws.is_connected():
                    continue
                self.kws.set_mode(new_mode, tokens)
            logger.info(f"[OptionsSession] Converge: Unsubscribed from {len(to_unsubscribe)} tokens.")
        
        self._desired_tokens_union = desired

import psycopg2
import psycopg2.extras
from database import get_db_connection

async def update_ticker_data_in_db(ticks: List[Dict[str, Any]]):
    """
    DEPRECATED: Database updates are now handled by finalize-baseline endpoint.
    Live tick data is stored in Redis overlay cache only.
    This function is kept for backward compatibility but does nothing.
    """
    pass


async def write_ticks_to_redis_overlay(ticks: List[Dict[str, Any]]):
    """
    Write the latest tick for each instrument to a Redis overlay cache with a TTL.
    """
    try:
        redis_client = get_redis()
        ttl_seconds = int(os.getenv("OVERLAY_TTL_SECONDS", "60"))
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        async with redis_client.pipeline(transaction=False) as pipe:
            for tick in ticks:
                token = tick.get("instrument_token")
                last_price = tick.get("last_price")

                if token is None or last_price is None or last_price == 0:
                    continue

                key = f"marketwatch:overlay:{today_iso}:{token}"
                exchange_timestamp = tick.get("exchange_timestamp") or datetime.now(timezone.utc)
                if isinstance(exchange_timestamp, str):
                    try:
                        exchange_timestamp = datetime.fromisoformat(exchange_timestamp.replace("Z", "+00:00"))
                    except Exception:
                        exchange_timestamp = datetime.now(timezone.utc)
                
                payload = {
                    "instrument_token": token,
                    "last_price": float(last_price),
                    "tick_timestamp": int(exchange_timestamp.timestamp() * 1000),
                    "source": "ws",
                }
                if "change" in tick:
                    payload["change_percent"] = float(tick["change"])

                await pipe.set(key, json.dumps(payload), ex=ttl_seconds)
            
            await pipe.execute()
            
    except RedisConnectionError as e:
        logger.warning("Redis connection error in overlay write: %s", e)
    except Exception as e:
        logger.error("Error writing ticks to Redis overlay: %s", e, exc_info=True)
