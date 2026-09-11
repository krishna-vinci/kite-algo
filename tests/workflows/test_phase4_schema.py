"""Phase 4 F10 authoring contract: parse, validate, hash, capabilities.

Every rejection here is a rule the assignment requires to fail with an
actionable issue rather than compile into a workflow that never evaluates:
unsupported breadth mode, dynamic hysteresis, cross-session pairs, ltp clocks
on bar-counted conditions, and the session-cap reset boundary.
"""

from __future__ import annotations

import hashlib

import pytest

from backend.workflows import registry
from backend.workflows.compiler import (
    WorkflowValidationError,
    canonical_json,
    compile_document,
)
from backend.workflows.parser import parse_workflow_dict

BASE = {
    "version": 1, "name": "p4-schema", "session": "nse_equity",
    "instruments": ["NSE:A", "NSE:B"], "stages": [], "alerts": [],
}
SIGNAL = {
    "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "day",
    "conditions": {"all": [{"field": "close", "op": "gt", "value": 100}]},
}


def _doc(**overrides):
    return {**BASE, **overrides}


def _stage(**overrides):
    return _doc(stages=[{**SIGNAL, **overrides}])


def _stage_without_conditions(**overrides):
    stage = {key: value for key, value in SIGNAL.items() if key != "conditions"}
    stage.update(overrides)
    return _doc(stages=[stage])


def _issues(doc):
    """Every rejection, whether the parser or the compiler raised it.

    Bounds that are structurally impossible (a non-positive count, an
    unqualified pair leg) fail at parse time with the offending path; the rest
    fail compilation with a named issue code. Tests assert on the path or the
    message, so both shapes are returned uniformly.
    """
    try:
        compile_document(parse_workflow_dict(doc))
    except WorkflowValidationError as exc:
        return exc.issues
    except Exception as exc:  # parse-level rejection
        message = str(exc)
        return [type("_ParseIssue", (), {"where": message, "code": "parse_error",
                                         "message": message})()]
    raise AssertionError(f"document unexpectedly compiled: {doc}")


def _message_text(doc):
    return " | ".join(f"{issue.where} {issue.message}" for issue in _issues(doc))


def _breadth(**overrides):
    spec = {
        "condition": {"all": [{"field": "close", "op": "gt", "value": 50}]},
        "distinct_instruments": 5,
        "window": "30m",
    }
    spec.update(overrides)
    return _doc(stages=[{
        "id": "b", "type": "breadth", "clock": "candle_close",
        "timeframe": "5minute", "breadth": spec,
    }])


# ---------------------------------------------------------------------------
# round trip and hash stability
# ---------------------------------------------------------------------------


def test_phase4_document_round_trips_exactly():
    doc = _doc(
        stages=[
            {**SIGNAL, "conditions": {"all": [
                {"left": {"field": "close"}, "op": "gt", "right": {"value": 100},
                 "hysteresis": {"release": 99}},
            ]}, "consecutive_bars": 3},
            _stage_without_conditions(id="seq", sequence={
                "first": {"any": [{"field": "close", "op": "crosses_above", "value": 100}]},
                "then": {"all": [{"field": "close", "op": "lt", "value": 99}]},
                "within_bars": 8, "within": "2h",
            })["stages"][0],
            {"id": "b", "type": "breadth", "clock": "candle_close", "timeframe": "5minute",
             "breadth": {"condition": {"all": [{"field": "close", "op": "gt", "value": 50}]},
                         "distinct_instruments": 5, "window": "30m"}},
        ],
        alerts=[{"id": "a", "source": "s", "max_per_session": 5,
                 "session_cap_reset": "session"}],
    )
    parsed = parse_workflow_dict(doc)
    again = parse_workflow_dict(parsed.to_document_dict())
    assert again == parsed
    assert compile_document(parsed).canonical_hash == compile_document(again).canonical_hash


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("basic-price.yaml", "f2c74c76ad9c4b24292c62dd3e12d25df5f7996941d16b3dfed5c78be32a9dcf"),
        ("nifty-quality-momentum-screener.yaml", "2389b31742d444839a39530b1ffb1fcfc3a85b7325caf252a78ab3b963b8290d"),
        ("quality-momentum.yaml", "1864f7f798e089476dbb872b254e2ae6fc28e31ad7722e2d985ad98268d9f534"),
    ],
)
def test_shipped_fixtures_keep_their_canonical_hash(fixture, expected):
    """Phase 4 keys are emitted only when set: existing revisions are re-hashed no."""
    from backend.workflows.parser import parse_workflow_yaml

    doc = parse_workflow_yaml(open(f"tests/fixtures/workflows/{fixture}").read())
    digest = hashlib.sha256(canonical_json(doc).encode()).hexdigest()
    assert digest == expected
    round_tripped = parse_workflow_dict(doc.to_document_dict())
    assert hashlib.sha256(canonical_json(round_tripped).encode()).hexdigest() == expected


