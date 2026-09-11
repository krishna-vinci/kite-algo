"""Runtime feeds + supervised evaluation worker loop (Task 7).

Feed protocols (unit tests inject fakes; redis/postgres are never touched):

- ``TickSource`` — async stream of :class:`Observation` (ticks or completed
  candles): ``start()`` / ``next_observation()`` / ``stop()``.
- ``CandleHistory`` — synchronous warmup reads: ``recent_bars(instrument_key,
  timeframe, limit) -> list[Observation]`` in ascending bar order.

Redis-backed sources (import ``redis`` lazily inside their constructors so
this module stays import-safe without redis):

- ``RedisTickSource`` consumes the ``market:ticks`` pub/sub channel published
  by the Go market-runtime (payload fields ``instrument_token``,
  ``last_price``, ``exchange_timestamp`` — see
  ``backend/broker_api/market/candle_aggregator.py``). Each worker boot gets
  a fresh ``epoch_id`` (uuid4): a restart starts a new observation epoch and
  can never manufacture a crossing (E-5). The factory must mint a NEW source
  (hence a NEW epoch) on every call — the worker relies on this when it
  rebuilds a source after a feed outage (spec D2).
- ``RedisCandleSource`` consumes the aggregator's completed-candle channel
  ``realtime_candles:{token}:{interval}`` (payload ``{"event": "candle",
  "instrument_token", "interval", "candle": [ts, o, h, l, c, v, oi?]}``) and
  maps it to a final ``Observation`` with the stable ``"candle"`` epoch id.

``PgCandleHistory`` reads completed candles from the
``public.historical_candles`` table maintained by
``backend/broker_api/market/candle_storage.py`` (columns ``instrument_token,
interval, ts, open, high, low, close, volume``) using a raw ``text()`` query
on a provided engine.

``EvaluationWorker`` materializes subscriptions for active revisions, warms
candle rules from history BEFORE processing live events (spec F2), dispatches
observations to the matching subscriptions, and keeps health counters
(last_evaluated_at, evaluations, emitted, suppressed-by-reason, gaps,
warmups, unresolved_channels, renewal_failures) for F11. A caller-provided
``channel_resolver`` maps ``(owner_id, channel names) -> {name: channel_id}``;
the worker wraps it to count unresolved names in health. Shutdown is
graceful on cancellation.

Supervision (hardening):

- subscription REFRESH: every ``refresh_interval_s`` the worker re-reads
  ``workflow_repo.list_active_subscriptions()`` and diffs against the
  in-memory dispatch tables — newly activated subscriptions are added and
  warmed (candle rules) before live dispatch, paused/archived ones are
  dropped; activation changes never require a restart.
- FEED OUTAGE RECOVERY: an exception from a feed source increments health
  ``gaps``, tears the source down and schedules a rebuild with a NEW ltp
  epoch boot id (via the source factory) after ``source_rebuild_backoff_s``
  (default 5s); the main loop survives repeated outages.
- OWNERSHIP RENEWAL: market-runtime leases expire unrenewed owners after
  ~90s (``MARKET_RUNTIME_OWNER_LEASE_TTL_SEC``); when a ``renewal``
  callable is provided it is awaited every ``renewal_interval_s``
  (default TTL/3). Renewal failures are counted and logged, never fatal.
- HEALTH REPORTING: every ``health_interval_s`` (default 30s) the worker
  logs one INFO line and, when ``ALERTS_HEALTH_FILE`` is set, atomically
  writes the JSON health snapshot (including any ``health_extra`` dict,
  e.g. delivery counters) to that file.
- WARMUP SAFETY: warmup replay always calls
  ``service.handle_observation(..., allow_emit=False)`` (pinned contract)
  and skips bars at or before the checkpoint's ``last_bar_ts`` replay
  boundary, so warming can never emit historical notifications; warmup
  bars never touch the live queues.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import uuid
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from zoneinfo import ZoneInfo

from backend.alerts.predicates import Observation
from backend.workflows.feature_engine import FeatureEngine, FeatureSpec  # noqa: F401 (re-export)
from backend.workflows.feature_planner import (
    SubscriptionPlan,
    build_subscription_plan,
    stage_chain,
)
from backend.workflows import external_context, pairs
from backend.workflows.instrument_bindings import BindingChange, InstrumentBindingRegistry
from backend.workflows.models import Stage, WorkflowDocument
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict
from backend.workflows import registry
from backend.workflows.repository import (
    ActiveSubscription,
    SqlAlchemyWorkflowRepository,
    WorkflowRevision,
)

__all__ = [
    "TickSource",
    "CandleHistory",
    "RedisTickSource",
    "RedisCandleSource",
    "PgCandleHistory",
    "build_nse_session_provider",
    "build_market_session_provider",
    "EvaluationWorker",
    "InstrumentBindingRegistry",
    "MARKET_TICKS_CHANNEL",
    "REALTIME_CANDLES_CHANNEL_PREFIX",
    "CANDLE_EPOCH_ID",
    "MARKET_RUNTIME_OWNER_LEASE_TTL_S",
]

logger = logging.getLogger(__name__)

MARKET_TICKS_CHANNEL = "market:ticks"
# The candle aggregator publishes completions to realtime_candles:{token}:{interval}
REALTIME_CANDLES_CHANNEL_PREFIX = "realtime_candles:"
CANDLE_EPOCH_ID = "candle"

# market-runtime expires unrenewed owner leases after this long
# (market-runtime/internal/config/config.go: MARKET_RUNTIME_OWNER_LEASE_TTL_SEC).
# The worker renews at TTL/3 so one missed renewal never expires the lease.
MARKET_RUNTIME_OWNER_LEASE_TTL_S = 90.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(acquired_at_iso: str, *, now: Optional[datetime] = None) -> float:
    """Hours since an ISO-8601 acquisition timestamp; ``inf`` when unparseable
    (stale-by-default: absent/legacy metadata never reads as fresh)."""
    try:
        acquired = datetime.fromisoformat(str(acquired_at_iso))
    except (TypeError, ValueError):
        return float("inf")
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    reference = now or _utcnow()
    return max(0.0, (reference - acquired).total_seconds() / 3600.0)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Normalize an exchange event timestamp; never fabricate one."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(raw, (int, float)):
        if isinstance(raw, bool) or not math.isfinite(float(raw)):
            return None
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# protocols
# ---------------------------------------------------------------------------


class TickSource(Protocol):
    """Async stream of observations (ticks or completed candles)."""

    async def start(self) -> None: ...

    async def next_observation(self) -> Optional[Observation]:
        """Return the next observation, or None when nothing is pending."""
        ...

    async def stop(self) -> None: ...


class CandleHistory(Protocol):
    """Synchronous warmup reads of completed candles (ascending by ts)."""

    def recent_bars(self, instrument_key: str, timeframe: str, limit: int) -> List[Observation]: ...

    def previous_session_levels(
        self, instrument_key: str, at: datetime
    ) -> Optional[Dict[str, float]]: ...


# ---------------------------------------------------------------------------
# redis-backed sources (redis imported lazily, never in unit tests)
# ---------------------------------------------------------------------------


def _default_redis_client():
    import redis.asyncio as aioredis  # lazy: keeps this module redis-free

    return aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )


class RedisTickSource:
    """Consumes ``market:ticks`` and maps tokens to instrument keys.

    The ``token_to_instrument`` dict restricts which ticks this source emits:
    ``{instrument_token: instrument_key}``. The epoch id is a per-construction
    uuid, so every worker boot opens a fresh ltp epoch (E-5).
    """

    def __init__(
        self,
        redis_client: Any = None,
        token_to_instrument: Optional[Dict[int, str]] = None,
        *,
        channel: str = MARKET_TICKS_CHANNEL,
    ) -> None:
        if redis_client is None:
            redis_client = _default_redis_client()
        self._redis = redis_client
        self._tokens: Dict[int, str] = {
            int(token): str(key) for token, key in (token_to_instrument or {}).items()
        }
        self._channel = channel
        self._epoch_id = str(uuid.uuid4())
        self._pubsub: Any = None
        self._started = False

    @property
    def epoch_id(self) -> str:
        return self._epoch_id

    async def start(self) -> None:
        if self._started:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._started = True

    async def next_observation(self) -> Optional[Observation]:
        if not self._started or self._pubsub is None:
            return None
        while True:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message.get("type") != "message":
                return None
            try:
                payload = json.loads(message.get("data"))
            except (TypeError, ValueError):
                logger.warning("discarding malformed tick payload")
                continue
            token = payload.get("instrument_token", payload.get("token"))
            try:
                token = int(token)
            except (TypeError, ValueError):
                continue
            instrument_key = self._tokens.get(token)
            if instrument_key is None:
                continue  # not in this worker's monitored set
            ltp = payload.get("last_price", payload.get("ltp"))
            if ltp is None:
                continue
            ts = _parse_ts(payload.get("exchange_timestamp", payload.get("ts")))
            try:
                ltp_value = float(ltp)
            except (TypeError, ValueError):
                continue
            if ts is None or not math.isfinite(ltp_value):
                logger.warning("discarding tick with missing/invalid exchange timestamp or ltp")
                continue
            return Observation(ts=ts, epoch_id=self._epoch_id, ltp=ltp_value)

    async def stop(self) -> None:
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(self._channel)
            except Exception:
                pass
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
        self._started = False


class RedisCandleSource:
    """Consumes completed candles from ``realtime_candles:{token}:{interval}``.

    Messages carry ``{"event": "candle", "instrument_token", "interval",
    "candle": [ts, open, high, low, close, volume, oi?]}``. Observations are
    final candles stamped with the stable ``"candle"`` epoch. When
    ``interval`` is given, only completions of that timeframe are emitted.
    """

    def __init__(
        self,
        redis_client: Any = None,
        token_to_instrument: Optional[Dict[int, str]] = None,
        *,
        interval: Optional[str] = None,
        channel_pattern: str = REALTIME_CANDLES_CHANNEL_PREFIX + "*",
    ) -> None:
        if redis_client is None:
            redis_client = _default_redis_client()
        self._redis = redis_client
        self._tokens: Dict[int, str] = {
            int(token): str(key) for token, key in (token_to_instrument or {}).items()
        }
        self._interval = interval
        self._pattern = channel_pattern
        self._pubsub: Any = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(self._pattern)
        self._started = True

    async def next_observation(self) -> Optional[Observation]:
        if not self._started or self._pubsub is None:
            return None
        while True:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message.get("type") not in ("message", "pmessage"):
                return None
            try:
                payload = json.loads(message.get("data"))
            except (TypeError, ValueError):
                logger.warning("discarding malformed candle payload")
                continue
            if payload.get("event") != "candle":
                continue
            interval = payload.get("interval")
            if self._interval is not None and interval != self._interval:
                continue
            try:
                token = int(payload.get("instrument_token"))
            except (TypeError, ValueError):
                continue
            instrument_key = self._tokens.get(token)
            if instrument_key is None:
                continue
            candle = payload.get("candle")
            if not isinstance(candle, list) or len(candle) < 6:
                logger.warning("discarding malformed candle body for token %s", token)
                continue
            try:
                ts = _parse_ts(candle[0])
                if ts is None:
                    raise ValueError("missing/invalid candle event timestamp")
                values = [float(value) for value in candle[1:6]]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("non-finite candle value")
                obs = Observation(
                    ts=ts,
                    epoch_id=CANDLE_EPOCH_ID,
                    ltp=values[3],
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=values[4],
                    final=True,
                )
            except (TypeError, ValueError):
                logger.warning("discarding candle with non-numeric body for token %s", token)
                continue
            return obs

    async def stop(self) -> None:
        if self._pubsub is not None:
            try:
                await self._pubsub.punsubscribe(self._pattern)
            except Exception:
                pass
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
        self._started = False


class PgCandleHistory:
    """Warmup reads of completed candles from ``public.historical_candles``.

    Built on a caller-provided SQLAlchemy engine. ``instrument_tokens`` is
    either a plain dict (snapshot copy, legacy behavior) or an
    :class:`InstrumentBindingRegistry`-like provider exposing ``get()``; with
    a provider every query resolves the CURRENT accepted binding instead of a
    startup snapshot (C1). The SQL is issued lazily so importing this module
    never touches a database.
    """

    def __init__(self, engine: Any, instrument_tokens: Any) -> None:
        self._engine = engine
        self._bindings = instrument_tokens
        self._instrument_tokens: Optional[Dict[str, int]] = (
            None
            if hasattr(instrument_tokens, "get") and hasattr(instrument_tokens, "snapshot")
            else {
                str(key): int(token)
                for key, token in (instrument_tokens or {}).items()
            }
        )

    def _token_for(self, instrument_key: str) -> Optional[int]:
        if self._instrument_tokens is not None:
            return self._instrument_tokens.get(instrument_key)
        return self._bindings.get(instrument_key)

    def recent_bars(self, instrument_key: str, timeframe: str, limit: int) -> List[Observation]:
        token = self._token_for(instrument_key)
        if token is None:
            return []
        try:
            from sqlalchemy import text  # lazy: unit tests never need this

            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT ts, open, high, low, close, volume "
                        "FROM public.historical_candles "
                        "WHERE instrument_token = :token AND interval = :interval "
                        "ORDER BY ts DESC LIMIT :limit"
                    ),
                    {"token": int(token), "interval": timeframe, "limit": int(limit)},
                ).fetchall()
        except Exception:
            logger.error(
                "candle warmup query failed for %s/%s", instrument_key, timeframe,
                exc_info=True,
            )
            return []
        bars: List[Observation] = []
        for row in reversed(rows):  # stored DESC -> return ascending
            ts = row[0]
            if isinstance(ts, datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            elif isinstance(ts, datetime):
                ts = ts.astimezone(timezone.utc)
            else:
                ts = _parse_ts(ts)
                if ts is None:
                    continue
            try:
                bars.append(
                    Observation(
                        ts=ts,
                        epoch_id=CANDLE_EPOCH_ID,
                        ltp=float(row[4]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5] or 0.0),
                        final=True,
                    )
                )
            except (TypeError, ValueError):
                continue
        return bars

    def previous_session_levels(
        self, instrument_key: str, at: datetime
    ) -> Optional[Dict[str, float]]:
        """Return levels from the previous completed daily candle.

        The query intentionally selects the latest stored trading-session date
        before the observation's local session date; subtracting one calendar
        day would be wrong across weekends and holidays.
        """
        token = self._token_for(instrument_key)
        if token is None:
            return None
        local_day = (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)).astimezone(
            ZoneInfo("Asia/Kolkata")
        ).date()
        try:
            from sqlalchemy import text

            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT high, low
                          FROM public.historical_candles
                         WHERE instrument_token = :token
                           AND interval = 'day'
                           AND (ts AT TIME ZONE 'Asia/Kolkata')::date = (
                               SELECT MAX((ts AT TIME ZONE 'Asia/Kolkata')::date)
                                 FROM public.historical_candles
                                WHERE instrument_token = :token
                                  AND interval = 'day'
                                  AND (ts AT TIME ZONE 'Asia/Kolkata')::date < :session_date
                           )
                         ORDER BY ts DESC
                         LIMIT 1
                        """
                    ),
                    {"token": int(token), "session_date": local_day},
                ).first()
        except Exception:
            logger.warning(
                "previous-session query failed for %s at %s", instrument_key, local_day,
                exc_info=True,
            )
            return None
        if row is None:
            return None
        try:
            high, low = float(row[0]), float(row[1])
            if not math.isfinite(high) or not math.isfinite(low):
                return None
            return {"prev_day_high": high, "prev_day_low": low}
        except (TypeError, ValueError):
            return None


