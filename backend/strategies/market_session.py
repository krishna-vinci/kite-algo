"""Exchange session state: market hours, weekends and imported trading holidays.

One place decides whether an exchange is open right now, so the live admission
gate and the hosted scheduler cannot drift apart:

* an exposure-INCREASING live plan needs an OPEN session;
* a daily/weekly schedule occurrence that lands on a weekend or an NSE holiday
  is recorded as skipped with that reason, never run and never silently dropped.

The trading-day answer comes exclusively from the imported, verified calendar
(``backend.broker_api.market.exchange_calendar``) through the same read path the
operator ``GET /strategies/calendar`` route uses. Nothing here infers a session
from a weekday: a weekday the calendar does not cover is UNKNOWN, and UNKNOWN
fails closed for live admission rather than reading as open.

MCX is the one gated exchange without an imported calendar of its own. Its
session is a real clock (09:00 to ``MCX_SESSION_CLOSE``, 23:30 IST by default)
and its weekends are closed, but a weekday carries no holiday claim: the answer
is marked ``not_verified_holiday`` so an unimported holiday reads as a stated
note rather than as a silent "closed" or a silent "open".
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: India Standard Time: the clock every gated exchange trades on. A fixed
#: offset, because India keeps no daylight saving.
IST = timezone(timedelta(hours=5, minutes=30))

#: The session every gated exchange keeps (equity and derivatives alike).
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

#: MCX's session clock: an early commodity open and a late close, because the
#: commodity segment trades one long session rather than the equity one.
MCX_SESSION_OPEN = time(9, 0)
DEFAULT_MCX_SESSION_CLOSE = time(23, 30)

#: The environment knob for the MCX close. A commodity close is a venue policy,
#: not a platform constant, so it is configuration with a stated default.
MCX_SESSION_CLOSE_ENV = "MCX_SESSION_CLOSE"

#: Exchanges whose sessions the platform gates. Anything else (currency, ...) is
#: deliberately NOT gated: no clock of its own is published, and inventing one
#: would gate trades on a guess.
GATED_EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "MCX")

#: Gated exchanges whose days come from the imported NSE calendar. MCX is gated
#: on its own clock instead: only its weekends are known closed.
CALENDAR_BACKED_EXCHANGES = ("NSE", "BSE", "NFO", "BFO")

#: The imported calendar that answers for every gated exchange: only NSE/CM is
#: imported today, and the equity/BSE/derivative exchanges trade on the same NSE
#: trading holidays.
CALENDAR_SOURCE = ("NSE", "CM")

#: The note carried by any MCX answer whose weekday is unverified: the MCX
#: holiday calendar is not imported, so a weekday is "not a weekend", not a
#: proven trading day.
UNVERIFIED_HOLIDAY = "not_verified_holiday"

#: How long a session-length hosted job may outlive the session before the
#: supervisor stops it. The close-time stop request remains the normal path.
SESSION_JOB_GRACE_SECONDS = 300

#: How far ahead a closed session looks for the next opening bell. A week and a
#: half covers a long holiday cluster; a gap beyond it reports no ``next_open``
#: rather than walking the calendar forever.
NEXT_OPEN_LOOKAHEAD_DAYS = 15

#: A trading-day predicate: ``reader(exchange, day) -> bool``. It must RAISE when
#: the calendar cannot be read - an unreadable calendar is not a trading day.
TradingDayReader = Callable[[str, date], bool]


def is_gated_exchange(exchange: Any) -> bool:
    """Whether the platform gates this exchange's session."""
    return str(exchange or "").strip().upper() in GATED_EXCHANGES


def is_calendar_backed_exchange(exchange: Any) -> bool:
    """Whether this exchange's days come from the imported calendar."""
    return str(exchange or "").strip().upper() in CALENDAR_BACKED_EXCHANGES