# ---------------------------------------------------------------------------
# consecutive bars
# ---------------------------------------------------------------------------


def test_consecutive_bars_requires_candle_clock():
    issues = _issues(_stage(clock="ltp", timeframe=None, consecutive_bars=3))
    assert any("consecutive_bars" in issue.where for issue in issues)
    assert "candle_close" in _message_text(_stage(clock="ltp", timeframe=None, consecutive_bars=3))


def test_consecutive_bars_bound_is_enforced():
    for bad in (0, registry.MAX_CONSECUTIVE_BARS + 1):
        issues = _issues(_stage(consecutive_bars=bad))
        assert any(issue.where.endswith("consecutive_bars") for issue in issues)


def test_consecutive_bars_cannot_combine_with_sequence_or_breadth():
    text = _message_text(_stage(consecutive_bars=3, breadth={
        "condition": {"all": [{"field": "close", "op": "gt", "value": 1}]},
        "distinct_instruments": 2, "window": "30m",
    }))
    assert "cannot be combined" in text


# ---------------------------------------------------------------------------
# sequences
# ---------------------------------------------------------------------------


def _sequence(**overrides):
    spec = {
        "first": {"all": [{"field": "close", "op": "gt", "value": 100}]},
        "then": {"all": [{"field": "close", "op": "lt", "value": 99}]},
        "within_bars": 5,
    }
    spec.update(overrides)
    return _stage_without_conditions(sequence=spec)


def test_sequence_requires_at_least_one_bound():
    with pytest.raises(Exception) as excinfo:
        parse_workflow_dict(_sequence(within_bars=None))
    assert "at least one bound" in str(excinfo.value)


def test_sequence_accepts_time_bound_alone():
    stage = parse_workflow_dict(_sequence(within_bars=None, within="2h")).stages[0]
    assert stage.sequence.within_s == 7200
    assert stage.sequence.within_bars is None


def test_sequence_requires_candle_clock():
    doc = _stage_without_conditions(
        clock="ltp", timeframe=None,
        sequence={"first": {"all": [{"field": "ltp", "op": "gt", "value": 1}]},
                  "then": {"all": [{"field": "ltp", "op": "lt", "value": 1}]},
                  "within_bars": 5},
    )
    assert "candle_close" in _message_text(doc)


def test_sequence_rejects_combination_with_own_conditions():
    doc = _stage(sequence={
        "first": {"all": [{"field": "close", "op": "gt", "value": 1}]},
        "then": {"all": [{"field": "close", "op": "lt", "value": 1}]},
        "within_bars": 5,
    })
    assert "cannot be combined" in _message_text(doc)


def test_sequence_bar_bound_range_is_enforced():
    doc = _sequence(within_bars=registry.MAX_SEQUENCE_WITHIN_BARS + 1)
    assert "within_bars" in _message_text(doc)


