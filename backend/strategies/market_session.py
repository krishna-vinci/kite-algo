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
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: India Standard Time: the clock every gated exchange trades on. A fixed
#: offset, because India keeps no daylight saving.
IST = timezone(timedelta(hours=5, minutes=30))

#: The session every gated exchange keeps (equity and derivatives alike).
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

#: Exchanges whose sessions follow the imported NSE trading calendar. Anything
#: else (MCX, currency, ...) is deliberately NOT gated: no imported calendar of
#: its own exists, and inventing a clock for it is worse than not gating.
GATED_EXCHANGES = ("NSE", "BSE", "NFO", "BFO")

#: The imported calendar that answers for every gated exchange: only NSE/CM is
#: imported today, and the other three trade on the same NSE trading holidays.
CALENDAR_SOURCE = ("NSE", "CM")

#: How far ahead a closed session looks for the next opening bell. A week and a
#: half covers a long holiday cluster; a gap beyond it reports no ``next_open``
#: rather than walking the calendar forever.
NEXT_OPEN_LOOKAHEAD_DAYS = 15

#: A trading-day predicate: ``reader(exchange, day) -> bool``. It must RAISE when
#: the calendar cannot be read - an unreadable calendar is not a trading day.
TradingDayReader = Callable[[str, date], bool]


def is_gated_exchange(exchange: Any) -> bool:
    """Whether this exchange's session is defined by the imported calendar."""
    return str(exchange or "").strip().upper() in GATED_EXCHANGES


def day_state(
    exchange: Any,
    day: date,
    *,
    trading_day_reader: Optional[TradingDayReader] = None,
) -> str:
    """One day's state: ``trading`` / ``weekend`` / ``holiday`` / ...

    Returns ``weekend``, ``holiday``, ``trading``, ``calendar_unavailable`` (the
    calendar could not be read) or ``not_gated`` (this exchange has no imported
    calendar, so no day-level claim is made). Weekends never consult the
    calendar: a Saturday is closed whether or not the calendar is readable.
    """
    key = str(exchange or "").strip().upper()
    if key not in GATED_EXCHANGES:
        return "not_gated"
    if day.weekday() >= 5:
        return "weekend"
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
    out, and ``None`` when it is not.
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
        return {
            "exchange": key,
            "open": False,
            "reason": state,
            "session_date": today.isoformat(),
            "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
        }

    clock = local.time().replace(tzinfo=None)
    if clock < SESSION_OPEN:
        return {
            "exchange": key,
            "open": False,
            "reason": "before_open",
            "session_date": today.isoformat(),
            "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
        }
    if clock >= SESSION_CLOSE:
        return {
            "exchange": key,
            "open": False,
            "reason": "after_close",
            "session_date": today.isoformat(),
            "next_open": _next_open_iso(key, today, reader=reader, now_local=local),
        }
    return {
        "exchange": key,
        "open": True,
        "reason": "open",
        "session_date": today.isoformat(),
        "next_open": None,
    }


def _next_open_iso(
    exchange: str,
    start_day: date,
    *,
    reader: TradingDayReader,
    now_local: datetime,
) -> Optional[str]:
    """The ISO instant of the next opening bell at or after ``start_day``."""
    day = start_day
    for _ in range(NEXT_OPEN_LOOKAHEAD_DAYS):
        state = day_state(exchange, day, trading_day_reader=reader)
        if state == "calendar_unavailable":
            return None
        if state in ("trading", "not_gated"):
            opening = datetime.combine(day, SESSION_OPEN, tzinfo=IST)
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
