"""Phase 4 F10 advanced-condition semantics (deterministic clocks).

Covers the semantics that tests could otherwise encode by accident:
- N consecutive completed bars, counted once each, with unknown resetting;
- bounded A-then-B sequences: same-bar exclusion, independent time/bar
  bounds, expiry, rearm, and unknown consuming the bound;
- explicit hysteresis holding across a boundary oscillation;
- restart/replay behaviour of the durable advanced state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.alerts.predicates import Observation, evaluate_stage
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _obs(ts, close, epoch="candle", final=True):
    return Observation(
        ts=ts, epoch_id=epoch, ltp=close, open=close, high=close, low=close,
        close=close, volume=1000.0, final=final,
    )


def _stage(doc_stage):
    doc = {
        "version": 1, "name": "p4", "session": "nse_equity",
        "instruments": ["NSE:A"], "stages": [doc_stage], "alerts": [],
    }
    return parse_workflow_dict(doc).stages[0]


def _run(stage, values, start=T0, step=DAY, epoch="candle"):
    """Feed one value per bar; returns [(matched, fired), ...]."""
    state = {}
    out = []
    for index, value in enumerate(values):
        result = evaluate_stage(stage, _obs(start + index * step, value, epoch), state)
        state = result.state
        out.append((result.matched, result.fired))
    return out


def _fired(stage, values, **kwargs):
    return [fired for _matched, fired in _run(stage, values, **kwargs)]


# ---------------------------------------------------------------------------
# consecutive bars
# ---------------------------------------------------------------------------


def _consecutive(n):
    return _stage({
        "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "day",
        "conditions": {"all": [{"field": "close", "op": "gt", "value": 100}]},
        "consecutive_bars": n,
    })


def test_consecutive_bars_counts_each_eligible_bar_once():
    stage = _consecutive(3)
    assert _fired(stage, [101, 102, 103, 104], step=DAY) == [False, False, True, False]
    # exactly N bars: fires on the Nth, not before
    assert _fired(stage, [101, 102], step=DAY) == [False, False]
    assert _fired(stage, [101, 102, 103], step=DAY) == [False, False, True]


def test_consecutive_bars_restart_after_a_break():
    stage = _consecutive(3)
    # A false bar resets, so the streak restarts and fires only on the second run
    assert _fired(stage, [101, 99, 101, 102, 103], step=DAY) == [
        False, False, False, False, True,
    ]


def test_consecutive_bars_realert_after_a_new_streak():
    stage = _consecutive(3)
    # a break resets the streak, and a second genuine 3-bar run fires again
    assert _fired(stage, [101, 102, 103, 99, 101, 102, 103], step=DAY) == [
        False, False, True, False, False, False, True,
    ]


def test_unknown_bar_resets_the_streak():
    stage = _consecutive(3)
    """Unknown is not `true` (spec §5.3): a data gap must never extend a streak."""
    # bar1 is unknown -> streak resets, so the 3-bar run only reaches 2
    assert _fired(stage, [101, None, 103, 104], step=DAY) == [False, False, False, False]
    # one more eligible bar completes a genuine 3-bar run after the gap
    assert _fired(stage, [101, None, 103, 104, 105], step=DAY) == [
        False, False, False, False, True,
    ]


def test_consecutive_bars_state_survives_restart():
    """The streak lives in the checkpoint state, so a worker restart resumes it."""
    stage = _consecutive(3)
    state = {}
    for index, value in enumerate([101, 102]):
        state = evaluate_stage(stage, _obs(T0 + index * DAY, value), state).state
    # Simulate a restart: the persisted state is replayed, not rebuilt.
    result = evaluate_stage(stage, _obs(T0 + 2 * DAY, 103), state)
    assert (result.matched, result.fired) == (True, True)


# ---------------------------------------------------------------------------
# sequences
# ---------------------------------------------------------------------------


def _sequence(**bounds):
    spec = {
        "first": {"all": [{"field": "close", "op": "gt", "value": 100}]},
        "then": {"all": [{"field": "close", "op": "lt", "value": 99}]},
    }
    spec.update(bounds)
    return _stage({
        "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "day",
        "sequence": spec,
    })


def test_sequence_cannot_satisfy_both_legs_on_one_bar():
    stage = _sequence(within_bars=5)
    """A arms; B may only complete on a bar STRICTLY after it (D5)."""
    # 101 arms on bar 0; 98 on bar 1 completes (1 bar elapsed)
    assert _fired(stage, [101, 98], step=DAY) == [False, True]


def test_sequence_waits_for_the_pullback_rather_than_requiring_it_next_bar():
    stage = _sequence(within_bars=5)
    assert _fired(stage, [101, 100, 98], step=DAY) == [False, False, True]
    assert _fired(stage, [101, 100, 100.5, 98], step=DAY) == [False, False, False, True]


def test_sequence_bar_bound_expires_and_does_not_fire():
    stage = _sequence(within_bars=3)
    # within_bars: 3 -> B may arrive on bars 1..3 after arming, not bar 4
    assert _fired(stage, [101, 100, 100, 100, 98], step=DAY) == [
        False, False, False, False, False,
    ]
    # the same shape inside the bound does fire
    assert _fired(stage, [101, 100, 100, 98], step=DAY) == [False, False, False, True]


def test_sequence_time_bound_is_elapsed_time_not_bar_count():
    stage = _sequence(within="2h")
    """within: 2h on daily bars can never complete, however few bars elapse."""
    assert _fired(stage, [101, 98], step=DAY) == [False, False]
    # 30 minutes apart is inside the bound
    assert _fired(stage, [101, 98], step=timedelta(minutes=30)) == [False, True]


def test_sequence_enforces_both_bounds_when_both_are_supplied():
    stage = _sequence(within_bars=5, within="2h")
    sequence = _sequence(within_bars=5, within="2h")
    # bar bound satisfied but the time bound is not (daily bars)
    assert _fired(stage, [101, 98], step=DAY) == [False, False]
    # both satisfied
    assert _fired(stage, [101, 98], step=timedelta(minutes=30)) == [False, True]


def test_sequence_rearms_after_completion():
    stage = _sequence(within_bars=5)
    assert _fired(stage, [101, 98, 101, 98], step=timedelta(minutes=30)) == [
        False, True, False, True,
    ]


def test_sequence_unknown_bar_consumes_the_bound_without_invalidating():
    stage = _sequence(within_bars=3)
    """Unknown neither completes nor cancels: it consumes a bar of the window."""
    # bar1 unknown consumes one of the three allowed bars; bar2 completes
    assert _fired(stage, [101, None, 98], step=timedelta(minutes=30)) == [False, False, True]
    # four unknown bars exhaust a within_bars: 3 window even though B arrives
    assert _fired(stage,
        [101, None, None, None, None, 98], step=timedelta(minutes=30)
    ) == [False, False, False, False, False, False]


def test_sequence_progress_survives_restart():
    stage = _sequence(within_bars=5)
    state = evaluate_stage(stage, _obs(T0, 101), {}).state
    result = evaluate_stage(stage, _obs(T0 + timedelta(minutes=30), 98), state)
    assert result.fired is True


def test_sequence_state_is_cleared_on_a_new_epoch():
    """A new observation epoch (restart/gap for ltp, or a fresh epoch) resets."""
    stage = _sequence(within_bars=5)
    state = evaluate_stage(stage, _obs(T0, 101, epoch="candle"), {}).state
    result = evaluate_stage(
        stage, _obs(T0 + timedelta(minutes=30), 98, epoch="candle-2"), state
    )
    # The armed sequence is gone: this bar can only arm a new one, not complete.
    assert result.fired is False


# ---------------------------------------------------------------------------
# hysteresis
# ---------------------------------------------------------------------------


def _hysteresis(op="gt", level=100, release=99):
    return _stage({
        "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "day",
        "conditions": {"all": [
            {"left": {"field": "close"}, "op": op, "right": {"value": level},
             "hysteresis": {"release": release}},
        ]},
    })


def test_hysteresis_holds_through_a_boundary_oscillation():
    stage = _hysteresis()
    stage = _hysteresis()
    matches = [m for m, _ in _run(stage, [101, 99.5, 99.2, 100.5, 101])]
    # stays matched while above the release level, releases below it
    assert matches == [True, True, True, True, True]
    matches = [m for m, _ in _run(stage, [101, 99.5, 98.9, 99.5, 101])]
    assert matches == [True, True, False, False, True]


def test_hysteresis_survives_restart():
    stage = _hysteresis()
    """The held state is durable, so a restart cannot re-fire on oscillation."""
    stage = _hysteresis()
    state = evaluate_stage(stage, _obs(T0, 101), {}).state
    # restart: replay the persisted state at a value inside the band
    result = evaluate_stage(stage, _obs(T0 + DAY, 99.5), state)
    assert result.matched is True


def test_hysteresis_downward_direction():
    stage = _hysteresis(op="lt", level=100, release=101)
    stage = _hysteresis(op="lt", level=100, release=101)
    matches = [m for m, _ in _run(stage, [99, 100.5, 100.8, 99.5, 99])]
    assert matches == [True, True, True, True, True]
    matches = [m for m, _ in _run(stage, [99, 100.5, 101.2, 100.5, 99])]
    assert matches == [True, True, False, False, True]


# ---------------------------------------------------------------------------
# compilation of the advanced forms
# ---------------------------------------------------------------------------


def test_advanced_stages_compile_and_hash_stably():
    doc = {
        "version": 1, "name": "p4-hash", "session": "nse_equity",
        "instruments": ["NSE:A"],
        "stages": [{
            "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "day",
            "conditions": {"all": [{"field": "close", "op": "gt", "value": 100}]},
            "consecutive_bars": 3,
        }],
        "alerts": [],
    }
    first = compile_document(parse_workflow_dict(doc))
    again = compile_document(parse_workflow_dict(first.document.to_document_dict()))
    assert first.canonical_hash == again.canonical_hash