# ---------------------------------------------------------------------------
# breadth
# ---------------------------------------------------------------------------


def test_breadth_compiles_with_the_windowed_mode():
    stage = parse_workflow_dict(_breadth()).stages[0]
    assert stage.breadth.distinct_instruments == 5
    assert stage.breadth.window_s == 1800
    assert stage.breadth.mode == "triggers_within"
    compile_document(parse_workflow_dict(_breadth()))


def test_simultaneous_breadth_is_rejected_as_not_implemented():
    """Reserved name, deliberately unimplemented — never silently accepted."""
    text = _message_text(_breadth(mode="simultaneous"))
    assert "not implemented" in text
    assert "simultaneous breadth is deferred" in text


def test_unknown_breadth_mode_is_a_capability_error():
    issues = _issues(_breadth(mode="whenever"))
    assert any(issue.code == "unknown_capability" for issue in issues)


def test_breadth_requires_a_breadth_block_and_matching_stage_type():
    doc = _doc(stages=[{"id": "b", "type": "breadth", "clock": "candle_close",
                        "timeframe": "5minute"}])
    assert "requires a 'breadth' block" in _message_text(doc)
    doc = _stage(breadth={"condition": {"all": [{"field": "close", "op": "gt", "value": 1}]},
                          "distinct_instruments": 2, "window": "30m"})
    assert "requires stage type 'breadth'" in _message_text(doc)


def test_breadth_limits_are_enforced():
    assert "distinct_instruments" in _message_text(_breadth(distinct_instruments=1))
    assert "window" in _message_text(_breadth(window="10s"))
    assert "window" in _message_text(_breadth(window="48h"))


def test_breadth_requires_the_candle_clock():
    doc = _doc(stages=[{
        "id": "b", "type": "breadth", "clock": "ltp",
        "breadth": {"condition": {"all": [{"field": "ltp", "op": "gt", "value": 1}]},
                    "distinct_instruments": 2, "window": "30m"},
    }])
    assert "candle_close" in _message_text(doc)


# ---------------------------------------------------------------------------
# hysteresis
# ---------------------------------------------------------------------------


def _hysteresis(right, op="gt", release=99):
    return _stage(conditions={"all": [
        {"left": {"field": "close"}, "op": op, "right": right,
         "hysteresis": {"release": release}},
    ]})


def test_hysteresis_requires_a_constant_threshold():
    text = _message_text(_hysteresis({"indicator": "sma", "period": 20}))
    assert "constant threshold" in text
    assert "dynamic release operands are not implemented" in text


def test_hysteresis_release_must_be_on_the_correct_side():
    assert "must be below" in _message_text(_hysteresis({"value": 100}, release=101))
    assert "must be above" in _message_text(
        _hysteresis({"value": 100}, op="lt", release=99)
    )


def test_hysteresis_only_applies_to_level_operators():
    text = _message_text(
        _hysteresis({"value": 100}, op="crosses_above", release=99)
    )
    assert "hysteresis is valid on" in text


# ---------------------------------------------------------------------------
# pair operands
# ---------------------------------------------------------------------------


def _pair(spec, op="gt", value=1.0):
    return _stage(conditions={"all": [
        {"left": spec, "op": op, "right": {"value": value}},
    ]})


def test_pair_ratio_compiles_and_round_trips():
    doc = _pair({"pair_ratio": {"instrument": "NSE:A", "reference": "NSE:B"}})
    parsed = parse_workflow_dict(doc)
    assert parsed.stages[0].conditions[0].left.kind == "pair"
    assert parse_workflow_dict(parsed.to_document_dict()) == parsed
    compile_document(parsed)


def test_relative_strength_requires_a_lookback():
    assert "requires 'lookback'" in _message_text(
        _pair({"relative_strength": {"instrument": "NSE:A", "reference": "NSE:B"}})
    )


