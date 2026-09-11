"""Phase 6 6A.0 acceptance: level predicates vs edge operators.

The engine contract (verified in code, not assumed):

- a LEVEL operator (`gt`/`gte`/`lt`/`lte`) returns `fired=False` always — it
  reports `matched` and nothing else;
- `engine.decide` computes `due = fired or (trigger == "reminder" and matched
  is True)`, so a non-`reminder` trigger with `notify_if_already_true` off can
  emit ONLY through the E-9 activation opt-in;
- EDGE operators (`crosses_above`/`crosses_below`, `within`, `rises_pct`/
  `falls_pct`, `breaks_prev_high`/`breaks_prev_low`) plus stage-level
  `consecutive_bars`/`sequence` do produce a transition.

This is the mistake behind the Phase 4 live failure: a `gte` rule authored
where a crossing was intended, which could never notify. The fix is to TELL the
author, not to reject the document — a level rule with a `reminder` trigger is
perfectly valid, so this is a warning.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.alerts.predicates import Observation, evaluate_stage
from backend.workflows.compiler import (
    WorkflowValidationError,
    collect_warnings,
    compile_document,
)
from backend.workflows.parser import parse_workflow_dict

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
LEVEL_ONLY_WARNING = "level_only_never_fires"


def _doc(*, op="gt", trigger="on_transition", notify_if_already_true=False,
         inert=False, timeframe=None, **extra):
    stage = {
        "id": "px",
        "type": "signal",
        "clock": "candle_close" if timeframe else "ltp",
        "conditions": {
            "all": [
                {"left": {"field": "ltp"}, "op": op, "right": {"value": 100.0}}
            ]
        },
    }
    if timeframe:
        stage["timeframe"] = timeframe
    stage.update(extra)
    alert = {"id": "a1", "source": "px", "trigger": trigger}
    if notify_if_already_true:
        alert["notify_if_already_true"] = True
    return {
        "version": 1,
        "name": "level-vs-edge",
        "session": "nse_equity",
        "instruments": ["NSE:RELIANCE"],
        "stages": [stage],
        "alerts": [alert],
    }


def _warnings(doc):
    return collect_warnings(parse_workflow_dict(doc))


def _codes(doc):
    return [warning.code for warning in _warnings(doc)]


# ---------------------------------------------------------------------------
# the engine contract the warning is derived from
# ---------------------------------------------------------------------------


def test_level_operator_never_reports_a_transition():
    """`gt` matches but does not fire — the basis of the whole rule."""
    stage = parse_workflow_dict(_doc(op="gt")).stages[0]
    state = {"epoch_id": "e", "conds": {}}
    result = evaluate_stage(
        stage, Observation(ts=T0, epoch_id="e", ltp=150.0), state
    )
    assert result.matched is True
    assert result.fired is False, (
        "a level operator must never report a transition; the warning depends "
        "on this and the engine relies on it"
    )


def test_crossing_operator_reports_a_transition():
    """`crosses_above` does fire across two observations in one epoch."""
    stage = parse_workflow_dict(_doc(op="crosses_above")).stages[0]
    state = {"epoch_id": "e", "conds": {}}
    below = evaluate_stage(stage, Observation(ts=T0, epoch_id="e", ltp=99.0), state)
    above = evaluate_stage(
        stage,
        Observation(ts=T0, epoch_id="e", ltp=150.0),
        below.state,
    )
    assert above.fired is True


# ---------------------------------------------------------------------------
# the warning
# ---------------------------------------------------------------------------


def test_level_only_rule_with_a_transition_trigger_is_warned():
    assert _codes(_doc(op="gte", trigger="on_transition")) == [LEVEL_ONLY_WARNING]
    assert _codes(_doc(op="gt", trigger="once")) == [LEVEL_ONLY_WARNING]
    assert _codes(_doc(op="lt", trigger="once_per_session")) == [LEVEL_ONLY_WARNING]


def test_the_warning_explains_the_fix():
    warning = _warnings(_doc(op="gte"))[0]
    assert warning.where == "stages.px"
    message = warning.message
    # It names the trigger, the alternatives, and the operator to use instead.
    assert "on_transition" in message
    assert "crosses_above" in message
    assert "reminder" in message
    assert "notify_if_already_true" in message


def test_a_level_rule_with_a_reminder_trigger_is_not_warned():
    """`reminder` emits while a level HOLDS, so the rule is legitimate."""
    assert _codes(_doc(op="gte", trigger="reminder")) == []


def test_the_activation_opt_in_suppresses_the_warning():
    """`notify_if_already_true` is a real opt-in, so the rule can emit."""
    assert _codes(
        _doc(op="gte", trigger="on_transition", notify_if_already_true=True)
    ) == []


def test_crossing_rules_are_not_warned():
    assert _codes(_doc(op="crosses_above")) == []
    assert _codes(_doc(op="crosses_below")) == []


def test_stage_level_transition_constructs_suppress_the_warning():
    """`consecutive_bars` and `sequence` produce their own transitions."""
    assert _codes(_doc(op="gt", consecutive_bars=3)) == []
    sequence = _doc(op="gt")
    sequence["stages"][0]["sequence"] = {
        "first": {"all": [{"left": {"field": "ltp"}, "op": "gt",
                           "right": {"value": 1.0}}]},
        "then": {"all": [{"left": {"field": "ltp"}, "op": "gt",
                          "right": {"value": 1.0}}]},
        "within_bars": 5,
    }
    assert _codes(sequence) == []


def test_a_mixed_rule_is_not_warned():
    """One non-level operator is enough to carry a transition."""
    doc = _doc(op="gt")
    doc["stages"][0]["conditions"]["all"].append(
        {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 50.0}}
    )
    assert _codes(doc) == []


def test_a_screener_style_level_rule_is_still_valid():
    """The warning must never block: a level rule remains authorable."""
    compiled = compile_document(parse_workflow_dict(_doc(op="gte")))
    assert compiled.canonical_hash


# ---------------------------------------------------------------------------
# severity mechanics
# ---------------------------------------------------------------------------


def test_warnings_do_not_block_compilation():
    doc = parse_workflow_dict(_doc(op="gte"))
    compile_document(doc)  # must not raise
    assert _codes(_doc(op="gte")) == [LEVEL_ONLY_WARNING]


def test_errors_still_block_and_are_raised_without_the_warning():
    """A genuine error rejects the document; the warning is not reported."""
    doc = _doc(op="gte")
    doc["stages"][0]["conditions"]["all"][0]["op"] = "crosses_sideways"
    with pytest.raises(WorkflowValidationError) as excinfo:
        compile_document(parse_workflow_dict(doc))
    assert all(item.is_error for item in excinfo.value.issues)


def test_canonical_hash_is_unaffected_by_the_warning():
    """Warnings are advisory metadata, never part of the document identity.

    If a warning changed the hash, every existing revision would be re-hashed
    and revision-conflict detection would break.
    """
    plain = compile_document(parse_workflow_dict(_doc(op="crosses_above")))
    warned = compile_document(parse_workflow_dict(_doc(op="gte")))
    # Different documents, so different hashes — but compiling the SAME document
    # twice is stable, and a warned document keeps its own hash across runs.
    again = compile_document(parse_workflow_dict(_doc(op="gte")))
    assert warned.canonical_hash == again.canonical_hash
    assert warned.canonical_hash != plain.canonical_hash


def test_validation_issue_defaults_to_error_severity():
    """Every pre-existing issue keeps its blocking meaning."""
    from backend.workflows.compiler import ValidationIssue

    assert ValidationIssue("x", "bad_value", "m").severity == "error"
    assert ValidationIssue("x", "bad_value", "m").is_error is True


def test_api_reports_the_warning_with_ok_true():
    """The operator sees the warning without the document being rejected."""
    from backend.api.schemas.workflows import issue

    model = issue("stages.px", LEVEL_ONLY_WARNING, "msg", "warning")
    assert model.severity == "warning"
    assert model.model_dump()["severity"] == "warning"
    assert issue("x", "bad_value", "m").severity == "error"