def build_nse_session_provider(engine: Any):
    """Build a fail-closed NSE CM session/calendar resolver.

    The active operator-imported calendar is authoritative. Missing schema or
    coverage produces an inactive session rather than silently treating a
    weekend/holiday as an open market.
    """
    ist = ZoneInfo("Asia/Kolkata")
    cache: Dict[Any, Tuple[Optional[int], Optional[dict]]] = {}

    def resolve(at: datetime) -> Tuple[bool, str]:
        moment = (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)).astimezone(ist)
        day = moment.date()
        session_id = f"NSE:CM:{day.isoformat()}"
        try:
            from sqlalchemy import text

            with engine.connect() as conn:
                version = conn.execute(
                    text(
                        "SELECT MAX(calendar_version) "
                        "FROM public.exchange_calendar_source_documents "
                        "WHERE exchange = 'NSE' AND segment = 'CM'"
                    )
                ).scalar()
                version = int(version) if version is not None else None
                cached = cache.get(day)
                if cached is not None and cached[0] == version:
                    row = cached[1]
                else:
                    row = None
                    if version is not None:
                        row = conn.execute(
                            text(
                                "SELECT session_type, opens_at, closes_at, verified "
                                "FROM public.exchange_calendar_sessions "
                                "WHERE exchange = 'NSE' AND segment = 'CM' "
                                "AND calendar_version = :version AND session_date = :day"
                            ),
                            {"version": version, "day": day},
                        ).mappings().first()
                    cache[day] = (version, dict(row) if row is not None else None)
        except Exception:
            logger.warning("NSE session calendar lookup failed", exc_info=True)
            return False, f"{session_id}:calendar_unavailable"

        if not row or not row.get("verified") or str(row.get("session_type", "")).upper() == "HOLIDAY":
            return False, session_id
        opens_at, closes_at = row.get("opens_at"), row.get("closes_at")
        if opens_at is None or closes_at is None:
            return False, f"{session_id}:invalid_hours"
        try:
            open_time = opens_at if hasattr(opens_at, "hour") else time.fromisoformat(str(opens_at))
            close_time = closes_at if hasattr(closes_at, "hour") else time.fromisoformat(str(closes_at))
            opened = moment.replace(
                hour=open_time.hour,
                minute=open_time.minute,
                second=open_time.second,
                microsecond=0,
            )
            closed = moment.replace(
                hour=close_time.hour,
                minute=close_time.minute,
                second=close_time.second,
                microsecond=0,
            )
            return opened <= moment <= closed, session_id
        except (TypeError, ValueError, AttributeError):
            return False, f"{session_id}:invalid_hours"

    return resolve