def test_pair_legs_must_be_distinct_and_qualified():
    assert "DIFFERENT instruments" in _message_text(
        _pair({"pair_ratio": {"instrument": "NSE:A", "reference": "NSE:A"}})
    )
    assert "exchange-qualified" in _message_text(
        _pair({"pair_ratio": {"instrument": "A", "reference": "NSE:B"}})
    )


def test_pair_lookback_and_skew_bounds_are_enforced():
    text = _message_text(_pair({"relative_strength": {
        "instrument": "NSE:A", "reference": "NSE:B",
        "lookback": registry.PAIR_LOOKBACK_BOUNDS[1] + 1}}))
    assert "lookback must be an integer between" in text
    text = _message_text(_pair({"relative_strength": {
        "instrument": "NSE:A", "reference": "NSE:B", "lookback": 5,
        "max_skew_bars": registry.PAIR_MAX_SKEW_BARS + 1}}))
    assert "max_skew_bars must be between" in text


def test_pair_rejects_mixing_with_other_operand_keys():
    with pytest.raises(Exception) as excinfo:
        parse_workflow_dict(_pair({"pair_ratio": {"instrument": "NSE:A",
                                                  "reference": "NSE:B"},
                                   "field": "close"}))
    assert "cannot be combined" in str(excinfo.value)


def test_groups_inside_conditions_are_preserved_not_silently_dropped():
    """Regression: ``conditions: {all, any, not}`` used to lose any/not.

    The documented authoring form puts all three groups inside ``conditions``.
    The parser validated the keys and then used only the AND group, silently
    turning an OR/NOT rule into an AND-only rule — a semantic change with no
    error. All three groups must survive parsing and the hash round trip.
    """
    doc = _stage(conditions={
        "all": [{"field": "close", "op": "gt", "value": 100}],
        "any": [{"field": "volume", "op": "gt", "value": 500}],
        "not": [{"field": "close", "op": "lt", "value": 50}],
    })
    stage = parse_workflow_dict(doc).stages[0]
    assert len(stage.conditions) == 1
    assert len(stage.any_conditions) == 1
    assert len(stage.not_conditions) == 1
    # the semantic content, not just the counts
    assert stage.any_conditions[0].left.name == "volume"
    assert stage.not_conditions[0].op == "lt"
    # and it survives the canonical round trip that hashing relies on
    assert parse_workflow_dict(
        parse_workflow_dict(doc).to_document_dict()
    ) == parse_workflow_dict(doc)


def test_top_level_group_alias_still_wins_over_the_inline_form():
    doc = _stage(
        conditions={"all": [{"field": "close", "op": "gt", "value": 100}]},
        any=[{"field": "volume", "op": "gt", "value": 500}],
    )
    stage = parse_workflow_dict(doc).stages[0]
    assert len(stage.any_conditions) == 1
    assert stage.any_conditions[0].left.name == "volume"


# ---------------------------------------------------------------------------
# session caps
# ---------------------------------------------------------------------------


def test_session_cap_compiles():
    doc = _doc(stages=[SIGNAL], alerts=[
        {"id": "a", "source": "s", "max_per_session": 5, "session_cap_reset": "session"},
    ])
    alert = parse_workflow_dict(doc).alerts[0]
    assert alert.max_per_session == 5
    assert alert.session_cap_reset == "session"


def test_exchange_hours_reset_is_rejected_with_an_actionable_message():
    doc = _doc(stages=[SIGNAL], alerts=[
        {"id": "a", "source": "s", "max_per_session": 5,
         "session_cap_reset": "market_hours"},
    ])
    text = _message_text(doc)
    assert "unsupported reset" in text
    assert "feed-driven" in text


def test_session_cap_bounds_and_dependencies():
    doc = _doc(stages=[SIGNAL], alerts=[
        {"id": "a", "source": "s", "max_per_session": 0}])
    assert "must be an integer between" in _message_text(doc)
    doc = _doc(stages=[SIGNAL], alerts=[
        {"id": "a", "source": "s", "session_cap_reset": "session"}])
    assert "requires 'max_per_session'" in _message_text(doc)


