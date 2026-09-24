"""Example 4 - Nifty-500 momentum portfolio over the governed execution path.

The ranking, allocation and delta arithmetic below is the owner-supplied
strategy, preserved verbatim except for ONE approved change (see
``evaluate_regime``): a failed constituent-breadth gate now means zero exposure
and an exit of the strategy's own holdings, instead of half exposure when the
index trend happens to pass. Everything else - the 252-session momentum
lookback, the 200/150-session asymmetric index trend, the 0.40 breadth gate,
the 15-name equal-weight target, the 10 %/1.5x position cap and the whole-share
allocation - is unchanged and is not a tuning surface.

What the adapter adds, and refuses to fake:

* the run's identity comes from the PERSISTED binding, never from a parameter;
* daily history is read with an explicit range and only COMPLETED sessions are
  used; the last usable session is the platform's own last-final session, not
  "whatever bar happened to arrive";
* coverage is a gate, not a footnote. A constituent whose history could not be
  read (transport error, empty body, unfinal, unparsable, contradictory, or
  truncated) is never re-labelled as "insufficient history" and never silently
  dropped: the run does nothing at all, which means no entry AND no exit based on
  a breadth reading we cannot trust. A series that starts late is used only when
  a wider probe proves the symbol genuinely has no earlier data, and the
  numerator/denominator/coverage of every breadth reading is reported;
* the regime is replayed forward day by day from a FIXED configured anchor using
  only data available up to each day. The replay starts from an explicit
  ``initial_regime``; it is not a sliding window and it does not look ahead.
  Replaying the index alone would be wrong, because the previous breadth gate is
  part of the state;
* the book is the strategy's OWN attributed CNC book. An unpublished projection is
  UNKNOWN, never flat, and unknown coverage produces a named no-action rather
  than an order;
* the plan carries EXACT whole-share target quantities as an ``intent_bundle``,
  so nothing is re-derived from rounded weights, and it goes through the ordinary
  proposal -> execution-request path (review-first waits for the owner; an
  autonomous grant admits it under the recorded limits).

STATUS: preview / paper-experimental.

This example is offered for review-first and paper (and paper-autonomous) use.
The supplied ``budget_inr`` bounds the strategy's OWN arithmetic, and the
platform's admission ceiling remains the authoritative bound; the adapter does
not claim that equality with the owner's recorded allocation is enforced on this
target kind. Where the platform's existing admission rules block a rebalance -
most visibly, a full-target plan on an already-invested book - the refusal is
reported by name with no order placed, which is the honest outcome rather than a
reason to widen admission inside this example.

Known, stated limitations
-------------------------

* Index membership is CURRENT membership applied to history: the platform stores
  no point-in-time constituent lists, so the replay carries survivorship bias.
  ``constituents_source`` plus ``regime_anchor_date`` are the configured,
  versioned statement of that choice.
* A monthly rebalance of an already-invested book is sized by the platform's
  admission arithmetic, which counts the strategy's existing attributed
  consumption plus the plan's own notional against the owner allocation. A
  full-target plan on an invested book can therefore be refused
  ``ALLOCATION_EXCEEDED``; the refusal is reported by name and nothing is placed.
* The owner's authoritative allocation is not readable by a hosted child, so this
  adapter cannot prove ``budget_inr`` equals it. The platform's
  ``CAPITAL_BASIS_MISMATCH`` refusal exists on the ``target_weights`` path, not on
  this exact-quantity path; the binding control here is the admission ceiling
  (``ALLOCATION_EXCEEDED``) plus the adapter's own affordability check.
"""

from __future__ import annotations

import calendar as calendar_module
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day
from decimal import Decimal, ROUND_FLOOR
from enum import StrEnum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Strategy configuration (owner-supplied; not a tuning surface)
# ---------------------------------------------------------------------------

LOOKBACK_SESSIONS = 252
TOP_N = 15

DEFENSIVE_SMA_SESSIONS = 200
REENTRY_SMA_SESSIONS = 150

BREADTH_SMA_SESSIONS = 200
BREADTH_THRESHOLD = Decimal("0.40")

MAX_POSITION_PERCENT = Decimal("0.10")
EQUAL_WEIGHT_CAP_MULTIPLIER = Decimal("1.50")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candle:
    session: date
    close: Decimal
    final: bool = True


@dataclass(frozen=True)
class RankedStock:
    symbol: str
    rank: int
    momentum: Decimal
    price: Decimal


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    rank: int
    momentum: Decimal
    price: Decimal
    quantity: int
    target_value: Decimal
    actual_value: Decimal
    actual_weight: Decimal


@dataclass(frozen=True)
class OrderDelta:
    symbol: str
    current_quantity: int
    target_quantity: int
    quantity_delta: int
    side: str  # BUY, SELL or HOLD
    reference_price: Decimal


@dataclass(frozen=True)
class PortfolioPlan:
    evaluation_date: date
    regime: "RegimeState"
    exposure: Decimal
    allocated_capital: Decimal
    equal_weight_target: Decimal
    position_cap: Decimal
    invested_value: Decimal
    residual_cash: Decimal
    targets: tuple[TargetPosition, ...]
    orders: tuple[OrderDelta, ...]
    excluded: Mapping[str, str]


class RegimeState(StrEnum):
    RISK_ON = "RISK_ON"
    CAUTIOUS = "CAUTIOUS"
    DEFENSIVE = "DEFENSIVE"


@dataclass(frozen=True)
class RegimeDecision:
    previous_state: RegimeState
    state: RegimeState
    exposure: Decimal
    index_close: Decimal
    index_sma: Decimal
    breadth: Decimal
    breadth_numerator: int
    breadth_denominator: int


class RebalanceScheduleKind(StrEnum):
    MONTHLY_LAST_SESSION = "MONTHLY_LAST_SESSION"
    MONTHLY_CALENDAR_DAY = "MONTHLY_CALENDAR_DAY"


@dataclass(frozen=True)
class RebalanceSchedule:
    kind: RebalanceScheduleKind
    day_of_month: int | None = None

    def __post_init__(self) -> None:
        if self.kind == RebalanceScheduleKind.MONTHLY_CALENDAR_DAY:
            if self.day_of_month is None:
                raise ValueError("day_of_month is required")

            if not 1 <= self.day_of_month <= 31:
                raise ValueError("day_of_month must be between 1 and 31")

        elif self.day_of_month is not None:
            raise ValueError(
                "MONTHLY_LAST_SESSION cannot specify day_of_month"
            )

    def resolve(
        self,
        trading_sessions: Sequence[date],
        year: int,
        month: int,
    ) -> date:
        month_sessions = sorted(
            session
            for session in trading_sessions
            if session.year == year and session.month == month
        )

        if not month_sessions:
            raise ValueError(
                f"No verified trading sessions for {year}-{month:02d}"
            )

        if self.kind == RebalanceScheduleKind.MONTHLY_LAST_SESSION:
            return month_sessions[-1]

        assert self.day_of_month is not None

        # First verified session on or after the configured calendar day.
        for session in month_sessions:
            if session.day >= self.day_of_month:
                return session

        # If day 29/30/31 does not exist or has no later session,
        # use the month's final verified trading session.
        return month_sessions[-1]

    def is_due(
        self,
        evaluation_date: date,
        trading_sessions: Sequence[date],
    ) -> bool:
        return evaluation_date == self.resolve(
            trading_sessions,
            evaluation_date.year,
            evaluation_date.month,
        )


# ---------------------------------------------------------------------------
# Data validation
# ---------------------------------------------------------------------------

def validate_history(
    symbol: str,
    candles: Sequence[Candle],
    minimum_sessions: int,
) -> tuple[Candle, ...]:
    ordered = tuple(sorted(candles, key=lambda candle: candle.session))

    if len(ordered) < minimum_sessions:
        raise ValueError(
            f"{symbol}: requires {minimum_sessions} completed candles"
        )

    dates = [candle.session for candle in ordered]

    if len(dates) != len(set(dates)):
        raise ValueError(f"{symbol}: duplicate candle dates")

    if any(not candle.final for candle in ordered):
        raise ValueError(f"{symbol}: contains an unfinished candle")

    if any(candle.close <= 0 for candle in ordered):
        raise ValueError(f"{symbol}: contains an invalid close price")

    return ordered


def simple_moving_average(
    candles: Sequence[Candle],
    window: int,
) -> Decimal:
    if len(candles) < window:
        raise ValueError(f"Requires at least {window} candles")

    return (
        sum((candle.close for candle in candles[-window:]), Decimal("0"))
        / Decimal(window)
    )


# ---------------------------------------------------------------------------
# Market regime
# ---------------------------------------------------------------------------

