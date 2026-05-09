from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def day_bounds_utc(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=IST)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def period_bounds_utc(period: str, anchor: date | None) -> tuple[date | None, date | None, datetime | None, datetime | None]:
    normalized = str(period or "").strip().lower()
    if normalized == "since_inception":
        return None, None, None, None
    if anchor is None:
        raise ValueError("anchor is required for bounded periods")
    if normalized == "day":
        from_date = to_date = anchor
    elif normalized == "week":
        from_date = anchor - timedelta(days=anchor.weekday())
        to_date = from_date + timedelta(days=6)
    elif normalized == "month":
        from_date = anchor.replace(day=1)
        if from_date.month == 12:
            next_month = from_date.replace(year=from_date.year + 1, month=1)
        else:
            next_month = from_date.replace(month=from_date.month + 1)
        to_date = next_month - timedelta(days=1)
    elif normalized == "year":
        from_date = anchor.replace(month=1, day=1)
        to_date = anchor.replace(month=12, day=31)
    else:
        raise ValueError(f"Unsupported period: {period}")
    start_at, _ = day_bounds_utc(from_date)
    _, end_at = day_bounds_utc(to_date)
    return from_date, to_date, start_at, end_at
