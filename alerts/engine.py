import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncio
from broker_api.ntfy import notify_alert_triggered
from broker_api.redis_events import publish_event

# External async DB client from database.py
# This is the `databases.Database` instance (already connected by main.py)
# We will receive it via constructor injection.
# from database import database as async_db  # do not import directly; pass in

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    id: str
    instrument_token: int
    comparator: str  # "gt" | "lt"
    absolute_target: float
    baseline_price: Optional[float]
    one_time: bool = True

@dataclass
class AlertState:
    """In-memory state per alert, not persisted."""
    prev_price: Optional[float] = None

class AlertsEngine:
    """
    Phase 0, single-process alerts evaluator.

    Responsibilities:
    - Periodic evaluation loop (default 500ms; env ALERT_ENGINE_INTERVAL_MS, fallback ALERTS_EVAL_MS)
    - Periodic refresh of active alerts set from DB (default 5s; env ALERT_ENGINE_REFRESH_SEC)
    - Maintain server-side subscriptions for tokens with active alerts when no clients are subscribed
      using the existing WebSocketManager (ltp mode only, and never downgrades client-requested modes)
    - Evaluate crossing logic and persist events/updates
    """

    def __init__(self, db, ws_manager, app=None):
        self.db = db
        self.ws_manager = ws_manager
        self.app = app

        # Timings
        self.interval_ms: int = self._get_int_env(
            ["ALERT_ENGINE_INTERVAL_MS", "ALERTS_EVAL_MS"], default=500, lo=50, hi=5000
        )
        self.refresh_sec: int = self._get_int_env(
            ["ALERT_ENGINE_REFRESH_SEC"], default=5, lo=1, hi=60
        )
        # Throttle for non-trigger persistence of last_evaluated_price
        self.persist_throttle_sec: float = float(
            os.getenv("ALERT_ENGINE_PERSIST_THROTTLE_SEC", "1.0")
        )

        # Runtime state
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Mapping of token -> alerts
        self._alerts_by_token: Dict[int, List[Alert]] = {}
        self._alert_state: Dict[str, AlertState] = {}  # alert.id -> AlertState
        self._active_tokens: Set[int] = set()

        # Server-side subscriptions that this engine owns (only when no clients are using the token)
        self._engine_subscribed_tokens: Set[int] = set()

        # Last time we persisted a non-trigger price per alert id (throttling)
        self._last_persist_ts: Dict[str, float] = {}

        # WS status tracking to re-assert subscriptions on reconnect
        self._last_ws_status: Optional[str] = None

        logger.info(
            "[ALERTS-ENGINE] init: interval=%dms refresh=%ds persist_throttle=%.2fs",
            self.interval_ms, self.refresh_sec, self.persist_throttle_sec,
        )

    @staticmethod
    def _get_int_env(names: List[str], default: int, lo: int, hi: int) -> int:
        for n in names:
            v = os.getenv(n)
            if v is not None:
                try:
                    val = int(v)
                    return max(lo, min(val, hi))
                except Exception:
                    continue
        return default

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run_loop())
        logger.info("[ALERTS-ENGINE] started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        # Best-effort unsubscribe engine-owned tokens if no clients are on them
        await self._engine_unsubscribe_all_safe()
        logger.info("[ALERTS-ENGINE] stopped")

    # ------------- Core loop -------------

    async def _run_loop(self) -> None:
        # Initial refresh
        await self._refresh_active_alerts_and_subscriptions()

        last_refresh = time.monotonic()
        interval = max(0.05, self.interval_ms / 1000.0)

        try:
            while self._running:
                await asyncio.sleep(interval)

                # Re-assert engine subscriptions on WS reconnect
                await self._handle_ws_reconnect()

                # Periodic active-set refresh
                now = time.monotonic()
                if now - last_refresh >= self.refresh_sec:
                    await self._refresh_active_alerts_and_subscriptions()
                    last_refresh = now

                # Evaluate alerts using latest tick cache
                await self._evaluate_once()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[ALERTS-ENGINE] loop error: %s", e, exc_info=True)

    # ------------- Evaluation -------------

    async def _evaluate_once(self) -> None:
        if not self._active_tokens:
            return

        # Snapshot to avoid mutation during evaluate
        tokens = list(self._active_tokens)

        # For each token, get current price and evaluate all active alerts mapped to it
        for token in tokens:
            price = self._get_latest_price(token)
            if price is None:
                continue

            alerts = self._alerts_by_token.get(token) or []
            if not alerts:
                continue

            for alert in list(alerts):
                try:
                    await self._evaluate_alert(alert, price)
                except Exception as e:
                    logger.error(
                        "[ALERTS-ENGINE] evaluate error id=%s token=%s: %s",
                        alert.id, alert.instrument_token, e, exc_info=True
                    )

    async def _evaluate_alert(self, alert: Alert, current_price: float) -> None:
        state = self._alert_state.get(alert.id)
        if not state:
            # Should not happen if registration logic is correct
            logger.warning("[ALERTS-ENGINE] no state for alert id=%s", alert.id)
            return

        prev_price = state.prev_price
        threshold = alert.absolute_target

        # If prev_price is still None, this is the first tick. Use baseline_price for one-time eval.
        if prev_price is None:
            effective_prev = alert.baseline_price if alert.baseline_price is not None else current_price
            logger.debug(
                "[ALERTS-ENGINE] first tick eval id=%s token=%s prev=%.6f cur=%.6f target=%.6f",
                alert.id, alert.instrument_token, effective_prev, current_price, threshold
            )
        else:
            effective_prev = prev_price

        # Crossing logic
        triggered = False
        direction: Optional[str] = None

        # Crossing logic for supported comparators
        if alert.comparator == "gt":
            if effective_prev < threshold and current_price >= threshold:
                triggered = True
                direction = "cross_up"
        elif alert.comparator == "lt":
            if effective_prev > threshold and current_price <= threshold:
                triggered = True
                direction = "cross_down"
        else:
            return  # Unknown comparator

        if triggered and direction:
            await self._handle_trigger(alert, current_price, direction)
            self._remove_alert_from_memory(alert)
            return

        # Update prev_price for next tick's evaluation
        if state.prev_price != current_price:
            state.prev_price = current_price
            # Also persist to DB for long-term state (throttled)
            await self._persist_last_evaluated_price(alert.id, current_price, force=False)

    async def _handle_trigger(self, alert: Alert, current_price: float, direction: str) -> None:
        logger.info(
            "[ALERTS-ENGINE] TRIGGER id=%s token=%s cmp=%s target=%.6f price=%.6f dir=%s",
            alert.id, alert.instrument_token, alert.comparator, alert.absolute_target, current_price, direction
        )

        triggered_at_ts = time.time()
        updated_id = None

        # Update alert row and insert event atomically
        async with self.db.transaction():
            # Status -> triggered (one-time semantics), set triggered_at and last_evaluated_price
            upd_sql = """
            UPDATE public.alerts
            SET status = 'triggered',
                triggered_at = NOW(),
                last_evaluated_price = :price,
                updated_at = NOW()
            WHERE id = :id AND status = 'active'
            RETURNING id
            """
            updated_id = await self.db.execute(upd_sql, {"id": alert.id, "price": float(current_price)})

            if updated_id:
                # Insert trigger event; tolerate duplicate via catching unique violation
                evt_sql = """
                INSERT INTO public.alert_events (
                    alert_id, instrument_token, event_type, price_at_event, direction, reason, meta
                ) VALUES (
                    :id, :token, 'triggered', :price, :direction, NULL, NULL
                )
                """
                try:
                    await self.db.execute(evt_sql, {
                        "id": alert.id,
                        "token": int(alert.instrument_token),
                        "price": float(current_price),
                        "direction": direction,
                    })
                except Exception as e:
                    # If unique index ux_alert_events_triggered_once fires, ignore
                    if 'ux_alert_events_triggered_once' in str(e):
                        logger.debug("[ALERTS-ENGINE] duplicate trigger event ignored for id=%s", alert.id)
                    else:
                        raise
        
        if not updated_id:
            # This can happen in a race condition where the alert is triggered by two parallel evaluations.
            # The first one will update the status, and the second will find no active alert to update.
            logger.info(f"[ALERTS-ENGINE] Trigger for alert {alert.id} was suppressed as it is no longer active.")
            return

        # Publish event to Redis Pub/Sub
        # event_payload = {
        #     "type": "alert.triggered",
        #     "id": alert.id,
        #     "status": "triggered",
        #     "triggered_at": triggered_at_ts,
        #     "instrument_token": alert.instrument_token,
        #     "comparator": alert.comparator,
        #     "absolute_target": alert.absolute_target,
        #     "baseline_price": alert.baseline_price,
        # }
        # await publish_event("alerts.events", event_payload)

        # Schedule non-blocking ntfy notification
        asyncio.create_task(
            notify_alert_triggered(
                alert_id=alert.id,
                instrument_token=alert.instrument_token,
                comparator=alert.comparator,
                absolute_target=alert.absolute_target,
                baseline_price=alert.baseline_price,
                triggered_at=triggered_at_ts,
                current_price=current_price,
            )
        )

    async def _persist_last_evaluated_price(self, alert_id: str, price: float, force: bool) -> None:
        now = time.monotonic()
        if not force:
            last = self._last_persist_ts.get(alert_id)
            if last is not None and (now - last) < self.persist_throttle_sec:
                return
        sql = """
        UPDATE public.alerts
        SET last_evaluated_price = :p, updated_at = NOW()
        WHERE id = :id AND (last_evaluated_price IS DISTINCT FROM :p)
        """
        try:
            await self.db.execute(sql, {"id": alert_id, "p": float(price)})
            self._last_persist_ts[alert_id] = now
        except Exception as e:
            logger.error("[ALERTS-ENGINE] persist last_evaluated_price failed id=%s: %s", alert_id, e)

    def _remove_alert_from_memory(self, alert: Alert) -> None:
        """
        Removes an alert from all in-memory caches.
        """
        tok = alert.instrument_token
        
        # Remove from token->alerts mapping
        lst = self._alerts_by_token.get(tok)
        if lst:
            self._alerts_by_token[tok] = [a for a in lst if a.id != alert.id]
            if not self._alerts_by_token[tok]:
                del self._alerts_by_token[tok]
                self._active_tokens.discard(tok)

        # Remove from state tracking
        self._alert_state.pop(alert.id, None)
        self._last_persist_ts.pop(alert.id, None)
        
        logger.info(f"[ALERTS-ENGINE] Removed alert {alert.id} from memory after trigger.")
    def _register_alert(self, alert: Alert) -> None:
            """
            Adds a new alert to in-memory state, including seeding its prev_price.
            """
            # Seed prev_price from best available source
            prev_price = self._get_latest_price(alert.instrument_token)
            if prev_price is None:
                prev_price = alert.baseline_price
    
            self._alert_state[alert.id] = AlertState(prev_price=prev_price)
            logger.info(
                "[ALERTS-ENGINE] register id=%s token=%s initial_prev_price=%.6f",
                alert.id, alert.instrument_token, prev_price if prev_price is not None else -1.0
            )
            # Ensure in-memory indices populated
            token = alert.instrument_token
            self._alerts_by_token.setdefault(token, []).append(alert)
            self._active_tokens.add(token)

    # ------------- Active set and subscriptions -------------

    async def _refresh_active_alerts_and_subscriptions(self) -> None:
            try:
                rows = await self.db.fetch_all(
                    """
                    SELECT id, instrument_token, comparator, target_type, absolute_target, one_time, baseline_price
                    FROM public.alerts
                    WHERE status = 'active'
                    """
                )
                new_alerts_map: Dict[str, Alert] = {}
                new_by_token: Dict[int, List[Alert]] = {}
                for r in rows:
                    try:
                        a = Alert(
                            id=str(r["id"]),
                            instrument_token=int(r["instrument_token"]),
                            comparator=str(r["comparator"]),
                            absolute_target=float(r["absolute_target"]),
                            baseline_price=float(r["baseline_price"]) if r["baseline_price"] is not None else None,
                            one_time=bool(r["one_time"]) if r["one_time"] is not None else True,
                        )
                        new_alerts_map[a.id] = a
                        new_by_token.setdefault(a.instrument_token, []).append(a)
                    except Exception as e:
                        logger.error("[ALERTS-ENGINE] bad row in active set: %s", e)
    
                old_alert_ids = set(self._alert_state.keys())
                new_alert_ids = set(new_alerts_map.keys())
    
                # Register new alerts
                for alert_id in (new_alert_ids - old_alert_ids):
                    self._register_alert(new_alerts_map[alert_id])
    
                # De-register alerts that are no longer active
                for alert_id in (old_alert_ids - new_alert_ids):
                    alert = next((a for a_list in self._alerts_by_token.values() for a in a_list if a.id == alert_id), None)
                    if alert:
                        self._remove_alert_from_memory(alert)
                    # Purge from state tracking so it can be re-registered cleanly if reactivated
                    self._alert_state.pop(alert_id, None)
                    self._last_persist_ts.pop(alert_id, None)
    
                self._alerts_by_token = new_by_token
                self._active_tokens = set(new_by_token.keys())
    
                # Manage server-side subscriptions
                await self._reconcile_engine_subscriptions(self._active_tokens)
    
                logger.debug(
                    "[ALERTS-ENGINE] refresh: tokens=%d alerts=%d engine_subs=%d",
                    len(self._active_tokens), len(self._alert_state), len(self._engine_subscribed_tokens)
                )
            except Exception:
                logger.exception("[ALERTS-ENGINE] refresh loop failed; continuing")

    async def _reconcile_engine_subscriptions(self, desired_tokens: Set[int]) -> None:
        # Filter to tokens with zero client refcount
        client_map: Dict[int, int] = getattr(self.ws_manager, "token_refcount", {}) or {}
        desired = {t for t in desired_tokens if int(client_map.get(t, 0) or 0) == 0}

        # Determine deltas
        add = sorted(list(desired - self._engine_subscribed_tokens))
        remove = sorted(list(self._engine_subscribed_tokens - desired))

        if add:
            await self._engine_subscribe_safe(add)
            self._engine_subscribed_tokens.update(add)

        if remove:
            await self._engine_unsubscribe_safe(remove)
            for t in remove:
                self._engine_subscribed_tokens.discard(t)

    async def _engine_subscribe_safe(self, tokens: List[int]) -> None:
        if not tokens:
            return
        # Ensure WS connected
        try:
            if self.ws_manager and getattr(self.ws_manager, "kws", None) and self.ws_manager.kws.is_connected():
                # Subscribe
                try:
                    self.ws_manager.kws.subscribe(tokens)
                    logger.info("[ALERTS-ENGINE] subscribe tokens=%s", tokens)
                except Exception as e:
                    logger.error("[ALERTS-ENGINE] subscribe failed: %s", e)

                # Set mode to ltp ONLY for tokens not already tracked with an aggregate mode
                token_mode_agg: Dict[int, str] = getattr(self.ws_manager, "token_mode_agg", {}) or {}
                set_mode_tokens = [t for t in tokens if t not in token_mode_agg]
                if set_mode_tokens:
                    try:
                        self.ws_manager.kws.set_mode("ltp", set_mode_tokens)
                        logger.info("[ALERTS-ENGINE] set_mode ltp tokens=%s", set_mode_tokens)
                    except Exception as e:
                        logger.error("[ALERTS-ENGINE] set_mode(ltp) failed: %s", e)
            else:
                logger.debug("[ALERTS-ENGINE] skip subscribe; WS not connected")
        except Exception as e:
            logger.error("[ALERTS-ENGINE] subscribe unexpected error: %s", e, exc_info=True)

    async def _engine_unsubscribe_safe(self, tokens: List[int]) -> None:
        if not tokens:
            return
        try:
            # Unsubscribe only when WS is connected
            if self.ws_manager and getattr(self.ws_manager, "kws", None) and self.ws_manager.kws.is_connected():
                # Double-check no clients currently hold the token
                client_map: Dict[int, int] = getattr(self.ws_manager, "token_refcount", {}) or {}
                safe_tokens = [t for t in tokens if int(client_map.get(t, 0) or 0) == 0]
                if not safe_tokens:
                    return
                try:
                    self.ws_manager.kws.unsubscribe(safe_tokens)
                    logger.info("[ALERTS-ENGINE] unsubscribe tokens=%s", safe_tokens)
                except Exception as e:
                    logger.error("[ALERTS-ENGINE] unsubscribe failed: %s", e)
        except Exception as e:
            logger.error("[ALERTS-ENGINE] unsubscribe unexpected error: %s", e, exc_info=True)

    async def _engine_unsubscribe_all_safe(self) -> None:
        if not self._engine_subscribed_tokens:
            return
        await self._engine_unsubscribe_safe(sorted(list(self._engine_subscribed_tokens)))
        self._engine_subscribed_tokens.clear()

    async def _handle_ws_reconnect(self) -> None:
        try:
            status = None
            if self.ws_manager and hasattr(self.ws_manager, "get_websocket_status"):
                status = self.ws_manager.get_websocket_status()

            if status != self._last_ws_status:
                prev_status = self._last_ws_status
                self._last_ws_status = status
                logger.info("[ALERTS-ENGINE] WS status change: %s -> %s", prev_status, status)

                # On transition to CONNECTED, reconcile all engine subscriptions
                if prev_status != "CONNECTED" and status == "CONNECTED":
                    logger.info("[ALERTS-ENGINE] WS reconnected; reconciling all engine subscriptions")
                    desired_tokens = set(self._alerts_by_token.keys())
                    await self._reconcile_engine_subscriptions(desired_tokens)
        except Exception as e:
            logger.error("[ALERTS-ENGINE] _handle_ws_reconnect failed: %s", e, exc_info=True)

    async def refresh_now(self) -> None:
        """
        Public method to refresh active alerts set and reconcile subscriptions immediately.
        """
        await self._refresh_active_alerts_and_subscriptions()

    # ------------- Adapters -------------

    def _get_latest_price(self, token: int) -> Optional[float]:
        try:
            lt: Dict[int, Dict[str, Any]] = getattr(self.ws_manager, "latest_ticks", {}) or {}
            tick = lt.get(int(token))
            if isinstance(tick, dict):
                lp = tick.get("last_price")
                if lp is None:
                    return None
                return float(lp)
        except Exception:
            return None
        return None