def evaluate_regime(
    previous_state: RegimeState,
    index_history: Sequence[Candle],
    member_histories: Mapping[str, Sequence[Candle]],
) -> RegimeDecision:
    """
    Daily asymmetric regime evaluation.

    When already risk-on, use the slower 200-session index SMA to exit.
    When cautious/defensive, use the faster 150-session SMA to re-enter.

    APPROVED DEVIATION (2026-09-24, owner): a failed constituent-breadth gate
    means zero exposure - no entry, and the strategy's existing holdings are
    exited on the daily completed session - regardless of the index trend. The
    original source supplied half exposure whenever either gate passed. The
    index-trend logic itself (including the 200/150 asymmetry) is unchanged and
    still decides RISK_ON versus CAUTIOUS when breadth passes.
    """

    sma_window = (
        DEFENSIVE_SMA_SESSIONS
        if previous_state == RegimeState.RISK_ON
        else REENTRY_SMA_SESSIONS
    )

    index_candles = validate_history(
        "NIFTY500",
        index_history,
        max(sma_window, BREADTH_SMA_SESSIONS),
    )

    index_close = index_candles[-1].close
    index_sma = simple_moving_average(index_candles, sma_window)
    trend_strong = index_close > index_sma

    breadth_numerator = 0
    breadth_denominator = 0

    for symbol, history in member_histories.items():
        try:
            candles = validate_history(
                symbol,
                history,
                BREADTH_SMA_SESSIONS,
            )
        except ValueError:
            continue

        breadth_denominator += 1
        member_sma = simple_moving_average(
            candles,
            BREADTH_SMA_SESSIONS,
        )

        if candles[-1].close > member_sma:
            breadth_numerator += 1

    if breadth_denominator == 0:
        raise ValueError(
            "No constituents have sufficient history for breadth calculation"
        )

    breadth = (
        Decimal(breadth_numerator)
        / Decimal(breadth_denominator)
    )

    breadth_strong = breadth >= BREADTH_THRESHOLD

    if not breadth_strong:
        state = RegimeState.DEFENSIVE
        exposure = Decimal("0")

    elif trend_strong:
        state = RegimeState.RISK_ON
        exposure = Decimal("1")

    else:
        state = RegimeState.CAUTIOUS
        exposure = Decimal("0.50")

    return RegimeDecision(
        previous_state=previous_state,
        state=state,
        exposure=exposure,
        index_close=index_close,
        index_sma=index_sma,
        breadth=breadth,
        breadth_numerator=breadth_numerator,
        breadth_denominator=breadth_denominator,
    )


# ---------------------------------------------------------------------------
# Momentum ranking
# ---------------------------------------------------------------------------

def rank_momentum_stocks(
    member_histories: Mapping[str, Sequence[Candle]],
    current_prices: Mapping[str, Decimal],
) -> tuple[tuple[RankedStock, ...], dict[str, str]]:
    """
    Momentum = latest completed close / close 252 sessions ago - 1.

    Ranking is descending. Symbol is used as a deterministic tie-breaker.
    """

    ranked_data: list[tuple[str, Decimal, Decimal]] = []
    excluded: dict[str, str] = {}

    for symbol, history in member_histories.items():
        price = current_prices.get(symbol)

        if price is None or price <= 0:
            excluded[symbol] = "missing_or_invalid_price"
            continue

        try:
            candles = validate_history(
                symbol,
                history,
                LOOKBACK_SESSIONS + 1,
            )
        except ValueError as error:
            excluded[symbol] = str(error)
            continue

        latest_close = candles[-1].close
        old_close = candles[-(LOOKBACK_SESSIONS + 1)].close

        momentum = latest_close / old_close - Decimal("1")

        ranked_data.append((symbol, momentum, price))

    ranked_data.sort(key=lambda item: (-item[1], item[0]))

    ranked = tuple(
        RankedStock(
            symbol=symbol,
            rank=rank,
            momentum=momentum,
            price=price,
        )
        for rank, (symbol, momentum, price)
        in enumerate(ranked_data, start=1)
    )

    return ranked, dict(sorted(excluded.items()))


# ---------------------------------------------------------------------------
# Equal-weight whole-share allocation
# ---------------------------------------------------------------------------

