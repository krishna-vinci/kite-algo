"""Cross-instrument pair computation (Phase 4 F10).

The rules that stop an unavailable ratio from being reported as a valid one:
head alignment, lookback endpoint alignment, freshness, zero denominators, and
same-period returns when a leg has a gap.
"""

from __future__ import annotations

import pytest

from backend.alerts.predicates import pair_operand_id
from backend.workflows import registry
from backend.workflows.models import Operand
from backend.workflows.pairs import PairResult, collect_pair_operands, resolve_pair
from backend.workflows.parser import parse_workflow_dict

from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


class _Bar:
    def __init__(self, ts, close):
        self.ts = ts
        self.close = close


class _History:
    """Minimal candle history: ``{instrument: {ts: close}}``."""

    def __init__(self, series):
        self.series = series

    def recent_bars(self, instrument_key, timeframe, limit):
        bars = sorted(self.series.get(instrument_key, {}).items())[-limit:]
        return [_Bar(ts, close) for ts, close in bars]


def _series(values, *, start=T0, step=STEP, skip=()):
    out = {}
    ts = start
    for index, value in enumerate(values):
        if index not in skip:
            out[start + index * step] = value
        ts += step
    return out


def _operand(kind="pair_ratio", **params):
    return Operand(kind="pair", name=kind, params=params)


def _grid(count, base=100.0):
    return [base + index for index in range(count)]


# ---------------------------------------------------------------------------
# pair_ratio
# ---------------------------------------------------------------------------


def test_pair_ratio_uses_the_same_completed_bar():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        "NSE:B": _series([50, 50, 50, 50, 100]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    assert result.value == pytest.approx(110 / 100)
    assert result.head_ts == head


def test_pair_ratio_reports_misaligned_heads():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        # B has no bar at the newest grid point: heads differ by one bar.
        "NSE:B": _series([50, 50, 50, 50, 60], skip=(4,)),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_misaligned"


def test_pair_ratio_tolerates_the_configured_skew():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        "NSE:B": _series([50, 50, 50, 60, 70], skip=(4,)),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B", max_skew_bars=1),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    assert result.value == pytest.approx(110 / 60)
    assert result.skew_bars == 1


def test_pair_ratio_zero_denominator_is_unknown():
    head = T0 + 2 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102]),
        "NSE:B": _series([50, 50, 0]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_zero_denominator"


def test_pair_ratio_missing_leg_is_unknown():
    history = _History({"NSE:A": _series([100, 101, 102])})
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=T0 + 2 * STEP,
    )
    assert result.reason == "pair_missing"


def test_pair_never_consumes_a_future_bar():
    history = _History({
        "NSE:A": _series([100, 101, 102, 103]),
        "NSE:B": _series([50, 50, 50, 50]),
    })
    # Cutoff at the third bar: the fourth must be invisible.
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=T0 + 2 * STEP,
    )
    assert result.value == pytest.approx(102 / 50)


def test_pair_stale_head_is_unknown():
    head = T0 + 40 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102]),
        "NSE:B": _series([50, 51, 52]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
        bar_age_limit_s=2 * registry.timeframe_seconds("15minute"),
    )
    assert result.value is None
    assert result.reason == "pair_stale"


# ---------------------------------------------------------------------------
# relative_strength
# ---------------------------------------------------------------------------


def test_relative_strength_compares_the_same_period():
    head = T0 + 3 * STEP
    history = _History({
        # A: +10% over two bars; B: +2% over the same two bars.
        "NSE:A": _series([100, 105, 108, 110]),
        "NSE:B": _series([200, 200, 202, 204]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    # head is index 3, anchor is head - 2 bars = index 1
    assert result.value == pytest.approx((110 / 105 - 1) * 100 - (204 / 200 - 1) * 100)
    assert result.anchor_ts == head - 2 * STEP


def test_relative_strength_rejects_mismatched_lookback_endpoints():
    """A leg missing a bar at the anchor is rejected, not silently shortened."""
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 108, 110, 112]),
        # B is missing the anchor bar (index 2) but present at the head.
        "NSE:B": _series([200, 200, 202, 204, 206], skip=(2,)),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_lookback_misaligned"


def test_interior_gaps_do_not_shift_either_window():
    """The formula reads only the endpoints, so an interior gap is harmless."""
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 108, 110, 112]),
        "NSE:B": _series([200, 200, 202, 204, 206], skip=(3,)),  # interior gap
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    # head is index 4; the anchor is head - 2 bars = index 2 on BOTH legs,
    # even though B has an interior gap at index 3.
    assert result.value == pytest.approx((112 / 108 - 1) * 100 - (206 / 202 - 1) * 100)


def test_relative_strength_insufficient_history_is_unknown():
    history = _History({
        "NSE:A": _series([100, 101]),
        "NSE:B": _series([50, 51]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5),
        history=history, timeframe="15minute", cutoff=T0 + STEP,
    )
    assert result.value is None
    assert result.reason in ("pair_lookback_misaligned", "pair_insufficient_history")


def test_relative_strength_zero_anchor_is_unknown():
    head = T0 + 2 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 110]),
        "NSE:B": _series([0, 200, 204]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_zero_denominator"


# ---------------------------------------------------------------------------
# identity and collection
# ---------------------------------------------------------------------------


def test_pair_identity_separates_legs_lookback_and_skew():
    base = _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5)
    other_leg = _operand("relative_strength", instrument="NSE:A", reference="NSE:C", lookback=5)
    other_lookback = _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=6)
    other_skew = _operand(
        "relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5, max_skew_bars=1
    )
    identities = {
        pair_operand_id(base),
        pair_operand_id(other_leg),
        pair_operand_id(other_lookback),
        pair_operand_id(other_skew),
    }
    assert len(identities) == 4
    assert pair_operand_id(base) == pair_operand_id(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5)
    )


def test_collect_pair_operands_finds_nested_references():
    doc = parse_workflow_dict({
        "version": 1, "name": "pairs", "session": "nse_equity",
        "instruments": ["NSE:A"],
        "stages": [{
            "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "15minute",
            "conditions": {"all": [
                {"left": {"pair_ratio": {"instrument": "NSE:A", "reference": "NSE:B"}},
                 "op": "gt", "right": {"value": 1.0}},
            ], "any": [
                {"left": {"relative_strength": {"instrument": "NSE:A", "reference": "NSE:B",
                                                "lookback": 5}},
                 "op": "gt", "right": {"value": 1.0}},
            ]},
        }],
        "alerts": [],
    })
    stage = doc.stages[0]
    operands = collect_pair_operands(
        list(stage.conditions) + list(stage.any_conditions) + list(stage.not_conditions)
    )
    assert len(operands) == 2
    assert {operand.name for operand in operands} == {"pair_ratio", "relative_strength"}
