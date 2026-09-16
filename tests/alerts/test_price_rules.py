"""Canonical predicate cases — plan Task 2, spec v2 §4 F3, §5, §6 E-11.

Pure unit tests: no DB, no redis, no network, no backend imports beyond
``backend.alerts``.

State shape (pinned): per-condition sub-dicts under ``state["conds"][key]``
where ``key = f"{op}:{left_key}:{right_key}"``; evidence is partitioned the
same way (``evidence[key]``).
"""

from datetime import datetime, timezone

from backend.alerts.predicates import (
    Observation,
    cond_key,
    evaluate_condition,
    evaluate_stage,
)
from backend.alerts.types import Condition, Operand, Stage

UTC = timezone.utc
T0 = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def obs(ltp=None, epoch="e1", ts=T0, **fields):
    return Observation(ts=ts, epoch_id=epoch, ltp=ltp, **fields)


def cond(op, level=None, left="ltp", right_name=None, **params):
    right = (
        Operand(kind="field", name=right_name)
        if right_name is not None
        else Operand(kind="value", value=level, params=params)
    )
    return Condition(left=Operand(kind="field", name=left), op=op, right=right)


def key(c):
    """Canonical condition key for the conditions built by ``cond``."""
    return cond_key(c)


def csub(result, c):
    """A condition's own sub-dict from a result state."""
    return result.state["conds"][key(c)]


def cev(result, c):
    """A condition's own evidence entry from a result."""
    return result.evidence[key(c)]


def stage(*conditions):
    return Stage(
        id="px",
        type="signal",
        clock="ltp",
        timeframe=None,
        conditions=tuple(conditions),
    )


# --- condition key format (pinned) ----------------------------------------


def test_condition_key_format():
    assert key(cond("crosses_above", 100.0)) == "crosses_above:field:ltp:value:100.0"
    assert key(cond("gt", 1000.0, left="volume")) == "gt:field:volume:value:1000.0"
    indicator = Condition(
        left=Operand(kind="field", name="ltp"),
        op="gt",
        right=Operand(kind="indicator", name="sma", params={"window": 5}),
    )
    assert cond_key(indicator) == 'gt:field:ltp:indicator:sma:{"window":5}'


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


def test_prev_tracks_last_value_in_own_condition_state():
    c = cond("crosses_above", 100.0)
    r = evaluate_condition(c, obs(101.5), {})
    assert csub(r, c)["prev"] == 101.5
    assert r.state["epoch_id"] == "e1"


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


# --- multi-condition state partitioning (fault 1) --------------------------


def test_two_crossing_conditions_fire_once_each_across_sequence():
    # Canonical audit case: with crossings at 100 and 110 and prices
    # 99 -> 105 -> 111, the 110 crossing MUST fire exactly once at 111.
    # Chained shared state used to clobber the 110 condition's prev.
    c100 = cond("crosses_above", 100.0)
    c110 = cond("crosses_above", 110.0)
    state = {}
    c100_fires, c110_fires = [], []
    for ltp in (99.0, 105.0, 111.0):
        o = obs(ltp)
        r1 = evaluate_condition(c100, o, state)
        state = r1.state  # condition 2 chains on condition 1's output state
        r2 = evaluate_condition(c110, o, state)
        state = r2.state
        c100_fires.append(r1.fired)
        c110_fires.append(r2.fired)
    # trace: 99 initializes both; 105 fires cond-100 only; 111 fires cond-110
    assert c100_fires == [False, True, False]
    assert c110_fires == [False, False, True]
    # each condition tracks its own prev, independently
    final = state["conds"]
    assert final[key(c100)]["prev"] == 111.0
    assert final[key(c110)]["prev"] == 111.0
    assert [c100_fires.count(True), c110_fires.count(True)] == [1, 1]


def test_stage_two_crossing_conditions_fire_exactly_once_at_111():
    c100 = cond("crosses_above", 100.0)
    c110 = cond("crosses_above", 110.0)
    s = stage(c100, c110)
    state = {}
    fired_at = []
    for ltp in (99.0, 105.0, 111.0):
        r = evaluate_stage(s, obs(ltp), state)
        state = r.state
        if r.fired:
            fired_at.append(ltp)
    assert fired_at == [111.0]  # both conditions fire exactly once across the run
    # evidence carries one entry per condition, never merged or clobbered
    assert set(state["conds"].keys()) == {key(c100), key(c110)}