def build_market_session_provider(engine: Any):
    """Build a session resolver for all Phase 1 market-session policies.

    NSE equity uses the verified imported NSE-CM calendar. MCX and currency
    sessions are feed-driven in Phase 1: a live observation is accepted and
    receives a stable IST market-date identity without consulting the NSE
    calendar. Unsupported or mismatched session/instrument pairs fail closed.
    """
    nse_provider = build_nse_session_provider(engine)
    ist = ZoneInfo("Asia/Kolkata")

    def resolve(
        session_name: str,
        instrument_key: str,
        at: datetime,
    ) -> Tuple[bool, str]:
        exchange = str(instrument_key or "").partition(":")[0].strip().upper()
        if not registry.is_supported_session(session_name):
            return False, f"unsupported_session:{session_name}"
        if not registry.session_accepts_exchange(session_name, exchange):
            return False, f"session_mismatch:{session_name}:{exchange or 'unknown'}"
        if session_name == "nse_equity":
            return nse_provider(at)

        moment = (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)).astimezone(ist)
        return True, f"{exchange}:{moment.date().isoformat()}"

    return resolve


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


class EvaluationWorker:
    """Supervised evaluation loop: feeds -> EvaluationService -> health."""

    def __init__(
        self,
        workflow_repo: SqlAlchemyWorkflowRepository,
        session_factory: Callable[[], Any],
        channel_resolver: Optional[Callable[[str, Sequence[str]], Dict[str, str]]],
        tick_source_factory: Callable[[str], TickSource],
        candle_source_factory: Callable[[str, str], TickSource],
        candle_history: CandleHistory,
        *,
        poll_interval_s: float = 2.0,
        warmup_bars: int = 10,
        service: Any = None,
        refresh_interval_s: float = 10.0,
        renewal: Optional[Callable[[], Any]] = None,
        renewal_interval_s: float = MARKET_RUNTIME_OWNER_LEASE_TTL_S / 3.0,
        health_interval_s: float = 30.0,
        health_file: Optional[str] = None,
        health_extra: Optional[Callable[[], Dict[str, Any]]] = None,
        source_rebuild_backoff_s: float = 5.0,
        session_provider: Optional[Callable[..., Optional[Tuple[bool, str]]]] = None,
        owner_id: Optional[str] = None,
        ownership_lease_s: float = 120.0,
        instrument_tokens: Optional[Dict[str, int]] = None,
        instrument_resolver: Optional[Callable[[set[str]], Any]] = None,
        binding_registry: Optional[InstrumentBindingRegistry] = None,
        bindings_changed: Optional[Callable[[BindingChange], Any]] = None,
        fundamentals_loader: Optional[Any] = None,
        fundamentals_stale_hours: float = 168.0,
        external_loader=None,
        pair_max_bar_age_s: float = 0.0,
    ) -> None:
        self.workflow_repo = workflow_repo
        self.session_factory = session_factory
        self.channel_resolver = channel_resolver
        self.tick_source_factory = tick_source_factory
        self.candle_source_factory = candle_source_factory
        self.candle_history = candle_history
        self.poll_interval_s = float(poll_interval_s)
        self.warmup_bars = int(warmup_bars)
        self.refresh_interval_s = float(refresh_interval_s)
        self.renewal = renewal
        self.renewal_interval_s = float(renewal_interval_s)
        self.health_interval_s = float(health_interval_s)
        self.health_file = health_file
        self.health_extra = health_extra
        self.source_rebuild_backoff_s = max(0.0, float(source_rebuild_backoff_s))
        self.owner_id = owner_id or f"evaluation-worker:{uuid.uuid4()}"
        self.session_provider = session_provider
        self.ownership_lease_s = max(1.0, float(ownership_lease_s))
        # C1: exactly one mutable binding owner. Source factories, history
        # readers, renewal, and health all read snapshots from the registry —
        # never a private copy that drifts out of sync.
        self.bindings = binding_registry or InstrumentBindingRegistry(instrument_tokens)
        self.instrument_resolver = instrument_resolver
        self._bindings_changed = bindings_changed
        # Phase 2 closure: production fundamentals context (latest stored
        # snapshot from public.fundamentals_features; missing data unknown).
        self.fundamentals_loader = fundamentals_loader
        self.fundamentals_stale_hours = max(0.0, float(fundamentals_stale_hours))
        # Phase 4 F10: external producer values and cross-instrument pairs.
        self.external_loader = external_loader
        self.pair_max_bar_age_s = max(0.0, float(pair_max_bar_age_s))
        self._stage_fundamentals_cache: Dict[Tuple[str, str], bool] = {}
        self._stage_external_cache: Dict[Tuple[str, str, str], list] = {}
        self._stage_pair_cache: Dict[Tuple[str, str, str], list] = {}

        self.health: Dict[str, Any] = {
            "started_at": None,
            "last_evaluated_at": None,
            "evaluations": 0,
            "emitted": 0,
            "suppressed": Counter(),
            "gaps": 0,
            "warmups": 0,
            "unresolved_channels": 0,
            "renewal_failures": 0,
            "last_renewal_at": None,
            "rebuilds": 0,
            "last_refresh_at": None,
            "refresh_failures": 0,
            "context_misses": 0,
            "unresolved_instruments": 0,
            "binding_revisions": 0,
            "fundamentals_hits": 0,
            "fundamentals_misses": 0,
            "fundamentals_stale": 0,
        }

        if service is not None:
            self.service = service
        else:
            from backend.workflows.service import EvaluationService

            self.service = EvaluationService(
                workflow_repo,
                session_factory,
                channel_resolver=self._channel_resolver_with_health(),
                session_provider=session_provider,
                owner_id=self.owner_id,
                ownership_lease_s=self.ownership_lease_s,
            )

        self._subscriptions: List[ActiveSubscription] = []
        self._ltp_subs: Dict[str, List[ActiveSubscription]] = {}
        self._candle_subs: Dict[Tuple[str, str], List[ActiveSubscription]] = {}
        self._tick_sources: Dict[str, TickSource] = {}
        self._candle_sources: Dict[Tuple[str, str], TickSource] = {}
        # Phase 2 (F8): shared feature computation + layered plans.
        self.feature_engine = FeatureEngine()
        self._sub_plans: Dict[str, SubscriptionPlan] = {}
        self._feature_sources: Dict[Tuple[str, str], TickSource] = {}
        self._feature_warmed: set = set()
        # Phase 2 (F7): universe membership resolution state.
        self.universe_service = None  # optional UniverseService, wired by entry
        self.universe_resolve_interval_s = 300.0
        self._universe_cache: Dict[str, Tuple[float, set]] = {}
        self._universe_health = {"stale_universes": 0, "resolution_failures": 0}
        self._document_cache: Dict[str, Optional[WorkflowDocument]] = {}
        self._pending_rebuilds: Dict[Tuple[str, Any], datetime] = {}
        self._bg_tasks: List[asyncio.Task] = []
        self._running = False
        self._allow_emit_supported: Optional[bool] = None
        self._resolved_health_file: Optional[str] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Materialize subscriptions, warm candle rules, open feed sources."""
        self._ensure_subscription_rows()
        await self._sync_universe_memberships()
        self._subscriptions = list(self.workflow_repo.list_active_subscriptions())
        await self._resolve_instrument_tokens({sub.instrument_key for sub in self._subscriptions})
        self._refresh_unresolved_instruments()

        self._ltp_subs = {}
        self._candle_subs = {}
        for sub in self._subscriptions:
            self._index_subscription(sub)

        # Warmup-before-live (spec F2): rebuild candle rule state from durable
        # history so the first live bar can fire on a real crossing. The
        # replay is emission-gated (allow_emit=False) and skips bars already
        # covered by the checkpoint, so warming can never emit historical
        # notifications.
        for (instrument_key, timeframe), group in list(self._candle_subs.items()):
            await self._warm_candle_group(instrument_key, timeframe, group)

        for instrument_key in sorted(self._ltp_subs):
            source = self.tick_source_factory(instrument_key)
            await source.start()
            self._tick_sources[instrument_key] = source
        for (instrument_key, timeframe) in sorted(self._candle_subs):
            source = self.candle_source_factory(instrument_key, timeframe)
            await source.start()
            self._candle_sources[(instrument_key, timeframe)] = source
        await self._sync_feature_sources()

        self._resolved_health_file = (
            self.health_file
            or (os.environ.get("ALERTS_HEALTH_FILE", "").strip() or None)
        )
        self.health["started_at"] = _utcnow().isoformat()
        # Booting materializes the member set from the database, so it counts
        # as a membership resolution: breadth freshness must be measured from
        # a real timestamp even before the first refresh pass.
        self.health["last_refresh_at"] = self.health["started_at"]
        self._running = True
        logger.info(
            "evaluation worker started: %d subscriptions (%d ltp instruments, %d candle groups)",
            len(self._subscriptions), len(self._tick_sources), len(self._candle_sources),
        )

    async def stop(self) -> None:
        self._running = False
        await self._cancel_bg_tasks()
        for source in list(self._tick_sources.values()) + list(self._candle_sources.values()):
            try:
                await source.stop()
            except Exception:
                logger.warning("feed source stop failed", exc_info=True)
        self._tick_sources.clear()
        self._candle_sources.clear()

    async def _cancel_bg_tasks(self) -> None:
        tasks, self._bg_tasks = list(self._bg_tasks), []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("background task ended with error", exc_info=True)

    async def run(self) -> None:
        """Run until cancelled; SIGTERM handling lives in the entrypoint."""
        await self.start()
        self._bg_tasks = [
            asyncio.create_task(self._refresh_loop(), name="subscription-refresh"),
            asyncio.create_task(self._renewal_loop(), name="ownership-renewal"),
            asyncio.create_task(self._health_loop(), name="health-reporter"),
        ]
        try:
            while self._running:
                progressed = await self.poll_once()
                if not progressed:
                    await asyncio.sleep(self.poll_interval_s)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def poll_once(self) -> bool:
        """Drain every feed source once. True when any observation arrived."""
        await self._rebuild_due_sources()
        saw_any = False
        for instrument_key, source in list(self._tick_sources.items()):
            try:
                obs = await source.next_observation()
            except Exception:
                await self._handle_source_failure("tick", instrument_key, source)
                continue
            if obs is None:
                continue
            saw_any = True
            for sub in self._ltp_subs.get(instrument_key, ()):
                features, layers = self._plan_dispatch(sub)
                self._dispatch(sub, obs, features=features, layers=layers)
        for key, source in list(self._candle_sources.items()):
            try:
                obs = await source.next_observation()
            except Exception:
                await self._handle_source_failure("candle", key, source)
                continue
            if obs is None:
                continue
            saw_any = True
            # F8: one window update + one feature computation per event,
            # shared by every rule that dispatches on this bar.
            snapshot = self.feature_engine.on_bar(key[0], key[1], obs)
            for sub in self._candle_subs.get(key, ()):
                features, layers = self._plan_dispatch(sub)
                if features is None:
                    features = dict(snapshot) if snapshot else None
                self._dispatch(sub, obs, features=features, layers=layers)
        # Feature-only windows (upstream timeframes of layered chains): their
        # completions update the shared snapshots but dispatch to no rule.
        for key, source in list(self._feature_sources.items()):
            try:
                obs = await source.next_observation()
            except Exception:
                # Distinct kind: a feature-source failure must not pop from
                # (or rebuild into) the dispatch candle-source table.
                await self._handle_source_failure("feature", key, source)
                continue
            if obs is None:
                continue
            saw_any = True
            self.feature_engine.on_bar(key[0], key[1], obs)
        return saw_any

    # ------------------------------------------------------------------
    # background supervision loops
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Re-read active subscriptions so activation/pause needs no restart."""
        while True:
            try:
                await asyncio.sleep(self.refresh_interval_s)
            except asyncio.CancelledError:
                return
            try:
                await self.refresh_subscriptions()
            except asyncio.CancelledError:
                return
            except Exception:
                self.health["refresh_failures"] += 1
                logger.warning("subscription refresh failed; continuing", exc_info=True)

    async def refresh_subscriptions(self) -> Dict[str, int]:
        """Diff the DB's active subscriptions against the dispatch tables.

        Added subscription ids are indexed and warmed up (candle rules)
        BEFORE live dispatch; removed ids are dropped from the dispatch
        tables and their now-orphaned feed sources are stopped. Dispatch
        always consults these tables, so the change takes effect on the
        next event.
        """
        # Universe membership first: newly admitted members must be part of
        # THIS pass's added set so they are indexed and warmed before dispatch.
        await self._sync_universe_memberships()
        current = list(self.workflow_repo.list_active_subscriptions())
        change = await self._resolve_instrument_tokens(
            {sub.instrument_key for sub in current}
        )
        if change is not None and (change.added or change.changed):
            # A token moved: re-persist the catalog provenance, then RELOAD so
            # the objects dispatch actually uses carry it. Evidence is copied
            # from ``sub.config`` in memory, so a database-only update would not
            # reach this pass's events. The reload costs one query and happens
            # only when the catalog genuinely moved.
            await self._rebind_moved_subscriptions(change)
            current = list(self.workflow_repo.list_active_subscriptions())
        current_by_id = {sub.id: sub for sub in current}
        known_ids = {sub.id for sub in self._subscriptions}

        added = [sub for sub in current if sub.id not in known_ids]
        removed = [sub for sub in self._subscriptions if sub.id not in current_by_id]

        for sub in added:
            await self._add_subscription(sub)
        for sub in removed:
            self._drop_subscription(sub)
        self._prune_orphan_sources()
        await self._sync_feature_sources()
        self._refresh_unresolved_instruments()

        self._subscriptions = current
        self.health["last_refresh_at"] = _utcnow().isoformat()
        if added or removed:
            logger.info(
                "subscription refresh: %d added, %d removed (%d active)",
                len(added), len(removed), len(current),
            )
        return {"added": len(added), "removed": len(removed)}

    @property
    def instrument_tokens(self) -> Dict[str, int]:
        """Snapshot of the currently accepted instrument bindings (C1)."""
        return self.bindings.snapshot()

    async def _resolve_instrument_tokens(self, instrument_keys: set[str]) -> None:
        """Run one catalog resolution pass and apply the binding diff.

        A resolver exception keeps the current bindings untouched (the next
        refresh pass retries); a successful pass applies additions, token
        replacements, and authoritative removals to the shared registry and
        rebuilds only the affected feed sources.

        Returns the applied ``BindingChange`` (or None when nothing was
        resolved), so the caller can re-persist catalog provenance for exactly
        the subscriptions whose binding moved.
        """
        if self.instrument_resolver is None:
            return None
        if instrument_keys:
            try:
                outcome = self.instrument_resolver(set(instrument_keys))
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                resolved, rejected = outcome
            except Exception:
                logger.warning(
                    "catalog instrument resolution failed; keeping current bindings",
                    exc_info=True,
                )
                self.health["refresh_failures"] += 1
                return None
            try:
                change = self.bindings.apply(resolved or {}, rejected or set())
            except (TypeError, ValueError):
                logger.warning(
                    "catalog returned invalid bindings; ignoring pass", exc_info=True
                )
                return None
        else:
            # No instrument is required this pass. There is nothing to resolve,
            # but the release below still has to run: it is the only thing that
            # stops the lease paying for feeds nothing evaluates any more.
            change = BindingChange(
                previous_revision=self.bindings.revision,
                revision=self.bindings.revision,
            )
        # `instrument_keys` is the COMPLETE set of instruments still required
        # this pass (both call sites pass every active subscription), so
        # anything else in the registry belongs to a subscription that no
        # longer exists. Releasing it here is what stops a removed dependency
        # from keeping a market-runtime feed open: the renewal callback
        # publishes the registry snapshot, so a retained key would be
        # re-subscribed on every lease refresh.
        stale = self.bindings.retain_only(instrument_keys)
        if stale:
            logger.info(
                "released %d binding(s) with no remaining subscription: %s",
                len(stale), ", ".join(sorted(stale)),
            )
            change = BindingChange(
                added=change.added,
                changed=change.changed,
                removed=set(change.removed) | stale,
                previous_revision=change.previous_revision,
                revision=self.bindings.revision,
            )
        if change.has_changes:
            await self._apply_binding_change(change)
        return change

    async def _rebind_moved_subscriptions(self, change: BindingChange) -> None:
        """Re-persist catalog provenance for subscriptions whose token moved.

        ``sub.config["instrument_binding"]`` is what a new event copies into its
        immutable evidence (C6), so it must describe the binding the evaluation
        ACTUALLY used. The registry above tracks tokens for feed construction,
        but the descriptor — identity and catalog generation — is written by
        ``ensure_subscriptions``. Without this pass, a token replaced while the
        worker is running would keep reporting the old generation in the
        evidence of events evaluated under the new one, which is worse than
        reporting nothing: it is confidently wrong.

        Only added/changed keys are re-bound, so the cost tracks real catalog
        movement rather than every refresh, and the single implementation is
        reused instead of a second binding writer. Existing events are never
        touched: their snapshot was copied at publication time and stays as it
        was.
        """
        moved = set(change.added) | set(change.changed)
        if not moved:
            return
        revision_ids = sorted({
            sub.revision_id for sub in self._subscriptions
            if sub.instrument_key in moved
        })
        for revision_id in revision_ids:
            try:
                with self.session_factory() as session:
                    revision = session.get(WorkflowRevision, revision_id)
                if revision is not None:
                    self.service.ensure_subscriptions(revision)
            except Exception:
                logger.warning(
                    "failed to re-bind moved subscriptions for revision %s",
                    revision_id, exc_info=True,
                )

    async def _apply_binding_change(self, change: BindingChange) -> None:
        """Rebuild feed sources after an accepted binding change (C1).

        - added/replaced tokens: rebuild that instrument's sources through the
          factories so ticks/candles/history use the accepted binding. A
          replaced token yields a fresh observation epoch via the factory;
          durable trigger state lives in checkpoints and is untouched.
        - removed (rejected) tokens: stop sources and clear dispatch groups so
          a retired/expired/ambiguous instrument stops evaluating.
        """
        if not change.has_changes:
            return
        self.health["binding_revisions"] += 1
        affected = set(change.added) | set(change.changed) | set(change.removed)

        for key in sorted(affected):
            old = self._tick_sources.pop(key, None)
            if old is not None:
                await self._safe_stop_source(old)
            if (
                key not in change.removed
                and self._ltp_subs.get(key)
                and self.bindings.get(key) is not None
            ):
                source = self.tick_source_factory(key)
                await source.start()
                self._tick_sources[key] = source

        candle_keys = [(k, tf) for (k, tf) in list(self._candle_subs) if k in affected]
        for key in candle_keys:
            old = self._candle_sources.pop(key, None)
            if old is not None:
                await self._safe_stop_source(old)
            if (
                key[0] not in change.removed
                and self._candle_subs.get(key)
                and self.bindings.get(key[0]) is not None
            ):
                # History replay completes BEFORE the rebuilt source is
                # exposed; a replaced token invalidates the stored replay
                # boundary because the history identity changed underneath.
                await self._warm_candle_group(
                    key[0], key[1], self._candle_subs.get(key, ()),
                    ignore_boundary=key in change.changed,
                )
                source = self.candle_source_factory(*key)
                await source.start()
                self._candle_sources[key] = source

        feature_keys = [(k, tf) for (k, tf) in list(self._feature_sources) if k in affected]
        for key in feature_keys:
            old = self._feature_sources.pop(key, None)
            if old is not None:
                await self._safe_stop_source(old)
            if (
                key[0] not in change.removed
                and self.bindings.get(key[0]) is not None
            ):
                if key in change.changed:
                    # A replaced token changes the instrument identity the
                    # window was built from: drop and re-warm from history so
                    # the old token's bars can never contaminate features.
                    self.feature_engine.release(key[0], key[1])
                    self._feature_warmed.discard(key)
                self.feature_engine.warm(key[0], key[1], self.candle_history)
                self._feature_warmed.add(key)
                source = self.candle_source_factory(*key)
                await source.start()
                self._feature_sources[key] = source

        for key in change.removed:
            self._ltp_subs.pop(key, None)
            for candle_key in [k for k in list(self._candle_subs) if k[0] == key]:
                self._candle_subs.pop(candle_key, None)
            # Release engine windows/specs and any surviving feature source so
            # a retired instrument stops consuming feeds and holding memory.
            for feature_key in [k for k in list(self._feature_sources) if k[0] == key]:
                old = self._feature_sources.pop(feature_key, None)
                if old is not None:
                    await self._safe_stop_source(old)
            self.feature_engine.release(key)
            self._feature_warmed = {
                wk for wk in self._feature_warmed if wk[0] != key
            }

        logger.info(
            "instrument bindings updated: %d added, %d replaced, %d removed (revision %d)",
            len(change.added), len(change.changed), len(change.removed), change.revision,
        )
        if self._bindings_changed is not None:
            try:
                result = self._bindings_changed(change)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("bindings-changed callback failed", exc_info=True)

    @staticmethod
    async def _safe_stop_source(source: TickSource) -> None:
        try:
            await source.stop()
        except Exception:
            logger.warning("feed source stop failed during binding rebuild", exc_info=True)

    def _refresh_unresolved_instruments(self) -> None:
        """Expose active subscriptions with no accepted token binding."""
        tokens = self.bindings.snapshot()
        if not tokens:
            self.health["unresolved_instruments"] = len(self._subscriptions)
            return
        self.health["unresolved_instruments"] = sum(
            1 for sub in self._subscriptions if sub.instrument_key not in tokens
        )

    async def _sync_universe_memberships(self) -> None:
        """Re-resolve universe membership and materialize/depart members (F7).

        The last persisted membership is used between resolutions; a failed
        resolution keeps the last valid membership and is counted in health
        (explicit freshness policy, degraded coverage visible).
        """
        if self.universe_service is None:
            return
        now = _utcnow().timestamp()
        try:
            from sqlalchemy import select as _select

            with self.session_factory() as session:
                from backend.workflows.repository import WorkflowRevision

                revisions = session.execute(
                    _select(WorkflowRevision).where(WorkflowRevision.status == "active")
                ).scalars().all()
                revision_rows = [
                    (r, self.workflow_repo.get_workflow(r.workflow_id, db=session))
                    for r in revisions
                ]
        except Exception:
            logger.warning("universe sync: active revision read failed", exc_info=True)
            return
        for revision, workflow in revision_rows:
            document = self._document_for_revision(revision)
            if document is None or document.universe is None:
                continue
            owner_id = workflow.owner_id if workflow is not None else None
            if owner_id is None:
                continue
            members: set = {inst.key() for inst in document.instruments}
            degraded = False
            skip_sync = False
            universe_revision = None
            for ref in document.universe.refs:
                cache_key = f"{owner_id}:{ref.kind}:{ref.name}"
                cached = self._universe_cache.get(cache_key)
                if cached is not None and now - cached[0] < self.universe_resolve_interval_s:
                    members |= cached[1]
                    continue
                try:
                    if ref.kind in ("universe", "watchlist"):
                        latest = self.universe_service.latest_revision(owner_id, ref.name)
                        if latest is None:
                            logger.warning(
                                "universe %s referenced by workflow %s has no resolved membership",
                                ref.name, revision.id,
                            )
                            self._universe_health["stale_universes"] += 1
                            degraded = True
                            continue
                        resolved = set(latest.get("members") or ())
                        try:
                            universe_revision = max(
                                universe_revision or 0, int(latest.get("revision") or 0)
                            ) or None
                        except (TypeError, ValueError):
                            pass
                    else:  # index source list
                        preview = self.universe_service.preview_membership(
                            owner_id, "index", {"source_list": ref.name}
                        )
                        resolved = set(preview.get("members") or ())
                    self._universe_cache[cache_key] = (now, resolved)
                    members |= resolved
                except Exception as exc:
                    # Explicit freshness policy: a failed resolution with no
                    # cached membership skips materialization entirely — the
                    # last valid DB state stays in force, never an empty union.
                    self._universe_health["resolution_failures"] += 1
                    if cached is not None:
                        members |= cached[1]
                        logger.warning(
                            "universe membership resolution failed for %s (%s); "
                            "keeping last cached membership",
                            ref.name, exc,
                        )
                    else:
                        logger.warning(
                            "universe membership resolution failed for %s (%s); "
                            "keeping last materialized state unchanged",
                            ref.name, exc,
                        )
                        skip_sync = True
                        break
                    degraded = True
            # F7 intersection: restrict the union to members present in EVERY
            # intersect reference (resolved with the same cache/freshness
            # semantics as union refs).
            for ref in document.universe.intersect:
                cache_key = f"{owner_id}:intersect:{ref.kind}:{ref.name}"
                cached = self._universe_cache.get(cache_key)
                try:
                    if ref.kind in ("universe", "watchlist"):
                        latest = self.universe_service.latest_revision(owner_id, ref.name)
                        if latest is None:
                            logger.warning(
                                "intersect universe %s referenced by workflow %s has no "
                                "resolved membership",
                                ref.name, revision.id,
                            )
                            self._universe_health["stale_universes"] += 1
                            degraded = True
                            continue
                        resolved = set(latest.get("members") or ())
                        try:
                            universe_revision = max(
                                universe_revision or 0, int(latest.get("revision") or 0)
                            ) or None
                        except (TypeError, ValueError):
                            pass
                    else:  # index source list
                        preview = self.universe_service.preview_membership(
                            owner_id, "index", {"source_list": ref.name}
                        )
                        resolved = set(preview.get("members") or ())
                    self._universe_cache[cache_key] = (now, resolved)
                except Exception as exc:
                    self._universe_health["resolution_failures"] += 1
                    if cached is not None:
                        resolved = cached[1]
                        logger.warning(
                            "intersect universe resolution failed for %s (%s); "
                            "using last cached membership",
                            ref.name, exc,
                        )
                    else:
                        logger.warning(
                            "intersect universe resolution failed for %s (%s); "
                            "keeping last materialized state unchanged",
                            ref.name, exc,
                        )
                        skip_sync = True
                        break
                members &= resolved
            for ref in document.universe.exclude:
                try:
                    if ref.kind in ("universe", "watchlist"):
                        latest = self.universe_service.latest_revision(owner_id, ref.name)
                        if latest:
                            members -= set(latest.get("members") or ())
                except Exception:
                    logger.warning("universe exclusion failed for %s", ref.name, exc_info=True)
            if skip_sync:
                continue
            try:
                self.service.sync_universe_members(
                    revision, sorted(members),
                    universe_revision=universe_revision,
                    # owner_id keys the breadth advisory lock so a re-admission
                    # eviction serializes against a concurrent aggregate.
                    owner_id=owner_id,
                )
            except Exception:
                logger.warning(
                    "universe membership materialization failed for revision %s",
                    revision.id, exc_info=True,
                )

    def _document_for_revision(self, revision):
        cached = self._document_cache.get(revision.id)
        if cached is not None:
            return cached if cached != "invalid" else None
        try:
            document = parse_workflow_dict(revision.document)
        except Exception:
            document = None
        self._document_cache[revision.id] = document
        return document

    async def _sync_feature_sources(self) -> None:
        """Open candle sources for feature timeframes beyond rule groups.

        Layered chains read upstream snapshots (e.g. a daily EMA200 filter
        evaluated on 5m events), so the engine needs completed-candle feeds
        for those timeframes even when no rule evaluates on them directly.
        """
        needed: Dict[Tuple[str, str], bool] = {}
        for sub in self._subscriptions:
            plan = self._sub_plans.get(sub.id)
            if plan is None:
                plan = build_subscription_plan(self._document_for(sub), sub.stage_id)
                self._sub_plans[sub.id] = plan
            for timeframe, spec in plan.specs:
                needed[(sub.instrument_key, timeframe)] = True
            for timeframe in plan.feature_timeframes:
                needed.setdefault((sub.instrument_key, timeframe), True)
        for (key, timeframe) in sorted(needed):
            # Warm the engine window from durable history whenever it has not
            # been warmed yet — including the dispatch timeframe itself, whose
            # feed source may already exist (start() opens dispatch sources
            # before this sync). Skipping warm for existing sources left the
            # dispatch timeframe's feature window empty after every restart.
            if (key, timeframe) not in self._feature_warmed:
                self._feature_warmed.add((key, timeframe))
                self.feature_engine.warm(key, timeframe, self.candle_history)
            if (key, timeframe) in self._feature_sources or (key, timeframe) in self._candle_sources:
                continue
            source = self.candle_source_factory(key, timeframe)
            await source.start()
            self._feature_sources[(key, timeframe)] = source
        # release feature sources that no plan needs anymore
        for window_key in list(self._feature_sources):
            if window_key not in needed:
                source = self._feature_sources.pop(window_key)
                self._close_source(source)

    def _plan_dispatch(self, sub: ActiveSubscription) -> tuple:
        """(features, layers) for one subscription from the shared engine."""
        plan = self._sub_plans.get(sub.id)
        if plan is None or not plan.has_features:
            return None, None
        key = sub.instrument_key
        engine = self.feature_engine
        merged: Dict[str, object] = {}
        for timeframe in plan.feature_timeframes:
            for source_map in (engine.snapshot(key, timeframe), engine.field_snapshot(key, timeframe)):
                for feature_id, value in source_map.items():
                    merged.setdefault(feature_id, value)
        # Stage references read the referenced feature stage's OWN-timeframe
        # snapshot under its "stage:<id>" alias.
        for alias, timeframe, feature_id in plan.stage_aliases:
            value = engine.snapshot(key, timeframe).get(feature_id)
            if value is not None:
                merged.setdefault(alias, value)
        layers = []
        for layer_stage, layer_tf in plan.layers:
            if layer_tf:
                layer_features = dict(engine.snapshot(key, layer_tf))
                layer_features.update(engine.field_snapshot(key, layer_tf))
                layers.append((layer_stage, layer_features))
            else:
                layers.append((layer_stage, None))
        return merged or None, layers or None

    async def _add_subscription(self, sub: ActiveSubscription) -> None:
        """Index a newly-activated subscription and warm it before dispatch."""
        self._index_subscription(sub)
        if sub not in self._subscriptions:
            self._subscriptions.append(sub)
        stage = self._stage_for(sub)
        if stage is not None and stage.clock == "candle_close" and stage.timeframe:
            key = (sub.instrument_key, stage.timeframe)
            group = self._candle_subs.get(key) or []
            if key not in self._candle_sources:
                source = self.candle_source_factory(*key)
                await source.start()
                self._candle_sources[key] = source
            # bring the new rule's state forward from history (silent)
            await self._warm_candle_group(key[0], key[1], [sub])
        elif stage is not None and stage.clock == "ltp":
            if sub.instrument_key not in self._tick_sources:
                source = self.tick_source_factory(sub.instrument_key)
                await source.start()
                self._tick_sources[sub.instrument_key] = source

    def _drop_subscription(self, sub: ActiveSubscription) -> None:
        self._subscriptions = [s for s in self._subscriptions if s.id != sub.id]
        self._sub_plans.pop(sub.id, None)
        for group in self._ltp_subs.values():
            group[:] = [s for s in group if s.id != sub.id]
        for group in self._candle_subs.values():
            group[:] = [s for s in group if s.id != sub.id]

    def _prune_orphan_sources(self) -> None:
        """Stop feed sources whose subscription group emptied."""
        live_instruments = {k for k, group in self._ltp_subs.items() if group}
        for instrument_key in list(self._tick_sources):
            if instrument_key not in live_instruments:
                source = self._tick_sources.pop(instrument_key)
                self._close_source(source)
        for key in list(self._candle_sources):
            if not self._candle_subs.get(key):
                source = self._candle_sources.pop(key)
                self._close_source(source)

    @staticmethod
    def _close_source(source: TickSource) -> None:
        async def _stop():
            try:
                await source.stop()
            except Exception:
                logger.warning("orphan source stop failed", exc_info=True)

        try:
            asyncio.get_running_loop().create_task(_stop())
        except RuntimeError:
            pass

    async def _renewal_loop(self) -> None:
        """Renew this worker's market-runtime owner lease every interval.

        A renewal outage (market-runtime down) is counted in health and
        logged, but must never crash the evaluation loop: the next interval
        simply retries.
        """
        if self.renewal is None or self.renewal_interval_s <= 0:
            return
        while True:
            try:
                await asyncio.sleep(self.renewal_interval_s)
            except asyncio.CancelledError:
                return
            try:
                result = self.renewal()
                if inspect.isawaitable(result):
                    await result
                self.health["last_renewal_at"] = _utcnow().isoformat()
            except asyncio.CancelledError:
                return
            except Exception:
                self.health["renewal_failures"] += 1
                logger.warning(
                    "market-runtime ownership renewal failed "
                    "(failures=%d); will retry in %.1fs",
                    self.health["renewal_failures"], self.renewal_interval_s,
                    exc_info=True,
                )

    async def _health_loop(self) -> None:
        """Report health: one INFO line + optional JSON file snapshot."""
        self._write_health()
        while True:
            try:
                await asyncio.sleep(self.health_interval_s)
            except asyncio.CancelledError:
                return
            self._write_health()

    def health_snapshot(self) -> Dict[str, Any]:
        """JSON-safe view of the health counters plus any extra providers."""
        snapshot: Dict[str, Any] = {}
        for key, value in self.health.items():
            snapshot[key] = dict(value) if isinstance(value, Counter) else value
        snapshot["universe_membership"] = dict(self._universe_health)
        snapshot["feature_windows"] = len(self._feature_sources) + len(self._candle_sources)
        # Phase 4 F10: external-producer resolution counters, so "why is this
        # condition unknown" is answerable from health rather than logs. The
        # reasons are counted by name (external_missing, _expired, _late,
        # _future, _revoked, ...).
        if self.external_loader is not None:
            try:
                snapshot["external_signals"] = self.external_loader.health()
            except Exception:
                logger.warning("external loader health failed", exc_info=True)
        if self.health_extra is not None:
            try:
                extra = self.health_extra()
                if extra:
                    snapshot.update(extra)
            except Exception:
                logger.warning("health_extra provider failed", exc_info=True)
        return snapshot

    def _write_health(self) -> None:
        snapshot = self.health_snapshot()
        logger.info("worker health: %s", json.dumps(snapshot, default=str))
        path = self._resolved_health_file
        if not path:
            return
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, default=str)
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("could not write health file %s", path, exc_info=True)

    # ------------------------------------------------------------------
    # feed outage recovery
    # ------------------------------------------------------------------

    async def _handle_source_failure(self, kind: str, key: Any, source: TickSource) -> None:
        """Tear down a failed feed source and schedule a fresh rebuild.

        The rebuild goes through the source factory, which must mint a NEW
        ltp epoch boot id so LTP rules re-initialize instead of firing on
        state carried across the outage (spec D2).
        """
        self.health["gaps"] += 1
        logger.error(
            "%s feed source for %s failed (gaps=%d); scheduling rebuild in %.1fs",
            kind, key, self.health["gaps"], self.source_rebuild_backoff_s,
            exc_info=True,
        )
        table = (
            self._tick_sources
            if kind == "tick"
            else self._feature_sources
            if kind == "feature"
            else self._candle_sources
        )
        table.pop(key, None)
        self._pending_rebuilds.pop((kind, key), None)
        try:
            await source.stop()
        except Exception:
            logger.warning("failed source stop raised; ignored", exc_info=True)
        self._pending_rebuilds[(kind, key)] = (
            _utcnow() + timedelta(seconds=self.source_rebuild_backoff_s)
        )

    async def _rebuild_due_sources(self) -> None:
        """Rebuild sources whose outage backoff has elapsed."""
        if not self._pending_rebuilds:
            return
        now = _utcnow()
        due = [k for k, at in self._pending_rebuilds.items() if at <= now]
        for kind_key in due:
            kind, key = kind_key
            del self._pending_rebuilds[kind_key]
            # Only catalog-managed bindings gate rebuilds; deployments without
            # a resolver (tests, env-token mode) rebuild unconditionally.
            binding_key = key if kind == "tick" else key[0]
            if self.instrument_resolver is not None and self.bindings.get(binding_key) is None:
                logger.info(
                    "skipping rebuild of %s feed source for %s: instrument is not bound",
                    kind, key,
                )
                continue
            try:
                if kind == "tick":
                    source = self.tick_source_factory(key)
                    await source.start()
                    self._tick_sources[key] = source
                elif kind == "feature":
                    # Backfill the engine window from stored completed candles
                    # BEFORE the rebuilt source is exposed: bars missed during
                    # the outage are recoverable from Postgres (the aggregator
                    # keeps persisting), so windows must not go silently stale.
                    self.feature_engine.warm(key[0], key[1], self.candle_history)
                    source = self.candle_source_factory(*key)
                    await source.start()
                    self._feature_sources[key] = source
                else:
                    source = self.candle_source_factory(*key)
                    # History replay must complete before the rebuilt live
                    # source is exposed to poll_once. This catches a one-bar
                    # outage even when elapsed time is below a coarse gap
                    # heuristic and keeps catch-up notifications silent.
                    await self._warm_candle_group(
                        key[0], key[1], self._candle_subs.get(key, ())
                    )
                    await source.start()
                    self._candle_sources[key] = source
                self.health["rebuilds"] += 1
                logger.warning(
                    "rebuilt %s feed source for %s after outage (fresh epoch)", kind, key,
                )
            except Exception:
                logger.error(
                    "rebuild of %s feed source for %s failed; retrying", kind, key,
                    exc_info=True,
                )
                self._pending_rebuilds[kind_key] = (
                    _utcnow() + timedelta(seconds=self.source_rebuild_backoff_s)
                )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _index_subscription(self, sub: ActiveSubscription) -> None:
        """Classify a subscription and register its feature dependencies."""
        stage = self._stage_for(sub)
        if stage is None:
            return
        # Phase 2: per-subscription layered plan + shared feature declaration
        plan = build_subscription_plan(self._document_for(sub), sub.stage_id)
        self._sub_plans[sub.id] = plan
        for timeframe, spec in plan.specs:
            self.feature_engine.declare(sub.instrument_key, timeframe, spec)
        if stage.clock == "ltp":
            group = self._ltp_subs.setdefault(sub.instrument_key, [])
            if all(s.id != sub.id for s in group):
                group.append(sub)
        elif stage.clock == "candle_close" and stage.timeframe:
            key = (sub.instrument_key, stage.timeframe)
            group = self._candle_subs.setdefault(key, [])
            if all(s.id != sub.id for s in group):
                group.append(sub)

    async def _warm_candle_group(
        self,
        instrument_key: str,
        timeframe: str,
        group: Sequence[ActiveSubscription],
        *,
        ignore_boundary: bool = False,
    ) -> None:
        """Replay recent history into candle rules silently (no emission).

        Bars at or before a subscription's checkpoint ``last_bar_ts`` were
        already processed and are skipped; every replayed bar goes through
        ``handle_observation(..., allow_emit=False)`` so warming can never
        write signal events, deliveries, or lifecycle changes. ``ignore_boundary``
        is used after a token replacement: the binding (and therefore the
        history identity) changed underneath the subscription, so all warmup
        bars are replayed to re-establish continuity safely.
        """
        bars = self.candle_history.recent_bars(instrument_key, timeframe, self.warmup_bars)
        for sub in group:
            boundary = None if ignore_boundary else self._replay_boundary(sub)
            for bar in bars:
                if boundary is not None and bar.ts <= boundary:
                    continue  # already processed before this boot
                self.health["warmups"] += 1
                self._dispatch(sub, bar, allow_emit=False)

    def _replay_boundary(self, sub: ActiveSubscription) -> Optional[datetime]:
        """Checkpoint ``last_bar_ts``: bars older or equal are already done."""
        try:
            state, _owner_epoch = self.workflow_repo.load_checkpoint(
                sub.id, sub.instrument_key, CANDLE_EPOCH_ID
            )
        except Exception:
            return None
        raw = (state or {}).get("last_bar_ts") if isinstance(state, dict) else None
        if not raw:
            return None
        return _parse_ts(raw)

    def _service_supports_allow_emit(self) -> bool:
        """Probe (once) whether the service honors the pinned allow_emit kwarg.

        Guards against a not-yet-upgraded EvaluationService so the worker
        never crashes on warmup; with such a service warmup falls back to a
        plain call (and the service's own history keeps warmup bars silent).
        """
        if self._allow_emit_supported is None:
            try:
                params = inspect.signature(self.service.handle_observation).parameters
                self._allow_emit_supported = "allow_emit" in params
            except (TypeError, ValueError):
                self._allow_emit_supported = False
        return self._allow_emit_supported

    def _channel_resolver_with_health(self):
        """Wrap the caller's resolver: count unresolved names in health (F11)."""
        inner = self.channel_resolver
        if inner is None:
            return None

        def resolve(owner_id: str, names: Sequence[str]) -> Dict[str, str]:
            mapping = inner(owner_id, names) or {}
            missing = [name for name in (names or ()) if name not in mapping]
            if missing:
                # E-24 at evaluation time: skipped channels are visible in
                # health, never silent.
                self.health["unresolved_channels"] += len(missing)
                logger.warning(
                    "channel(s) %s not found/disabled for owner %s; skipped",
                    missing, owner_id,
                )
            return mapping

        return resolve

    def _ensure_subscription_rows(self) -> None:
        """Create alert_subscriptions rows for every active revision."""
        from sqlalchemy import select

        with self.session_factory() as session:
            revisions = session.execute(
                select(WorkflowRevision).where(WorkflowRevision.status == "active")
            ).scalars().all()
        for revision in revisions:
            try:
                created = self.service.ensure_subscriptions(revision)
                if created:
                    logger.info(
                        "materialized %d subscription(s) for revision %s",
                        created, revision.id,
                    )
            except Exception:
                logger.error(
                    "failed to materialize subscriptions for revision %s",
                    revision.id, exc_info=True,
                )

    def _document_for(self, sub: ActiveSubscription) -> Optional[WorkflowDocument]:
        if sub.revision_id in self._document_cache:
            return self._document_cache[sub.revision_id]
        try:
            document: Optional[WorkflowDocument] = parse_workflow_dict(sub.document)
        except (WorkflowParseError, ValueError, TypeError):
            logger.warning(
                "unparseable revision document for subscription %s", sub.id, exc_info=True,
            )
            document = None
        self._document_cache[sub.revision_id] = document
        return document

    def _is_breadth_stage(self, sub: ActiveSubscription) -> bool:
        """Whether this subscription's stage is a breadth aggregate (F10)."""
        stage = self._stage_for(sub)
        return stage is not None and stage.breadth is not None

    def _breadth_membership_for(self, sub: ActiveSubscription) -> Dict[str, Any]:
        """The member context a breadth aggregate evaluates over.

        The member set is the instrument keys materialized for this stage and
        revision — the very rows the worker is dispatching from, so the
        aggregate can never count an instrument the worker is not observing.
        ``resolved_at`` is when membership was last materialized (the
        subscription refresh, which is also when universes re-resolve), which
        is what the freshness bound measures against.
        """
        members = sorted(
            {
                candidate.instrument_key
                for candidate in self._subscriptions
                if candidate.revision_id == sub.revision_id
                and candidate.stage_id == sub.stage_id
            }
        )
        universe_revision = None
        raw = (sub.config or {}).get("universe_revision")
        if isinstance(raw, int) and not isinstance(raw, bool):
            universe_revision = raw
        return {
            "members": members,
            "universe_revision": universe_revision,
            "resolved_at": self.health.get("last_refresh_at") or _utcnow().isoformat(),
        }

    def _external_references_for(self, sub: ActiveSubscription) -> list:
        """``external.*`` field names this stage (or an ancestor layer) reads."""
        cache_key = (sub.revision_id, sub.stage_id, "external")
        cached = self._stage_external_cache.get(cache_key)
        if cached is not None:
            return cached
        names: list = []
        document = self._document_for(sub)
        if document is not None:
            stage = next((s for s in document.stages if s.id == sub.stage_id), None)
            if stage is not None:
                for candidate in (stage, *stage_chain(document, sub.stage_id)):
                    names.extend(
                        external_context.collect_external_references(
                            [(candidate.id, list(candidate.conditions)
                              + list(candidate.any_conditions)
                              + list(candidate.not_conditions))]
                        )
                    )
        names = list(dict.fromkeys(names))
        self._stage_external_cache[cache_key] = names
        return names

    def _pair_operands_for(self, sub: ActiveSubscription) -> list:
        """Pair operands this stage (or an ancestor layer) references."""
        cache_key = (sub.revision_id, sub.stage_id, "pairs")
        cached = self._stage_pair_cache.get(cache_key)
        if cached is not None:
            return cached
        operands: list = []
        document = self._document_for(sub)
        if document is not None:
            stage = next((s for s in document.stages if s.id == sub.stage_id), None)
            if stage is not None:
                for candidate in (stage, *stage_chain(document, sub.stage_id)):
                    operands.extend(
                        pairs.collect_pair_operands(
                            list(candidate.conditions)
                            + list(candidate.any_conditions)
                            + list(candidate.not_conditions)
                        )
                    )
        self._stage_pair_cache[cache_key] = operands
        return operands

    def _resolve_pairs(self, sub: ActiveSubscription, obs, operands: list) -> dict:
        """Compute every pair operand for this observation.

        The stage's own timeframe identifies the bar grid; a pair on a stage
        without one cannot be placed on a bar grid and is reported unknown.
        """
        stage = self._stage_for(sub)
        timeframe = getattr(stage, "timeframe", None) if stage is not None else None
        out: dict = {}
        for operand in operands:
            key = pair_operand_id(operand)
            if key in out:
                continue
            if timeframe is None or self.candle_history is None:
                out[key] = {"reason": "pair_missing"}
                continue
            limit = self.pair_max_bar_age_s or (2 * registry.timeframe_seconds(timeframe))
            result = pairs.resolve_pair(
                operand,
                history=self.candle_history,
                timeframe=timeframe,
                cutoff=obs.ts,
                bar_age_limit_s=limit,
            )
            if result.value is None:
                entry = {"reason": result.reason}
                if result.head_ts is not None:
                    entry["head_ts"] = result.head_ts.isoformat()
                if result.anchor_ts is not None:
                    entry["anchor_ts"] = result.anchor_ts.isoformat()
                out[key] = entry
            else:
                out[key] = {"value": result.value, "head_ts": result.head_ts.isoformat()}
        return out

    def _stage_needs_fundamentals(self, sub: ActiveSubscription) -> bool:
        """Whether this subscription's stage — or any ancestor stage of its
        layer chain — can reference fundamentals fields (cached per
        revision+stage)."""
        cache_key = (sub.revision_id, sub.stage_id)
        cached = self._stage_fundamentals_cache.get(cache_key)
        if cached is not None:
            return cached
        needed = False
        document = self._document_for(sub)
        if document is not None:
            stage = next((s for s in document.stages if s.id == sub.stage_id), None)
            if stage is not None:
                chain = [stage, *stage_chain(document, sub.stage_id)]
                needed = any(registry.stage_uses_fundamentals(s) for s in chain)
        self._stage_fundamentals_cache[cache_key] = needed
        return needed

    def _stage_for(self, sub: ActiveSubscription) -> Optional[Stage]:
        document = self._document_for(sub)
        if document is None:
            return None
        return next((s for s in document.stages if s.id == sub.stage_id), None)

    def _dispatch(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        *,
        allow_emit: bool = True,
        features: Optional[dict] = None,
        layers: Optional[list] = None,
    ) -> None:
        self.health["evaluations"] += 1
        self.health["last_evaluated_at"] = _utcnow().isoformat()
        context = None
        levels_loader = getattr(self.candle_history, "previous_session_levels", None)
        if callable(levels_loader):
            try:
                context = levels_loader(sub.instrument_key, obs.ts)
                if context is None:
                    self.health["context_misses"] += 1
            except Exception:
                self.health["context_misses"] += 1
                logger.warning(
                    "previous-session context unavailable for %s", sub.instrument_key,
                    exc_info=True,
                )
        if self.fundamentals_loader is not None and self._stage_needs_fundamentals(sub):
            try:
                fundamentals = self.fundamentals_loader.context_for(sub.instrument_key)
            except Exception:
                fundamentals = None
                logger.warning(
                    "fundamentals context unavailable for %s", sub.instrument_key,
                    exc_info=True,
                )
            if fundamentals:
                context = dict(context) if context else {}
                context.update(fundamentals)
                self.health["fundamentals_hits"] += 1
                acquired_at = fundamentals.get("fundamentals.acquired_at")
                if acquired_at and _age_hours(acquired_at) > self.fundamentals_stale_hours:
                    self.health["fundamentals_stale"] += 1
            else:
                self.health["fundamentals_misses"] += 1
        # Phase 4 F10: registered external producer values, sampled at this
        # observation's event time. Absent/expired/late/future/revoked inputs
        # resolve to unknown with a named reason; nothing here can manufacture
        # a signal.
        if self.external_loader is not None:
            references = self._external_references_for(sub)
            if references:
                try:
                    external = self.external_loader.context_for(
                        sub.owner_id,
                        references,
                        cutoff=obs.ts,
                        instrument_key=sub.instrument_key,
                    )
                except Exception:
                    external = None
                    logger.warning(
                        "external signal context unavailable for %s",
                        sub.instrument_key, exc_info=True,
                    )
                if external:
                    context = dict(context) if context else {}
                    context.update(external)
        # Phase 4 F10: cross-instrument pair values for this observation.
        pair_specs = self._pair_operands_for(sub)
        if pair_specs:
            pairs = self._resolve_pairs(sub, obs, pair_specs)
            if pairs:
                context = dict(context) if context else {}
                context["pairs"] = pairs
        # Phase 4 F10: a breadth stage aggregates its member subscriptions, so
        # it needs the member set (and when that set was materialized) at
        # dispatch time. The set is the instrument keys materialized for THIS
        # stage in THIS revision — the same rows the worker is dispatching
        # from — and the timestamp is the last membership materialization.
        breadth_membership = None
        if self._is_breadth_stage(sub):
            breadth_membership = self._breadth_membership_for(sub)
        supports_context = False
        supports_features = False
        supports_layers = False
        supports_breadth = False
        try:
            params = inspect.signature(self.service.handle_observation).parameters
            supports_context = "context" in params
            supports_features = "features" in params
            supports_layers = "layers" in params
            supports_breadth = "breadth_membership" in params
        except (TypeError, ValueError):
            supports_context = False
        try:
            if allow_emit or not self._service_supports_allow_emit():
                kwargs = {"context": context} if supports_context else {}
                if supports_features:
                    kwargs["features"] = features
                if supports_layers:
                    kwargs["layers"] = layers
                if supports_breadth and breadth_membership is not None:
                    kwargs["breadth_membership"] = breadth_membership
                result = self.service.handle_observation(sub, obs, **kwargs)
            else:
                kwargs = {"allow_emit": False}
                if supports_context:
                    kwargs["context"] = context
                if supports_features:
                    kwargs["features"] = features
                if supports_layers:
                    kwargs["layers"] = layers
                if supports_breadth and breadth_membership is not None:
                    kwargs["breadth_membership"] = breadth_membership
                result = self.service.handle_observation(sub, obs, **kwargs)
        except TypeError:
            logger.error(
                "evaluation rejected observation for subscription %s (%s)",
                sub.id, sub.instrument_key, exc_info=True,
            )
            return
        except Exception:
            logger.error(
                "evaluation crashed for subscription %s (%s)", sub.id, sub.instrument_key,
                exc_info=True,
            )
            return
        if result.emitted:
            self.health["emitted"] += 1
        if result.suppression_reason:
            self.health["suppressed"][result.suppression_reason] += 1
            if result.suppression_reason == "feed_gap":
                self.health["gaps"] += 1
