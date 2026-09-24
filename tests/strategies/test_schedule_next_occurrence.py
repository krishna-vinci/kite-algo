"""The operator's "next run" is the scheduler's own forward rule.

``next_occurrence`` must agree with what ``ScheduleScheduler.due_occurrences``
materialises, or the UI would show a time the runtime never fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.strategies.scheduling import ScheduleScheduler, next_occurrence


def _schedule(**overrides):
    body = {
        "id": "sch-1",
        "schedule_kind": "daily",
        "at_time": "09:30",
        "timezone": "Asia/Kolkata",
        "weekday": None,
        "day_of_month": None,
        "calendar_dates": [],
    }
    body.update(overrides)
    return body


def test_daily_next_is_today_before_the_clock_time():
    # 2026-09-23 04:00 UTC == 09:30 IST; ask a minute earlier.
    now = datetime(2026, 9, 23, 3, 59, tzinfo=timezone.utc)
    occurrence = next_occurrence(_schedule(), now=now)
    assert occurrence is not None
    # 09:30 IST on 2026-09-23 is 04:00 UTC.
    assert occurrence.due_at == datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)
    assert occurrence.occurrence_key == "sch-1:2026-09-23"


def test_daily_next_rolls_to_tomorrow_after_the_clock_time():
    now = datetime(2026, 9, 23, 4, 1, tzinfo=timezone.utc)
    occurrence = next_occurrence(_schedule(), now=now)
    assert occurrence is not None
    assert occurrence.due_at == datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)


def test_weekly_next_lands_on_the_configured_weekday():
    # 2026-09-23 is a Wednesday (weekday 2).
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    occurrence = next_occurrence(_schedule(schedule_kind="weekly", weekday=0), now=now)
    assert occurrence is not None
    # The next Monday is 2026-09-28.
    assert occurrence.occurrence_key == "sch-1:2026-09-28"


def test_monthly_clamps_to_the_month_length():
    # Day 31 in a 30-day month clamps to the 30th, exactly as the due pass does.
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    occurrence = next_occurrence(
        _schedule(schedule_kind="monthly", day_of_month=31), now=now
    )
    assert occurrence is not None
    assert occurrence.occurrence_key == "sch-1:2026-09-30"


def test_calendar_returns_the_next_listed_date_and_none_when_exhausted():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    schedule = _schedule(
        schedule_kind="calendar",
        calendar_dates=["2026-09-01", "2026-10-01", "2026-10-15"],
    )
    occurrence = next_occurrence(schedule, now=now)
    assert occurrence is not None
    assert occurrence.occurrence_key == "sch-1:2026-10-01"
    assert next_occurrence(schedule, now=datetime(2027, 1, 1, tzinfo=timezone.utc)) is None


def test_unknown_kind_has_no_next_occurrence():
    assert next_occurrence(_schedule(schedule_kind="nonsense"), now=datetime.now(timezone.utc)) is None


def test_next_occurrence_is_the_one_the_due_pass_materialises():
    """The forward and backward walks agree on the same instant."""

    now = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)
    for schedule in (
        _schedule(),
        _schedule(schedule_kind="weekly", weekday=4),
        _schedule(schedule_kind="monthly", day_of_month=31),
        _schedule(schedule_kind="calendar", calendar_dates=["2026-10-02"]),
    ):
        upcoming = next_occurrence(schedule, now=now)
        assert upcoming is not None, schedule
        due = ScheduleScheduler.due_occurrences(schedule, now=upcoming.due_at)
        assert due, schedule
        assert due[-1].occurrence_key == upcoming.occurrence_key, schedule
        # And nothing between now and it was materialised.
        earlier = ScheduleScheduler.due_occurrences(
            schedule, now=upcoming.due_at - timedelta(seconds=1)
        )
        assert all(item.occurrence_key != upcoming.occurrence_key for item in earlier), schedule
