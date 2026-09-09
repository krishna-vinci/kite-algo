"""Canonical predicate cases — plan Task 2, spec v2 §4 F3, §5, §6 E-11.

Pure unit tests: no DB, no redis, no network, no backend imports beyond
``backend.alerts``.
"""

from datetime import datetime, timezone

from backend.alerts.predicates import (
    Observation,
    evaluate_condition,
    evaluate_stage,
)
from backend.alerts.types import Condition, Operand, Stage

UTC = timezone.utc
T0 = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def obs(ltp=None, epoch="e1", ts=T0, **fields):
    return Observation(ts=ts, epoch_id=epoch, ltp=ltp, **fields)


def cond(op, level=None, left="ltp", **params):
    return Condition(
        left=Operand(kind="field", name=left),
        op=op,
        right=Operand(kind="value", value=level, params=params),
    )


def stage(*conditions):
    return Stage(
        id="px",
        type="signal",
        clock="ltp",
        timeframe=None,
        conditions=tuple(conditions),
    )


# --- crosses_above / crosses_below ---------------------------------------


def test_crossing_sequence_fires_exactly_once():
    c = cond("crosses_above", 100.0)
    r1 = evaluate_condition(c, obs(99.0), {})
    assert r1.matched is False
    assert r1.fired is False
    r2 = evaluate_condition(c, obs(101.0), r1.state)
    assert r2.matched is True
    assert r2.fired is True
    r3 = evaluate_condition(c, obs(102.0), r2.state)
    assert r3.matched is True
    assert r3.fired is False
    assert [r1.fired, r2.fired, r3.fired].count(True) == 1


def test_starting_above_level_never_fires():
    c = cond("crosses_above", 100.0)
    r1 = evaluate_condition(c, obs(101.0), {})
    assert r1.fired is False
    r2 = evaluate_condition(c, obs(102.0), r1.state)
    assert r2.fired is False


def test_recross_fires_twice():
    c = cond("crosses_above", 100.0)
    state = {}
    fires = []
    for ltp in (99.0, 101.0, 99.0, 101.0):
        r = evaluate_condition(c, obs(ltp), state)
        state = r.state
        fires.append(r.fired)
    assert fires == [False, True, False, True]


def test_crossing_at_exact_equality_fires():
    # E-11: crosses_above fires when prev < level and cur == level (cur >= level).
    up = cond("crosses_above", 100.0)
    r1 = evaluate_condition(up, obs(99.0), {})
    r2 = evaluate_condition(up, obs(100.0), r1.state)
    assert r2.fired is True
    # Mirrored: crosses_below fires when prev > level and cur == level.
    down = cond("crosses_below", 100.0)
    r3 = evaluate_condition(down, obs(101.0), {})
    r4 = evaluate_condition(down, obs(100.0), r3.state)
    assert r4.fired is True


def test_level_ops_equality_is_fixed_per_operator():
    # E-11: gt/gte/lt/lte equality behaviour explicit and tested.
    assert evaluate_condition(cond("gt", 100.0), obs(100.0), {}).matched is False
    assert evaluate_condition(cond("gte", 100.0), obs(100.0), {}).matched is True
    assert evaluate_condition(cond("lt", 100.0), obs(100.0), {}).matched is False
    assert evaluate_condition(cond("lte", 100.0), obs(100.0), {}).matched is True


def test_level_ops_match_but_never_fire():
    for op in ("gt", "gte", "lt", "lte"):
        r = evaluate_condition(cond(op, 100.0), obs(105.0 if "g" in op else 95.0), {})
        assert r.matched is True
        assert r.fired is False


def test_crosses_below_mirrors_above():
    c = cond("crosses_below", 100.0)
    r1 = evaluate_condition(c, obs(101.0), {})
    assert r1.fired is False
    r2 = evaluate_condition(c, obs(99.0), r1.state)
    assert r2.fired is True
    r3 = evaluate_condition(c, obs(98.0), r2.state)
    assert r3.fired is False


