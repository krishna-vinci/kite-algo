import sys
import importlib.util
from pathlib import Path

import pytest


SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

MODULE_PATH = SDK_ROOT / "kite_algo_worker" / "live_protection_certification.py"
SPEC = importlib.util.spec_from_file_location("live_protection_certification_testmod", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

choose_immediate_threshold = MODULE.choose_immediate_threshold
flatten_result_succeeded = MODULE.flatten_result_succeeded
normalize_leg_side = MODULE.normalize_leg_side
summarize_scenario_verdict = MODULE.summarize_scenario_verdict


def test_choose_immediate_threshold_for_stoploss_uses_current_loss_magnitude():
    assert choose_immediate_threshold(-5.0, rule_kind="stoploss") == 4.0


def test_choose_immediate_threshold_for_target_uses_current_profit_magnitude():
    assert choose_immediate_threshold(3.5, rule_kind="target") == 2.8


def test_choose_immediate_threshold_returns_none_for_wrong_pnl_sign():
    assert choose_immediate_threshold(1.0, rule_kind="stoploss") is None
    assert choose_immediate_threshold(-1.0, rule_kind="target") is None


def test_choose_immediate_threshold_honors_minimum_threshold():
    assert choose_immediate_threshold(-0.00001, rule_kind="stoploss") == 0.0001


def test_flatten_result_succeeded_accepts_known_success_statuses():
    assert flatten_result_succeeded({"status": "closed"}) is True
    assert flatten_result_succeeded({"status": "success"}) is True
    assert flatten_result_succeeded({}) is False
    assert flatten_result_succeeded({"status": "failed"}) is False


def test_normalize_leg_side_maps_long_short_and_quantity_fallback():
    assert normalize_leg_side("LONG") == "BUY"
    assert normalize_leg_side("SHORT") == "SELL"
    assert normalize_leg_side(None, net_quantity=1) == "BUY"
    assert normalize_leg_side(None, net_quantity=-1) == "SELL"


def test_summarize_scenario_verdict_passes_when_expected_rule_observed():
    verdict = summarize_scenario_verdict(
        "position stoploss",
        expected_rule="position_stoploss",
        observed_facts={
            "triggered_rule": "position_stoploss",
            "threshold_pct": 4.0,
            "run_status": "closed",
            "broker_flat": True,
            "worker_orders_visible": True,
            "worker_trades_visible": True,
        },
    )

    assert verdict.status == "passed"
    assert verdict.reason == "observed expected protection trigger and clean terminal exit"


def test_summarize_scenario_verdict_fails_when_triggered_but_not_flat():
    verdict = summarize_scenario_verdict(
        "position target",
        expected_rule="position_target",
        observed_facts={
            "triggered_rule": "position_target",
            "run_status": "closed",
            "broker_flat": False,
            "worker_orders_visible": True,
            "worker_trades_visible": True,
        },
    )

    assert verdict.status == "failed"
    assert "not confirmed flat" in verdict.reason


def test_summarize_scenario_verdict_fails_loudly_when_flatten_confirmation_missing():
    verdict = summarize_scenario_verdict(
        "basket target",
        expected_rule="basket_target",
        observed_facts={
            "triggered_rule": "basket_target",
            "flatten_required": True,
            "flatten_confirmed": False,
        },
    )

    assert verdict.status == "failed"
    assert "KITE_ALGO_CONFIRM_FLATTEN=YES" in verdict.reason


def test_summarize_scenario_verdict_fails_when_flatten_attempt_did_not_succeed():
    verdict = summarize_scenario_verdict(
        "worker stale",
        expected_rule="worker_stale",
        observed_facts={
            "triggered_rule": "worker_stale",
            "flatten_required": True,
            "flatten_confirmed": True,
            "flatten_attempted": True,
            "flatten_result": {"status": "failed"},
        },
    )

    assert verdict.status == "failed"
    assert "did not report a successful terminal status" in verdict.reason


def test_summarize_scenario_verdict_fails_when_cleanup_safety_check_failed():
    verdict = summarize_scenario_verdict(
        "worker stale",
        expected_rule="worker_stale",
        observed_facts={"cleanup_error": "pnl lookup failed", "triggered_rule": "worker_stale"},
    )

    assert verdict.status == "failed"
    assert "cleanup safety check failed" in verdict.reason


def test_summarize_patch_mutability_verdict_passes_when_generation_and_version_advance():
    verdict = summarize_scenario_verdict(
        "protection patch mutability",
        observed_facts={
            "version_before": 1,
            "version_after": 2,
            "generation_before": 1,
            "generation_after": 2,
        },
    )

    assert verdict.status == "passed"
    assert "advanced runtime generation/version" in verdict.reason


def test_summarize_patch_mutability_verdict_fails_when_version_does_not_advance():
    verdict = summarize_scenario_verdict(
        "protection patch mutability",
        observed_facts={
            "version_before": 1,
            "version_after": 1,
            "generation_before": 1,
            "generation_after": 1,
        },
    )

    assert verdict.status == "failed"
    assert "did not advance runtime generation/version" in verdict.reason
