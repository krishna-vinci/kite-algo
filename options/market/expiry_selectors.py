from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable


class ExpirySelectorError(ValueError):
    """Raised when an expiry selector cannot be resolved."""


def _coerce_expiry(value: date | str, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ExpirySelectorError(
                f"Invalid {field_name} date format: {value!r}. Expected YYYY-MM-DD"
            ) from exc
    raise ExpirySelectorError(
        f"Unsupported {field_name} type: {type(value).__name__}. Expected date or YYYY-MM-DD string"
    )


def _future_expiries(expiries: Iterable[date | str], *, today: date) -> list[date]:
    normalized = [_coerce_expiry(item, field_name="expiry") for item in expiries]
    return sorted(expiry for expiry in normalized if expiry >= today)


def resolve_expiry_selector(
    selector: date | str | None,
    expiries: Iterable[date | str],
    *,
    today: date | None = None,
) -> date:
    current_day = today or date.today()
    future_expiries = _future_expiries(expiries, today=current_day)
    if not future_expiries:
        raise ExpirySelectorError("No future expiries are available")

    if selector is None:
        return future_expiries[0]

    semantic_selector: str | None = None
    if isinstance(selector, str):
        semantic_candidate = selector.strip().lower()
        if semantic_candidate in {"nearest", "current_week", "next_week", "current_month"}:
            semantic_selector = semantic_candidate

    if semantic_selector == "nearest":
        return future_expiries[0]

    if semantic_selector is None:
        explicit_expiry = _coerce_expiry(selector, field_name="selector")
        if explicit_expiry not in future_expiries:
            raise ExpirySelectorError(
                f"Explicit expiry {explicit_expiry.isoformat()} is not available"
            )
        return explicit_expiry

    week_end = current_day + timedelta(days=6 - current_day.weekday())
    if semantic_selector == "current_week":
        for expiry in future_expiries:
            if current_day <= expiry <= week_end:
                return expiry
        raise ExpirySelectorError("No current-week expiry is available")

    if semantic_selector == "next_week":
        next_week_start = week_end + timedelta(days=1)
        next_week_end = next_week_start + timedelta(days=6)
        for expiry in future_expiries:
            if next_week_start <= expiry <= next_week_end:
                return expiry
        raise ExpirySelectorError("No next-week expiry is available")

    month_expiries = [
        expiry
        for expiry in future_expiries
        if expiry.year == current_day.year and expiry.month == current_day.month
    ]
    if not month_expiries:
        raise ExpirySelectorError("No current-month expiry is available")
    return month_expiries[-1]
