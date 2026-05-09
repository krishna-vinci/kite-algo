from datetime import date

import pytest

from backend.journaling.periods import day_bounds_utc, period_bounds_utc


def test_day_bounds_use_ist_calendar_and_return_utc() -> None:
    start, end = day_bounds_utc(date(2026, 5, 4))

    assert start.isoformat() == "2026-05-03T18:30:00+00:00"
    assert end.isoformat() == "2026-05-04T18:30:00+00:00"


def test_week_bounds_start_on_monday_in_ist() -> None:
    start_date, end_date, start, end = period_bounds_utc("week", date(2026, 5, 7))

    assert start_date == date(2026, 5, 4)
    assert end_date == date(2026, 5, 10)
    assert start is not None
    assert end is not None
    assert start.isoformat() == "2026-05-03T18:30:00+00:00"
    assert end.isoformat() == "2026-05-10T18:30:00+00:00"


def test_month_and_year_bounds() -> None:
    assert period_bounds_utc("month", date(2026, 2, 12))[:2] == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds_utc("year", date(2026, 5, 4))[:2] == (date(2026, 1, 1), date(2026, 12, 31))


def test_since_inception_has_no_bounds() -> None:
    assert period_bounds_utc("since_inception", date(2026, 5, 4)) == (None, None, None, None)


def test_unsupported_period_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported period"):
        period_bounds_utc("quarter", date(2026, 5, 4))