def test_prev_tracks_last_value_in_state():
    c = cond("crosses_above", 100.0)
    r = evaluate_condition(c, obs(101.5), {})
    assert r.state["prev"] == 101.5


# --- unknown operand handling ---------------------------------------------


def test_unknown_operand_is_unknown_and_leaves_state_untouched():
    c = cond("crosses_above", 100.0)
    state = {"prev": 99.0, "epoch_id": "e1"}
    r = evaluate_condition(c, obs(None), state)
    assert r.matched is None
    assert r.fired is False
    assert r.state == state
    assert r.state is not state
    # input dict not mutated
    assert state == {"prev": 99.0, "epoch_id": "e1"}


def test_unknown_level_operand_is_unknown():
    c = cond("crosses_above", None)
    r = evaluate_condition(c, obs(101.0), {"prev": 99.0, "epoch_id": "e1"})
    assert r.matched is None
    assert r.fired is False
    assert r.state == {"prev": 99.0, "epoch_id": "e1"}


def test_unknown_level_op_leaves_state_untouched():
    state = {"epoch_id": "e1"}
    r = evaluate_condition(cond("gt", 100.0), obs(None), state)
    assert r.matched is None
    assert r.fired is False
    assert r.state == {"epoch_id": "e1"}


# --- epochs --------------------------------------------------------------


def test_first_observation_initializes_without_firing():
    c = cond("crosses_above", 100.0)
    r = evaluate_condition(c, obs(101.0), {})
    assert r.fired is False
    assert r.state["prev"] == 101.0


def test_epoch_change_reinitializes_without_firing():
    c = cond("crosses_above", 100.0)
    r1 = evaluate_condition(c, obs(99.0), {})
    r2 = evaluate_condition(c, obs(101.0), r1.state)
    assert r2.fired is True
    # New epoch: prev is re-initialized from the first observation, never fires.
    r3 = evaluate_condition(c, obs(99.5, epoch="e2"), r2.state)
    assert r3.fired is False
    assert r3.state["prev"] == 99.5
    assert r3.state["epoch_id"] == "e2"
    r4 = evaluate_condition(c, obs(100.5, epoch="e2"), r3.state)
    assert r4.fired is True


# --- breaks_prev_high / breaks_prev_low ----------------------------------


def test_breaks_prev_high_uses_context_level():
    c = cond("breaks_prev_high", 3100.0)
    state = {}
    outcomes = []
    for ltp in (3050.0, 3150.0, 3200.0, 3080.0, 3150.0):
        r = evaluate_condition(c, obs(ltp), state)
        state = r.state
        outcomes.append((r.matched, r.fired))
    assert outcomes == [
        (False, False),  # below level
        (True, True),    # first strict cross above
        (True, False),   # still above: broken guard prevents refire
        (False, False),  # back below: resets
        (True, True),    # breaks again
    ]


def test_breaks_prev_low_mirrors_high():
    c = cond("breaks_prev_low", 2900.0)
    state = {}
    fires = []
    for ltp in (2950.0, 2850.0, 2800.0, 2950.0, 2850.0):
        r = evaluate_condition(c, obs(ltp), state)
        state = r.state
        fires.append(r.fired)
    assert fires == [False, True, False, False, True]


def test_breaks_prev_high_first_observation_above_does_not_fire():
    # A crossing requires two observations; activation-while-true is engine-level (E-9).
    c = cond("breaks_prev_high", 3100.0)
    r = evaluate_condition(c, obs(3200.0), {})
    assert r.matched is True
    assert r.fired is False
    assert r.state["prev_day_high_broken"] is True


# --- rises_pct / falls_pct ------------------------------------------------


def test_rises_pct_uses_stored_baseline_and_never_rederives():
    c = cond("rises_pct", 2.0)
    r1 = evaluate_condition(c, obs(100.0), {})
    assert r1.fired is False
    assert r1.state["baseline"] == 100.0
    baseline_ts = r1.state["baseline_ts"]
    assert isinstance(baseline_ts, str)
    r2 = evaluate_condition(c, obs(101.0), r1.state)
    assert r2.matched is False and r2.fired is False
    assert r2.state["baseline"] == 100.0  # not re-derived
    assert r2.state["baseline_ts"] == baseline_ts
    r3 = evaluate_condition(c, obs(102.5), r2.state)
    assert r3.matched is True and r3.fired is True  # +2.5% vs baseline
    r4 = evaluate_condition(c, obs(103.0), r3.state)
    assert r4.matched is True and r4.fired is False
    assert r4.state["baseline"] == 100.0  # baseline still the first capture