def allocate_equal_weight(
    ranked_stocks: Sequence[RankedStock],
    strategy_fund: Decimal,
    exposure: Decimal,
    target_count: int = TOP_N,
    reserved_costs: Decimal = Decimal("0"),
) -> tuple[
    tuple[TargetPosition, ...],
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    dict[str, str],
]:
    """
    Allocate capital close to equal weight using whole shares.

    A stock is affordable only if one share fits within:

        min(
            10% of investible capital,
            1.5 × equal-weight allocation
        )

    Example:
        Fund = ₹75,000
        15 stocks
        Equal target = ₹5,000
        Position cap = min(₹7,500, ₹7,500) = ₹7,500

    A ₹400 stock receives approximately 12 shares, not one share.
    A ₹17,000 stock is rejected because one share exceeds the cap.
    """

    if strategy_fund < 0:
        raise ValueError("strategy_fund cannot be negative")

    if not Decimal("0") <= exposure <= Decimal("1"):
        raise ValueError("exposure must be between zero and one")

    if target_count <= 0:
        raise ValueError("target_count must be positive")

    if reserved_costs < 0:
        raise ValueError("reserved_costs cannot be negative")

    allocated_capital = strategy_fund * exposure
    investible = max(
        allocated_capital - reserved_costs,
        Decimal("0"),
    )

    equal_weight_target = investible / Decimal(target_count)

    position_cap = min(
        investible * MAX_POSITION_PERCENT,
        equal_weight_target * EQUAL_WEIGHT_CAP_MULTIPLIER,
    )

    excluded: dict[str, str] = {}

    # Walk down the ranking until 15 affordable stocks are obtained.
    selected: list[RankedStock] = []

    for stock in ranked_stocks:
        if stock.price > position_cap:
            excluded[stock.symbol] = "single_share_exceeds_position_cap"
            continue

        selected.append(stock)

        if len(selected) == target_count:
            break

    selected_symbols = {stock.symbol for stock in selected}

    for stock in ranked_stocks:
        if stock.symbol not in selected_symbols and stock.symbol not in excluded:
            excluded[stock.symbol] = "below_selected_rank_cutoff"

    quantities: dict[str, int] = {}
    invested_value = Decimal("0")

    # First pass: buy approximately the equal-weight quantity.
    for stock in selected:
        maximum_quantity = int(
            (position_cap / stock.price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

        preferred_quantity = int(
            (equal_weight_target / stock.price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

        quantity = min(maximum_quantity, preferred_quantity)

        # Every selected stock should receive at least one share.
        if quantity < 1:
            quantity = 1

        line_value = Decimal(quantity) * stock.price

        if invested_value + line_value > investible:
            quantity = 0
            excluded[stock.symbol] = "insufficient_remaining_capital"
        else:
            invested_value += line_value

        quantities[stock.symbol] = quantity

    # Second pass: use residual cash to reduce equal-weight deviations.
    while True:
        best_stock: RankedStock | None = None
        best_improvement = Decimal("0")

        for stock in selected:
            quantity = quantities[stock.symbol]

            if quantity <= 0:
                continue

            current_value = Decimal(quantity) * stock.price
            next_value = Decimal(quantity + 1) * stock.price

            if next_value > position_cap:
                continue

            if invested_value + stock.price > investible:
                continue

            current_deviation = abs(
                current_value - equal_weight_target
            )
            next_deviation = abs(
                next_value - equal_weight_target
            )

            improvement = current_deviation - next_deviation

            if improvement > best_improvement:
                best_stock = stock
                best_improvement = improvement

            elif (
                improvement == best_improvement
                and improvement > 0
                and best_stock is not None
                and stock.rank < best_stock.rank
            ):
                best_stock = stock

        if best_stock is None or best_improvement <= 0:
            break

        quantities[best_stock.symbol] += 1
        invested_value += best_stock.price

    targets: list[TargetPosition] = []

    for stock in selected:
        quantity = quantities[stock.symbol]

        if quantity <= 0:
            continue

        actual_value = Decimal(quantity) * stock.price

        targets.append(
            TargetPosition(
                symbol=stock.symbol,
                rank=stock.rank,
                momentum=stock.momentum,
                price=stock.price,
                quantity=quantity,
                target_value=equal_weight_target,
                actual_value=actual_value,
                actual_weight=(
                    actual_value / strategy_fund
                    if strategy_fund > 0
                    else Decimal("0")
                ),
            )
        )

    residual_cash = allocated_capital - reserved_costs - invested_value

    return (
        tuple(targets),
        allocated_capital,
        equal_weight_target,
        position_cap,
        residual_cash,
        dict(sorted(excluded.items())),
    )


# ---------------------------------------------------------------------------
# Delta-order generation
# ---------------------------------------------------------------------------

def calculate_order_deltas(
    targets: Sequence[TargetPosition],
    current_holdings: Mapping[str, int],
    current_prices: Mapping[str, Decimal],
) -> tuple[OrderDelta, ...]:
    """
    Generate only required changes.

    Stocks that remain in the portfolio are not sold and repurchased.
    Existing quantity is compared directly with the new target quantity.
    """

    target_quantities = {
        target.symbol: target.quantity
        for target in targets
    }

    all_symbols = sorted(
        set(current_holdings) | set(target_quantities)
    )

    orders: list[OrderDelta] = []

    for symbol in all_symbols:
        current_quantity = current_holdings.get(symbol, 0)
        target_quantity = target_quantities.get(symbol, 0)
        difference = target_quantity - current_quantity

        if difference > 0:
            side = "BUY"
        elif difference < 0:
            side = "SELL"
        else:
            side = "HOLD"

        target = next(
            (
                item
                for item in targets
                if item.symbol == symbol
            ),
            None,
        )

        reference_price = (
            target.price
            if target is not None
            else current_prices.get(symbol, Decimal("0"))
        )

        orders.append(
            OrderDelta(
                symbol=symbol,
                current_quantity=current_quantity,
                target_quantity=target_quantity,
                quantity_delta=difference,
                side=side,
                reference_price=reference_price,
            )
        )

    return tuple(orders)


# ---------------------------------------------------------------------------
# Complete monthly strategy evaluation
# ---------------------------------------------------------------------------

def build_monthly_momentum_plan(
    *,
    evaluation_date: date,
    trading_sessions: Sequence[date],
    schedule: RebalanceSchedule,
    strategy_fund: Decimal,
    previous_regime: RegimeState,
    index_history: Sequence[Candle],
    member_histories: Mapping[str, Sequence[Candle]],
    current_prices: Mapping[str, Decimal],
    current_holdings: Mapping[str, int],
    reserved_costs: Decimal = Decimal("0"),
) -> PortfolioPlan:
    """
    Run the complete monthly Momentum evaluation.

    This function returns a proposal only. The hosting system should separately
    decide whether to execute automatically or request manual approval.
    """

    if not schedule.is_due(evaluation_date, trading_sessions):
        raise ValueError(
            f"{evaluation_date} is not the configured rebalance session"
        )

    regime = evaluate_regime(
        previous_state=previous_regime,
        index_history=index_history,
        member_histories=member_histories,
    )

    ranked, ranking_exclusions = rank_momentum_stocks(
        member_histories=member_histories,
        current_prices=current_prices,
    )

    (
        targets,
        allocated_capital,
        equal_weight_target,
        position_cap,
        residual_cash,
        allocation_exclusions,
    ) = allocate_equal_weight(
        ranked_stocks=ranked,
        strategy_fund=strategy_fund,
        exposure=regime.exposure,
        target_count=TOP_N,
        reserved_costs=reserved_costs,
    )

    orders = calculate_order_deltas(
        targets=targets,
        current_holdings=current_holdings,
        current_prices=current_prices,
    )

    invested_value = sum(
        (target.actual_value for target in targets),
        Decimal("0"),
    )

    excluded = {
        **ranking_exclusions,
        **allocation_exclusions,
    }

    return PortfolioPlan(
        evaluation_date=evaluation_date,
        regime=regime.state,
        exposure=regime.exposure,
        allocated_capital=allocated_capital,
        equal_weight_target=equal_weight_target,
        position_cap=position_cap,
        invested_value=invested_value,
        residual_cash=residual_cash,
        targets=targets,
        orders=orders,
        excluded=dict(sorted(excluded.items())),
    )


# ===========================================================================
# Hosted adapter (not part of the owner-supplied strategy math)
# ===========================================================================

PRODUCT_CNC = "CNC"

_STATUS_OK = "ok"
_STATUS_LATE_START = "late_start"
_STATUS_GAPPED = "gapped"
_STATUS_UNAVAILABLE = "unavailable"

#: Calendar days fetched before the regime anchor so the first replayed session
#: already has its 200-session warm-up for the index and every member.
_WARMUP_SESSIONS = BREADTH_SMA_SESSIONS
_CALENDAR_DAYS_PER_SESSION = 2

#: How many constituent reads between two progress notes while the frame is
#: being built. The platform's progress deadline counts from the last progress
#: write, so a long fan-out must report liveness while it runs.
_PROGRESS_EVERY = 25

#: How many replayed sessions between two progress notes.
_REPLAY_PROGRESS_EVERY = 25

#: Calendar days probed further back when checking whether an apparently late
#: series is a genuine listing or a truncated read.
_TRUNCATION_PROBE_DAYS = 5 * 365

#: The platform's own finality delay after a session close (mirrors
#: ``assess_daily_completeness(finality_delay_seconds=900)``).
_FINALITY_DELAY_SECONDS = 900

_IST = timezone(timedelta(hours=5, minutes=30))

#: States after which polling this request tells the child nothing new.
#:
#: ``awaiting_approval`` is deliberately NOT here. A review-first request is
#: parked for the OWNER, and the child's authority is what makes the approval
#: actionable while the attempt is alive: a child that returned as soon as it
#: saw ``awaiting_approval`` would hand the platform an abandoned attempt whose
#: lease then expires, and any later "approval" would be acting on a request
#: nobody is waiting for. The child therefore stays alive, keeps publishing
#: progress, and waits within its own bound; if the owner does not decide in
#: time the run ends honestly UNRESOLVED rather than pretending to be parked.
_TERMINAL_REQUEST_STATES = {
    "executed",
    "refused",
    "rejected",
    "dispatch_unresolved",
}


class _Refusal(Exception):
    """A named no-action: it never carries an order."""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.reason = code
        self.detail = detail


def _require(condition: bool, code: str, **detail: Any) -> None:
    if not condition:
        raise _Refusal(code, **detail)


def _note(ctx, text: str) -> None:  # noqa: ANN001
    """Mirror the decision to the child log, then report it as progress."""
    print(f"[strategy] {text}", file=sys.stderr, flush=True)
    try:
        ctx.progress(text[:200])
    except Exception as exc:  # noqa: BLE001 - the platform is the authority
        print(f"[strategy] progress refused: {exc}", file=sys.stderr, flush=True)
        raise


def _stop(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"no action: {reason}{(' ' + payload) if payload else ''}")
    return 0


def _unresolved(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"unresolved: {reason}{(' ' + payload) if payload else ''}")
    return 2


def _number(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return fallback
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def _decimal(value: Any) -> Optional[Decimal]:
    number = _number(value)
    if number is None:
        return None
    return Decimal(str(number))


def _ist_session_date(raw: Any) -> Optional[date]:
    """The IST trading-session date a candle timestamp belongs to."""
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(_IST).date()


# -- parameters -------------------------------------------------------------

_PARAM_DEFAULTS: Dict[str, Any] = {
    "index_symbol": "NSE:NIFTY 500",
    "constituents_source": "Nifty500",
    "product": PRODUCT_CNC,
    "rebalance_kind": "MONTHLY_LAST_SESSION",
    "rebalance_day_of_month": 25,
    "initial_regime": RegimeState.DEFENSIVE.value,
    "regime_replay_max_sessions": 260,
    "fetch_concurrency": 8,
    "deadline_seconds": 120,
}


def _params(ctx) -> Dict[str, Any]:  # noqa: ANN001
    raw = dict(getattr(ctx, "params", None) or {})
    resolved = dict(_PARAM_DEFAULTS)
    resolved.update({key: value for key, value in raw.items() if value is not None})

    capital = _number(resolved.get("budget_inr"))
    if capital is None or not math.isfinite(capital) or capital <= 0:
        raise _Refusal(
            "CAPITAL_BASIS_INVALID",
            param="budget_inr",
            stated=resolved.get("budget_inr"),
            message="budget_inr must be a finite positive number",
        )
    resolved["budget_inr"] = capital

    anchor_raw = str(resolved.get("regime_anchor_date") or "").strip()
    if not anchor_raw:
        raise _Refusal(
            "REGIME_ANCHOR_MISSING",
            param="regime_anchor_date",
            message=(
                "the regime replay needs a fixed configured anchor; without it the "
                "asymmetric 200/150 state cannot be reproduced without lookahead"
            ),
        )
    try:
        anchor = date.fromisoformat(anchor_raw)
    except ValueError as exc:
        raise _Refusal(
            "REGIME_ANCHOR_INVALID", param="regime_anchor_date", value=anchor_raw, reason=str(exc)
        ) from exc
    resolved["regime_anchor"] = anchor

    try:
        resolved["initial_state"] = RegimeState(str(resolved["initial_regime"]).strip().upper())
    except ValueError as exc:
        raise _Refusal(
            "INITIAL_REGIME_INVALID",
            value=resolved.get("initial_regime"),
            allowed=[state.value for state in RegimeState],
        ) from exc

    kind_raw = str(resolved.get("rebalance_kind") or "").strip().upper()
    try:
        kind = RebalanceScheduleKind(kind_raw)
    except ValueError as exc:
        raise _Refusal(
            "REBALANCE_KIND_INVALID",
            value=resolved.get("rebalance_kind"),
            allowed=[item.value for item in RebalanceScheduleKind],
        ) from exc
    day_raw = resolved.get("rebalance_day_of_month")
    day: Optional[int] = None
    if kind == RebalanceScheduleKind.MONTHLY_CALENDAR_DAY:
        day_number = _number(day_raw)
        if day_number is None or int(day_number) != day_number:
            raise _Refusal("REBALANCE_DAY_INVALID", value=day_raw)
        day = int(day_number)
    try:
        resolved["schedule"] = RebalanceSchedule(kind=kind, day_of_month=day)
    except ValueError as exc:
        raise _Refusal("REBALANCE_SCHEDULE_INVALID", reason=str(exc)) from exc

    if str(resolved.get("product") or "").strip().upper() != PRODUCT_CNC:
        raise _Refusal(
            "PRODUCT_UNSUPPORTED",
            product=resolved.get("product"),
            message="this strategy plans CNC (delivery) positions only",
        )
    resolved["product"] = PRODUCT_CNC

    replay_max = _number(resolved.get("regime_replay_max_sessions"))
    if replay_max is None or int(replay_max) != replay_max or not 1 <= int(replay_max) <= 750:
        raise _Refusal(
            "REGIME_REPLAY_BOUND_INVALID",
            value=resolved.get("regime_replay_max_sessions"),
            allowed="1..750 sessions",
        )
    resolved["regime_replay_max_sessions"] = int(replay_max)

    concurrency = _number(resolved.get("fetch_concurrency"))
    if concurrency is None or int(concurrency) != concurrency or not 1 <= int(concurrency) <= 32:
        raise _Refusal("FETCH_CONCURRENCY_INVALID", value=resolved.get("fetch_concurrency"))
    resolved["fetch_concurrency"] = int(concurrency)

    deadline = _number(resolved.get("deadline_seconds"), 120.0) or 0.0
    resolved["deadline_seconds"] = max(0.0, float(deadline))

    return resolved


# -- market data ------------------------------------------------------------

def _calendar_sessions(payload: Mapping[str, Any]) -> List[date]:
    """Verified REGULAR/SPECIAL sessions from the platform calendar."""
    sessions: List[date] = []
    for row in list(payload.get("sessions") or []):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("session_type") or "").upper() not in {"REGULAR", "SPECIAL"}:
            continue
        raw = row.get("session_date") or row.get("date")
        if raw is None:
            continue
        try:
            sessions.append(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    return sorted(set(sessions))


def _month_end(day: date) -> date:
    """The last calendar day of ``day``'s month."""
    return date(
        day.year,
        day.month,
        calendar_module.monthrange(day.year, day.month)[1],
    )


def _strict_history_bars(
    payload: Mapping[str, Any],
    *,
    verified_sessions: Iterable[date],
    today_ist: date,
) -> Tuple[Dict[date, Decimal], Dict[str, Any]]:
    """Parse a daily-history response without inventing or repairing data.

    A duplicate date is a contradictory series, not a later value to win. A
    missing, non-numeric, non-finite or non-positive close is a broken bar, not
    an ``NaN`` to carry into a moving average. A bar outside the verified
    calendar - or dated in the future - is not a session this strategy may trade
    on. Each of those is counted and reported; the caller refuses rather than
    silently dropping them.
    """
    allowed = set(verified_sessions)
    bars: Dict[date, Decimal] = {}
    duplicates: List[str] = []
    invalid: List[str] = []
    off_calendar: List[str] = []
    future: List[str] = []

    for row in list(payload.get("candles") or []):
        if not isinstance(row, Mapping):
            invalid.append("non_object_row")
            continue
        session = _ist_session_date(
            row.get("ts") or row.get("timestamp") or row.get("time")
        )
        if session is None:
            invalid.append("unparsable_timestamp")
            continue
        close = _decimal(row.get("close"))
        if close is None or not close.is_finite() or close <= 0:
            invalid.append(f"{session.isoformat()}:close")
            continue
        if session not in allowed:
            off_calendar.append(session.isoformat())
            continue
        if session > today_ist:
            future.append(session.isoformat())
            continue
        if session in bars:
            duplicates.append(session.isoformat())
            continue
        bars[session] = close

    return bars, {
        "duplicates": sorted(duplicates),
        "invalid": sorted(invalid),
        "off_calendar": sorted(off_calendar),
        "future": sorted(future),
    }


def _finality_state(payload: Mapping[str, Any]) -> Optional[bool]:
    """The platform's own finality verdict, or ``None`` when unavailable.

    ``None`` is not "probably final": without the flag there is no evidence that
    the newest session closed, and the caller refuses instead of assuming that a
    past-looking date implies a finished session.
    """
    flag = payload.get("last_candle_final")
    if isinstance(flag, bool):
        return flag
    return None


def _false_finality_leaves_a_later_bar(
    payload: Mapping[str, Any],
    bars: Mapping[date, Decimal],
    *,
    as_of: date,
) -> bool:
    """Whether a finality verdict leaves the AS-OF bar usable.

    ``last_candle_final`` describes the NEWEST bar the response returned, so an
    explicit ``False`` is only evidence about that bar. It therefore leaves the
    as-of session alone when the response carries a LATER session too: the verdict
    belongs to a session this run is not deciding on (a still-open session after
    the as-of session), and the as-of bar itself is proven finished by the
    verified calendar. When the newest returned bar IS the as-of bar, ``False``
    says the bar the strategy would trade on is unfinished, and it is refused.

    ``True`` and a missing verdict are not this function's question: the caller
    refuses ``None`` separately, and never reads ``None`` as "probably final".
    """
    if _finality_state(payload) is not False:
        return True
    return bool(bars) and max(bars) > as_of


def _calendar_closes(payload: Mapping[str, Any]) -> Dict[date, datetime]:
    """The verified session close INSTANT per session, from the platform calendar.

    The calendar route reports ``closes_at`` as an IST wall-clock time
    (``"15:30:00"``), so the instant is that time on that session's own date -
    the same combination the platform's completeness assessment performs. It is
    the platform's own session definition, not a guess from the date alone.
    """
    closes: Dict[date, datetime] = {}
    for row in list(payload.get("sessions") or []):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("session_type") or "").upper() not in {"REGULAR", "SPECIAL"}:
            continue
        raw_day = row.get("session_date") or row.get("date")
        raw_close = row.get("closes_at")
        if raw_day is None or raw_close is None:
            continue
        try:
            session = date.fromisoformat(str(raw_day)[:10])
            close_time = time_of_day.fromisoformat(str(raw_close))
        except ValueError:
            continue
        closes[session] = datetime.combine(session, close_time, tzinfo=_IST)
    return closes


def _read_daily_history(client, token: int, from_date: date, to_date: date) -> Mapping[str, Any]:
    # The SDK's signature is ``get_historical_candles(instrument, timeframe, ...)``:
    # the instrument is POSITIONAL. Calling it with an ``instrument_token``
    # keyword raises TypeError inside the child, which the adapter would then
    # have reported as an unavailable history - a wiring bug dressed up as a
    # data problem.
    return client.get_historical_candles(
        int(token),
        timeframe="day",
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )


def _classify_series(
    bars: Mapping[date, Decimal],
    *,
    verified_sessions: Sequence[date],
    as_of: date,
    missing_sessions: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Coverage of one series over the whole replay window, not just the tail.

    The window is the verified calendar restricted to ``<= as_of``. A hole
    inside the range a symbol is expected to cover is a GAP; a series that
    starts late is only ``late_start`` when a wider probe proves the symbol has
    no earlier data (see ``_late_start_is_genuine``), because a truncated read
    looks identical from this response alone.
    """
    if not bars:
        return {"status": _STATUS_UNAVAILABLE, "reason": "empty_history", "gaps": []}

    window = [session for session in verified_sessions if session <= as_of]
    if not window:
        return {"status": _STATUS_UNAVAILABLE, "reason": "no_session_in_window", "gaps": []}

    missing_le_as_of = []
    for raw in missing_sessions or ():
        try:
            session = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if session <= as_of:
            missing_le_as_of.append(session)

    first = min(bars)
    relevant = [session for session in window if first <= session <= as_of]
    if not relevant:
        return {"status": _STATUS_UNAVAILABLE, "reason": "no_session_in_window", "gaps": []}

    gaps = sorted(
        {session for session in relevant if session not in bars} | set(missing_le_as_of)
    )
    if gaps:
        return {
            "status": _STATUS_GAPPED,
            "reason": "missing_sessions_in_window",
            "gaps": [session.isoformat() for session in gaps],
            "first_session": first.isoformat(),
        }

    status = _STATUS_OK if first <= window[0] else _STATUS_LATE_START
    return {"status": status, "reason": "", "gaps": [], "first_session": first.isoformat()}


def _late_start_is_genuine(client, token: int, first: date) -> bool:
    """Distinguish a genuine listing date from a truncated read.

    A response that starts later than the requested window is ambiguous on its
    own: a symbol may simply not have existed yet, or the provider may have
    silently clipped the range. Asking once more for a much earlier window
    resolves it - if bars older than ``first`` come back, the first read was
    truncated and the series is NOT trustworthy.
    """
    probe_from = first - timedelta(days=_TRUNCATION_PROBE_DAYS)
    try:
        payload = _read_daily_history(client, token, probe_from, first - timedelta(days=1))
    except Exception:  # noqa: BLE001 - an unprovable late start is not evidence
        return False
    for row in list(payload.get("candles") or []):
        if not isinstance(row, Mapping):
            continue
        session = _ist_session_date(
            row.get("ts") or row.get("timestamp") or row.get("time")
        )
        close = _decimal(row.get("close"))
        if session is not None and close is not None and close > 0 and session < first:
            return False
    return True


def _candles(
    bars: Mapping[date, Decimal],
    *,
    upto: date,
    unavailable: Iterable[date] = (),
) -> Tuple[Candle, ...]:
    if unavailable:
        return tuple(
            Candle(session=session, close=Decimal("0"), final=False) for session in unavailable
        )
    return tuple(
        Candle(session=session, close=close, final=True)
        for session, close in sorted(bars.items())
        if session <= upto
    )


# -- regime replay ----------------------------------------------------------

def _regime_replay(
    *,
    replay_sessions: Sequence[date],
    as_of: date,
    initial_state: RegimeState,
    index_bars: Mapping[date, Decimal],
    member_bars: Mapping[str, Mapping[date, Decimal]],
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Replay the asymmetric regime day by day from the anchor, with no lookahead.

    The state machine is carried across the whole replay window instead of being
    restarted each day, because the 200/150 window choice depends on the state
    that the PREVIOUS session produced -- and that previous state depends on the
    previous breadth gate, not only on the index. Replaying the index alone (or
    reading a sliding window) would silently change the exposure.
    """
    decision: Optional[RegimeDecision] = None
    previous_state = initial_state
    days = 0

    for session in replay_sessions:
        if session > as_of:
            break
        index_history = _candles(index_bars, upto=session)
        histories = {
            symbol: _candles(bars, upto=session)
            for symbol, bars in member_bars.items()
        }
        decision = evaluate_regime(
            previous_state=previous_state,
            index_history=index_history,
            member_histories=histories,
        )
        previous_state = decision.state
        days += 1
        if progress is not None and (
            days % _REPLAY_PROGRESS_EVERY == 0 or days == len(replay_sessions)
        ):
            progress(
                f"regime replay {days}/{len(replay_sessions)} sessions "
                f"(breadth {decision.breadth_numerator}/{decision.breadth_denominator})"
            )

    if decision is None:
        raise _Refusal(
            "REGIME_REPLAY_EMPTY",
            as_of=as_of.isoformat(),
        )

    return {
        "decision": decision,
        "days": days,
        "previous_state": decision.previous_state,
        "state": decision.state,
        "exposure": decision.exposure,
        "index_close": decision.index_close,
        "index_sma": decision.index_sma,
        "breadth": decision.breadth,
        "numerator": decision.breadth_numerator,
        "denominator": decision.breadth_denominator,
    }


# -- owned book -------------------------------------------------------------

def _owned_book(
    snapshot: Mapping[str, Any],
) -> Tuple[Dict[str, int], Dict[str, Mapping[str, Any]]]:
    """Own NSE/CNC positions, keyed by symbol, with their broker identities.

    Every row must be usable evidence on its own: an integral, non-negative
    quantity, a canonical token, an NSE coordinate and the CNC product. A row
    that fails any of those is a refusal - a fractional quantity must never be
    truncated into a smaller exit, a negative quantity must never be folded into
    a long, and two different instruments sharing a tradingsymbol must never be
    collapsed into one position.
    """
    if str(snapshot.get("coverage") or "") != "known":
        raise _Refusal(
            "OWNED_WORK_COVERAGE_UNKNOWN",
            notes=list(snapshot.get("notes") or []),
            message="an unpublished or stale projection is UNKNOWN, never a flat book",
        )

    pending = [row for row in list(snapshot.get("pending") or []) if isinstance(row, Mapping)]
    if pending:
        raise _Refusal(
            "OWNED_WORK_PENDING",
            pending=len(pending),
            message="outstanding work must settle before another target is planned",
        )

    book: Dict[str, int] = {}
    identities: Dict[str, Mapping[str, Any]] = {}
    for row in list(snapshot.get("positions") or []):
        if not isinstance(row, Mapping):
            raise _Refusal("OWNED_WORK_POSITION_MALFORMED", row=repr(row)[:120])

        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        exchange = str(row.get("exchange") or "").strip().upper()
        product = str(row.get("product") or "").strip().upper()
        token_raw = row.get("instrument_token")
        try:
            token = int(token_raw)
        except (TypeError, ValueError):
            token = 0

        if not symbol or token <= 0 or exchange != "NSE" or product != PRODUCT_CNC:
            raise _Refusal(
                "OWNED_WORK_POSITION_UNSUPPORTED",
                tradingsymbol=symbol or None,
                exchange=exchange or None,
                product=product or None,
                instrument_token=token_raw,
                message=(
                    "this strategy plans NSE CNC positions with a canonical broker "
                    "token only; it will not guess at an identity"
                ),
            )

        if row.get("unresolved_reason"):
            raise _Refusal(
                "OWNED_WORK_POSITION_UNRESOLVED",
                tradingsymbol=symbol,
                reason=str(row.get("unresolved_reason"))[:200],
            )

        raw_quantity = row.get("net_quantity")
        quantity_decimal = _decimal(raw_quantity)
        if quantity_decimal is None or not quantity_decimal.is_finite():
            raise _Refusal(
                "OWNED_WORK_POSITION_MALFORMED",
                tradingsymbol=symbol,
                net_quantity=raw_quantity,
            )
        if quantity_decimal != quantity_decimal.to_integral_value():
            raise _Refusal(
                "OWNED_WORK_POSITION_NOT_INTEGRAL",
                tradingsymbol=symbol,
                net_quantity=str(raw_quantity),
                message="a fractional quantity is not flattened by truncation",
            )
        quantity = int(quantity_decimal)
        if quantity < 0:
            raise _Refusal(
                "OWNED_WORK_POSITION_NEGATIVE",
                tradingsymbol=symbol,
                net_quantity=quantity,
                message="this strategy declares long-only CNC holdings",
            )
        if quantity == 0:
            continue

        existing = identities.get(symbol)
        if existing is not None and int(existing["instrument_token"]) != token:
            raise _Refusal(
                "OWNED_WORK_IDENTITY_AMBIGUOUS",
                tradingsymbol=symbol,
                first_token=existing["instrument_token"],
                second_token=token,
                message="two instruments share this tradingsymbol; a symbol-keyed target cannot tell them apart",
            )

        identities[symbol] = {
            "instrument_token": token,
            "exchange": exchange,
            "tradingsymbol": symbol,
        }
        book[symbol] = book.get(symbol, 0) + quantity
    return book, identities


# -- plan construction ------------------------------------------------------

def _leg(
    identity: Mapping[str, Any],
    *,
    target_quantity: int,
    reference_price: Optional[Decimal],
) -> Dict[str, Any]:
    leg: Dict[str, Any] = {
        "instrument_token": int(identity["instrument_token"]),
        "exchange": str(identity["exchange"]).upper(),
        "tradingsymbol": str(identity["tradingsymbol"]).upper(),
        "product": PRODUCT_CNC,
        # The exact whole-share TARGET. The executor derives the delta from the
        # run's own attributed book, so nothing is re-rounded from a weight.
        "target_quantity": int(target_quantity),
    }
    if target_quantity and reference_price is not None and reference_price > 0:
        leg["reference_price"] = float(reference_price)
    return leg


def _legs_for_targets(
    *,
    target_quantities: Mapping[str, int],
    book: Mapping[str, int],
    identities: Mapping[str, Mapping[str, Any]],
    prices: Mapping[str, Decimal],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Exact-quantity legs for every symbol whose target differs from the book."""
    legs: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []

    for symbol in sorted(set(target_quantities) | set(book)):
        target = int(target_quantities.get(symbol, 0))
        current = int(book.get(symbol, 0))
        if target == current:
            continue
        identity = identities.get(symbol)
        _require(
            identity is not None,
            "SYMBOL_IDENTITY_UNRESOLVED",
            symbol=symbol,
            message="a planned symbol has no resolved broker coordinate",
        )
        legs.append(
            _leg(
                identity,
                target_quantity=target,
                reference_price=prices.get(symbol),
            )
        )
        changes.append(
            {
                "symbol": symbol,
                "current_quantity": current,
                "target_quantity": target,
                "quantity_delta": target - current,
            }
        )
    return legs, changes


def _await_request(ctx, request_id: str, deadline_seconds: float) -> Dict[str, Any]:  # noqa: ANN001
    if not request_id:
        return {"status": "unknown"}

    # The first read always happens: a zero deadline means "do not wait", not
    # "do not look". Returning a synthetic timeout without asking would throw
    # away the platform's own answer about a request that may already be done.
    started = time.monotonic()
    row = dict(ctx.run.execution_request(request_id))
    status = str(row.get("status") or "")
    while status not in _TERMINAL_REQUEST_STATES and (
        time.monotonic() - started
    ) < deadline_seconds:
        ctx.progress(f"waiting on execution request {request_id}")
        time.sleep(2.0)
        row = dict(ctx.run.execution_request(request_id))
        status = str(row.get("status") or "")

    if status in _TERMINAL_REQUEST_STATES:
        return row

    # The child's own authority is an ATTEMPT-scoped credential: it expires with
    # the attempt, while the request row stays durable. So a deadline that passes
    # while the platform is still dispatching is reported as unresolved: the row
    # keeps the decision for the record, but a claim re-reads the job's run, token,
    # lease epoch and attempt
    # (``backend/strategies/execution_requests.py:_attempt_refusal``), so once this
    # attempt ends the row is NOT dispatchable - the claim refuses it by name
    # (``HOSTED_ATTEMPT_FENCED``). A FRESH attempt is what acts on the decision,
    # and this child cannot claim it decided anything.
    return {
        "status": "timeout",
        "request_id": request_id,
        "last_status": status,
        "note": (
            "the request stayed non-terminal past this attempt's deadline; this "
            "attempt's authority ends with the child, so the row is not "
            "dispatchable afterwards and a fresh attempt is required"
        ),
    }


def _submit(ctx, *, strategy_id: str, account_scope: str, label: str, legs, budget_inr, extra):  # noqa: ANN001
    evaluation_id = f"n500mom-{label}-{ctx.run_id}-{extra['as_of_session']}"
    proposal = {
        "evaluation_id": evaluation_id,
        "evaluation_kind": "run_now",
        "strategy_id": strategy_id,
        "strategy_run_id": ctx.run_id,
        "account_scope": account_scope,
        "target_kind": "intent_bundle",
        "payload": {
            "legs": legs,
            # Stated for the plan record. The exact-quantity path is bounded by
            # the platform's admission ceiling rather than by this number.
            "capital_basis_inr": budget_inr,
            "intent": label,
            **extra,
        },
    }
    submitted = ctx.run.submit_proposal(proposal)
    plan = dict(submitted.get("plan") or {})
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        refusal = dict(submitted.get("refusal") or {})
        reason = str(
            refusal.get("rejection_reason")
            or refusal.get("code")
            or submitted.get("status")
            or "PLAN_NOT_FROZEN"
        )
        return None, refusal, reason

    request = ctx.run.request_execution(plan_id, idempotency_key=evaluation_id)
    return request, {}, ""


# -- the hosted entrypoint --------------------------------------------------

def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    try:
        return _run(ctx)
    except _Refusal as refusal:
        return _stop(ctx, refusal.reason, **refusal.detail)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return _unresolved(ctx, "momentum evaluation failed", error=f"{type(exc).__name__}: {exc}")


def _run(ctx) -> int:  # noqa: ANN001
    identity = ctx.run.attribution()
    if not identity.get("attributed"):
        raise _Refusal(
            "RUN_NOT_ATTRIBUTED",
            reason=identity.get("reason"),
            message="strategy and account come from the persisted run binding",
        )
    strategy_id = str(identity.get("strategy_id") or "")
    account_scope = str(identity.get("account_id") or "")
    _require(bool(strategy_id) and bool(account_scope), "RUN_BINDING_INCOMPLETE", identity=identity)

    params = _params(ctx)
    budget = Decimal(str(params["budget_inr"]))
    anchor: date = params["regime_anchor"]
    schedule: RebalanceSchedule = params["schedule"]

    now = datetime.now(timezone.utc)
    today_ist = now.astimezone(_IST).date()

    # The replay window needs its 200-session warm-up before the anchor, so the
    # fetch starts further back than the anchor itself. Both bounds are derived
    # from the configured anchor - never from "today" - so the same anchor means
    # the same window on every run.
    warmup_days = _WARMUP_SESSIONS * _CALENDAR_DAYS_PER_SESSION
    fetch_from = anchor - timedelta(days=warmup_days)
    fetch_to = today_ist

    # The calendar is read through the END OF THE MONTH, not through today. A
    # monthly schedule is decided against the month's own verified session list:
    # reading only up to today would make today the last known session of the
    # month on every single day, so ``MONTHLY_LAST_SESSION`` would report "due"
    # every day and the strategy would rebalance daily.
    calendar_to = _month_end(max(today_ist, anchor))

    calendar = ctx.client.get_market_calendar(
        fetch_from.isoformat(), calendar_to.isoformat(), exchange="NSE", segment="CM"
    )
    verified_sessions = _calendar_sessions(calendar)
    _require(
        bool(verified_sessions),
        "CALENDAR_UNAVAILABLE",
        exchange="NSE",
        segment="CM",
        message="no verified sessions; nothing is planned without the platform calendar",
    )
    known_sessions = [session for session in verified_sessions if session <= today_ist]
    _require(
        bool(known_sessions),
        "CALENDAR_UNAVAILABLE",
        reason="no verified session at or before today",
    )
    session_closes = _calendar_closes(calendar)
    # The as-of session is the latest session the VERIFIED CALENDAR says is
    # finished (close + the platform's finality delay), not "whatever bar came
    # back" and not "today". Before the close, the previous session is the
    # signal date; after it, today becomes the signal date. Nothing about the
    # unfinished session is read.
    completed_sessions = [
        session
        for session in known_sessions
        if (close_at := session_closes.get(session)) is not None
        and now >= close_at + timedelta(seconds=_FINALITY_DELAY_SECONDS)
    ]
    _require(
        bool(completed_sessions),
        "NO_COMPLETED_SESSION",
        message="the verified calendar reports no session finished yet",
    )
    latest_completed = max(completed_sessions)

    resolved = ctx.client.resolve_ticker(params["index_symbol"])
    instrument = dict(resolved.get("instrument") or resolved or {})
    index_token = instrument.get("instrument_token")
    _require(
        index_token is not None,
        "INDEX_TICKER_UNRESOLVED",
        index_symbol=params["index_symbol"],
    )
    index_member = {
        "instrument_token": int(index_token),
        "exchange": str(instrument.get("exchange") or "NSE").upper(),
        "tradingsymbol": str(instrument.get("tradingsymbol") or params["index_symbol"]),
        "symbol": str(instrument.get("tradingsymbol") or params["index_symbol"]).upper(),
    }

    try:
        index_payload = _read_daily_history(
            ctx.client, index_member["instrument_token"], fetch_from, fetch_to
        )
    except Exception as exc:  # noqa: BLE001 - availability, not a signal
        raise _Refusal(
            "INDEX_HISTORY_UNAVAILABLE",
            index_symbol=params["index_symbol"],
            error=f"{type(exc).__name__}: {exc}",
        ) from exc
    index_bars, index_defects = _strict_history_bars(
        index_payload, verified_sessions=verified_sessions, today_ist=today_ist
    )
    _require(
        not index_defects["duplicates"] and not index_defects["invalid"],
        "INDEX_HISTORY_CONTRADICTORY",
        detail=index_defects,
        message="a duplicated date or an unusable close is refused, never repaired",
    )
    _require(bool(index_bars), "INDEX_HISTORY_UNAVAILABLE", index_symbol=params["index_symbol"])

    present = [
        session
        for session in known_sessions
        if session in index_bars and session <= latest_completed
    ]
    _require(
        bool(present),
        "INDEX_HISTORY_UNAVAILABLE",
        reason="no completed session has an index bar",
        latest_completed=latest_completed.isoformat(),
    )
    as_of = max(present)
    # The as-of session must BE the newest session the verified calendar reports
    # as finished, not merely an older session that happens to have a bar. Rolling
    # back here would silently re-date a stale signal (and shrink the replay
    # window with it), so a missing newest index bar is refused by name: an old
    # bar is not this session's evidence.
    _require(
        as_of == latest_completed,
        "INDEX_HISTORY_STALE",
        as_of=as_of.isoformat(),
        latest_completed=latest_completed.isoformat(),
        newest_index_bar=max(index_bars).isoformat(),
        message=(
            "the newest session the verified calendar reports as finished has no "
            "index bar; a stale bar is never re-dated as this session's signal"
        ),
    )
    _require(
        _finality_state(index_payload) is not None,
        "SESSION_FINALITY_UNKNOWN",
        index_symbol=params["index_symbol"],
        message=(
            "the platform reported no finality evidence for the requested range; a "
            "past-looking date is not proof that a session is complete"
        ),
    )
    _require(
        _false_finality_leaves_a_later_bar(index_payload, index_bars, as_of=as_of),
        "INDEX_SESSION_NOT_FINAL",
        as_of=as_of.isoformat(),
        newest_index_bar=max(index_bars).isoformat(),
        message=(
            "the platform reports the newest returned index bar as unfinished and "
            "no later session is present, so the as-of bar itself is not final"
        ),
    )
    if index_defects["off_calendar"] or index_defects["future"]:
        _note(
            ctx,
            "index bars outside the verified calendar were not used: "
            f"off_calendar={len(index_defects['off_calendar'])} "
            f"future={len(index_defects['future'])}",
        )

    # Sessions the replay may walk: verified, at or before the as-of session, and
    # with the 200-session warm-up the anchor needs.
    calendar_window = [session for session in known_sessions if session <= as_of]
    index_coverage = _classify_series(
        index_bars,
        verified_sessions=calendar_window,
        as_of=as_of,
        missing_sessions=index_payload.get("missing_sessions") or (),
    )
    _require(
        index_coverage["status"] in {_STATUS_OK, _STATUS_LATE_START},
        "INDEX_HISTORY_INCOMPLETE",
        detail=index_coverage,
    )

    snapshot = ctx.client.get_index_constituents(params["constituents_source"])
    _require(
        bool(snapshot.get("complete")),
        "INDEX_UNIVERSE_INCOMPLETE",
        source=params["constituents_source"],
    )
    members = [dict(row) for row in list(snapshot.get("members") or []) if isinstance(row, Mapping)]
    _require(bool(members), "INDEX_UNIVERSE_EMPTY", source=params["constituents_source"])

    replay_sessions = [session for session in calendar_window if session >= anchor]
    _require(
        bool(replay_sessions),
        "REGIME_ANCHOR_IN_FUTURE",
        anchor=anchor.isoformat(),
        as_of=as_of.isoformat(),
    )
    _require(
        len(replay_sessions) <= params["regime_replay_max_sessions"],
        "REGIME_REPLAY_TOO_LONG",
        sessions=len(replay_sessions),
        limit=params["regime_replay_max_sessions"],
        message="move the configured anchor forward or raise the bound deliberately",
    )

    member_bars: Dict[str, Dict[date, Decimal]] = {}
    identities: Dict[str, Mapping[str, Any]] = {}
    unavailable: List[str] = []
    gapped: List[str] = []
    unfinal: List[str] = []
    late_start: List[str] = []
    contradictory: List[str] = []

    def _fetch(row: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any], Optional[Exception]]:
        token = row.get("instrument_token")
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        try:
            payload = _read_daily_history(ctx.client, int(token), fetch_from, fetch_to)
        except Exception as exc:  # noqa: BLE001 - availability, not exclusion
            return symbol, {}, exc
        return symbol, payload, None

    concurrency = params["fetch_concurrency"]
    results: List[Tuple[str, Mapping[str, Any], Optional[Exception]]] = []
    _note(
        ctx,
        f"reading daily history for {len(members)} constituents "
        f"from {fetch_from.isoformat()} to {fetch_to.isoformat()}",
    )
    if concurrency > 1 and len(members) > 1:
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                # Progress is written WHILE the fan-out runs: a strategy that
                # reports liveness only after ~500 reads can be mistaken for a
                # hung child and have its attempt recycled by the progress
                # deadline.
                for index, result in enumerate(pool.map(_fetch, members), start=1):
                    results.append(result)
                    if index % _PROGRESS_EVERY == 0 or index == len(members):
                        _note(ctx, f"constituent history {index}/{len(members)} read")
        except (RuntimeError, OSError) as exc:
            # A hosted child runs under the platform's own rlimits, and a thread
            # pool can be refused outright (RLIMIT_NPROC) depending on how busy
            # the container's uid is. Concurrency is an optimisation here, never a
            # correctness requirement: fall back to sequential reads and say so
            # rather than reporting an unavailable market.
            _note(ctx, f"thread pool unavailable ({type(exc).__name__}); reading sequentially")
            results = []
    if not results:
        for index, row in enumerate(members, start=1):
            results.append(_fetch(row))
            if index % _PROGRESS_EVERY == 0 or index == len(members):
                _note(ctx, f"constituent history {index}/{len(members)} read")

    by_symbol = {
        str(row.get("tradingsymbol") or "").strip().upper(): row for row in members
    }
    for symbol, payload, error in results:
        identities[symbol] = by_symbol.get(symbol, {})
        if error is not None:
            unavailable.append(symbol)
            continue
        finality = _finality_state(payload)
        if finality is None:
            # A member response with NO finality evidence is not usable: the
            # adapter will not infer a finished session from the date alone.
            unavailable.append(symbol)
            continue
        bars, defects = _strict_history_bars(
            payload, verified_sessions=verified_sessions, today_ist=today_ist
        )
        if defects["duplicates"] or defects["invalid"]:
            contradictory.append(symbol)
            continue
        # An explicit ``False`` is the platform saying the NEWEST returned bar is
        # still open. That is only compatible with this run when a later session
        # is present in the response; if the newest bar IS the as-of bar, the
        # member's signal bar is unfinished.
        if not _false_finality_leaves_a_later_bar(payload, bars, as_of=as_of):
            unfinal.append(symbol)
            continue
        coverage = _classify_series(
            bars,
            verified_sessions=calendar_window,
            as_of=as_of,
            missing_sessions=payload.get("missing_sessions") or (),
        )
        status = coverage["status"]
        if status == _STATUS_UNAVAILABLE:
            unavailable.append(symbol)
            continue
        if status == _STATUS_GAPPED:
            gapped.append(symbol)
            continue
        if status == _STATUS_LATE_START:
            # A series that starts late is either a genuine listing or a truncated
            # read. Only a wider probe can tell them apart; an unexplained late
            # start is treated as unknown data, never as an exclusion.
            if not _late_start_is_genuine(ctx.client, int(identities[symbol]["instrument_token"]), min(bars)):
                unavailable.append(symbol)
                continue
            late_start.append(symbol)
        member_bars[symbol] = bars

    _require(
        not contradictory,
        "CONSTITUENT_HISTORY_CONTRADICTORY",
        count=len(contradictory),
        symbols=sorted(contradictory)[:20],
        message="a duplicated date or an unusable close is refused, never repaired",
    )
    # Availability is never an exclusion: a failed or empty read means we do not
    # know the breadth, so the run does nothing at all (no entry AND no exit).
    _require(
        not unavailable,
        "CONSTITUENT_HISTORY_UNAVAILABLE",
        count=len(unavailable),
        symbols=sorted(unavailable)[:20],
        message="an unreadable, unfinal, or truncated constituent is not 'insufficient history'",
    )
    _require(
        not unfinal,
        "CONSTITUENT_SESSION_NOT_FINAL",
        count=len(unfinal),
        symbols=sorted(unfinal)[:20],
        as_of=as_of.isoformat(),
        message=(
            "a constituent's newest returned bar is the as-of session and the "
            "platform reports it as unfinished; an unfinished bar is not a signal"
        ),
    )
    if gapped:
        raise _Refusal(
            "CONSTITUENT_HISTORY_GAP",
            count=len(gapped),
            symbols=sorted(gapped)[:20],
            message=(
                "coverage is the gate: a hole inside the replay window is unknown "
                "evidence, so there is no entry and no exit"
            ),
        )

    if not member_bars:
        raise _Refusal("NO_CONSTITUENT_HISTORY", source=params["constituents_source"])

    replay = _regime_replay(
        replay_sessions=replay_sessions,
        as_of=as_of,
        initial_state=params["initial_state"],
        index_bars=index_bars,
        member_bars=member_bars,
        progress=lambda note: _note(ctx, note),
    )
    decision = replay["decision"]

    prices: Dict[str, Decimal] = {}
    for symbol, bars in member_bars.items():
        price = bars.get(as_of)
        if price is not None and price > 0 and price.is_finite():
            prices[symbol] = price

    _note(
        ctx,
        "regime {state} exposure={exposure} breadth={num}/{den} ({ratio}) "
        "index={close}>{sma} anchor={anchor} replay_days={days}".format(
            state=replay["state"].value,
            exposure=replay["exposure"],
            num=replay["numerator"],
            den=replay["denominator"],
            ratio=round(float(replay["breadth"]), 4),
            close=replay["index_close"],
            sma=round(float(replay["index_sma"]), 2),
            anchor=anchor.isoformat(),
            days=replay["days"],
        ),
    )
    if late_start:
        _note(
            ctx,
            f"late-listed constituents excluded from ranking only: {len(late_start)}",
        )
    if gapped:
        _note(ctx, f"gapped constituents: {sorted(gapped)[:20]}")

    try:
        snapshot = ctx.run.owned_work()
        book, book_identities = _owned_book(snapshot)
    except _Refusal as refusal:
        if refusal.reason != "OWNED_WORK_COVERAGE_UNKNOWN":
            raise
        # An unknown book is a named no-action with a readable preview. Nothing is
        # submitted and no projection is manufactured to make the question go away.
        return _stop(
            ctx,
            refusal.reason,
            as_of=as_of.isoformat(),
            regime=replay["state"].value,
            exposure=str(replay["exposure"]),
            breadth=f"{replay['numerator']}/{replay['denominator']}",
            preview="targets would be sized against the owner allocation once the book is known",
            notes=refusal.detail.get("notes"),
        )

    # The schedule is decided against the COMPLETE verified month, not against
    # the sessions walked so far: with only the historical window, "last session
    # of the month" would resolve to the most recent session and the strategy
    # would rebalance on every run.
    month_sessions = [
        session
        for session in verified_sessions
        if session.year == as_of.year and session.month == as_of.month
    ]
    _require(
        bool(month_sessions),
        "CALENDAR_INCOMPLETE_FOR_MONTH",
        as_of=as_of.isoformat(),
        message="the verified calendar does not cover the as-of month",
    )
    due = schedule.is_due(as_of, month_sessions)
    known_identities = {**identities, **book_identities}

    # --- failed breadth: exit own holdings, never enter ---------------------
    if decision.state == RegimeState.DEFENSIVE:
        if not book:
            return _stop(
                ctx,
                "breadth gate failed and the strategy holds nothing",
                as_of=as_of.isoformat(),
                breadth=f"{replay['numerator']}/{replay['denominator']}",
            )
        target_quantities = {symbol: 0 for symbol in book}
        legs, changes = _legs_for_targets(
            target_quantities=target_quantities,
            book=book,
            identities=known_identities,
            prices=prices,
        )
        if not legs:
            return _stop(ctx, "no exit legs to place", as_of=as_of.isoformat())
        return _dispatch(
            ctx,
            strategy_id=strategy_id,
            account_scope=account_scope,
            label="breadth_exit",
            legs=legs,
            changes=changes,
            budget_inr=params["budget_inr"],
            deadline_seconds=params["deadline_seconds"],
            extra={
                "as_of_session": as_of.isoformat(),
                "regime": replay["state"].value,
                "breadth_numerator": replay["numerator"],
                "breadth_denominator": replay["denominator"],
            },
        )

    # --- off-schedule recovery: no entry ------------------------------------
    if not due:
        return _stop(
            ctx,
            "off the monthly rebalance session; no entry",
            as_of=as_of.isoformat(),
            regime=replay["state"].value,
            breadth=f"{replay['numerator']}/{replay['denominator']}",
        )

    plan = build_monthly_momentum_plan(
        evaluation_date=as_of,
        trading_sessions=month_sessions,
        schedule=schedule,
        strategy_fund=budget,
        previous_regime=decision.previous_state,
        index_history=_candles(index_bars, upto=as_of),
        member_histories={
            symbol: _candles(bars, upto=as_of) for symbol, bars in member_bars.items()
        },
        current_prices=prices,
        current_holdings=dict(book),
    )

    target_quantities = {target.symbol: target.quantity for target in plan.targets}
    legs, changes = _legs_for_targets(
        target_quantities=target_quantities,
        book=book,
        identities=known_identities,
        prices=prices,
    )

    invested = sum(
        (Decimal(target.quantity) * prices.get(target.symbol, Decimal("0"))
         for target in plan.targets),
        Decimal("0"),
    )
    # The stated basis is the strategy's own affordability bound; the platform's
    # admission ceiling is the authoritative one.
    if invested > budget:
        raise _Refusal(
            "TARGET_EXCEEDS_STATED_BASIS",
            target_notional_inr=float(invested),
            stated_capital_basis_inr=params["budget_inr"],
        )

    _note(
        ctx,
        f"monthly rebalance {as_of.isoformat()}: targets={len(plan.targets)} "
        f"notional={round(float(invested), 2)} changes={len(changes)} "
        f"excluded={len(plan.excluded)}",
    )

    if not legs:
        return _stop(
            ctx,
            "the book already matches the momentum target; no proposal",
            as_of=as_of.isoformat(),
            targets=len(plan.targets),
            excluded=len(plan.excluded),
        )

    return _dispatch(
        ctx,
        strategy_id=strategy_id,
        account_scope=account_scope,
        label="monthly_rebalance",
        legs=legs,
        changes=changes,
        budget_inr=params["budget_inr"],
        deadline_seconds=params["deadline_seconds"],
        extra={
            "as_of_session": as_of.isoformat(),
            "regime": plan.regime.value,
            "exposure": str(plan.exposure),
            "target_count": len(plan.targets),
            "breadth_numerator": replay["numerator"],
            "breadth_denominator": replay["denominator"],
            "excluded": dict(list(plan.excluded.items())[:50]),
        },
    )


def _dispatch(  # noqa: ANN001
    ctx,
    *,
    strategy_id: str,
    account_scope: str,
    label: str,
    legs: List[Dict[str, Any]],
    changes: List[Dict[str, Any]],
    budget_inr: float,
    deadline_seconds: float,
    extra: Dict[str, Any],
) -> int:
    _note(ctx, f"{label}: submitting {len(legs)} exact-quantity legs {changes[:5]}")
    request, refusal, reason = _submit(
        ctx,
        strategy_id=strategy_id,
        account_scope=account_scope,
        label=label,
        legs=legs,
        budget_inr=budget_inr,
        extra=extra,
    )
    if request is None:
        return _stop(
            ctx,
            "the platform refused the plan; no order was sent",
            code=reason,
            detail=refusal,
        )

    request_id = str(request.get("request_id") or "")
    _note(ctx, f"{label} requested {request_id} status={request.get('status')}")
    final = _await_request(ctx, request_id, deadline_seconds)
    status = str(final.get("status") or "")
    if status in {"refused", "rejected"}:
        return _stop(ctx, f"{label} was refused", code=final.get("refusal_code") or final.get("detail"))
    if status == "executed":
        _note(ctx, f"{label} dispatch status=executed")
        return 0
    if status == "dispatch_unresolved":
        return _unresolved(
            ctx, f"{label} outcome is unknown", code=final.get("refusal_code")
        )

    last_status = str(final.get("last_status") or status)
    if last_status == "awaiting_approval":
        # Review-first parks the work for the OWNER, and this attempt waited the
        # whole bound for that decision. The durable row records the request, but
        # it is NOT actionable once this attempt ends: the claim re-reads the
        # job's run/token/epoch/attempt and refuses a request whose attempt is
        # gone, so a later approval needs a FRESH attempt rather than this parked
        # row. This child therefore cannot claim it decided anything and ends
        # honestly UNRESOLVED, instead of reporting a parked request as a
        # finished run.
        return _unresolved(
            ctx,
            f"{label} was still waiting for the owner's decision at the attempt's deadline",
            status=last_status,
            request_id=request_id,
            bound_seconds=deadline_seconds,
        )

    # Anything else - queued, dispatching, claimed, releasing, partial, timeout,
    # unknown - is NOT an owner wait. Reporting it as "waiting for the owner"
    # would be the same false claim: in autonomous mode no owner is involved, and
    # in manual mode the owner's part is done.
    return _unresolved(
        ctx,
        f"{label} is not finished and is not an owner wait",
        status=last_status or "unknown",
        request_id=request_id,
        note=str(final.get("note") or ""),
    )
