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
  can never manufacture a crossing (E-5).
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
warmups, unresolved_channels) for F11. A caller-provided ``channel_resolver``
maps ``(owner_id, channel names) -> {name: channel_id}``; the worker wraps it
to count unresolved names in health. Shutdown is graceful on cancellation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
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

from backend.alerts.predicates import Observation
from backend.workflows.models import Stage, WorkflowDocument
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict
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
    "EvaluationWorker",
    "MARKET_TICKS_CHANNEL",
    "REALTIME_CANDLES_CHANNEL_PREFIX",
    "CANDLE_EPOCH_ID",
]

logger = logging.getLogger(__name__)

MARKET_TICKS_CHANNEL = "market:ticks"
# The candle aggregator publishes completions to realtime_candles:{token}:{interval}
REALTIME_CANDLES_CHANNEL_PREFIX = "realtime_candles:"
CANDLE_EPOCH_ID = "candle"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime:
    """Normalize runtime timestamps (ISO strings or datetimes) to aware UTC."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return _utcnow()
    elif isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    else:
        return _utcnow()
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
            return Observation(ts=ts, epoch_id=self._epoch_id, ltp=float(ltp))

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
                obs = Observation(
                    ts=ts,
                    epoch_id=CANDLE_EPOCH_ID,
                    ltp=float(candle[4]),
                    open=float(candle[1]),
                    high=float(candle[2]),
                    low=float(candle[3]),
                    close=float(candle[4]),
                    volume=float(candle[5] or 0.0),
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

    Built on a caller-provided SQLAlchemy engine; the SQL is issued lazily so
    importing this module never touches a database.
    """

    def __init__(self, engine: Any, instrument_tokens: Dict[str, int]) -> None:
        self._engine = engine
        self._instrument_tokens: Dict[str, int] = {
            str(key): int(token) for key, token in (instrument_tokens or {}).items()
        }

    def recent_bars(self, instrument_key: str, timeframe: str, limit: int) -> List[Observation]:
        token = self._instrument_tokens.get(instrument_key)
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
    ) -> None:
        self.workflow_repo = workflow_repo
        self.session_factory = session_factory
        self.channel_resolver = channel_resolver
        self.tick_source_factory = tick_source_factory
        self.candle_source_factory = candle_source_factory
        self.candle_history = candle_history
        self.poll_interval_s = float(poll_interval_s)
        self.warmup_bars = int(warmup_bars)

        self.health: Dict[str, Any] = {
            "started_at": None,
            "last_evaluated_at": None,
            "evaluations": 0,
            "emitted": 0,
            "suppressed": Counter(),
            "gaps": 0,
            "warmups": 0,
            "unresolved_channels": 0,
        }

        if service is not None:
            self.service = service
        else:
            from backend.workflows.service import EvaluationService

            self.service = EvaluationService(
                workflow_repo,
                session_factory,
                channel_resolver=self._channel_resolver_with_health(),
            )

        self._subscriptions: List[ActiveSubscription] = []
        self._ltp_subs: Dict[str, List[ActiveSubscription]] = {}
        self._candle_subs: Dict[Tuple[str, str], List[ActiveSubscription]] = {}
        self._tick_sources: Dict[str, TickSource] = {}
        self._candle_sources: Dict[Tuple[str, str], TickSource] = {}
        self._document_cache: Dict[str, Optional[WorkflowDocument]] = {}
        self._running = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Materialize subscriptions, warm candle rules, open feed sources."""
        self._ensure_subscription_rows()
        self._subscriptions = list(self.workflow_repo.list_active_subscriptions())

        self._ltp_subs = {}
        self._candle_subs = {}
        for sub in self._subscriptions:
            stage = self._stage_for(sub)
            if stage is None:
                continue
            if stage.clock == "ltp":
                self._ltp_subs.setdefault(sub.instrument_key, []).append(sub)
            elif stage.clock == "candle_close" and stage.timeframe:
                key = (sub.instrument_key, stage.timeframe)
                self._candle_subs.setdefault(key, []).append(sub)

        # Warmup-before-live (spec F2): rebuild candle rule state from durable
        # history so the first live bar can fire on a real crossing.
        for (instrument_key, timeframe), group in self._candle_subs.items():
            bars = self.candle_history.recent_bars(
                instrument_key, timeframe, self.warmup_bars
            )
            for bar in bars:
                self.health["warmups"] += 1
                for sub in group:
                    self._dispatch(sub, bar)

        for instrument_key in sorted({sub.instrument_key for sub in self._ltp_subs}):
            source = self.tick_source_factory(instrument_key)
            await source.start()
            self._tick_sources[instrument_key] = source
        for (instrument_key, timeframe) in sorted(self._candle_subs):
            source = self.candle_source_factory(instrument_key, timeframe)
            await source.start()
            self._candle_sources[(instrument_key, timeframe)] = source

        self.health["started_at"] = _utcnow().isoformat()
        self._running = True
        logger.info(
            "evaluation worker started: %d subscriptions (%d ltp instruments, %d candle groups)",
            len(self._subscriptions), len(self._tick_sources), len(self._candle_sources),
        )

    async def stop(self) -> None:
        self._running = False
        for source in list(self._tick_sources.values()) + list(self._candle_sources.values()):
            try:
                await source.stop()
            except Exception:
                logger.warning("feed source stop failed", exc_info=True)
        self._tick_sources.clear()
        self._candle_sources.clear()

    async def run(self) -> None:
        """Run until cancelled; SIGTERM handling lives in the entrypoint."""
        await self.start()
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
        saw_any = False
        for instrument_key, source in list(self._tick_sources.items()):
            obs = await source.next_observation()
            if obs is None:
                continue
            saw_any = True
            for sub in self._ltp_subs.get(instrument_key, ()):
                self._dispatch(sub, obs)
        for key, source in list(self._candle_sources.items()):
            obs = await source.next_observation()
            if obs is None:
                continue
            saw_any = True
            for sub in self._candle_subs.get(key, ()):
                self._dispatch(sub, obs)
        return saw_any

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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

    def _stage_for(self, sub: ActiveSubscription) -> Optional[Stage]:
        document = self._document_for(sub)
        if document is None:
            return None
        return next((s for s in document.stages if s.id == sub.stage_id), None)

    def _dispatch(self, sub: ActiveSubscription, obs: Observation) -> None:
        self.health["evaluations"] += 1
        self.health["last_evaluated_at"] = _utcnow().isoformat()
        try:
            result = self.service.handle_observation(sub, obs)
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
