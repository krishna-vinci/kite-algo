"""Phase 3: screener block authoring contract (parse + compile)."""

from __future__ import annotations

import pytest

from backend.workflows.compiler import WorkflowValidationError, compile_document
from backend.workflows.models import WorkflowDocument
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict


def _doc(screener, *, alerts=None, stages=None, universe=None):
    return {
        "version": 1,
        "name": "scr",
        "session": "nse_equity",
        **({"universe": universe} if universe is not None else {"universe": {"union": [{"index": "nifty50"}]}}),
        "stages": stages
        or [
            {
                "id": "scan",
                "type": "filter",
                "clock": "candle_close",
                "timeframe": "1d",
                "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]},
            }
        ],
        "alerts": alerts or [],
        **({"screener": screener} if screener is not None else {}),
    }


VALID = {
    "schedule": {"every": "1d", "at": "session_close"},
    "rank": {"by": {"field": "change_pct"}, "direction": "desc"},
    "top_n": 20,
    "attachments": [
        {"id": "en", "trigger": "top_n", "top_n": 10, "channels": ["telegram_primary"]}
    ],
    "freshness_limit": "2d",
}


def test_valid_screener_document_compiles_and_round_trips():
    compiled = compile_document(parse_workflow_dict(_doc(VALID)))
    again = parse_workflow_dict(compiled.document.to_document_dict())
    assert compiled.document.screener.top_n == 20
    assert compiled.document.screener.attachments[0].entry_rank == 10
    # default exit band: top_n + max(1, top_n//2)
    assert compiled.document.screener.attachments[0].exit_rank == 15
    assert compiled.canonical_hash == compile_document(again).canonical_hash


def test_mcx_schedule_calendar_rejected_with_actionable_error():
    screener = {"schedule": {"every": "1d", "calendar": "mcx_commodity"}}
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(_doc(screener))
    assert "mcx" in str(exc.value).lower()
    assert "calendar" in str(exc.value).lower()


def test_ltp_stage_rejected_in_screener_document():
    stages = [
        {
            "id": "live",
            "type": "signal",
            "clock": "ltp",
            "conditions": {"all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": 1}}]},
        }
    ]
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(parse_workflow_dict(_doc(VALID, stages=stages)))
    assert any(i.code == "unknown_capability" for i in exc.value.issues)


def test_alerts_rejected_in_screener_document():
    alerts = [{"id": "a", "source": "scan", "channels": ["t"]}]
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(parse_workflow_dict(_doc(VALID, alerts=alerts)))
    assert any("attachments" in i.message for i in exc.value.issues)


def test_missing_universe_rejected_in_screener_document():
    doc = _doc(VALID)
    doc["universe"] = None
    parsed = parse_workflow_dict({k: v for k, v in doc.items() if k != "universe"})
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(parsed)
    assert any(i.code == "missing_reference" and "universe" in i.message for i in exc.value.issues)


def test_multiple_terminal_stages_rejected():
    stages = [
        {
            "id": "scan1",
            "type": "filter",
            "clock": "candle_close",
            "timeframe": "1d",
            "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]},
        },
        {
            "id": "scan2",
            "type": "filter",
            "clock": "candle_close",
            "timeframe": "1d",
            "conditions": {"all": [{"left": {"field": "volume"}, "op": "gt", "right": {"value": 1}}]},
        },
    ]
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(parse_workflow_dict(_doc(VALID, stages=stages)))
    assert any("terminal" in i.message for i in exc.value.issues)


def test_hysteresis_requires_exit_rank_greater_than_entry_rank():
    screener = {
        "schedule": {"every": "1d"},
        "attachments": [
            {
                "id": "tn",
                "trigger": "top_n",
                "top_n": 10,
                "entry_rank": 15,
                "exit_rank": 12,  # inverted
                "channels": ["t"],
            }
        ],
    }
    with pytest.raises(WorkflowParseError) as exc:
        parse_workflow_dict(_doc(screener))
    assert "oscillate" in str(exc.value)


def test_rank_delta_requires_threshold():
    screener = {
        "schedule": {"every": "1d"},
        "attachments": [{"id": "rd", "trigger": "rank_delta", "channels": ["t"]}],
    }
    with pytest.raises(WorkflowParseError):
        parse_workflow_dict(_doc(screener))


def test_screener_only_fields_rejected_in_alert_documents():
    doc = {
        "version": 1,
        "name": "alert-doc",
        "session": "nse_equity",
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {"all": [{"left": {"field": "change_pct"}, "op": "gt", "right": {"value": 1}}]},
            }
        ],
        "alerts": [{"id": "a", "source": "px", "channels": ["t"]}],
    }
    with pytest.raises(WorkflowValidationError) as exc:
        compile_document(parse_workflow_dict(doc))
    assert any("screener" in i.message for i in exc.value.issues)


def test_screener_only_fields_accepted_in_screener_documents():
    screener = {
        "schedule": {"every": "30m"},
        "rank": {"by": {"field": "turnover"}, "direction": "desc"},
    }
    compiled = compile_document(parse_workflow_dict(_doc(screener)))
    assert compiled.document.screener.rank.by.name == "turnover"


def test_reference_screener_fixture_compiles_and_round_trips():
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests" / "fixtures" / "workflows" / "nifty-quality-momentum-screener.yaml"
    )
    from backend.workflows.parser import parse_workflow_yaml

    text = fixture.read_text()
    compiled = compile_document(parse_workflow_yaml(text))
    again = parse_workflow_dict(compiled.document.to_document_dict())
    assert compile_document(again).canonical_hash == compiled.canonical_hash
    assert len(compiled.document.screener.attachments) == 2
