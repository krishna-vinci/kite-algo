"""Cross-instrument pair computation for evaluation (Phase 4 F10).

Resolves ``pair_ratio`` and ``relative_strength`` operands into context values
at dispatch time. The rules that make a result trustworthy live here:

- **Head alignment** — by default both legs must resolve to the IDENTICAL bar
  timestamp for the stage timeframe (``max_skew_bars`` widens that, bounded).
- **Lookback endpoint alignment** — the anchor is derived from the head bar and
  the timeframe (``anchor = head - lookback x timeframe``), never from a leg's
  own availability, so both returns describe the SAME period. An independently
  chosen "Nth previous available bar" per leg would compare different windows
  whenever one leg has a gap.
- **Freshness** — a leg whose head is older than ``ALERTS_PAIR_MAX_BAR_AGE_S``
  makes the pair unknown: a halted instrument's last close must not be paired
  against a live one.
- **No silent shortening** — a missing bar at either endpoint is
  ``pair_lookback_misaligned``, never a shorter window.

Basis compatibility is structural: both legs read ``historical_candles`` on
the stage timeframe and that store carries no adjustment column, so there is
no split/dividend-adjusted series in the system to mix with an unadjusted one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.alerts.predicates import pair_operand_id
from backend.workflows import registry

__all__ = [
    "PairResult",
    "resolve_pair",
    "collect_pair_operands",
]


@dataclass(frozen=True)
class PairResult:
    value: Optional[float]
    reason: Optional[str] = None
    head_ts: Optional[datetime] = None
    anchor_ts: Optional[datetime] = None
    skew_bars: int = 0


def collect_pair_operands(conditions: Iterable[Any]) -> List[Any]:
    """Every pair operand referenced by a sequence of conditions."""
    found: List[Any] = []
    for condition in conditions or ():
        for operand in (condition.left, condition.right):
            if operand is not None and operand.kind == "pair":
                found.append(operand)
    return found


def _bars_between(a: datetime, b: datetime, step_s: int) -> Optional[int]:
    if step_s <= 0:
        return None
    delta = abs((a - b).total_seconds())
    if delta % step_s:
        return None  # not an exact multiple: different bar grid
    return int(delta // step_s)


def resolve_pair(
    operand: Any,
    *,
    history: Any,
    timeframe: str,
    cutoff: datetime,
    bar_age_limit_s: Optional[int] = None,
) -> PairResult:
    """Compute one pair operand as of ``cutoff``.

    ``history`` is any object exposing ``recent_bars(instrument_key, timeframe,
    limit)`` (``PgCandleHistory`` in production, a stub in tests).
    """
    params = dict(operand.params or {})
    kind = operand.name or ""
    spec = registry.PAIR_COMPUTATIONS.get(kind)
    if spec is None:
        return PairResult(None, "pair_missing")
    left_key = params.get("instrument")
    right_key = params.get("reference")
    if not left_key or not right_key:
        return PairResult(None, "pair_missing")

    step_s = registry.timeframe_seconds(timeframe)
    lookback = int(params.get("lookback") or 0)
    skew_limit = int(params.get("max_skew_bars") or 0)
    # Fetch generously: the endpoints plus any skew, never the whole history.
    fetch = max(2, lookback + 1 + skew_limit + 1)
    try:
        left_bars = list(history.recent_bars(left_key, timeframe, fetch))
        right_bars = list(history.recent_bars(right_key, timeframe, fetch))
    except Exception:
        return PairResult(None, "pair_missing")
    left = _index_by_ts(left_bars, cutoff)
    right = _index_by_ts(right_bars, cutoff)
    if not left or not right:
        return PairResult(None, "pair_missing")

    left_head_ts = max(left)
    right_head_ts = max(right)
    skew_bars = _bars_between(left_head_ts, right_head_ts, step_s)
    if skew_bars is None or skew_bars > skew_limit:
        return PairResult(None, "pair_misaligned")
    head_ts = max(left_head_ts, right_head_ts)

    now_age = (cutoff - head_ts).total_seconds()
    if bar_age_limit_s is not None and now_age > bar_age_limit_s:
        return PairResult(None, "pair_stale", head_ts=head_ts)

    if kind == "pair_ratio":
        denominator = right.get(right_head_ts)
        if denominator is None:
            return PairResult(None, "pair_missing", head_ts=head_ts)
        if abs(float(denominator)) < 1e-12:
            return PairResult(None, "pair_zero_denominator", head_ts=head_ts)
        numerator = left.get(left_head_ts)
        if numerator is None:
            return PairResult(None, "pair_missing", head_ts=head_ts)
        return PairResult(
            float(numerator) / float(denominator),
            None,
            head_ts=head_ts,
            skew_bars=skew_bars,
        )

    # relative_strength: the anchor comes from the HEAD BAR and the timeframe,
    # never from a leg's own availability, so both returns span the same period.
    if lookback <= 0 or step_s <= 0:
        return PairResult(None, "pair_insufficient_history", head_ts=head_ts)
    anchor_ts = head_ts - timedelta(seconds=lookback * step_s)
    left_head = left.get(left_head_ts)
    right_head = right.get(right_head_ts)
    left_anchor = left.get(anchor_ts)
    right_anchor = right.get(anchor_ts)
    if None in (left_head, right_head):
        return PairResult(None, "pair_missing", head_ts=head_ts, anchor_ts=anchor_ts)
    if None in (left_anchor, right_anchor):
        # Rejected, never silently shortened: a shorter window would describe a
        # different period and the difference would be meaningless.
        return PairResult(
            None, "pair_lookback_misaligned", head_ts=head_ts, anchor_ts=anchor_ts
        )
    if abs(float(left_anchor)) < 1e-12 or abs(float(right_anchor)) < 1e-12:
        return PairResult(
            None, "pair_zero_denominator", head_ts=head_ts, anchor_ts=anchor_ts
        )
    left_return = (float(left_head) / float(left_anchor) - 1.0) * 100.0
    right_return = (float(right_head) / float(right_anchor) - 1.0) * 100.0
    return PairResult(
        left_return - right_return,
        None,
        head_ts=head_ts,
        anchor_ts=anchor_ts,
        skew_bars=skew_bars,
    )


def _index_by_ts(bars: Sequence[Any], cutoff: datetime) -> Dict[datetime, float]:
    """Map bar timestamp -> close, keeping only bars at or before the cutoff."""
    out: Dict[datetime, float] = {}
    for bar in bars:
        ts = getattr(bar, "ts", None)
        close = getattr(bar, "close", None)
        if ts is None or close is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=cutoff.tzinfo)
        if ts > cutoff:
            continue  # never consume a future bar
        out[ts] = float(close)
    return out
