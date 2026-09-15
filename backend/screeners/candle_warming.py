"""Bounded, idempotent daily-candle warming for screener universes.

Screeners scan *stored* candles, so a universe whose members have never been
ingested evaluates zero members and reports `unavailable` coverage. For NSE
names that rarely happens (the investment/index pipelines backfill them), but an
MCX futures universe had no path that acquired daily history at all: the
post-close finalizer targets NSE indexes, and the worker-facing history API only
ingests when a caller asks for one symbol.

This module fills that gap with one bounded operation:

* only the members the screener definition actually resolved are considered —
  never the instrument catalog, never thousands of symbols;
* members that already hold ``required_bars`` final daily candles are skipped
  (so repeated runs and restarts are no-ops);
* the fetch itself goes through the existing authenticated adapter
  (:class:`backend.broker_api.market.candle_ingestion.CandleIngestion`), which
  chunks by the broker's per-interval limit, sleeps between requests and upserts
  (idempotent by construction);
* the run is bounded by member count and a wall-clock budget, and anything left
  over is reported as ``skipped`` so a later run continues where this one
  stopped;
* every member carries its catalog-resolved broker token and the catalog
  generation it came from, so a stale or expired binding is visible instead of
  silently fetching the wrong instrument.

Failure is never fatal: a member that cannot be fetched is reported
``unavailable`` with a reason, and the caller keeps its existing
coverage semantics.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

#: Daily bar finality per session policy. NSE equity closes at 15:30 IST and the
#: screener schedules align to that close, so its daily bar is final when the
#: bucket fires. MCX (and currency) sessions run well past the NSE close, and
#: their schedules are anchored to the NSE calendar, so a bucket can fire while
#: the exchange is still trading — those bars must not be ranked until the
#: session is closed and the provider has settled the row.
SESSION_DAILY_CLOSE_IST: dict[str, dtime] = {
    "nse_equity": dtime(15, 30),
    "mcx_commodity": dtime(23, 30),
    "currency": dtime(17, 0),
}

#: Sessions whose schedule is NOT the session's own close (feed-driven policies).
FEED_DRIVEN_SESSIONS = frozenset({"mcx_commodity", "currency"})

#: Grace period after the close before a daily row is trusted as final.
FINALITY_DELAY = timedelta(minutes=15)

DEFAULT_REQUIRED_BARS = 30
DEFAULT_MAX_MEMBERS = 25
DEFAULT_LOOKBACK_DAYS = 200
DEFAULT_DEADLINE_S = 90.0

STATUS_FRESH = "fresh"
STATUS_WARMED = "warmed"
STATUS_UNAVAILABLE = "unavailable"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"

OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_SKIPPED = "skipped"
OUTCOME_UNAVAILABLE = "unavailable"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def bar_session_date(bar_ts: datetime) -> date:
    """The IST session date a stored daily bar belongs to."""
    return _as_utc(bar_ts).astimezone(IST).date()


def daily_session_is_final(session: str, session_date: date, now: datetime) -> bool:
    """Whether the daily bar for ``session_date`` may be treated as final.

    Unknown sessions return ``True``: finality filtering is an extra guard for
    sessions whose schedule differs from their own close, and inventing a close
    time for a policy we do not know would silently drop data.
    """
    close = SESSION_DAILY_CLOSE_IST.get(str(session or "").strip())
    if close is None:
        return True
    closes_at = datetime.combine(session_date, close, tzinfo=IST) + FINALITY_DELAY
    return _as_utc(now).astimezone(IST) >= closes_at


def applies_daily_finality(session: str) -> bool:
    """Whether the screener should drop a still-forming daily bar.

    Only feed-driven sessions (MCX, currency): their schedules run on the NSE
    calendar, so a bucket can fire mid-session. NSE equity schedules fire at its
    own close, where the newest bar already is the session's close.
    """
    return str(session or "").strip() in FEED_DRIVEN_SESSIONS


@dataclass(frozen=True)
class WarmMember:
    instrument_key: str
    status: str
    broker_token: Optional[int] = None
    bars: int = 0
    required_bars: int = DEFAULT_REQUIRED_BARS
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "status": self.status,
            "broker_token": self.broker_token,
            "bars": self.bars,
            "required_bars": self.required_bars,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WarmOutcome:
    status: str
    requested: int
    fresh: int
    warmed: int
    unavailable: int
    skipped: int
    expired: int
    required_bars: int
    catalog_generation: Optional[str]
    duration_s: float
    budget_exhausted: bool
    members: tuple[WarmMember, ...] = field(default=())

    @property
    def changed(self) -> int:
        return self.warmed

    def to_coverage(self) -> dict:
        """Compact, durable summary for a screener run's coverage."""
        return {
            "status": self.status,
            "requested": self.requested,
            "warmed": self.warmed,
            "fresh": self.fresh,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "expired": self.expired,
            "required_bars": self.required_bars,
            "catalog_generation": self.catalog_generation,
            "budget_exhausted": self.budget_exhausted,
            "duration_s": round(self.duration_s, 3),
            "members": [m.to_dict() for m in self.members],
        }