def test_condition_writes_never_leak_into_another_conditions_state():
    c100 = cond("crosses_above", 100.0)
    c110 = cond("crosses_above", 110.0)
    state = {}
    r1 = evaluate_condition(c100, obs(105.0), state)  # inits c100.prev=105
    chained = r1.state
    r2 = evaluate_condition(c110, obs(105.0), chained)
    # c110's sub-dict must not have inherited c100's prev from this observation
    assert csub(r2, c110)["prev"] == 105.0  # its own initialization
    assert csub(r2, c100)["prev"] == 105.0
    # both conditions now coexist in the OUTPUT state, partitioned by key
    assert set(r2.state["conds"]) == {key(c100), key(c110)}
    assert state == {}  # input untouched


def test_stage_evidence_partitioned_per_condition():
    c100 = cond("crosses_above", 100.0)
    c110 = cond("crosses_above", 110.0)
    s = stage(c100, c110)
    state = {}
    for ltp in (99.0, 105.0, 111.0):
        r = evaluate_stage(s, obs(ltp), state)
        state = r.state
    # final evidence is keyed per condition with its own values
    assert cev(r, c100) == {"ltp": 111.0, "level": 100.0, "prev_ltp": 105.0}
    assert cev(r, c110) == {"ltp": 111.0, "level": 110.0, "prev_ltp": 105.0}


# --- unknown operand handling ---------------------------------------------


def test_unknown_operand_is_unknown_and_leaves_state_untouched():
    c = cond("crosses_above", 100.0)
    k = key(c)
    state = {"conds": {k: {"prev": 99.0}}, "epoch_id": "e1"}
    r = evaluate_condition(c, obs(None), state)
    assert r.matched is None
    assert r.fired is False
    assert r.state == state
    assert r.state is not state
    # input dict not mutated
    assert state == {"conds": {k: {"prev": 99.0}}, "epoch_id": "e1"}


def test_unknown_level_operand_is_unknown():
    c = cond("crosses_above", None)
    k = key(cond("crosses_above", None))
    state = {"conds": {k: {"prev": 99.0}}, "epoch_id": "e1"}
    r = evaluate_condition(c, obs(101.0), state)
    assert r.matched is None
    assert r.fired is False
    assert r.state == state


def test_unknown_level_op_leaves_state_untouched():
    c = cond("gt", 100.0)
    k = key(c)
    state = {"epoch_id": "e1", "conds": {key(cond("crosses_above", 100.0)): {"prev": 1.0}}}
    r = evaluate_condition(c, obs(None), state)
    assert r.matched is None
    assert r.fired is False
    assert r.state == state


# --- epochs --------------------------------------------------------------


def test_first_observation_initializes_without_firing():
    c = cond("crosses_above", 100.0)
    r = evaluate_condition(c, obs(101.0), {})
    assert r.fired is False
    assert csub(r, c)["prev"] == 101.0


def test_epoch_change_reinitializes_without_firing():
    c = cond("crosses_above", 100.0)
    r1 = evaluate_condition(c, obs(99.0), {})
    r2 = evaluate_condition(c, obs(101.0), r1.state)
    assert r2.fired is True
    # New epoch: prev is re-initialized from the first observation, never fires.
    r3 = evaluate_condition(c, obs(99.5, epoch="e2"), r2.state)
    assert r3.fired is False
    assert csub(r3, c)["prev"] == 99.5
    assert r3.state["epoch_id"] == "e2"
    r4 = evaluate_condition(c, obs(100.5, epoch="e2"), r3.state)
    assert r4.fired is True


def test_epoch_change_clears_every_conditions_epoch_keys():
    c100 = cond("crosses_above", 100.0)
    c110 = cond("crosses_above", 110.0)
    s = stage(c100, c110)
    state = {}
    for ltp in (99.0, 105.0):
        r = evaluate_stage(s, obs(ltp), state)
        state = r.state
    r = evaluate_stage(s, obs(111.0, epoch="e2"), state)
    # new epoch: both conditions re-initialize; nothing fires
    assert r.fired is False
    assert csub(r, c100).get("prev") == 111.0
    assert csub(r, c110).get("prev") == 111.0


# --- breaks_prev_high / breaks_prev_low (literal value operands) ----------


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
    assert csub(r, c)["prev_day_high_broken"] is True


# --- breaks_prev_high / breaks_prev_low via context (fault 2) ---------------


def test_breaks_prev_high_resolves_level_from_context():
    c = cond("breaks_prev_high", right_name="prev_day_high")
    context = {"prev_day_high": 3100.0}
    state = {}
    outcomes = []
    for ltp in (3050.0, 3150.0, 3200.0, 3080.0, 3150.0):
        r = evaluate_condition(c, obs(ltp), state, context)
        state = r.state
        outcomes.append((r.matched, r.fired))
    assert outcomes == [
        (False, False),  # below the previous day's high
        (True, True),    # fires exactly once on the break
        (True, False),   # guard holds while above
        (False, False),  # back below resets the guard
        (True, True),    # re-armed: breaks again
    ]
    assert state["conds"][key(c)]["prev_day_high_broken"] is True


