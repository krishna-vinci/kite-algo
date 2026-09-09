"""Contract tests for the workflow document parser (Task 1).

Covers: safe-YAML rules (duplicate keys, tags, size, aliases), shorthand
normalization, unknown-field rejection, and the round-trip
YAML -> doc -> JSON -> doc -> compile -> same canonical_hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.workflows.compiler import (
    WorkflowValidationError,
    compile_document,
)
from backend.workflows.models import InstrumentRef
from backend.workflows.parser import (
    WorkflowParseError,
    parse_workflow_dict,
    parse_workflow_yaml,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "workflows"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


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
                        {"left": {"field": "ltp"}, "op": "gt", "right": {"value": 10}}
                    ]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "once"}],
    }


def test_basic_price_round_trip_preserves_hash() -> None:
    text = _fixture_text("basic-price.yaml")
    doc = parse_workflow_yaml(text)
    compiled = compile_document(doc)
    assert compiled.to_json() == json.dumps(
        json.loads(compiled.to_json()), sort_keys=True, separators=(",", ":")
    )

    reparsed = parse_workflow_dict(json.loads(compiled.to_json()))
    recompiled = compile_document(reparsed)
    assert recompiled.canonical_hash == compiled.canonical_hash
    assert reparsed == doc


def test_basic_price_fixture_parses_and_compiles() -> None:
    doc = parse_workflow_yaml(_fixture_text("basic-price.yaml"))
    assert doc.name == "reliance-breakout"
    assert doc.instruments == (InstrumentRef(symbol="RELIANCE", exchange="NSE"),)
    assert doc.stages[0].clock == "ltp"
    assert doc.alerts[0].trigger == "once"
    compiled = compile_document(doc)
    assert len(compiled.canonical_hash) == 64
    assert compiled.canonical_hash == compiled.canonical_hash.lower()


def test_duplicate_key_yaml_rejected_and_named() -> None:
    text = _fixture_text("basic-price.yaml").replace(
        "name: reliance-breakout", "name: reliance-breakout\nname: duplicate-name"
    )
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_yaml(text)
    assert "name" in str(exc.value)


def test_duplicate_nested_key_rejected() -> None:
    text = """
version: 1
name: dup-nested
instruments: ["NSE:TCS"]
stages:
  - id: px
    type: signal
    clock: ltp
    clock: candle_close
    conditions:
      all:
        - left: {field: ltp}
          op: gt
          right: {value: 1}
alerts:
  - id: a
    source: px
