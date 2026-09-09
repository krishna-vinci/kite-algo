"""Compiler tests for the workflow document contract (Task 1).

Covers: canonical JSON/hash stability across key order, hash sensitivity to
semantic changes, WorkflowValidationError.issue contents (where paths name the
offending stage), and stage-graph cycle detection via the optional `input`.
"""

from __future__ import annotations

import json

import pytest

from backend.workflows.compiler import (
    WorkflowValidationError,
    canonical_json,
    compile_document,
)
from backend.workflows.parser import parse_workflow_dict


def _minimal_doc() -> dict:
    return {
        "version": 1,
        "name": "minimal",
        "instruments": ["NSE:RELIANCE"],
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [
                        {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 3000}}
                    ]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "once"}],
    }


def _with_ema_indicator(period: int) -> dict:
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0] = {
        "left": {"field": "close"},
        "op": "gt",
        "right": {"indicator": "ema", "period": period},
    }
    return obj


def test_canonical_hash_stable_across_key_order() -> None:
    obj_a = _minimal_doc()
    obj_b = {
        "alerts": [{"trigger": "once", "source": "px", "id": "a1"}],
        "stages": [
            {
                "conditions": {
                    "all": [
                        {"right": {"value": 3000}, "op": "crosses_above", "left": {"field": "ltp"}}
                    ]
                },
                "clock": "ltp",
                "type": "signal",
                "id": "px",
            }
        ],
        "instruments": ["NSE:RELIANCE"],
        "name": "minimal",
        "version": 1,
    }
    doc_a = parse_workflow_dict(obj_a)
    doc_b = parse_workflow_dict(obj_b)
    assert canonical_json(doc_a) == canonical_json(doc_b)
    assert compile_document(doc_a).canonical_hash == compile_document(doc_b).canonical_hash


def test_to_json_is_canonical_sorted_keys() -> None:
    compiled = compile_document(parse_workflow_dict(_minimal_doc()))
    raw = compiled.to_json()
    assert json.loads(raw)
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))


def test_hash_changes_when_level_changes() -> None:
    low = parse_workflow_dict(_minimal_doc())
    obj_high = _minimal_doc()
    obj_high["stages"][0]["conditions"]["all"][0]["right"]["value"] = 3001
    high = parse_workflow_dict(obj_high)
    assert compile_document(high).canonical_hash != compile_document(low).canonical_hash


def test_hash_changes_when_indicator_param_changes() -> None:
    ema20 = parse_workflow_dict(_with_ema_indicator(20))
    ema50 = parse_workflow_dict(_with_ema_indicator(50))
    assert canonical_json(ema20) != canonical_json(ema50)
    with pytest.raises(WorkflowValidationError):
        compile_document(ema20)


def test_compile_raises_with_issues_naming_offending_stage() -> None:
    doc = parse_workflow_dict(_minimal_doc())
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0]["op"] = "whatever_unknown"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert exc.value.issues
    assert all(issue.where and issue.code and issue.message for issue in exc.value.issues)
    assert any(
        issue.code == "unknown_operator" and issue.where.startswith("stages.px")
        for issue in exc.value.issues
    )


def test_unknown_field_operand_reports_unknown_field() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0]["left"] = {"field": "mid_price"}
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "unknown_field" for issue in exc.value.issues)


def test_namespaced_future_field_reports_unknown_capability() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0]["left"] = {
        "field": "fundamentals.latest_roce_pct"
    }
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "unknown_capability" for issue in exc.value.issues)


def test_unknown_trigger_rejected() -> None:
    obj = _minimal_doc()
    obj["alerts"][0]["trigger"] = "whenever"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "bad_value" for issue in exc.value.issues)


def test_session_mismatch_rejects_mcx_in_nse_equity_workflow() -> None:
    obj = _minimal_doc()
    obj["instruments"] = ["MCX:GOLD26OCTFUT"]
    doc = parse_workflow_dict(obj)

    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)

    assert any(issue.code == "session_mismatch" for issue in exc.value.issues)


def test_stage_input_cycle_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"] = [
        {
            "id": "s1",
            "type": "signal",
            "clock": "ltp",
            "input": "s2",
            "conditions": {"all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": 1}}]},
        },
        {
            "id": "s2",
            "type": "signal",
            "clock": "ltp",
            "input": "s1",
            "conditions": {"all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": 1}}]},
        },
    ]
    obj["alerts"] = [{"id": "a1", "source": "s1"}]
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "cycle" for issue in exc.value.issues)


def test_stage_self_input_cycle_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["input"] = "px"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "cycle" for issue in exc.value.issues)


def test_stage_input_missing_reference_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["input"] = "ghost"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "missing_reference" for issue in exc.value.issues)


def test_acyclic_input_chain_parses_but_flags_unknown_capability() -> None:
    """Upstream stage evaluation is unimplemented in Phase 1: a *resolved*
    ``input`` reference must fail compile with ``unknown_capability``."""
    obj = _minimal_doc()
    obj["stages"].append(
        {
            "id": "px2",
            "type": "signal",
            "clock": "ltp",
            "input": "px",
            "conditions": {"all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": 1}}]},
        }
    )
    obj["alerts"].append({"id": "a2", "source": "px2"})
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(
        issue.code == "unknown_capability" and ".input" in issue.where
        for issue in exc.value.issues
    )


def test_oversized_stage_count_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"] = [
        {
            "id": f"s{i}",
            "type": "signal",
            "clock": "ltp",
            "conditions": {
                "all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": i}}]
            },
        }
        for i in range(100)
    ]
    obj["alerts"] = [{"id": "a1", "source": "s0"}]
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "bad_value" for issue in exc.value.issues)


def _within_doc(right: dict) -> dict:
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0] = {
        "left": {"field": "ltp"},
        "op": "within",
        "right": right,
    }
    return obj


def test_within_without_upper_bound_is_rejected() -> None:
    """`within` needs a finite numeric upper bound (right params 'hi');
    an unbounded range would evaluate to permanent unknown (never fires)."""
    doc = parse_workflow_dict(_within_doc({"value": 100}))
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    issue = next(i for i in exc.value.issues if "hi" in i.where or i.code == "bad_value")
    assert issue.code == "bad_value"
    assert "within" in issue.message


def test_within_with_finite_hi_compiles() -> None:
    doc = parse_workflow_dict(_within_doc({"value": 100, "params": {"hi": 110}}))
    compiled = compile_document(doc)  # no raise
    assert compiled.canonical_hash

    # explicit serialized form without the params wrapper
    doc = parse_workflow_dict(_within_doc({"kind": "value", "value": 100, "params": {"hi": 110}}))
    compile_document(doc)  # no raise


@pytest.mark.parametrize("hi", [None, "110", True])
def test_within_non_numeric_upper_bound_rejected(hi) -> None:
    doc = parse_workflow_dict(_within_doc({"value": 100, "params": {"hi": hi}}))
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "bad_value" for issue in exc.value.issues)