# ---------------------------------------------------------------------------
# capability discovery stays in sync with validation
# ---------------------------------------------------------------------------


def test_arithmetic_depth_is_enforced_and_advertised():
    """MAX_ARITHMETIC_DEPTH was advertised but never checked before Phase 4."""
    deep = _stage(conditions={"all": [
        {"left": {"field": "close"}, "op": "gt", "right": {
            "add": [1, {"multiply": [2, {"subtract": [3, {"divide": [4, {"field": "volume"}]}]}]}]}},
    ]})
    assert "arithmetic nesting exceeds" in _message_text(deep)
    allowed = _stage(conditions={"all": [
        {"left": {"field": "close"}, "op": "gt", "right": {
            "add": [1, {"multiply": [2, {"field": "volume"}]}]}},
    ]})
    compile_document(parse_workflow_dict(allowed))


def test_capabilities_expose_every_phase4_surface():
    caps = registry.CAPABILITIES
    assert set(caps["stage_types"]) == {"signal", "filter", "feature", "breadth"}
    assert set(caps["pairs"]) == {"pair_ratio", "relative_strength"}
    assert caps["breadth_modes"]["simultaneous"]["implemented"] is False
    assert caps["breadth_modes"]["triggers_within"]["implemented"] is True
    limits = caps["limits"]
    for key in ("max_consecutive_bars", "max_sequence_within_bars",
                "max_breadth_instruments", "max_breadth_window_s",
                "max_per_session", "max_arithmetic_depth"):
        assert key in limits


def test_capabilities_endpoint_matches_the_registry():
    """The API advertises what validation enforces — same source, no drift.

    The payload is a pure function of the registry, so this asserts on the
    real object the endpoint returns rather than on a copy of it.
    """
    from backend.api.routers.worker_workflows import capabilities_payload

    caps = capabilities_payload()
    assert set(caps["stage_types"]) == set(registry.STAGE_TYPES)
    assert set(caps["operators"]) == set(registry.OPERATORS)
    assert set(caps["pairs"]) == set(registry.PAIR_COMPUTATIONS)
    assert set(caps["breadth_modes"]) == set(registry.BREADTH_MODES)
    assert caps["breadth_modes"]["simultaneous"] == {"implemented": False}
    for key, value in (
        ("max_consecutive_bars", registry.MAX_CONSECUTIVE_BARS),
        ("max_sequence_within_bars", registry.MAX_SEQUENCE_WITHIN_BARS),
        ("max_breadth_instruments", registry.MAX_BREADTH_INSTRUMENTS),
        ("max_breadth_window_s", registry.MAX_BREADTH_WINDOW_S),
        ("max_per_session", registry.MAX_PER_SESSION),
        ("max_arithmetic_depth", registry.MAX_ARITHMETIC_DEPTH),
    ):
        assert caps["limits"][key] == value
    assert caps["limits"]["max_instruments"] == 1000


# ---------------------------------------------------------------------------
# shipped Phase 4 fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "phase4-consecutive-closes.yaml",
        "phase4-breakout-pullback.yaml",
        "phase4-breadth-window.yaml",
        "phase4-pair-ratio.yaml",
        "phase4-external-producer.yaml",
    ],
)
def test_phase4_fixtures_compile_and_round_trip(fixture):
    """Every shipped example must compile AND survive the hash round trip."""
    from backend.workflows.parser import parse_workflow_yaml

    text = open(f"tests/fixtures/workflows/{fixture}").read()
    parsed = parse_workflow_yaml(text)
    compiled = compile_document(parsed)
    again = parse_workflow_dict(parsed.to_document_dict())
    assert compile_document(again).canonical_hash == compiled.canonical_hash
    # Re-parsing the exported document reproduces the same document.
    assert again == parsed
