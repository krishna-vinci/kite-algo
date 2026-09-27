"""The market-session clock: hours, weekends, imported holidays (fail closed).

The gate exists so a live plan cannot OPEN exposure while the market is shut,
and so the hosted scheduler can say WHY a daily/weekly occurrence never ran.
The calendar is the imported, verified NSE one; a day the calendar does not
answer for is UNKNOWN, and unknown is never "open" for admission.

MCX is gated on its own clock instead (09:00 to 23:30 IST, weekends closed) and
says so: its holiday calendar is not imported, so a commodity weekday carries the
``not_verified_holiday`` note rather than a verified trading-day claim.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from backend.broker_api.market.exchange_calendar import CalendarUnavailable
from backend.strategies.market_session import (
    day_state,
    is_gated_exchange,
    mcx_session_close,
    session_job_duration_s,
    session_state,
    session_window,
)


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


def test_an_exchange_without_a_published_clock_is_not_gated():
    """Currency publishes no clock of its own, so no day-level claim is made."""
    state = session_state("CDS", _utc(2026, 10, 10, 4, 30), trading_day_reader=_raising_reader)
    assert state["open"] is True
    assert state["reason"] == "not_gated"


def test_day_state_separates_weekend_holiday_and_unknown():
    assert day_state("NSE", date(2026, 10, 10), trading_day_reader=_raising_reader) == "weekend"
    assert day_state("NSE", date(2026, 10, 14), trading_day_reader=lambda *_: False) == "holiday"
    assert day_state("NSE", date(2026, 10, 14), trading_day_reader=lambda *_: True) == "trading"
    assert day_state("CDS", date(2026, 10, 14), trading_day_reader=_raising_reader) == "not_gated"


def test_the_gated_set_covers_equities_derivatives_and_commodities():
    assert [
        key for key in ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS") if is_gated_exchange(key)
    ] == ["NSE", "BSE", "NFO", "BFO", "MCX"]


def test_the_commodity_window_is_the_published_one():
    assert session_window("MCX") == (time(9, 0), time(23, 30))
    # Case-folded, because the scheduler passes a stored exchange value.
    assert session_window("mcx") == (time(9, 0), time(23, 30))
    # A session job outlives the 14.5-hour window by the fence margin only.
    assert session_job_duration_s("MCX") == 14 * 3600 + 30 * 60 + 300


def test_the_equity_window_is_unchanged():
    assert session_window("NSE") == (time(9, 15), time(15, 30))
    assert session_job_duration_s("NSE") == 22500 + 300


def test_the_commodity_close_is_configurable(monkeypatch):
    monkeypatch.setenv("MCX_SESSION_CLOSE", "19:00")
    assert mcx_session_close() == time(19, 0)
    assert session_window("MCX") == (time(9, 0), time(19, 0))
    # 14:00 IST is inside the shortened window; 20:00 IST is after it.
    assert session_state("MCX", _utc(2026, 10, 14, 8, 30))["open"] is True
    after = session_state("MCX", _utc(2026, 10, 14, 14, 30))
    assert after["open"] is False
    assert after["reason"] == "after_close"


def test_an_unusable_commodity_close_falls_back_to_the_default(monkeypatch):
    for raw in ("", "   ", "not-a-time", "23:30:00", "05:00"):
        monkeypatch.setenv("MCX_SESSION_CLOSE", raw)
        assert mcx_session_close() == time(23, 30)


def test_an_open_commodity_session_says_its_holidays_are_unverified():
    # 04:30 UTC is 10:00 IST: past the 09:00 commodity open.
    state = session_state("MCX", _utc(2026, 10, 14, 4, 30), trading_day_reader=_raising_reader)
    assert state["open"] is True
    assert state["reason"] == "open"
    assert state["holiday_status"] == "not_verified_holiday"
    assert state["detail"]["reason"] == "not_verified_holiday"
    assert state["detail"]["holiday_source"] == "not_imported"


def test_the_commodity_open_is_earlier_than_the_equity_one():
    # 03:30 UTC is 09:00 IST: already open for MCX, not yet open for NSE.
    assert session_state("MCX", _utc(2026, 10, 14, 3, 30))["open"] is True
    equity = session_state(
        "NSE", _utc(2026, 10, 14, 3, 30), trading_day_reader=lambda *_: True
    )
    assert equity["reason"] == "before_open"


def test_a_late_commodity_session_runs_past_the_equity_close():
    # 17:30 UTC is 23:00 IST: inside the commodity window, hours after 15:30.
    assert session_state("MCX", _utc(2026, 10, 14, 17, 30))["open"] is True
    # 18:15 UTC is 23:45 IST: past the 23:30 close, still the same trading day.
    closed = session_state("MCX", _utc(2026, 10, 14, 18, 15))
    assert closed["open"] is False
    assert closed["reason"] == "after_close"
    assert closed["next_open"] == "2026-10-15T09:00:00+05:30"


def test_a_commodity_weekend_is_closed_without_a_calendar_claim():
    # 2026-10-10 is a Saturday.
    state = session_state("MCX", _utc(2026, 10, 10, 8, 0), trading_day_reader=_raising_reader)
    assert state["open"] is False
    assert state["reason"] == "weekend"
    assert state["holiday_status"] == "not_verified_holiday"


def test_an_nse_holiday_does_not_close_the_commodity_session():
    """The NSE calendar is not an MCX calendar, so it is never consulted."""

    def nse_holiday_reader(_exchange, _day):
        return False

    assert day_state("MCX", date(2026, 10, 14), trading_day_reader=nse_holiday_reader) == "trading"
    state = session_state("MCX", _utc(2026, 10, 14, 5, 0), trading_day_reader=nse_holiday_reader)
    assert state["open"] is True
