"""Operator live-price stream for the alerts UI.

The browser must not hold broker credentials, talk to the market-runtime control
API, or open its own broker socket. So the app exposes ONE cookie-authenticated
websocket per browser tab, and behind it:

* the market-runtime keeps owning the single Kite connection;
* this process registers a *subscription owner* with the runtime for exactly the
  instruments that browser currently needs, and deletes it when the connection
  ends (the runtime's owner lease TTL is the second safety net);
* ticks are read from the runtime's Redis stream (``market:ticks``) — one
  process-wide subscription shared by every browser connection — and coalesced
  before they are sent, so an exchange burst cannot turn into a render storm.

Two invariants are worth stating because they are easy to break:

* a cached price is **never** labelled ``LIVE`` — the first frame for an
  instrument is a snapshot carrying its real age and is classified accordingly;
* a slow browser is **never** allowed to grow memory: each connection has a
  bounded frame queue, and tick frames are dropped (newest value wins on the
  next flush) instead of blocking the hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

FRESHNESS_LIVE = "LIVE"
FRESHNESS_DELAYED = "DELAYED"
FRESHNESS_STALE = "STALE"
FRESHNESS_CLOSED = "MARKET CLOSED"
FRESHNESS_NO_DATA = "NO DATA"

SESSION_OPEN = "open"
SESSION_CLOSED = "closed"
SESSION_UNKNOWN = "unknown"

ENV_MAX_INSTRUMENTS = "ALERTS_MARKET_MAX_INSTRUMENTS"
ENV_FLUSH_MS = "ALERTS_MARKET_FLUSH_MS"
ENV_QUEUE_MAX = "ALERTS_MARKET_QUEUE_MAX"
ENV_LIVE_MAX_S = "ALERTS_MARKET_LIVE_MAX_S"
ENV_DELAYED_MAX_S = "ALERTS_MARKET_DELAYED_MAX_S"
ENV_OWNER_RENEW_S = "ALERTS_MARKET_OWNER_RENEW_S"
ENV_IDLE_TIMEOUT_S = "ALERTS_MARKET_IDLE_TIMEOUT_S"

RUNTIME_TICKS_CHANNEL = "market:ticks"
RUNTIME_TICK_KEY = "market:tick:{}"

#: Close codes. 4401/4403 mirror the HTTP semantics the operator API uses.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_IDLE = 4408
CLOSE_UNSUPPORTED = 4400


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


@dataclass(frozen=True)
class StreamLimits:
    max_instruments: int = 25
    flush_ms: int = 250
    queue_max: int = 256
    live_max_s: float = 5.0
    delayed_max_s: float = 30.0
    owner_renew_s: float = 20.0
    idle_timeout_s: float = 300.0
    heartbeat_s: float = 15.0
    state_s: float = 5.0

    @classmethod
    def from_env(cls) -> "StreamLimits":
        return cls(
            max_instruments=max(1, _env_int(ENV_MAX_INSTRUMENTS, 25)),
            flush_ms=max(50, _env_int(ENV_FLUSH_MS, 250)),
            queue_max=max(16, _env_int(ENV_QUEUE_MAX, 256)),
            live_max_s=max(0.5, _env_float(ENV_LIVE_MAX_S, 5.0)),
            delayed_max_s=max(1.0, _env_float(ENV_DELAYED_MAX_S, 30.0)),
            owner_renew_s=max(5.0, _env_float(ENV_OWNER_RENEW_S, 20.0)),
            idle_timeout_s=max(30.0, _env_float(ENV_IDLE_TIMEOUT_S, 300.0)),
        )

    def to_wire(self) -> Dict[str, Any]:
        return {
            "max_instruments": self.max_instruments,
            "flush_ms": self.flush_ms,
            "live_max_s": self.live_max_s,
            "delayed_max_s": self.delayed_max_s,
        }


# ---------------------------------------------------------------------------
# session state
# ---------------------------------------------------------------------------

#: Exchange trading windows, in IST. NSE is additionally calendar-checked; MCX
#: and currency have no imported holiday calendar on this platform, so the window
#: is the only honest thing to claim and the state stays ``unknown`` when the
#: window cannot be consulted.
SESSION_WINDOWS: Dict[str, tuple] = {
    "NSE": (time(9, 15), time(15, 30)),
    "MCX": (time(9, 0), time(23, 30)),
    "CDS": (time(9, 0), time(17, 0)),
    "BCD": (time(9, 0), time(17, 0)),
}

#: Exchange → the session policy name the rest of the platform uses.
SESSION_BY_EXCHANGE: Dict[str, str] = {
    "NSE": "nse_equity",
    "MCX": "mcx_commodity",
    "CDS": "currency",
    "BCD": "currency",
}

_CALENDAR_CACHE_TTL_S = 60.0
#: (exchange, day) → (session dates, populated_at). Read-only for the stream:
#: the database lookup happens off the event loop (see ``refresh_calendar_cache``)
#: so a slow or unavailable calendar degrades to ``unknown`` instead of stalling
#: every connection's tick fan-out.
_calendar_cache: Dict[str, tuple] = {}


def _default_calendar_loader(exchange: str, day) -> Optional[Set[str]]:
    """Today's verified session dates for ``exchange``/``CM``, or None.

    ``None`` means "the calendar could not answer" — the caller must not read
    that as "closed". This never touches the database: it reads the cache the
    hub keeps warm.
    """
    if str(exchange or "").strip().upper() != "NSE":
        return None
    key = f"{exchange.upper()}:{day.isoformat()}"
    cached = _calendar_cache.get(key)
    if cached is None:
        return None
    now = datetime.now(timezone.utc)
    if (now - cached[1]).total_seconds() >= _CALENDAR_CACHE_TTL_S * 4:
        # Long-expired data is worse than no data: it could claim a session
        # state for the wrong day.
        return None
    return cached[0]


def refresh_calendar_cache(exchange: str = "NSE", *, day: Optional[Any] = None) -> bool:
    """Populate the session-date cache. Blocking; call it from a worker thread."""
    code = str(exchange or "").strip().upper()
    target_day = day or datetime.now(timezone.utc).astimezone(IST).date()
    try:
        from backend.app.database import get_db_connection
        from backend.broker_api.market.exchange_calendar import get_calendar_sessions

        conn = get_db_connection()
        try:
            payload = get_calendar_sessions(
                conn, exchange=code, segment="CM", from_date=target_day, to_date=target_day
            )
        finally:
            conn.close()
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        dates = {
            str(item.get("session_date"))
            for item in (sessions or [])
            if str(item.get("session_type") or "REGULAR") in {"REGULAR", "SPECIAL"}
        }
        _calendar_cache[f"{code}:{target_day.isoformat()}"] = (dates, datetime.now(timezone.utc))
        return True
    except Exception:
        logger.debug("Calendar refresh failed for %s", code, exc_info=True)
        return False


def exchange_session_state(
    exchange: str,
    *,
    now: Optional[datetime] = None,
    calendar_loader: Optional[Callable[[str, Any], Optional[Set[str]]]] = None,
) -> Dict[str, Any]:
    """Whether ``exchange`` is trading right now, and on what basis.

    Returns ``{"state": open|closed|unknown, "session": <policy>, "basis": ...}``.
    ``unknown`` is a real answer: it is what we say when the calendar is
    unreadable rather than pretending the market is closed.
    """
    code = str(exchange or "").strip().upper()
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    session = SESSION_BY_EXCHANGE.get(code)
    window = SESSION_WINDOWS.get(code)
    if window is None:
        return {"state": SESSION_UNKNOWN, "session": session, "basis": "unknown_exchange"}

    opens_at, closes_at = window
    in_window = opens_at <= moment.time() <= closes_at
    loader = calendar_loader or _default_calendar_loader

    if code == "NSE":
        dates = loader(code, moment.date())
        if dates is None:
            # The calendar is the authority for NSE; without it we do not claim
            # a session state at all.
            return {"state": SESSION_UNKNOWN, "session": session, "basis": "calendar_unavailable"}
        if moment.date().isoformat() not in dates:
            return {"state": SESSION_CLOSED, "session": session, "basis": "calendar_holiday"}
        return {
            "state": SESSION_OPEN if in_window else SESSION_CLOSED,
            "session": session,
            "basis": "calendar",
        }

    # Feed-driven exchanges: no holiday calendar exists, so the window decides.
    return {
        "state": SESSION_OPEN if in_window else SESSION_CLOSED,
        "session": session,
        "basis": "session_window",
    }


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def classify_freshness(*, age_s: Optional[float], session_state: str) -> str:
    """The single freshness vocabulary (see the design document).

    Order matters: a closed market outranks age (an old price on a closed market
    is "market closed", not "stale"), and no data is reported before anything
    else because there is nothing to age.
    """
    if age_s is None:
        return FRESHNESS_NO_DATA
    if session_state == SESSION_CLOSED:
        return FRESHNESS_CLOSED
    limits = _LIMITS
    if age_s <= limits.live_max_s and session_state == SESSION_OPEN:
        return FRESHNESS_LIVE
    if age_s <= limits.delayed_max_s:
        return FRESHNESS_DELAYED
    return FRESHNESS_STALE


_LIMITS = StreamLimits()


def set_limits(limits: StreamLimits) -> None:
    """Install process limits (startup + tests)."""
    global _LIMITS
    _LIMITS = limits


def current_limits() -> StreamLimits:
    return _LIMITS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_quote(
    *,
    instrument_key: str,
    descriptor: Any,
    tick: Dict[str, Any],
    now: datetime,
    session_state: str,
    origin: str,
    limits: Optional[StreamLimits] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize one runtime tick into the frame the UI consumes.

    Field names follow the platform's existing quote payload
    (``MarketDataService._quote_payload``) so the UI has one shape to learn, and
    the alerts-specific additions (canonical key, freshness, session state,
    catalog generation) are additive.
    """
    if not isinstance(tick, dict):
        return None
    last_price = tick.get("last_price")
    if last_price is None:
        return None
    try:
        price = float(last_price)
    except (TypeError, ValueError):
        return None

    active = limits or _LIMITS
    received_at = _parse_time(tick.get("received_at")) or now
    exchange_timestamp = _parse_time(tick.get("exchange_timestamp"))
    age_s = max(0.0, (now - received_at).total_seconds())

    ohlc = tick.get("ohlc") if isinstance(tick.get("ohlc"), dict) else None
    previous_close = None
    if ohlc and ohlc.get("close") is not None:
        try:
            previous_close = float(ohlc["close"])
        except (TypeError, ValueError):
            previous_close = None

    change_absolute: Optional[float] = None
    raw_change = tick.get("change")
    if raw_change is not None:
        try:
            candidate = float(raw_change)
        except (TypeError, ValueError):
            candidate = None
        # ``change`` is the day change in quote mode and is 0 in pure LTP mode,
        # where the OHLC close is the honest baseline.
        if candidate not in (None, 0.0):
            change_absolute = candidate
    if change_absolute is None and previous_close:
        change_absolute = price - previous_close

    change_percent: Optional[float] = None
    if change_absolute is not None and previous_close:
        change_percent = 100.0 * (change_absolute / previous_close)

    return {
        "instrument_key": instrument_key,
        "broker_token": int(getattr(descriptor, "broker_token", 0) or tick.get("instrument_token") or 0),
        "catalog_generation": getattr(descriptor, "catalog_generation", None),
        "exchange": getattr(descriptor, "exchange", None) or tick.get("exchange"),
        "last_price": price,
        "change_absolute": change_absolute,
        "change_percent": change_percent,
        "ohlc": ohlc,
        "volume": tick.get("volume_traded", tick.get("volume")),
        "exchange_timestamp": exchange_timestamp.isoformat() if exchange_timestamp else None,
        "received_at": received_at.isoformat(),
        "server_time": now.isoformat(),
        "age_ms": int(age_s * 1000),
        "session_state": session_state,
        "freshness": classify_freshness(age_s=age_s, session_state=session_state),
        "origin": origin,
        "_age_s": age_s,
    }