def _run_sync(factory: Callable[[], Any]) -> Any:
    """Run an awaitable factory from synchronous code.

    The screener pipeline is synchronous and is driven either from the queue
    worker's executor thread (scheduled runs) or from a request handler
    (manual runs), so there is normally no running loop in this thread. When
    there is one, run the coroutine in a private loop on another thread rather
    than trying to nest loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


class ScreenerCandleWarmer:
    """Acquire the daily history a screener definition needs, bounded."""

    def __init__(
        self,
        candle_history: Any,
        *,
        interval: str = "day",
        required_bars: int = DEFAULT_REQUIRED_BARS,
        max_members: int = DEFAULT_MAX_MEMBERS,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        deadline_s: float = DEFAULT_DEADLINE_S,
        catalog: Any = None,
        ingestion_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.candle_history = candle_history
        self.interval = interval
        self.required_bars = max(2, int(required_bars))
        self.max_members = max(1, int(max_members))
        self.lookback_days = max(self.required_bars, int(lookback_days))
        self.deadline_s = float(deadline_s)
        self._catalog = catalog
        self._ingestion_factory = ingestion_factory
        self._clock = clock

    # -- read-only view -------------------------------------------------

    def _catalog_client(self) -> Any:
        if self._catalog is not None:
            return self._catalog
        from backend.broker_api.instruments.catalog import InstrumentCatalog

        return InstrumentCatalog()

    def _describe(self, instrument_key: str) -> Any:
        """Resolve the catalog identity (and its CURRENT broker token)."""
        return self._catalog_client().resolve_public_key(instrument_key)

    def _final_bars(self, instrument_key: str, *, session: str, as_of: datetime) -> list:
        """Stored daily bars the run may consume (bounded, final, <= as_of)."""
        try:
            recent = self.candle_history.recent_bars(
                instrument_key, self.interval, self.required_bars + 5
            )
        except Exception as exc:  # unreadable history is "unknown", not empty
            logger.warning("candle history read failed for %s: %s", instrument_key, exc)
            return []
        consumed = []
        for bar in recent or []:
            ts = getattr(bar, "ts", None)
            if ts is None:
                continue
            if _as_utc(ts) > _as_utc(as_of):
                continue
            if applies_daily_finality(session) and not daily_session_is_final(
                session, bar_session_date(ts), self._clock()
            ):
                continue
            consumed.append(bar)
        return consumed

    def data_status(
        self, members: Sequence[str], *, session: str, as_of: Optional[datetime] = None
    ) -> list[dict]:
        """Per-member candle availability, for operator visibility."""
        moment = as_of or self._clock()
        rows = []
        for key in members:
            bars = self._final_bars(str(key), session=session, as_of=moment)
            latest = bars[-1].ts if bars else None
            rows.append(
                {
                    "instrument_key": str(key),
                    "bars": len(bars),
                    "required_bars": self.required_bars,
                    "sufficient": len(bars) >= self.required_bars,
                    "last_candle_ts": latest.isoformat() if latest is not None else None,
                    "warming": len(bars) < self.required_bars,
                }
            )
        return rows

    # -- warming --------------------------------------------------------

    def _ingestion(self) -> Any:
        if self._ingestion_factory is not None:
            return self._ingestion_factory()
        from backend.broker_api.market.candle_ingestion import CandleIngestion
        from backend.app.database import SessionLocal
        from backend.broker_api.session.kite_session import (
            build_kite_client,
            get_system_access_token,
        )

        with SessionLocal() as db:
            access_token = get_system_access_token(db)
        if not access_token:
            raise RuntimeError("no system broker session is available for candle warming")
        return CandleIngestion(build_kite_client(access_token, session_id="system"))

    def ensure_members(
        self,
        members: Iterable[str],
        *,
        session: str,
        as_of: Optional[datetime] = None,
    ) -> WarmOutcome:
        """Warm the members that need it, bounded by count and by time.

        Idempotent: a member that already holds ``required_bars`` final candles
        is untouched, and the fetch itself upserts.
        """
        started = time.monotonic()
        moment = as_of or self._clock()
        ordered = sorted({str(m) for m in members if str(m).strip()})
        selected = ordered[: self.max_members]
        overflow = ordered[self.max_members :]

        results: list[WarmMember] = [
            WarmMember(
                instrument_key=key,
                status=STATUS_SKIPPED,
                required_bars=self.required_bars,
                detail="member cap reached; run again to continue warming",
            )
            for key in overflow
        ]

        generation: Optional[str] = None
        warmed = fresh = unavailable = expired = 0
        budget_exhausted = False

        for index, key in enumerate(selected):
            if time.monotonic() - started > self.deadline_s:
                budget_exhausted = True
                for remaining in selected[index:]:
                    results.append(
                        WarmMember(
                            instrument_key=remaining,
                            status=STATUS_SKIPPED,
                            required_bars=self.required_bars,
                            detail="wall-clock budget reached; run again to continue warming",
                        )
                    )
                break

            try:
                descriptor = self._describe(key)
            except Exception as exc:
                unavailable += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_UNAVAILABLE,
                        required_bars=self.required_bars,
                        detail=f"catalog resolution failed: {type(exc).__name__}",
                    )
                )
                continue

            generation = getattr(descriptor, "catalog_generation", None) or generation
            token = int(getattr(descriptor, "broker_token", 0) or 0)
            lifecycle = str(getattr(descriptor, "lifecycle_status", "") or "").lower()

            bars = self._final_bars(key, session=session, as_of=moment)
            if len(bars) >= self.required_bars:
                fresh += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_FRESH,
                        broker_token=token,
                        bars=len(bars),
                        required_bars=self.required_bars,
                    )
                )
                continue

            if lifecycle and lifecycle != "active":
                # An expired/delisted contract must never be silently bound to a
                # token that may already belong to something else.
                expired += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_EXPIRED,
                        broker_token=token,
                        bars=len(bars),
                        required_bars=self.required_bars,
                        detail=f"catalog lifecycle is {lifecycle!r}; not warming",
                    )
                )
                continue

            try:
                fetched = self._ingest(token, moment)
            except Exception as exc:
                unavailable += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_UNAVAILABLE,
                        broker_token=token,
                        bars=len(bars),
                        required_bars=self.required_bars,
                        detail=f"{type(exc).__name__}: {str(exc)[:160]}",
                    )
                )
                continue

            after = self._final_bars(key, session=session, as_of=moment)
            if len(after) >= self.required_bars:
                warmed += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_WARMED,
                        broker_token=token,
                        bars=len(after),
                        required_bars=self.required_bars,
                        detail=str(fetched.get("status")) if isinstance(fetched, dict) else None,
                    )
                )
            else:
                unavailable += 1
                results.append(
                    WarmMember(
                        instrument_key=key,
                        status=STATUS_UNAVAILABLE,
                        broker_token=token,
                        bars=len(after),
                        required_bars=self.required_bars,
                        detail=(
                            f"broker returned {str(fetched.get('status')) if isinstance(fetched, dict) else 'unknown'}; "
                            f"{len(after)}/{self.required_bars} final bars"
                        ),
                    )
                )

        if not selected and not results:
            status = OUTCOME_UNAVAILABLE
        elif budget_exhausted:
            status = OUTCOME_SKIPPED
        elif warmed and (unavailable or expired):
            status = OUTCOME_PARTIAL
        elif unavailable and not warmed and not fresh:
            status = OUTCOME_UNAVAILABLE
        elif expired and not warmed:
            status = OUTCOME_PARTIAL
        elif warmed or fresh:
            status = OUTCOME_COMPLETE
        else:
            status = OUTCOME_PARTIAL

        return WarmOutcome(
            status=status,
            requested=len(ordered),
            fresh=fresh,
            warmed=warmed,
            unavailable=unavailable,
            skipped=sum(1 for m in results if m.status == STATUS_SKIPPED),
            expired=expired,
            required_bars=self.required_bars,
            catalog_generation=generation,
            duration_s=time.monotonic() - started,
            budget_exhausted=budget_exhausted,
            members=tuple(results),
        )

    def _ingest(self, broker_token: int, as_of: datetime) -> Any:
        """Fetch the bounded lookback window through the authenticated adapter."""
        from_dt = _as_utc(as_of) - timedelta(days=self.lookback_days)
        ingestion = self._ingestion()
        return _run_sync(
            lambda: ingestion.ingest_historical_data(
                int(broker_token),
                self.interval,
                from_dt,
                _as_utc(as_of),
                force_refresh=True,
            )
        )


def build_screener_warmer(candle_history: Any, *, required_bars: int, env: Any = None) -> ScreenerCandleWarmer:
    """Production warmer: same catalog/history the pipeline reads, bounded by env.

    Every bound is configurable so an operator can widen or narrow it without a
    code change, and every default is deliberately small: the warmer is meant to
    make progress across runs, not to hold a request open.
    """
    import os

    values = os.environ if env is None else env
    return ScreenerCandleWarmer(
        candle_history,
        required_bars=required_bars,
        max_members=int(values.get("ALERTS_SCREENER_WARM_MAX_MEMBERS", DEFAULT_MAX_MEMBERS)),
        lookback_days=int(values.get("ALERTS_SCREENER_WARM_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)),
        deadline_s=float(values.get("ALERTS_SCREENER_WARM_DEADLINE_S", DEFAULT_DEADLINE_S)),
    )
