"""The Phase 5 harness's own acceptance rules.

These are the regressions for the ways the harness could hand back a green result
that proves nothing:

* a terminal request count is not a finished child (the child must exit by
  itself, with its own final marker);
* a request that executed is not a settled book (the four-axis assessment is the
  evidence, and a missing assessment is a failure);
* a holding scenario must show exactly the expected open exposure, and the paper
  orders must agree with the attributed book;
* an owner decision must not follow an order;
* a repeated observation must not produce a second close.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "hosted_platform"


def _load_assertions():
    spec = importlib.util.spec_from_file_location(
        "phase5_assertions", EXAMPLES / "_assertions.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _axes(**states):
    axes = {
        name: {"state": states.get(name, "satisfied"), "satisfied": True}
        for name in (
            "quiescence",
            "attribution_scoped_flatness",
            "terminal_domain_state",
            "no_live_evaluation_authority",
        )
    }
    return {"overall": "settled", "axes": axes}


def _evidence(**overrides):
    evidence = {
        "requests": [{"request_id": "r1", "status": "executed"}],
        "execution_events": [{"event": "submitted", "step_no": 1, "filled_quantity": 5}],
        "paper_orders": [
            {
                "order_id": "o1",
                "tradingsymbol": "RELIANCE",
                "transaction_type": "BUY",
                "quantity": 5,
                "status": "COMPLETE",
            }
        ],
        "attributed_positions": [
            {"tradingsymbol": "RELIANCE", "net_quantity": 5, "projection_version": 3}
        ],
        "child_log": "settled: coverage=known pending=0 positions={'RELIANCE': 5}",
        "settlement": _axes(attribution_scoped_flatness="failed"),
        "orders_before_approval": 0,
    }
    evidence.update(overrides)
    return evidence


SUPERVISOR = {"outcome": "exited", "exit_code": 0, "stop": "exited"}

HOLDING_SPEC = {
    "expected_requests": 1,
    "expects_manual": True,
    "expected_orders": 1,
    "expected_positions": {"RELIANCE": 5},
    "final_marker": "settled: coverage=known pending=0 positions=",
    "expected_settlement_axes": {"attribution_scoped_flatness": "failed"},
}

OPTIONS_SPEC = {
    "expected_requests": 2,
    "expected_status_sequence": ["executed", "executed"],
    "requires_option_close": True,
    "expected_orders": 2,
    "final_marker": "structure closed with no outstanding work",
    "expected_settlement_axes": {
        "attribution_scoped_flatness": "satisfied",
        "terminal_domain_state": "satisfied",
        "no_live_evaluation_authority": "satisfied",
    },
}


def test_the_holding_scenario_passes_on_evidence():
    module = _load_assertions()
    result = module.assert_scenario("index_indicator", HOLDING_SPEC, _evidence(), SUPERVISOR)
    assert result["ok"] is True, result["failures"]
    assert result["axes"]["settlement_axes"]["attribution_scoped_flatness"] == "failed"


def test_a_child_that_was_stopped_is_not_evidence():
    module = _load_assertions()
    stopped = {"outcome": "stop_requested", "exit_code": None, "stop": "terminated"}
    result = module.assert_scenario("index_indicator", HOLDING_SPEC, _evidence(), stopped)
    assert result["ok"] is False
    assert any("never exited on its own" in failure for failure in result["failures"])


def test_a_nonzero_child_exit_is_not_evidence():
    module = _load_assertions()
    result = module.assert_scenario(
        "index_indicator",
        HOLDING_SPEC,
        _evidence(),
        {"outcome": "exited", "exit_code": 2, "stop": "exited"},
    )
    assert result["ok"] is False
    assert any("exited 2 instead of 0" in failure for failure in result["failures"])


def test_a_missing_final_marker_is_not_evidence():
    module = _load_assertions()
    result = module.assert_scenario(
        "index_indicator", HOLDING_SPEC, _evidence(child_log="nothing happened"), SUPERVISOR
    )
    assert result["ok"] is False
    assert any("final marker" in failure for failure in result["failures"])


def test_missing_settlement_assessment_is_a_failure():
    module = _load_assertions()
    result = module.assert_scenario(
        "index_indicator", HOLDING_SPEC, _evidence(settlement=None), SUPERVISOR
    )
    assert result["ok"] is False
    assert any("four-axis settlement assessment was not collected" in f for f in result["failures"])


def test_open_exposure_that_differs_from_the_target_is_a_failure():
    module = _load_assertions()
    evidence = _evidence(
        attributed_positions=[
            {"tradingsymbol": "RELIANCE", "net_quantity": 0, "projection_version": 3}
        ]
    )
    result = module.assert_scenario("index_indicator", HOLDING_SPEC, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("instead of 5" in failure for failure in result["failures"])


def test_a_book_that_disagrees_with_the_paper_orders_is_a_failure():
    """A prior attempt's book that no order explains must not pass."""
    module = _load_assertions()
    evidence = _evidence(
        attributed_positions=[
            {"tradingsymbol": "RELIANCE", "net_quantity": 15, "projection_version": 3}
        ]
    )
    spec = dict(HOLDING_SPEC, expected_positions={"RELIANCE": 15})
    result = module.assert_scenario("index_indicator", spec, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("paper orders net to 5" in failure for failure in result["failures"])


def test_an_order_before_the_owner_decision_is_a_failure():
    module = _load_assertions()
    result = module.assert_scenario(
        "index_indicator", HOLDING_SPEC, _evidence(orders_before_approval=1), SUPERVISOR
    )
    assert result["ok"] is False
    assert any("before the owner decision" in failure for failure in result["failures"])


def test_a_manual_request_never_observed_waiting_is_a_failure():
    module = _load_assertions()
    evidence = _evidence()
    evidence.pop("orders_before_approval")
    result = module.assert_scenario("index_indicator", HOLDING_SPEC, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("never observed waiting" in failure for failure in result["failures"])


def test_an_unpublished_book_is_a_failure():
    module = _load_assertions()
    result = module.assert_scenario(
        "index_indicator", HOLDING_SPEC, _evidence(attributed_positions=[]), SUPERVISOR
    )
    assert result["ok"] is False
    assert any("attributed position RELIANCE" in f for f in result["failures"])


def _options_evidence(**overrides):
    evidence = {
        "requests": [
            {"request_id": "r1", "status": "executed"},
            {"request_id": "r2", "status": "executed"},
        ],
        "execution_events": [
            {"event": "submitted", "step_no": 1, "filled_quantity": 50},
            {"event": "submitted", "step_no": 2, "filled_quantity": 50},
        ],
        "paper_orders": [
            {"order_id": "o1", "tradingsymbol": "CE22500", "transaction_type": "BUY", "quantity": 50},
            {"order_id": "o2", "tradingsymbol": "CE22500", "transaction_type": "SELL", "quantity": 50},
        ],
        "attributed_positions": [],
        "child_log": "structure closed with no outstanding work (runs ['opt-1'] are closed)",
        "settlement": _axes(),
        "option_runs": [
            {"option_run_id": "opt-1", "phase": "entry", "run_status": "settled"},
            {"option_run_id": "opt-1", "phase": "exit", "run_status": "settled"},
        ],
    }
    evidence.update(overrides)
    return evidence


def test_the_closed_options_scenario_passes_on_evidence():
    module = _load_assertions()
    result = module.assert_scenario(
        "options_adjustment", OPTIONS_SPEC, _options_evidence(), SUPERVISOR
    )
    assert result["ok"] is True, result["failures"]
    assert result["axes"]["option_runs_closed"] == 1


def test_a_duplicated_close_is_a_failure():
    """The 'no change' rule: a repeat observation must not add a third request or
    a second close edge."""
    module = _load_assertions()
    evidence = _options_evidence()
    evidence["option_runs"].append(
        {"option_run_id": "opt-1", "phase": "exit", "run_status": "settled"}
    )
    evidence["requests"].append({"request_id": "r3", "status": "executed"})
    result = module.assert_scenario("options_adjustment", OPTIONS_SPEC, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("must not duplicate the adjustment" in f for f in result["failures"])
    assert any("expected exactly 2 requests" in f for f in result["failures"])


def test_an_open_option_run_is_a_failure():
    module = _load_assertions()
    evidence = _options_evidence()
    evidence["option_runs"][0]["run_status"] = "entered"
    result = module.assert_scenario("options_adjustment", OPTIONS_SPEC, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("was never closed" in failure for failure in result["failures"])


def test_closed_options_still_require_the_four_axes():
    module = _load_assertions()
    evidence = _options_evidence()
    evidence["settlement"] = _axes(terminal_domain_state="failed")
    result = module.assert_scenario("options_adjustment", OPTIONS_SPEC, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("terminal_domain_state" in failure for failure in result["failures"])


def test_a_deferral_scenario_passes_only_without_requests_and_a_named_reason():
    module = _load_assertions()
    spec = {"expects_deferral": True, "deferral_markers": ["no action: workbook"]}
    evidence = {
        "requests": [],
        "child_log": "no action: workbook already matches the target",
        "execution_events": [],
        "paper_orders": [],
        "attributed_positions": [],
    }
    result = module.assert_scenario("deferral", spec, evidence, SUPERVISOR)
    assert result["ok"] is True, result["failures"]

    evidence["requests"] = [{"request_id": "r1", "status": "executed"}]
    result = module.assert_scenario("deferral", spec, evidence, SUPERVISOR)
    assert result["ok"] is False
    assert any("must not submit anything" in failure for failure in result["failures"])
