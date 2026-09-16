"""Unit tests for hosted-strategy validation and child-token composition.

These pin the properties that must hold BEFORE any execution exists:

- parameter schemas are validated (invalid schema rejected) and remote ``$ref``
  is refused;
- parameter values are validated against the schema;
- strategy source is length-checked and hashed but never executed;
- a child run token can NEVER hold ``heartbeat`` (lifecycle is supervisor-only);
- the policy snapshot carries the effective max duration and progress deadline.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.strategies import service  # noqa: E402


VALID_SCHEMA = {
    "type": "object",
    "properties": {"lots": {"type": "integer", "minimum": 1}},
    "required": ["lots"],
    "additionalProperties": False,
}


def test_valid_schema_and_params_round_trip():
    schema = service.validate_parameters_schema(VALID_SCHEMA)
    assert schema["type"] == "object"
    assert service.validate_parameters(VALID_SCHEMA, {"lots": 2}) == {"lots": 2}


def test_invalid_schema_is_rejected():
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema({"type": "not-a-real-type"})


def test_non_object_schema_is_rejected():
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema(["not", "an", "object"])  # type: ignore[arg-type]


def test_remote_ref_is_rejected():
    with pytest.raises(service.StrategyValidationError) as excinfo:
        service.validate_parameters_schema({"$ref": "https://example.com/schema.json"})
    assert "remote $ref" in str(excinfo.value)


def test_local_ref_is_allowed():
    schema = {
        "$defs": {"lot": {"type": "integer"}},
        "type": "object",
        "properties": {"lots": {"$ref": "#/$defs/lot"}},
    }
    assert service.validate_parameters_schema(schema)["type"] == "object"


def test_dynamic_ref_and_remote_base_are_rejected():
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema({"$dynamicRef": "https://evil.example/s.json"})
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema({"$id": "https://evil.example/s.json", "type": "object"})
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema({"$recursiveRef": "#"})


def test_local_ref_resolves_without_network():
    schema = {
        "$defs": {"lots": {"type": "integer"}},
        "type": "object",
        "properties": {"lots": {"$ref": "#/$defs/lots"}},
        "required": ["lots"],
    }
    assert service.validate_parameters(schema, {"lots": 5}) == {"lots": 5}


def test_self_recursive_schema_fails_boundedly():
    # A cyclic schema must surface as a validation error, not crash the caller.
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters({"$ref": "#"}, {})


def test_schedule_validation_bounds():
    daily = service.validate_schedule(schedule_kind="daily", at_time="09:15")
    assert daily["at_time"] == "09:15" and daily["weekday"] is None
    weekly = service.validate_schedule(schedule_kind="weekly", at_time="09:15", weekday=0)
    assert weekly["weekday"] == 0
    with pytest.raises(service.StrategyValidationError):
        service.validate_schedule(schedule_kind="weekly", at_time="09:15")  # weekday required
    with pytest.raises(service.StrategyValidationError):
        service.validate_schedule(schedule_kind="daily", at_time="session_close")  # deferred
    with pytest.raises(service.StrategyValidationError):
        service.validate_schedule(schedule_kind="daily", at_time="9:15")  # not HH:MM
    with pytest.raises(service.StrategyValidationError):
        service.validate_schedule(schedule_kind="monthly", at_time="09:15")  # unsupported


def test_invalid_params_are_rejected_with_location():
    with pytest.raises(service.StrategyValidationError) as excinfo:
        service.validate_parameters(VALID_SCHEMA, {"lots": 0})
    assert "invalid parameters" in str(excinfo.value)


def test_oversized_schema_is_rejected():
    huge = {"type": "object", "const": "x" * (service.MAX_SCHEMA_BYTES + 1)}
    with pytest.raises(service.StrategyValidationError):
        service.validate_parameters_schema(huge)


def test_source_is_hashed_not_executed():
    # A source that would do damage if imported/executed is only hashed.
    source = "import sys\nraise SystemExit('should never run')\n"
    stored, digest = service.validate_source(source)
    assert stored == source
    assert len(digest) == 64


def test_empty_and_oversized_source_are_rejected():
    with pytest.raises(service.StrategyValidationError):
        service.validate_source("   ")
    with pytest.raises(service.StrategyValidationError):
        service.validate_source("x" * (service.MAX_SOURCE_BYTES + 1))


def test_child_token_excludes_heartbeat():
    with pytest.raises(service.StrategyValidationError) as excinfo:
        service.validate_child_token_actions(["runs:read", "heartbeat"])
    assert "heartbeat" in str(excinfo.value)


def test_child_token_composition_order_and_notify():
    order = service.child_run_token_actions(order_capable=True)
    assert "intents:submit" in order and "heartbeat" not in order
    notify = service.child_run_token_actions(notify=True)
    assert notify == ["notifications:publish", "runs:log", "runs:progress", "runs:read"]
    assert "intents:submit" not in notify


def test_child_token_rejects_unknown_action():
    with pytest.raises(service.StrategyValidationError):
        service.validate_child_token_actions(["runs:read", "orders:submit"])


def test_policy_snapshot_carries_effective_values():
    policy = service.build_policy_snapshot(
        stale_exit_policy="exit_on_worker_stale", max_duration_s=21600, progress_deadline_s=600
    )
    assert policy["max_duration_s"] == 21600
    assert policy["progress_deadline_s"] == 600
    assert policy["stale_exit_policy"] == "exit_on_worker_stale"


def test_policy_snapshot_requires_explicit_values():
    with pytest.raises(service.StrategyValidationError):
        service.build_policy_snapshot(
            stale_exit_policy="none", max_duration_s=0, progress_deadline_s=600
        )
    with pytest.raises(service.StrategyValidationError):
        service.build_policy_snapshot(
            stale_exit_policy="maybe", max_duration_s=600, progress_deadline_s=600
        )


def test_account_scope_mode_consistency():
    # Parsing is not authorization; this only checks mode/scope consistency.
    assert service.validate_account_scope("kite:paper", "paper") == "kite:paper"
    with pytest.raises(service.StrategyValidationError):
        service.validate_account_scope("kite:live-account", "paper")


def test_template_and_id_helpers():
    sid = service.new_strategy_id()
    assert sid.startswith("hs_")
    assert service.template_id_for(sid) == f"hosted:{sid}"
