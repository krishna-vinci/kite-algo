"""Pure-Python indicator numerics for backend alert feature computation.

This module is the runtime feature layer for the alerts engine.  It is
dependency-free on purpose (stdlib ``math``/``typing`` only): the backend is
deployed separately from the strategy SDK, so it cannot import
``kite_algo_worker.indicators`` (pandas/numpy based) at runtime.  The numerics
here mirror the SDK kernels exactly so alert features agree with
SDK-computed indicators; parity is asserted to 1e-9 absolute in
``tests/alerts/test_feature_numerics.py``.

Mirrored SDK semantics (``sdk/python/kite_algo_worker/indicators/``):

* ``sma`` / ``volume_sma`` -- ``base._sma_kernel``: rolling arithmetic mean;
  any non-finite value inside the window makes the position unknown.
* ``ema`` -- ``trend._ema_kernel``: per run of consecutive finite values,
  seeded with the SMA of the first ``period`` values, then
  ``prev = (price - prev) * alpha + prev`` with ``alpha = 2 / (period + 1)``.
  A run shorter than ``period`` yields no output.
* ``wma`` -- ``trend._wma_kernel``: linear weights ``1..period`` over the
  window, divided by ``period * (period + 1) / 2``.
* ``rsi`` -- ``momentum._rsi_kernel`` (Wilder): per finite run, the seed
  averages are the simple mean of the first ``period`` up/down deltas, then
  ``avg = (avg * (period - 1) + current) / period``.  A zero average loss maps
  to ``100.0`` when there is any gain and to ``50.0`` for a flat series.  The
  first output lands at index ``period`` (one bar later than the EMA family).
* ``macd`` -- ``momentum.macd``: ``EMA(fast) - EMA(slow)``; the signal line is
  a second EMA applied to the macd line (whose warmup NaNs restart the EMA
  segmentation, so the signal seed lands at index ``slow + signal - 2`` for
  finite input); histogram = macd - signal.
* ``atr`` -- ``volatility.atr``: true range (first bar ``high - low``, then
  ``max(h - l, |h - prev_close|, |prev_close - l|)``) smoothed with Wilder's
  method seeded by the SMA of the first ``period`` true ranges.
* ``bollinger`` -- ``volatility._bbands_kernel``: mid = SMA, spread =
  *population* standard deviation (``ddof=0``) of the window.
* ``supertrend`` -- ``trend._supertrend_frame``: bands = ``hl2 +- multiplier *
  ATR``; direction flips when close crosses the previous *final* band, the
  active band ratchets only while the direction continues, state carries
  through non-finite closes, and the first bar takes direction ``+1`` with the
  lower band as its value.
* ``vwap_session`` -- ``volume._vwap_kernel`` applied per session: price is
  the typical price ``(h + l + c) / 3`` and the cumulative numerator and
  denominator reset at every session change (and restart after non-finite
  bars, matching the SDK's finite-run segmentation; positions with a zero
  running denominator stay unknown).
* ``volume_ratio`` -- ``volumes[i] / sma(volumes, period)[i - offset]``.

Contract notes: every function returns a list (or dict of lists) aligned to
the input length where ``None`` marks insufficient warmup, an invalid
denominator, or non-finite input; functions never raise on bad data, only on
invalid configuration (period outside ``[1, 500]``, non-positive
``num_std``/``multiplier``).  The SDK kernels guard with ``isnan`` in a few
places; this module treats *any* non-finite input (NaN, inf, None) as unknown,
which produces identical results for finite data.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "CALC_VERSION",
    "atr",
    "bollinger",
    "ema",
    "macd",
    "rsi",
    "sma",
    "supertrend",
    "volume_ratio",
    "volume_sma",
    "vwap_session",
    "wma",
]

CALC_VERSION = 1

_MAX_PERIOD = 500


# ---------------------------------------------------------------------------
# validation and small helpers
# ---------------------------------------------------------------------------


def _validate_period(period: Any, name: str = "period") -> int:
    try:
        value = int(period)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer between 1 and {_MAX_PERIOD}") from None
    if value < 1 or value > _MAX_PERIOD:
        raise ValueError(f"{name} must be between 1 and {_MAX_PERIOD}, got {period!r}")
    return value


def _validate_positive(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return number


def _is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(value)
    except TypeError:
        return False


def _bar_float(bar: Any, key: str) -> Optional[float]:
    """Extract a bar field as a finite float; ``None`` when missing/invalid."""
    if not isinstance(bar, dict):
        return None
    value = bar.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _finite_segments(values: Sequence[Optional[float]]) -> List:
    """Contiguous runs of finite values; mirrors the SDK ``_finite_segments``."""
    segments = []
    start = None
    for idx, value in enumerate(values):
        if _is_finite(value):
            if start is None:
                start = idx
        elif start is not None:
            segments.append((start, idx))
            start = None
    if start is not None:
        segments.append((start, len(values)))
    return segments


def _mean(window: Sequence[float]) -> float:
    return sum(window) / float(len(window))


# ---------------------------------------------------------------------------
# rolling means
# ---------------------------------------------------------------------------


def _rolling_mean(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """Rolling arithmetic mean (SDK ``base._sma_kernel``)."""
    n = len(values)
    result: List[Optional[float]] = [None] * n
    for idx in range(period - 1, n):
        window = values[idx - period + 1 : idx + 1]
        if not all(_is_finite(value) for value in window):
            continue
        result[idx] = _mean(window)
    return result


def sma(values: list, period: int) -> list:
    """Simple moving average; first value at index ``period - 1``."""
    period = _validate_period(period)
    if not values:
        return []
    return _rolling_mean(values, period)


def _ema_kernel(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """Segment EMA (SDK ``trend._ema_kernel``): SMA seed, then recursion."""
    result: List[Optional[float]] = [None] * len(values)
    alpha = 2.0 / (float(period) + 1.0)
    for start, end in _finite_segments(values):
        size = end - start
        if size < period:
            continue
        seed = _mean(values[start : start + period])
        seed_index = start + period - 1
        result[seed_index] = seed
        previous = seed
        for offset in range(period, size):
            price = values[start + offset]
            previous = (price - previous) * alpha + previous
            result[start + offset] = previous
    return result


def ema(values: list, period: int) -> list:
    """EMA seeded with the SMA of the first ``period`` values (SDK parity)."""
    period = _validate_period(period)
    if not values:
        return []
    return _ema_kernel(values, period)


def wma(values: list, period: int) -> list:
    """Linearly weighted moving average with weights ``1..period``."""
    period = _validate_period(period)
    n = len(values)
    if n == 0:
        return []
    result: List[Optional[float]] = [None] * n
    denominator = period * (period + 1) / 2.0
    for idx in range(period - 1, n):
        window = values[idx - period + 1 : idx + 1]
        if not all(_is_finite(value) for value in window):
            continue
        weighted = 0.0
        for offset, value in enumerate(window):
            weighted += (offset + 1) * value
        result[idx] = weighted / denominator
    return result


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def _rsi_from_avgs(gain: float, loss: float) -> float:
    """SDK convention: zero average loss -> 100 with any gain, else 50."""
    if loss == 0.0:
        return 100.0 if gain > 0.0 else 50.0
    rs = gain / loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(values: list, period: int = 14) -> list:
    """Wilder's RSI; first value at index ``period`` (SDK ``_rsi_kernel``)."""
    period = _validate_period(period)
    n = len(values)
    if n == 0:
        return []
    result: List[Optional[float]] = [None] * n
    for start, end in _finite_segments(values):
        size = end - start
        if size <= period:
            continue
        gains: List[float] = []
        losses: List[float] = []
        for idx in range(start + 1, end):
            delta = values[idx] - values[idx - 1]
            gains.append(delta if delta > 0 else 0.0)
            losses.append(-delta if delta < 0 else 0.0)

        avg_gain = _mean(gains[:period])
        avg_loss = _mean(losses[:period])
        result[start + period] = _rsi_from_avgs(avg_gain, avg_loss)

        for offset in range(period, len(gains)):
            avg_gain = ((avg_gain * (period - 1)) + gains[offset]) / float(period)
            avg_loss = ((avg_loss * (period - 1)) + losses[offset]) / float(period)
            result[start + offset + 1] = _rsi_from_avgs(avg_gain, avg_loss)
    return result


