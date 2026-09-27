from datetime import date

import pytest

from backend.options.market.expiry_selectors import ExpirySelectorError, resolve_expiry_selector


def test_resolve_explicit_expiry_from_string():
    expiries = [date(2026, 5, 7), date(2026, 5, 14)]
    assert resolve_expiry_selector("2026-05-14", expiries, today=date(2026, 5, 1)) == date(2026, 5, 14)


def test_resolve_explicit_expiry_from_date_object():
    expiries = ["2026-05-07", "2026-05-14"]
    assert resolve_expiry_selector(date(2026, 5, 7), expiries, today=date(2026, 5, 1)) == date(2026, 5, 7)


def test_resolve_nearest_future_expiry_ignores_expired_and_unsorted():
    expiries = [date(2026, 5, 14), date(2026, 4, 30), date(2026, 5, 7)]
    assert resolve_expiry_selector("nearest", expiries, today=date(2026, 5, 1)) == date(2026, 5, 7)


def test_resolve_current_and_next_week():
    expiries = [date(2026, 5, 7), date(2026, 5, 14), date(2026, 5, 28)]
    assert resolve_expiry_selector("current_week", expiries, today=date(2026, 5, 4)) == date(2026, 5, 7)
    assert resolve_expiry_selector("next_week", expiries, today=date(2026, 5, 4)) == date(2026, 5, 14)


def test_resolve_current_month_prefers_last_current_month_expiry():
    expiries = [date(2026, 5, 7), date(2026, 5, 14), date(2026, 5, 28), date(2026, 6, 25)]
    assert resolve_expiry_selector("current_month", expiries, today=date(2026, 5, 1)) == date(2026, 5, 28)


def test_raises_when_no_future_expiries_available():
    with pytest.raises(ExpirySelectorError, match="No future expiries are available"):
        resolve_expiry_selector("nearest", [date(2026, 4, 24), date(2026, 4, 30)], today=date(2026, 5, 1))


def test_raises_when_explicit_expiry_is_expired_or_missing():
    with pytest.raises(ExpirySelectorError, match="Explicit expiry 2026-04-30 is not available"):
        resolve_expiry_selector("2026-04-30", [date(2026, 4, 30), date(2026, 5, 7)], today=date(2026, 5, 1))


def test_raises_when_current_week_is_unavailable():
    with pytest.raises(ExpirySelectorError, match="No current-week expiry is available"):
        resolve_expiry_selector("current_week", [date(2026, 5, 14), date(2026, 5, 28)], today=date(2026, 5, 4))


def test_raises_when_next_week_is_unavailable():
    with pytest.raises(ExpirySelectorError, match="No next-week expiry is available"):
        resolve_expiry_selector("next_week", [date(2026, 5, 7), date(2026, 5, 28)], today=date(2026, 5, 4))


def test_raises_for_invalid_selector_date_format():
    with pytest.raises(ExpirySelectorError, match="Invalid selector date format"):
        resolve_expiry_selector("2026/05/14", [date(2026, 5, 7), date(2026, 5, 14)], today=date(2026, 5, 1))


def test_raises_for_invalid_expiry_date_format_in_list():
    with pytest.raises(ExpirySelectorError, match="Invalid expiry date format"):
        resolve_expiry_selector("nearest", ["2026-05-07", "2026/05/14"], today=date(2026, 5, 1))


def test_resolve_next_month_picks_the_following_months_last_expiry():
    expiries = [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 27), date(2026, 11, 24)]
    assert resolve_expiry_selector("next_month", expiries, today=date(2026, 9, 28)) == date(2026, 10, 27)


def test_resolve_next_month_rolls_over_the_year():
    expiries = [date(2026, 12, 29), date(2027, 1, 26)]
    assert resolve_expiry_selector("next_month", expiries, today=date(2026, 12, 1)) == date(2027, 1, 26)


def test_next_month_without_a_listed_expiry_raises():
    with pytest.raises(ExpirySelectorError, match="No next-month expiry is available"):
        resolve_expiry_selector("next_month", [date(2026, 9, 29)], today=date(2026, 9, 28))
