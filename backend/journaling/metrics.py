from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional


ZERO = Decimal("0")
ONE = Decimal("1")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _sum_decimals(values: Iterable[Any]) -> Decimal:
    total = ZERO
    for value in values:
        if value is None:
            continue
        total += _to_decimal(value)
    return total


def _coerce_date(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return value


def _sorted_points(points: Iterable[Any]) -> list[Any]:
    return sorted(points, key=lambda point: _coerce_date(getattr(point, "as_of", None) or getattr(point, "trading_day", None)))


def _point_return(point: Any) -> Optional[Decimal]:
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


def gross_profit(trade_pnls: Iterable[Any]) -> Decimal:
    return _sum_decimals(value for value in trade_pnls if value is not None and _to_decimal(value) > ZERO)


def gross_loss(trade_pnls: Iterable[Any]) -> Decimal:
    return _sum_decimals(-_to_decimal(value) for value in trade_pnls if value is not None and _to_decimal(value) < ZERO)


def total_fees(fees: Iterable[Any]) -> Decimal:
    return _sum_decimals(fees)


def net_pnl(trade_pnls: Iterable[Any], fees: Iterable[Any] | None = None) -> Decimal:
    pnl_total = _sum_decimals(trade_pnls)
    if fees is None:
        return pnl_total
    return pnl_total - total_fees(fees)


def win_rate(trade_pnls: Iterable[Any]) -> Optional[Decimal]:
    outcomes = [_to_decimal(value) for value in trade_pnls if value is not None]
    if not outcomes:
        return None
    wins = sum(1 for value in outcomes if value > ZERO)
    return Decimal(wins) / Decimal(len(outcomes))


def average_win(trade_pnls: Iterable[Any]) -> Optional[Decimal]:
    wins = [_to_decimal(value) for value in trade_pnls if value is not None and _to_decimal(value) > ZERO]
    if not wins:
        return None
    return _sum_decimals(wins) / Decimal(len(wins))


def average_loss(trade_pnls: Iterable[Any]) -> Optional[Decimal]:
    losses = [-_to_decimal(value) for value in trade_pnls if value is not None and _to_decimal(value) < ZERO]
    if not losses:
        return None
    return _sum_decimals(losses) / Decimal(len(losses))


def profit_factor(trade_pnls: Iterable[Any]) -> Optional[Decimal]:
    losses = gross_loss(trade_pnls)
    if losses == ZERO:
        return None
    return gross_profit(trade_pnls) / losses


def expectancy(trade_pnls: Iterable[Any], fees: Iterable[Any] | None = None) -> Optional[Decimal]:
    outcomes = [_to_decimal(value) for value in trade_pnls if value is not None]
    if not outcomes:
        return None
    return net_pnl(outcomes, fees) / Decimal(len(outcomes))


def average_hold_time(hold_times: Iterable[timedelta]) -> Optional[timedelta]:
    durations = [duration for duration in hold_times if duration is not None]
    if not durations:
        return None
    total_seconds = sum(duration.total_seconds() for duration in durations)
    return timedelta(seconds=total_seconds / len(durations))


def cumulative_return(equity_points: Iterable[Any]) -> Optional[Decimal]:
    cumulative_multiplier = ONE
    has_returns = False
    for point in _sorted_points(equity_points):
        point_return = _point_return(point)
        if point_return is None:
            continue
        cumulative_multiplier *= ONE + point_return
        has_returns = True
    if not has_returns:
        return None
    return cumulative_multiplier - ONE


def return_series(equity_points: Iterable[Any]) -> list[Decimal]:
    returns: list[Decimal] = []
    for point in _sorted_points(equity_points):
        point_return = _point_return(point)
        if point_return is None:
            continue
        returns.append(point_return)
    return returns


def _stddev(values: list[Decimal]) -> Optional[Decimal]:
    if len(values) < 2:
        return None
    mean = sum(values) / Decimal(len(values))
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return Decimal(str(math.sqrt(float(variance))))


def sharpe_ratio(equity_points: Iterable[Any], *, risk_free_rate_per_period: Decimal = ZERO) -> Optional[Decimal]:
    returns = return_series(equity_points)
    if len(returns) < 2:
        return None
    adjusted = [value - risk_free_rate_per_period for value in returns]
    volatility = _stddev(adjusted)
    if volatility in (None, ZERO):
        return None
    mean = sum(adjusted) / Decimal(len(adjusted))
    return mean / volatility


def sortino_ratio(equity_points: Iterable[Any], *, risk_free_rate_per_period: Decimal = ZERO) -> Optional[Decimal]:
    returns = return_series(equity_points)
    if len(returns) < 2:
        return None
    adjusted = [value - risk_free_rate_per_period for value in returns]
    downside = [value for value in adjusted if value < ZERO]
    if not downside:
        return None
    downside_deviation = _stddev(downside)
    if downside_deviation in (None, ZERO):
        return None
    mean = sum(adjusted) / Decimal(len(adjusted))
    return mean / downside_deviation


def max_drawdown_from_equity_points(equity_points: Iterable[Any]) -> Optional[Decimal]:
    ordered_points = _sorted_points(equity_points)
    if not ordered_points:
        return None

    peak: Optional[Decimal] = None
    max_drawdown = ZERO
    for point in ordered_points:
        equity = getattr(point, "ending_equity", None)
        if equity is None:
            continue
        equity_value = _to_decimal(equity)
        if peak is None or equity_value > peak:
            peak = equity_value
            continue
        if peak == ZERO:
            continue
        drawdown = (peak - equity_value) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def max_drawdown_duration(equity_points: Iterable[Any]) -> int:
    ordered_points = _sorted_points(equity_points)
    peak: Optional[Decimal] = None
    current_duration = 0
    max_duration = 0
    for point in ordered_points:
        equity = getattr(point, "ending_equity", None)
        if equity is None:
            continue
        equity_value = _to_decimal(equity)
        if peak is None or equity_value >= peak:
            peak = equity_value
            current_duration = 0
        else:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
    return max_duration


def streaks(trade_pnls: Iterable[Any]) -> dict[str, int]:
    max_win = 0
    max_loss = 0
    current_win = 0
    current_loss = 0
    for value in trade_pnls:
        if value is None:
            continue
        pnl = _to_decimal(value)
        if pnl > ZERO:
            current_win += 1
            current_loss = 0
        elif pnl < ZERO:
            current_loss += 1
            current_win = 0
        else:
            current_win = 0
            current_loss = 0
        max_win = max(max_win, current_win)
        max_loss = max(max_loss, current_loss)
    return {"max_win_streak": max_win, "max_loss_streak": max_loss}


__all__ = [
    "average_hold_time",
    "average_loss",
    "average_win",
    "cumulative_return",
    "expectancy",
    "gross_loss",
    "gross_profit",
    "max_drawdown_duration",
    "max_drawdown_from_equity_points",
    "net_pnl",
    "profit_factor",
    "return_series",
    "sharpe_ratio",
    "sortino_ratio",
    "streaks",
    "total_fees",
    "win_rate",
]
