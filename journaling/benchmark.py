from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional


ZERO = Decimal("0")
ONE = Decimal("1")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _extract_subject_return(point: Any) -> Optional[Decimal]:
    explicit_return = getattr(point, "return_pct", None)
    if explicit_return is not None:
        return _to_decimal(explicit_return)

    starting_equity = getattr(point, "starting_equity", None)
    ending_equity = getattr(point, "ending_equity", None)
    cash_flow = getattr(point, "cash_flow", ZERO)
    if starting_equity in (None, ZERO) or ending_equity is None:
        return None
    starting_value = _to_decimal(starting_equity)
    if starting_value == ZERO:
        return None
    ending_value = _to_decimal(ending_equity)
    cash_flow_value = _to_decimal(cash_flow or ZERO)
    return (ending_value - starting_value - cash_flow_value) / starting_value


@dataclass(frozen=True)
class BenchmarkReturnPoint:
    trading_day: date
    close: Decimal
    return_pct: Decimal
    cumulative_return: Decimal


@dataclass(frozen=True)
class ReturnComparisonPoint:
    trading_day: date
    subject_return_pct: Decimal
    benchmark_return_pct: Decimal
    excess_return_pct: Decimal
    subject_cumulative_return: Decimal
    benchmark_cumulative_return: Decimal
    excess_cumulative_return: Decimal


def normalize_benchmark_series(prices: Iterable[Any]) -> list[BenchmarkReturnPoint]:
    ordered_prices = sorted(prices, key=lambda price: getattr(price, "trading_day"))
    if not ordered_prices:
        return []

    normalized: list[BenchmarkReturnPoint] = []
    baseline_close: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None

    for price in ordered_prices:
        close = _to_decimal(getattr(price, "close"))
        trading_day = getattr(price, "trading_day")
        if baseline_close is None:
            baseline_close = close
            return_pct = ZERO
        elif getattr(price, "daily_return", None) is not None:
            return_pct = _to_decimal(getattr(price, "daily_return"))
        else:
            if previous_close in (None, ZERO):
                return_pct = ZERO
            else:
                return_pct = (close / previous_close) - ONE

        cumulative_return = ZERO if baseline_close in (None, ZERO) else (close / baseline_close) - ONE
        normalized.append(
            BenchmarkReturnPoint(
                trading_day=trading_day,
                close=close,
                return_pct=return_pct,
                cumulative_return=cumulative_return,
            )
        )
        previous_close = close

    return normalized


def compare_return_series(subject_points: Iterable[Any], benchmark_points: Iterable[Any]) -> list[ReturnComparisonPoint]:
    normalized_benchmark = {
        point.trading_day: point
        for point in normalize_benchmark_series(benchmark_points)
    }

    normalized_subject = {}
    for point in subject_points:
        trading_day = _coerce_day(getattr(point, "trading_day", None) or getattr(point, "as_of", None) or getattr(point, "date", None))
        point_return = _extract_subject_return(point)
        if trading_day is None or point_return is None:
            continue
        normalized_subject[trading_day] = point_return

    overlapping_days = sorted(set(normalized_subject) & set(normalized_benchmark))
    comparisons: list[ReturnComparisonPoint] = []
    subject_multiplier = ONE
    benchmark_multiplier = ONE

    for trading_day in overlapping_days:
        subject_return = normalized_subject[trading_day]
        benchmark_return = normalized_benchmark[trading_day].return_pct
        subject_multiplier *= ONE + subject_return
        benchmark_multiplier *= ONE + benchmark_return
        subject_cumulative = subject_multiplier - ONE
        benchmark_cumulative = benchmark_multiplier - ONE
        comparisons.append(
            ReturnComparisonPoint(
                trading_day=trading_day,
                subject_return_pct=subject_return,
                benchmark_return_pct=benchmark_return,
                excess_return_pct=subject_return - benchmark_return,
                subject_cumulative_return=subject_cumulative,
                benchmark_cumulative_return=benchmark_cumulative,
                excess_cumulative_return=subject_cumulative - benchmark_cumulative,
            )
        )

    return comparisons


__all__ = [
    "BenchmarkReturnPoint",
    "ReturnComparisonPoint",
    "compare_return_series",
    "normalize_benchmark_series",
]