def test_falls_pct_fires_on_threshold_cross():
    c = cond("falls_pct", 2.0)
    r1 = evaluate_condition(c, obs(100.0), {})
    r2 = evaluate_condition(c, obs(99.0), r1.state)
    assert r2.matched is False and r2.fired is False
    r3 = evaluate_condition(c, obs(97.0), r2.state)
    assert r3.matched is True and r3.fired is True  # -3% vs baseline


# --- within ---------------------------------------------------------------


def test_within_fires_on_transition_outside_to_inside():
    c = cond("within", 90.0, hi=110.0)
    r1 = evaluate_condition(c, obs(120.0), {})
    assert r1.matched is False and r1.fired is False
    r2 = evaluate_condition(c, obs(100.0), r1.state)
    assert r2.matched is True and r2.fired is True
    r3 = evaluate_condition(c, obs(105.0), r2.state)
    assert r3.matched is True and r3.fired is False


def test_within_bounds_are_inclusive():
    c = cond("within", 90.0, hi=110.0)
    r1 = evaluate_condition(c, obs(120.0), {})
    lo_edge = evaluate_condition(c, obs(90.0), r1.state)
    assert lo_edge.matched is True and lo_edge.fired is True
    hi_edge = evaluate_condition(c, obs(110.0), lo_edge.state)
    assert hi_edge.matched is True and hi_edge.fired is False


def test_within_first_observation_inside_does_not_fire():
    c = cond("within", 90.0, hi=110.0)
    r = evaluate_condition(c, obs(100.0), {})
    assert r.matched is True and r.fired is False


# --- stage combination ----------------------------------------------------


def test_stage_ands_conditions_all_must_match():
    s = stage(cond("crosses_above", 100.0), cond("gt", 1000.0, left="volume"))
    r1 = evaluate_stage(s, obs(99.0, volume=2000.0), {})
    assert r1.fired is False and r1.matched is False
    r2 = evaluate_stage(s, obs(101.0, volume=500.0), r1.state)
    assert r2.fired is False and r2.matched is False  # cross fired but volume false
    r3 = evaluate_stage(s, obs(99.0, volume=2000.0), r2.state)
    assert r3.fired is False and r3.matched is False
    r4 = evaluate_stage(s, obs(101.0, volume=2000.0), r3.state)
    assert r4.fired is True and r4.matched is True


def test_stage_unknown_condition_propagates_and_blocks_fire():
    s = stage(cond("crosses_above", 100.0), cond("gt", 1000.0, left="volume"))
    # volume missing -> unknown; even a fresh crossing must not fire.
    r = evaluate_stage(s, obs(101.0), {})
    assert r.matched is None
    assert r.fired is False


def test_stage_merges_evidence():
    s = stage(cond("crosses_above", 100.0))
    r = evaluate_stage(s, obs(101.0), {"prev": 99.0, "epoch_id": "e1"})
    assert r.evidence["ltp"] == 101.0
    assert r.evidence["level"] == 100.0
    assert r.evidence["prev_ltp"] == 99.0


# --- purity ----------------------------------------------------------------


def test_evaluate_condition_never_mutates_input_state():
    c = cond("crosses_above", 100.0)
    state = {"prev": 99.0}
    r = evaluate_condition(c, obs(101.0), state)
    assert r.state["prev"] == 101.0
    assert state == {"prev": 99.0}


# --- public surface --------------------------------------------------------


def test_predicates_public_surface():
    import backend.alerts.predicates as predicates

    assert set(predicates.__all__) == {
        "Observation",
        "PredicateResult",
        "evaluate_condition",
        "evaluate_stage",
    }