"""
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_yaml(text)
    assert "clock" in str(exc.value)


def test_unknown_top_level_field_rejected_with_field_name() -> None:
    obj = _minimal_doc()
    obj["bogus_field"] = 1
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(obj)
    assert "bogus_field" in str(exc.value)
    assert "document" in str(exc.value)


def test_unknown_nested_field_rejected_with_path() -> None:
    obj = _minimal_doc()
    obj["alerts"][0]["nope"] = True
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(obj)
    assert "alerts.a1.nope" in str(exc.value)


def test_unknown_operator_rejected_via_fixture() -> None:
    doc = parse_workflow_yaml(_fixture_text("invalid-unknown-op.yaml"))
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    codes = {issue.code for issue in exc.value.issues}
    assert "unknown_operator" in codes
    assert any(issue.where.startswith("stages.px") for issue in exc.value.issues)


def test_repeat_shorthand_normalized() -> None:
    off = _minimal_doc()
    del off["alerts"][0]["trigger"]
    off["alerts"][0]["repeat"] = False
    doc = parse_workflow_dict(off)
    assert doc.alerts[0].trigger == "once"

    on = _minimal_doc()
    del on["alerts"][0]["trigger"]
    on["alerts"][0]["repeat"] = True
    doc = parse_workflow_dict(on)
    assert doc.alerts[0].trigger == "on_transition"


def test_repeat_conflict_with_trigger_rejected() -> None:
    obj = _minimal_doc()
    obj["alerts"][0]["repeat"] = False
    obj["alerts"][0]["trigger"] = "on_transition"
    with pytest.raises(WorkflowParseError):
        parse_workflow_dict(obj)


def test_instrument_shorthand_normalized() -> None:
    obj = _minimal_doc()
    obj["instruments"] = ["NSE:RELIANCE", {"exchange": "BSE", "symbol": "TCS"}]
    doc = parse_workflow_dict(obj)
    assert doc.instruments[0] == InstrumentRef(symbol="RELIANCE", exchange="NSE")
    assert doc.instruments[1] == InstrumentRef(symbol="TCS", exchange="BSE")
    assert doc.instruments[0].key() == "NSE:RELIANCE"


def test_instrument_shorthand_requires_exchange() -> None:
    obj = _minimal_doc()
    obj["instruments"] = ["RELIANCE"]
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(obj)
    assert "EXCHANGE:SYMBOL" in str(exc.value)


def test_candle_close_without_timeframe_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["clock"] = "candle_close"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "timeframe_missing" for issue in exc.value.issues)


def test_unsupported_timeframe_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["clock"] = "candle_close"
    obj["stages"][0]["timeframe"] = "1d"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "timeframe_unsupported" for issue in exc.value.issues)


def test_duplicate_stage_and_alert_ids_rejected() -> None:
    obj = _minimal_doc()
    obj["stages"].append(dict(obj["stages"][0]))
    obj["alerts"].append(dict(obj["alerts"][0]))
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    dupes = [issue for issue in exc.value.issues if issue.code == "duplicate_id"]
    assert len(dupes) >= 2


def test_alert_referencing_missing_stage_rejected() -> None:
    obj = _minimal_doc()
    obj["alerts"][0]["source"] = "ghost"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "missing_reference" for issue in exc.value.issues)


def test_quality_momentum_parses_despite_future_capabilities() -> None:
    doc = parse_workflow_yaml(_fixture_text("quality-momentum.yaml"))
    assert doc.name == "quality-momentum"
    assert [stage.id for stage in doc.stages] == ["quality", "trend", "breakout"]
    # proposal shorthand: evaluate_on -> clock, on_signal -> on_transition,
    # cooldown: 30m -> cooldown_s = 1800
    assert doc.stages[0].clock == "fundamentals_refresh"
    assert doc.stages[1].clock == "candle_close"
    assert doc.alerts[0].trigger == "on_transition"
    assert doc.alerts[0].cooldown_s == 1800
    assert doc.instruments == ()


def test_quality_momentum_flags_unknown_capabilities() -> None:
    doc = parse_workflow_yaml(_fixture_text("quality-momentum.yaml"))
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    messages = " | ".join(issue.message for issue in exc.value.issues)
    assert any(issue.code == "unknown_capability" for issue in exc.value.issues)
    assert "fundamentals.quarterly_revenue_yoy_pct" in messages
    assert any("ema" in issue.message for issue in exc.value.issues)
    assert not exc.value.issues == [] and all(issue.where for issue in exc.value.issues)


def test_indicator_operand_reports_unknown_capability() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["conditions"]["all"][0] = {
        "left": {"field": "close"},
        "op": "gt",
        "right": {"indicator": "ema", "period": 200},
    }
    doc = parse_workflow_dict(obj)
    assert doc.stages[0].conditions[0].right.kind == "indicator"
    assert doc.stages[0].conditions[0].right.params == {"period": 200}
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "unknown_capability" for issue in exc.value.issues)


def test_oversized_document_rejected() -> None:
    big = "x" * (256 * 1024 + 1)
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_yaml(f"version: 1\nname: {big}\n")
    assert "256" in str(exc.value)


def test_custom_yaml_tag_rejected() -> None:
    text = _fixture_text("basic-price.yaml").replace(
        "name: reliance-breakout", "name: !dangerous reliance-breakout"
    )
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_yaml(text)
    assert "tag" in str(exc.value).lower()


def test_bounded_alias_expansion_rejected() -> None:
    # classic billion-laughs shape: nested anchors multiply the logical node
    # count far beyond the parse budget even though the text itself is small
    lines = ["a0: &a0 [" + ",".join(["1"] * 10) + "]"]
    for i in range(1, 7):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 10) + "]")
    text = "\n".join(lines) + "\nversion: 1\nname: bombs\n"
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_yaml(text)
    assert "complex" in str(exc.value).lower() or "alias" in str(exc.value).lower()


def test_unknown_clock_rejected_at_compile() -> None:
    obj = _minimal_doc()
    obj["stages"][0]["clock"] = "telepathy"
    doc = parse_workflow_dict(obj)
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(doc)
    assert any(issue.code == "unknown_capability" for issue in exc.value.issues)


def test_version_must_be_one() -> None:
    obj = _minimal_doc()
    obj["version"] = 2
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(obj)
    assert "version" in str(exc.value)


def test_non_mapping_document_rejected() -> None:
    with pytest.raises(WorkflowParseError):
        parse_workflow_yaml("- just\n- a\n- list\n")
    with pytest.raises(WorkflowParseError):
        parse_workflow_dict([1, 2, 3])


def test_data_policy_round_trips() -> None:
    obj = _minimal_doc()
    obj["data_policy"] = {
        "missing": "exclude_and_report",
        "insufficient_history": "wait",
        "require_closed_candles": False,
    }
    doc = parse_workflow_dict(obj)
    assert doc.data_policy.require_closed_candles is False
    reparsed = parse_workflow_dict(doc.to_document_dict())
    assert reparsed == doc