def mcx_session_close() -> time:
    """MCX's close (IST): ``MCX_SESSION_CLOSE`` when usable, else 23:30.

    A malformed value, or one that would not leave a session at all, falls back
    to the default rather than opening a window the venue does not run - and says
    so in the log instead of failing silently.
    """
    raw = str(os.environ.get(MCX_SESSION_CLOSE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MCX_SESSION_CLOSE
    try:
        hour, minute = raw.split(":", 1)
        parsed = time(int(hour), int(minute))
    except (TypeError, ValueError):
        logger.warning(
            "mcx_session_close_invalid",
            extra={"variable": MCX_SESSION_CLOSE_ENV, "value": raw},
        )
        return DEFAULT_MCX_SESSION_CLOSE
    if parsed <= MCX_SESSION_OPEN:
        logger.warning(
            "mcx_session_close_before_open",
            extra={"variable": MCX_SESSION_CLOSE_ENV, "value": raw},
        )
        return DEFAULT_MCX_SESSION_CLOSE
    return parsed


def session_window(exchange: Any) -> tuple:
    """The open/close clock (IST) this exchange trades on."""
    if str(exchange or "").strip().upper() == "MCX":
        return (MCX_SESSION_OPEN, mcx_session_close())
    return (SESSION_OPEN, SESSION_CLOSE)


def session_job_duration_s(exchange: Any) -> int:
    """How long one session-length hosted job may run for this exchange."""
    opens_at, closes_at = session_window(exchange)
    minutes = (closes_at.hour * 60 + closes_at.minute) - (
        opens_at.hour * 60 + opens_at.minute
    )
    return int(minutes * 60) + SESSION_JOB_GRACE_SECONDS


def day_state(
    exchange: Any,
    day: date,
    *,
    trading_day_reader: Optional[TradingDayReader] = None,
) -> str:
    """One day's state: ``trading`` / ``weekend`` / ``holiday`` / ...

    Returns ``weekend``, ``holiday``, ``trading``, ``calendar_unavailable`` (the
    calendar could not be read) or ``not_gated`` (this exchange has no imported
    clock, so no day-level claim is made). Weekends never consult the calendar: a
    Saturday is closed whether or not the calendar is readable. An MCX weekday is
    ``trading`` on its own clock and never reads the NSE calendar, so an NSE
    holiday cannot silently close the commodity session.
    """
    key = str(exchange or "").strip().upper()
    if key not in GATED_EXCHANGES:
        return "not_gated"
    if day.weekday() >= 5:
        return "weekend"
    if not is_calendar_backed_exchange(key):
        # MCX: the weekday is real, its holidays are not imported, and that gap
        # is named (``not_verified_holiday``) on the session answer.
        return "trading"
    reader = trading_day_reader or _default_trading_day_reader
    try:
        return "trading" if bool(reader(key, day)) else "holiday"
    except Exception as exc:  # noqa: BLE001 - an unreadable calendar is never "open"
        logger.warning(
            "market_calendar_unavailable",
            extra={"exchange": key, "day": day.isoformat(), "error": repr(exc)},
        )
        return "calendar_unavailable"


def session_state(
    exchange: Any,
    now: Optional[datetime] = None,
    *,
    trading_day_reader: Optional[TradingDayReader] = None,
) -> Dict[str, Any]:
    """Whether ``exchange`` is open at ``now``, and why not when it is not.

    ``reason`` is one of ``open`` / ``before_open`` / ``after_close`` /
    ``weekend`` / ``holiday`` / ``calendar_unavailable`` / ``not_gated``.
    ``next_open`` is the next opening bell (IST, ISO) when it is cheap to work
    out, and ``None`` when it is not. An exchange whose holidays are not imported
    (MCX) carries ``holiday_status: not_verified_holiday``, so no caller reads an
    unverified weekday as a verified trading day.
    """
    key = str(exchange or "").strip().upper()
    if key not in GATED_EXCHANGES:
        return {"exchange": key, "open": True, "reason": "not_gated", "next_open": None}

    moment = _as_aware(now)
    local = moment.astimezone(IST)
    today = local.date()
    reader = trading_day_reader or _default_trading_day_reader

    state = day_state(key, today, trading_day_reader=reader)
    if state in ("weekend", "holiday", "calendar_unavailable"):
        return _with_notes(
            key,
            {
                "exchange": key,
                "open": False,
                "reason": state,
                "session_date": today.isoformat(),
                "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
            },
        )

    opens_at, closes_at = session_window(key)
    clock = local.time().replace(tzinfo=None)
    if clock < opens_at:
        return _with_notes(
            key,
            {
                "exchange": key,
                "open": False,
                "reason": "before_open",
                "session_date": today.isoformat(),
                "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
            },
        )
    if clock >= closes_at:
        return _with_notes(
            key,
            {
                "exchange": key,
                "open": False,
                "reason": "after_close",
                "session_date": today.isoformat(),
                "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
            },
        )
    return _with_notes(
        key,
        {
            "exchange": key,
            "open": True,
            "reason": "open",
            "session_date": today.isoformat(),
            "next_open": None,
        },
    )


def _with_notes(key: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Add the provenance note this exchange's clock can honestly make."""
    if is_calendar_backed_exchange(key):
        state["session_source"] = "calendar"
        return state
    state["session_source"] = "session_window"
    state["holiday_status"] = UNVERIFIED_HOLIDAY
    state["detail"] = {
        "session_source": "session_window",
        "holiday_source": "not_imported",
        "reason": UNVERIFIED_HOLIDAY,
    }
    return state


def _next_open_iso(
    exchange: str,
    start_day: date,
    *,
    reader: TradingDayReader,
    now_local: datetime,
) -> Optional[str]:
    """The ISO instant of the next opening bell at or after ``start_day``."""
    day = start_day
    opens_at = session_window(exchange)[0]
    for _ in range(NEXT_OPEN_LOOKAHEAD_DAYS):
        state = day_state(exchange, day, trading_day_reader=reader)
        if state == "calendar_unavailable":
            return None
        if state in ("trading", "not_gated"):
            opening = datetime.combine(day, opens_at, tzinfo=IST)
            if opening > now_local:
                return opening.isoformat()
        day += timedelta(days=1)
    return None


def _as_aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _default_trading_day_reader(exchange: str, day: date) -> bool:
    """The imported calendar's own answer for one day, or a raise (fail closed).

    Reads through the audited calendar service - never a weekday guess - and
    opens its own short-lived connection because the caller (admission, the
    scheduler) holds no calendar transaction of its own.
    """
    source_exchange, segment = CALENDAR_SOURCE
    from backend.app.database import get_db_connection
    from backend.broker_api.market.exchange_calendar import get_calendar_sessions

    conn = get_db_connection()
    try:
        payload = get_calendar_sessions(
            conn,
            exchange=source_exchange,
            segment=segment,
            from_date=day,
            to_date=day,
        )
    finally:
        conn.close()
    sessions = list(payload.get("sessions") or [])
    if len(sessions) != 1:
        raise RuntimeError("CALENDAR_DAY_NOT_COVERED")
    session_type = str(sessions[0].get("session_type") or "").upper()
    return session_type in ("REGULAR", "SPECIAL")
