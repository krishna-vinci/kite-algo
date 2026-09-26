"""The market-session clock: hours, weekends, imported holidays (fail closed).

The gate exists so a live plan cannot OPEN exposure while the market is shut,
and so the hosted scheduler can say WHY a daily/weekly occurrence never ran.
The calendar is the imported, verified NSE one; a day the calendar does not
answer for is UNKNOWN, and unknown is never "open" for admission.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from backend.broker_api.market.exchange_calendar import CalendarUnavailable
from backend.strategies.market_session import day_state, is_gated_exchange, session_state


def _utc(*args: int) -> datetime:
    """A UTC instant (the session clock converts it to IST itself)."""
    return datetime(*args, tzinfo=timezone.utc)


def _raising_reader(*_args):
    raise AssertionError("the calendar must not be read for this case")


def test_a_weekend_is_closed_without_reading_the_calendar():
    # 2026-10-10 is a Saturday; 04:30 UTC is 10:00 IST, inside the session clock.
    state = session_state("NSE", _utc(2026, 10, 10, 4, 30), trading_day_reader=_raising_reader)
    assert state["open"] is False
    assert state["reason"] == "weekend"


def test_a_holiday_is_closed_before_the_open():
    def reader(_exchange, day):
        return day != date(2026, 10, 14)

    state = session_state("NFO", _utc(2026, 10, 14, 4, 0), trading_day_reader=reader)
    assert state["open"] is False
    assert state["reason"] == "holiday"
    # The next session is the following trading day at 09:15 IST.
    assert state["next_open"] == "2026-10-15T09:15:00+05:30"


def test_a_trading_day_before_the_open_names_the_opening_bell():
    state = session_state("NSE", _utc(2026, 10, 14, 3, 40), trading_day_reader=lambda *_: True)
    assert state["open"] is False
    assert state["reason"] == "before_open"
    assert state["next_open"] == "2026-10-14T09:15:00+05:30"


def test_a_trading_day_after_the_close_rolls_past_the_next_holiday():
    def reader(_exchange, day):
        return day != date(2026, 10, 15)

    state = session_state("BSE", _utc(2026, 10, 14, 10, 30), trading_day_reader=reader)
    assert state["open"] is False
    assert state["reason"] == "after_close"
    # 2026-10-15 is a holiday for this reader, so the bell is Friday's.
    assert state["next_open"] == "2026-10-16T09:15:00+05:30"


def test_an_open_session_is_open():
    state = session_state("NSE", _utc(2026, 10, 14, 5, 0), trading_day_reader=lambda *_: True)
    assert state["open"] is True
    assert state["reason"] == "open"


def test_an_unreadable_calendar_fails_closed():
    def reader(_exchange, _day):
        raise CalendarUnavailable("CALENDAR_UNAVAILABLE")

    state = session_state("NSE", _utc(2026, 10, 14, 5, 0), trading_day_reader=reader)
    assert state["open"] is False
    assert state["reason"] == "calendar_unavailable"
    assert state["next_open"] is None


def test_an_exchange_without_an_imported_calendar_is_not_gated():
    state = session_state("MCX", _utc(2026, 10, 10, 4, 30), trading_day_reader=_raising_reader)
    assert state["open"] is True
    assert state["reason"] == "not_gated"


def test_day_state_separates_weekend_holiday_and_unknown():
    assert day_state("NSE", date(2026, 10, 10), trading_day_reader=_raising_reader) == "weekend"
    assert day_state("NSE", date(2026, 10, 14), trading_day_reader=lambda *_: False) == "holiday"
    assert day_state("NSE", date(2026, 10, 14), trading_day_reader=lambda *_: True) == "trading"
    assert day_state("MCX", date(2026, 10, 14), trading_day_reader=_raising_reader) == "not_gated"


def test_the_gated_set_covers_equity_and_derivatives_only():
    assert [key for key in ("NSE", "BSE", "NFO", "BFO") if is_gated_exchange(key)] == [
        "NSE",
        "BSE",
        "NFO",
        "BFO",
    ]
    assert is_gated_exchange("MCX") is False