def strip_internal(frame: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in frame.items() if not key.startswith("_")}


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------


class MarketStreamHub:
    """One Redis ``market:ticks`` subscription, fanned out to local connections."""

    def __init__(
        self,
        *,
        redis_client: Any = None,
        limits: Optional[StreamLimits] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._redis = redis_client
        self.limits = limits or _LIMITS
        self._clock = clock
        self._connections: Set[Any] = set()
        self._task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._calendar_task: Optional[asyncio.Task] = None
        self._running = False
        self.health: Dict[str, Any] = {
            "started": False,
            "connections": 0,
            "ticks_seen": 0,
            "ticks_fanned_out": 0,
            "last_tick_at": None,
            "last_error": None,
        }

    # -- redis -----------------------------------------------------------

    def _redis_client(self) -> Any:
        if self._redis is None:
            from backend.broker_api.core.redis_events import get_redis

            self._redis = get_redis()
        return self._redis

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.health["started"] = True
        self._task = asyncio.create_task(self._ticks_loop())
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._calendar_task = asyncio.create_task(self._calendar_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._flush_task, self._calendar_task):
            if task is not None:
                task.cancel()
        for task in (self._task, self._flush_task, self._calendar_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._flush_task = None
        self._calendar_task = None
        self.health["started"] = False

    def add_connection(self, connection: Any) -> None:
        self._connections.add(connection)
        self.health["connections"] = len(self._connections)

    def remove_connection(self, connection: Any) -> None:
        self._connections.discard(connection)
        self.health["connections"] = len(self._connections)

    def connections(self) -> List[Any]:
        return list(self._connections)

    # -- ticks -----------------------------------------------------------

    async def _ticks_loop(self) -> None:
        pubsub = None
        retry_delay = 1.0
        while self._running:
            try:
                if pubsub is None:
                    pubsub = self._redis_client().pubsub()
                    await pubsub.subscribe(RUNTIME_TICKS_CHANNEL)
                    retry_delay = 1.0
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message or message.get("type") != "message":
                    continue
                raw = message.get("data")
                payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                self.dispatch_tick(payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # redis down, malformed payload, ...
                self.health["last_error"] = f"{type(exc).__name__}"
                logger.warning("Market stream hub lost its tick subscription: %s", exc)
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                    pubsub = None
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(RUNTIME_TICKS_CHANNEL)
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass

    def dispatch_tick(self, payload: Any) -> None:
        """Fan one tick out to the connections that asked for its token."""
        if not isinstance(payload, dict):
            return
        token = payload.get("instrument_token")
        if token is None:
            return
        try:
            token_int = int(token)
        except (TypeError, ValueError):
            return
        self.health["ticks_seen"] = int(self.health["ticks_seen"]) + 1
        self.health["last_tick_at"] = self._clock().isoformat()
        for connection in self._connections:
            if token_int in connection.tokens():
                connection.offer_tick(token_int, payload)
                self.health["ticks_fanned_out"] = int(self.health["ticks_fanned_out"]) + 1

    async def _flush_loop(self) -> None:
        interval = max(0.05, self.limits.flush_ms / 1000.0)
        while self._running:
            try:
                await asyncio.sleep(interval)
                for connection in self._connections:
                    try:
                        connection.flush()
                    except Exception:
                        logger.warning("Market stream flush failed for a connection", exc_info=True)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Market stream flush loop failed", exc_info=True)

    async def _calendar_loop(self) -> None:
        """Keep the NSE session-date cache warm without blocking the loop."""
        while self._running:
            try:
                await asyncio.to_thread(refresh_calendar_cache, "NSE")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Calendar cache refresh failed", exc_info=True)
            try:
                await asyncio.sleep(_CALENDAR_CACHE_TTL_S)
            except asyncio.CancelledError:
                break

    def snapshot(self) -> Dict[str, Any]:
        return dict(self.health)


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


class OperatorMarketStream:
    """One browser connection: subscriptions, snapshots, coalescing, cleanup."""

    def __init__(
        self,
        websocket: Any,
        *,
        hub: MarketStreamHub,
        scope: str,
        connection_id: Optional[str] = None,
        catalog: Any = None,
        runtime_client_factory: Optional[Callable[[], Any]] = None,
        redis_client: Any = None,
        limits: Optional[StreamLimits] = None,
        clock: Callable[[], datetime] = _utcnow,
        calendar_loader: Optional[Callable[[str, Any], Optional[Set[str]]]] = None,
    ) -> None:
        self.websocket = websocket
        self.hub = hub
        self.scope = scope
        self.connection_id = connection_id or str(uuid.uuid4())
        self.owner_id = f"alerts-ui:{scope}:{self.connection_id}"
        self.limits = limits or hub.limits
        self._clock = clock
        self._calendar_loader = calendar_loader
        self._catalog = catalog
        self._runtime_client_factory = runtime_client_factory
        self._redis = redis_client

        self._descriptors: Dict[str, Any] = {}
        self._token_to_key: Dict[int, str] = {}
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._last_sent: Dict[int, str] = {}
        #: token -> when its newest quote was produced, so an instrument that
        #: stops ticking ages visibly instead of freezing at its last label.
        self._last_quote_at: Dict[int, datetime] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.limits.queue_max)
        self._tasks: List[asyncio.Task] = []
        self._stopping = False
        self._owner_dirty = False
        self._owner_created = False
        self._last_client_at = self._clock()
        self.health: Dict[str, Any] = {
            "connection_id": self.connection_id,
            "scope": scope,
            "instruments": 0,
            "frames_sent": 0,
            "ticks_coalesced": 0,
            "ticks_dropped": 0,
            "backpressure_events": 0,
            "owner_sync_failures": 0,
            "closed_reason": None,
        }

    # -- helpers ---------------------------------------------------------

    def tokens(self) -> Set[int]:
        return set(self._token_to_key.keys())

    def _redis_client(self) -> Any:
        if self._redis is None:
            from backend.broker_api.core.redis_events import get_redis

            self._redis = get_redis()
        return self._redis

    def _catalog_client(self) -> Any:
        if self._catalog is None:
            from backend.broker_api.instruments.catalog import InstrumentCatalog

            self._catalog = InstrumentCatalog()
        return self._catalog

    def _runtime(self) -> Any:
        if self._runtime_client_factory is not None:
            return self._runtime_client_factory()
        from backend.broker_api.orders.market_runtime_client import get_market_runtime_client

        return get_market_runtime_client()

    def session_state_for(self, instrument_key: str) -> Dict[str, Any]:
        exchange = str(instrument_key or "").partition(":")[0].strip().upper()
        return exchange_session_state(
            exchange, now=self._clock(), calendar_loader=self._calendar_loader
        )

    # -- framing ---------------------------------------------------------

    def enqueue(self, frame: Dict[str, Any], *, kind: str = "control") -> bool:
        """Queue one frame; tick frames are dropped first under pressure."""
        payload = strip_internal(frame) if kind == "tick" else frame
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            pass
        # Queue is full: make room by discarding the oldest tick frame. Control
        # frames are never discarded — they carry state the UI must not miss.
        self.health["backpressure_events"] = int(self.health["backpressure_events"]) + 1
        dropped = self._drop_oldest_tick()
        if dropped or kind == "tick":
            self.health["ticks_dropped"] = int(self.health["ticks_dropped"]) + 1
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            return False

    def _drop_oldest_tick(self) -> bool:
        kept: List[Dict[str, Any]] = []
        dropped = False
        while True:
            try:
                frame = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not dropped and frame.get("type") == "quote":
                dropped = True
                continue
            kept.append(frame)
        for frame in kept:
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                break
        return dropped

    async def _send(self, payload: Dict[str, Any]) -> None:
        self.health["frames_sent"] = int(self.health["frames_sent"]) + 1
        await self.websocket.send_json(payload)

    # -- hub callbacks ---------------------------------------------------

    def offer_tick(self, token: int, tick: Dict[str, Any]) -> None:
        key = self._token_to_key.get(int(token))
        if key is None:
            return
        descriptor = self._descriptors.get(key)
        now = self._clock()
        quote = build_quote(
            instrument_key=key,
            descriptor=descriptor,
            tick=tick,
            now=now,
            session_state=self.session_state_for(key)["state"],
            origin="tick",
            limits=self.limits,
        )
        if quote is None:
            return
        existing = self._pending.get(int(token))
        if existing is not None:
            self.health["ticks_coalesced"] = int(self.health["ticks_coalesced"]) + 1
        self._pending[int(token)] = quote

    def flush(self) -> None:
        """Emit the newest value per instrument, plus freshness transitions."""
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        for token, quote in pending.items():
            self.enqueue({"type": "quote", **quote}, kind="tick")
            self._last_quote_at[token] = self._clock()
            state = quote["freshness"]
            if self._last_sent.get(token) != state:
                self._last_sent[token] = state
                self.enqueue(
                    {
                        "type": "state",
                        "instruments": {
                            quote["instrument_key"]: {
                                "freshness": state,
                                "age_ms": quote["age_ms"],
                                "session_state": quote["session_state"],
                            }
                        },
                    }
                )

    def refresh_states(self) -> None:
        """Age out instruments that stopped ticking (no new ticks to piggyback).

        A price that stops arriving must not keep its last label: this is what
        turns a live row into DELAYED/STALE without waiting for the next tick,
        and what shows MARKET CLOSED when the session ends.
        """
        now = self._clock()
        states: Dict[str, Any] = {}
        for token, key in self._token_to_key.items():
            if token in self._pending:
                continue
            session = self.session_state_for(key)["state"]
            last = self._last_quote_at.get(token)
            age_s = max(0.0, (now - last).total_seconds()) if last is not None else None
            state = classify_freshness(age_s=age_s, session_state=session)
            if self._last_sent.get(token) != state:
                self._last_sent[token] = state
                states[key] = {
                    "freshness": state,
                    "session_state": session,
                    "age_ms": int(age_s * 1000) if age_s is not None else None,
                }
        if states:
            self.enqueue({"type": "state", "instruments": states})

    # -- subscription changes -------------------------------------------

    async def subscribe(self, keys: Sequence[str]) -> Dict[str, Any]:
        """Resolve, cap, snapshot and register the requested instruments."""
        requested = [str(key).strip() for key in keys if str(key).strip()]
        errors: List[Dict[str, Any]] = []
        resolved: List[str] = []

        for key in requested:
            if key in self._descriptors or key in self._token_to_key.values():
                resolved.append(key)
                continue
            if ":" not in key:
                # Canonical keys only: a bare symbol would have to be guessed,
                # and guessing is how the wrong instrument gets streamed.
                errors.append(
                    {
                        "type": "error",
                        "code": "INSTRUMENT_KEY_NOT_CANONICAL",
                        "instrument": key,
                        "message": "use a canonical EXCHANGE:SYMBOL instrument key",
                    }
                )
                continue
            try:
                descriptor = self._catalog_client().resolve_public_key(key)
            except Exception as exc:
                errors.append(
                    {
                        "type": "error",
                        "code": "INSTRUMENT_UNKNOWN",
                        "instrument": key,
                        "message": f"{type(exc).__name__}",
                    }
                )
                continue
            lifecycle = str(getattr(descriptor, "lifecycle_status", "") or "").lower()
            if lifecycle and lifecycle != "active":
                errors.append(
                    {
                        "type": "error",
                        "code": "INSTRUMENT_INACTIVE",
                        "instrument": key,
                        "message": f"catalog lifecycle is {lifecycle!r}",
                    }
                )
                continue
            self._descriptors[key] = descriptor
            resolved.append(key)

        subscribed = set(self._token_to_key.values())
        new_keys = [key for key in resolved if key not in subscribed]
        room = max(0, self.limits.max_instruments - len(self._token_to_key))
        accepted_new = new_keys[:room]
        for key in new_keys[room:]:
            self._descriptors.pop(key, None)
            errors.append(
                {
                    "type": "error",
                    "code": "INSTRUMENT_LIMIT",
                    "instrument": key,
                    "message": f"at most {self.limits.max_instruments} instruments per connection",
                }
            )

        for key in accepted_new:
            token = self._token_for(key)
            self._token_to_key[token] = key
        self.health["instruments"] = len(self._token_to_key)

        for error in errors:
            self.enqueue(error)

        accepted = sorted(set(resolved) & set(self._token_to_key.values()))
        if accepted_new:
            await self._prime_snapshots(accepted_new)
            await self.sync_owner()

        self.enqueue(
            {
                "type": "subscriptions",
                "instruments": accepted,
                "count": len(self._token_to_key),
                "max_instruments": self.limits.max_instruments,
            }
        )
        return {"accepted": accepted, "rejected": [error.get("instrument") for error in errors]}

    def _token_for(self, key: str) -> int:
        descriptor = self._descriptors.get(key)
        return int(getattr(descriptor, "broker_token", 0) or 0)

    def _token_to_key_token(self, key: str) -> int:
        descriptor = self._descriptors.get(key)
        return int(getattr(descriptor, "broker_token", 0) or 0)

    def _token_to_key_key(self) -> Set[str]:
        return set(self._token_to_key.values())

    async def unsubscribe(self, keys: Sequence[str]) -> Dict[str, Any]:
        removed: List[str] = []
        for key in [str(item).strip() for item in keys if str(item).strip()]:
            descriptor = self._descriptors.pop(key, None)
            if descriptor is None:
                continue
            token = int(getattr(descriptor, "broker_token", 0) or 0)
            self._token_to_key.pop(token, None)
            self._pending.pop(token, None)
            self._last_sent.pop(token, None)
            self._last_quote_at.pop(token, None)
            removed.append(key)
        self.health["instruments"] = len(self._token_to_key)
        if removed:
            self._owner_dirty = True
            await self.sync_owner()
        return {"removed": removed}

    async def _prime_snapshots(self, keys: Sequence[str]) -> None:
        """Send whatever the runtime last published, with its real age."""
        tokens = [self._token_for(key) for key in keys]
        pairs = [(token, self._token_to_key.get(token)) for token in tokens]
        raw_values: List[Any] = []
        try:
            raw_values = await self._redis_client().mget(
                [RUNTIME_TICK_KEY.format(token) for token, _ in pairs]
            )
        except Exception:
            logger.debug("Snapshot priming failed", exc_info=True)
            raw_values = []
        now = self._clock()
        for (token, key), raw in zip(pairs, raw_values):
            if key is None or raw is None:
                continue
            try:
                tick = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except (TypeError, ValueError):
                continue
            quote = build_quote(
                instrument_key=key,
                descriptor=self._descriptors.get(key),
                tick=tick,
                now=now,
                session_state=self.session_state_for(key)["state"],
                origin="snapshot",
                limits=self.limits,
            )
            if quote is not None:
                self.enqueue({"type": "quote", **quote}, kind="tick")

    async def sync_owner(self) -> None:
        """Register the complete token set with the runtime (single PUT)."""
        tokens = {token: "ltp" for token in self._token_to_key}
        try:
            runtime = self._runtime()
            await runtime.set_owner_subscriptions(self.owner_id, tokens)
            self._owner_created = bool(tokens)
            self._owner_dirty = False
        except Exception as exc:
            self.health["owner_sync_failures"] = int(self.health["owner_sync_failures"]) + 1
            logger.warning("Market stream owner sync failed for %s: %s", self.owner_id, exc)
            self.enqueue(
                {
                    "type": "error",
                    "code": "RUNTIME_UNAVAILABLE",
                    "message": "the market runtime did not accept the subscription",
                }
            )

    async def release_owner(self) -> None:
        if not self._owner_created and not self._token_to_key:
            return
        try:
            runtime = self._runtime()
            await runtime.delete_owner(self.owner_id)
        except Exception as exc:
            # The runtime's owner lease TTL is the second safety net.
            logger.warning("Market stream owner release failed for %s: %s", self.owner_id, exc)
        self._owner_created = False

    # -- loops -----------------------------------------------------------

    async def sender_loop(self) -> None:
        while not self._stopping:
            payload = await self._queue.get()
            try:
                await self._send(payload)
            except asyncio.CancelledError:
                break
            except Exception:
                # A failed send means the socket is gone: stop the connection so
                # the owner is released instead of lingering until its lease
                # expires.
                self._stopping = True
                try:
                    await self.websocket.close(code=1011, reason="send failed")
                except Exception:
                    pass
                break

    async def heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.limits.heartbeat_s)
            except asyncio.CancelledError:
                break
            self.enqueue(
                {
                    "type": "heartbeat",
                    "server_time": self._clock().isoformat(),
                    "runtime": self.hub.snapshot(),
                }
            )

    async def state_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.limits.state_s)
            except asyncio.CancelledError:
                break
            self.refresh_states()

    async def renewal_loop(self) -> None:
        """Keep the runtime owner lease alive while this connection is open."""
        while not self._stopping:
            try:
                await asyncio.sleep(self.limits.owner_renew_s)
            except asyncio.CancelledError:
                break
            if self._token_to_key:
                await self.sync_owner()

    async def idle_watchdog(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            idle = (self._clock() - self._last_client_at).total_seconds()
            if idle > self.limits.idle_timeout_s:
                self.health["closed_reason"] = "idle_timeout"
                try:
                    await self.websocket.close(code=CLOSE_IDLE, reason="idle")
                except Exception:
                    pass
                break

    # -- main ------------------------------------------------------------

    async def run(self) -> None:
        """Serve one browser connection until it goes away."""
        self.hub.add_connection(self)
        self._tasks = [
            asyncio.create_task(self.sender_loop()),
            asyncio.create_task(self.heartbeat_loop()),
            asyncio.create_task(self.state_loop()),
            asyncio.create_task(self.renewal_loop()),
            asyncio.create_task(self.idle_watchdog()),
        ]
        try:
            self.enqueue(
                {
                    "type": "welcome",
                    "connection_id": self.connection_id,
                    "scope": self.scope,
                    "limits": self.limits.to_wire(),
                    "runtime": self.hub.snapshot(),
                    "server_time": self._clock().isoformat(),
                }
            )
            while not self._stopping:
                try:
                    payload = await self.websocket.receive_json()
                except Exception:
                    break
                self._last_client_at = self._clock()
                await self.handle_message(payload)
        finally:
            self._stopping = True
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            self.hub.remove_connection(self)
            await self.release_owner()

    async def handle_message(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            self.enqueue(
                {"type": "error", "code": "BAD_FRAME", "message": "expected a JSON object"}
            )
            return
        kind = str(payload.get("type") or "").strip().lower()
        if kind == "subscribe":
            await self.subscribe(payload.get("instruments") or [])
        elif kind == "unsubscribe":
            await self.unsubscribe(payload.get("instruments") or [])
        elif kind == "ping":
            self.enqueue({"type": "pong", "server_time": self._clock().isoformat()})
        elif kind == "status":
            self.enqueue(
                {
                    "type": "state",
                    "instruments": {},
                    "runtime": self.hub.snapshot(),
                    "server_time": self._clock().isoformat(),
                }
            )
        else:
            self.enqueue(
                {
                    "type": "error",
                    "code": "UNSUPPORTED_FRAME",
                    "message": f"unsupported message type {kind!r}",
                }
            )


_hub: Optional[MarketStreamHub] = None
_hub_lock = asyncio.Lock()


async def get_market_stream_hub() -> MarketStreamHub:
    """The process-wide hub (one Redis subscription, many connections)."""
    global _hub
    if _hub is not None:
        return _hub
    async with _hub_lock:
        if _hub is None:
            _hub = MarketStreamHub(limits=StreamLimits.from_env())
            set_limits(_hub.limits)
            await _hub.start()
    return _hub


def hub_snapshot() -> Optional[Dict[str, Any]]:
    return _hub.snapshot() if _hub is not None else None