def test_breaks_prev_low_resolves_level_from_context():
    c = cond("breaks_prev_low", right_name="prev_day_low")
    context = {"prev_day_low": 2900.0}
    state = {}
    fires = []
    for ltp in (2950.0, 2850.0, 2800.0, 2950.0, 2850.0):
        r = evaluate_condition(c, obs(ltp), state, context)
        state = r.state
        fires.append(r.fired)
    assert fires == [False, True, False, False, True]


def test_prev_day_break_unknown_when_context_missing():
    c = cond("breaks_prev_high", right_name="prev_day_high")
    k = key(c)
    state = {"conds": {k: {"prev_day_high_seen": True, "prev_day_high_broken": True}}}
    # no context at all
    r = evaluate_condition(c, obs(3150.0), state, None)
    assert r.matched is None
    assert r.fired is False
    assert r.state == state  # untouched
    # context present but the key missing
    r2 = evaluate_condition(c, obs(3150.0), state, {"prev_day_low": 1.0})
    assert r2.matched is None
    assert r2.fired is False
    assert r2.state == state


def test_stage_prev_day_break_via_context():
    s = stage(cond("breaks_prev_high", right_name="prev_day_high"))
    context = {"prev_day_high": 100.0}
    state = {}
    fired_at = []
    for ltp in (95.0, 105.0, 106.0, 99.0, 101.0):
        r = evaluate_stage(s, obs(ltp), state, context)
        state = r.state
        if r.fired:
            fired_at.append(ltp)
    assert fired_at == [105.0, 101.0]  # fires once per break, guard resets below


# --- rises_pct / falls_pct ------------------------------------------------


def test_rises_pct_uses_stored_baseline_and_never_rederives():
    c = cond("rises_pct", 2.0)
    r1 = evaluate_condition(c, obs(100.0), {})
    assert r1.fired is False
    assert csub(r1, c)["baseline"] == 100.0
    baseline_ts = csub(r1, c)["baseline_ts"]
    assert isinstance(baseline_ts, str)
    r2 = evaluate_condition(c, obs(101.0), r1.state)
    assert r2.matched is False and r2.fired is False
    assert csub(r2, c)["baseline"] == 100.0  # not re-derived
    assert csub(r2, c)["baseline_ts"] == baseline_ts
    r3 = evaluate_condition(c, obs(102.5), r2.state)
    assert r3.matched is True and r3.fired is True  # +2.5% vs baseline
    r4 = evaluate_condition(c, obs(103.0), r3.state)
    assert r4.matched is True and r4.fired is False
    assert csub(r4, c)["baseline"] == 100.0  # baseline still the first capture


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
    assert csub(r3, c)["within"] is True


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


def test_stage_merges_evidence_per_condition():
    c = cond("crosses_above", 100.0)
    s = stage(c)
    r = evaluate_stage(
        s,
        obs(101.0),
        {"conds": {key(c): {"prev": 99.0}}, "epoch_id": "e1"},
    )
    assert r.evidence[key(c)] == {"ltp": 101.0, "level": 100.0, "prev_ltp": 99.0}
    assert r.fired is True  # prev restored into the condition's own sub-dict


# --- purity ----------------------------------------------------------------


def test_evaluate_condition_never_mutates_input_state():
    c = cond("crosses_above", 100.0)
    k = key(c)
    state = {"conds": {k: {"prev": 99.0}}}
    r = evaluate_condition(c, obs(101.0), state)
    assert csub(r, c)["prev"] == 101.0
    assert state == {"conds": {k: {"prev": 99.0}}}


def test_stage_never_mutates_input_state():
    c1 = cond("crosses_above", 100.0)
    c2 = cond("crosses_above", 110.0)
    s = stage(c1, c2)
    state = {"conds": {key(c1): {"prev": 99.0}, key(c2): {"prev": 105.0}}}
    snapshot = {
        "conds": {key(c1): {"prev": 99.0}, key(c2): {"prev": 105.0}}
    }
    evaluate_stage(s, obs(111.0), state)
    assert state == snapshot


# --- public surface --------------------------------------------------------


def test_predicates_public_surface():
    import backend.alerts.predicates as predicates

    assert set(predicates.__all__) == {
        "Observation",
        "PredicateResult",
        "evaluate_condition",
        "evaluate_stage",
    }