def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line and histogram (SDK ``momentum.macd``).

    The signal line is an EMA applied to the macd line itself; the macd
    line's warmup ``None``s segment that EMA exactly like the SDK's NaNs do.
    """
    fast = _validate_period(fast, "fast")
    slow = _validate_period(slow, "slow")
    signal = _validate_period(signal, "signal")
    if not values:
        return {"macd": [], "signal": [], "hist": []}

    fast_line = _ema_kernel(values, fast)
    slow_line = _ema_kernel(values, slow)
    macd_line = [
        None if (fast_value is None or slow_value is None) else fast_value - slow_value
        for fast_value, slow_value in zip(fast_line, slow_line)
    ]
    signal_line = _ema_kernel(macd_line, signal)
    hist = [
        None if (macd_value is None or signal_value is None) else macd_value - signal_value
        for macd_value, signal_value in zip(macd_line, signal_line)
    ]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


def _true_range(
    highs: Sequence[Optional[float]],
    lows: Sequence[Optional[float]],
    closes: Sequence[Optional[float]],
) -> List[Optional[float]]:
    """SDK ``trend._true_range_kernel`` (first bar: ``high - low``)."""
    n = len(highs)
    result: List[Optional[float]] = [None] * n
    if n == 0:
        return result
    if _is_finite(highs[0]) and _is_finite(lows[0]):
        result[0] = highs[0] - lows[0]
    for idx in range(1, n):
        high = highs[idx]
        low = lows[idx]
        previous_close = closes[idx - 1]
        if not (_is_finite(high) and _is_finite(low) and _is_finite(previous_close)):
            continue
        result[idx] = max(high - low, abs(high - previous_close), abs(previous_close - low))
    return result


def _atr_kernel(true_range: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """Wilder smoothing seeded with the SMA of the first ``period`` TRs."""
    result: List[Optional[float]] = [None] * len(true_range)
    for start, end in _finite_segments(true_range):
        size = end - start
        if size < period:
            continue
        seed = _mean(true_range[start : start + period])
        result[start + period - 1] = seed
        previous = seed
        for offset in range(period, size):
            current = true_range[start + offset]
            previous = ((previous * (period - 1)) + current) / float(period)
            result[start + offset] = previous
    return result


def atr(bars: list, period: int = 14) -> list:
    """Average true range over OHLC bar dicts (Wilder smoothing)."""
    period = _validate_period(period)
    if not bars:
        return []
    highs = [_bar_float(bar, "high") for bar in bars]
    lows = [_bar_float(bar, "low") for bar in bars]
    closes = [_bar_float(bar, "close") for bar in bars]
    return _atr_kernel(_true_range(highs, lows, closes), period)


def bollinger(values: list, period: int = 20, num_std: float = 2.0) -> dict:
    """Bollinger bands with *population* standard deviation (``ddof=0``)."""
    period = _validate_period(period)
    num_std = _validate_positive(num_std, "num_std")
    n = len(values)
    mid = _rolling_mean(values, period)
    upper: List[Optional[float]] = [None] * n
    lower: List[Optional[float]] = [None] * n
    for idx in range(period - 1, n):
        window = values[idx - period + 1 : idx + 1]
        center = mid[idx]
        if center is None or not all(_is_finite(value) for value in window):
            continue
        variance = sum((value - center) ** 2 for value in window) / float(period)
        stddev = math.sqrt(variance)
        upper[idx] = center + (num_std * stddev)
        lower[idx] = center - (num_std * stddev)
    return {"mid": mid, "upper": upper, "lower": lower}


def supertrend(bars: list, period: int = 10, multiplier: float = 3.0) -> dict:
    """Supertrend (SDK ``trend._supertrend_frame`` recursion).

    Returns ``{"value": [...], "direction": [...]}`` where direction is
    ``1`` (bullish), ``-1`` (bearish) or ``None`` (unknown / warmup).
    """
    period = _validate_period(period)
    multiplier = _validate_positive(multiplier, "multiplier")
    n = len(bars)
    if n == 0:
        return {"value": [], "direction": []}

    highs = [_bar_float(bar, "high") for bar in bars]
    lows = [_bar_float(bar, "low") for bar in bars]
    closes = [_bar_float(bar, "close") for bar in bars]

    hl2 = [
        (high + low) / 2.0 if (high is not None and low is not None) else None
        for high, low in zip(highs, lows)
    ]
    atr_values = _atr_kernel(_true_range(highs, lows, closes), period)
    upper_band = [
        None if (hl2_value is None or atr_value is None) else hl2_value + (multiplier * atr_value)
        for hl2_value, atr_value in zip(hl2, atr_values)
    ]
    lower_band = [
        None if (hl2_value is None or atr_value is None) else hl2_value - (multiplier * atr_value)
        for hl2_value, atr_value in zip(hl2, atr_values)
    ]

    direction: List[Optional[int]] = [None] * n
    trend: List[Optional[float]] = [None] * n

    first_ready = None
    for idx, atr_value in enumerate(atr_values):
        if atr_value is not None:
            first_ready = idx
            break
    if first_ready is None:
        return {"value": trend, "direction": direction}

    direction[first_ready] = 1
    trend[first_ready] = lower_band[first_ready]

    final_upper = list(upper_band)
    final_lower = list(lower_band)

    for idx in range(first_ready + 1, n):
        previous_upper = final_upper[idx - 1]
        previous_lower = final_lower[idx - 1]
        if previous_upper is None or previous_lower is None:
            continue

        close = closes[idx]
        if not _is_finite(close):
            # Carry the prior state through the missing bar (SDK NaN branch).
            carried = direction[idx - 1]
            direction[idx] = carried
            final_upper[idx] = previous_upper
            final_lower[idx] = previous_lower
            if carried is not None and carried > 0:
                trend[idx] = final_lower[idx]
            elif carried is not None and carried < 0:
                trend[idx] = final_upper[idx]
            continue

        if close > previous_upper:
            direction[idx] = 1
        elif close < previous_lower:
            direction[idx] = -1
        else:
            direction[idx] = direction[idx - 1]
            current = direction[idx]
            if current is not None and current > 0:
                if final_lower[idx] is not None and final_lower[idx] < previous_lower:
                    final_lower[idx] = previous_lower
            if current is not None and current < 0:
                if final_upper[idx] is not None and final_upper[idx] > previous_upper:
                    final_upper[idx] = previous_upper

        # The SDK's final block treats a NaN direction as "not > 0" and falls
        # through to the upper band; mirror that with the explicit None guard.
        if direction[idx] is not None and direction[idx] > 0:
            trend[idx] = final_lower[idx]
        else:
            trend[idx] = final_upper[idx]

    return {"value": trend, "direction": direction}


# ---------------------------------------------------------------------------
# volume features
# ---------------------------------------------------------------------------


def vwap_session(bars: list, session_ids: list) -> list:
    """Session-anchored VWAP of the typical price ``(h + l + c) / 3``.

    The cumulative numerator/denominator reset at every session change and
    restart after non-finite bars (SDK ``volume._vwap_kernel`` semantics).
    """
    if len(bars) != len(session_ids):
        raise ValueError("bars and session_ids must have the same length")
    n = len(bars)
    if n == 0:
        return []
    result: List[Optional[float]] = [None] * n
    numerator = 0.0
    denominator = 0.0
    for idx in range(n):
        if idx == 0 or session_ids[idx] != session_ids[idx - 1]:
            numerator = 0.0
            denominator = 0.0
        high = _bar_float(bars[idx], "high")
        low = _bar_float(bars[idx], "low")
        close = _bar_float(bars[idx], "close")
        volume = _bar_float(bars[idx], "volume")
        if high is None or low is None or close is None or volume is None:
            # Non-finite bar ends the finite run: the next valid bar starts a
            # fresh cumulative window (SDK mask segmentation).
            numerator = 0.0
            denominator = 0.0
            continue
        typical = (high + low + close) / 3.0
        numerator += typical * volume
        denominator += volume
        if denominator != 0.0:
            result[idx] = numerator / denominator
    return result


def volume_sma(volumes: list, period: int) -> list:
    """Simple moving average of volumes."""
    return sma(volumes, period)


def volume_ratio(volumes: list, period: int, offset: int = 0) -> list:
    """``volumes[i]`` relative to the SMA of ``period`` volumes ending at
    ``i - offset`` (``offset=1`` compares against the previous bar's SMA).

    ``None`` when the anchored SMA is unknown, ``<= 0``, or the current
    volume is non-finite.
    """
    period = _validate_period(period)
    try:
        shift = int(offset)
    except (TypeError, ValueError):
        raise ValueError("offset must be a non-negative integer") from None
    if shift < 0:
        raise ValueError("offset must be a non-negative integer")
    n = len(volumes)
    if n == 0:
        return []
    base = _rolling_mean(volumes, period)
    result: List[Optional[float]] = [None] * n
    for idx in range(n):
        anchor = idx - shift
        if anchor < 0:
            continue
        denominator = base[anchor]
        if denominator is None or denominator <= 0:
            continue
        value = volumes[idx]
        if not _is_finite(value):
            continue
        result[idx] = value / denominator
    return result
