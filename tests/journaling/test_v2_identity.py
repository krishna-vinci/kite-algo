from decimal import Decimal

from journaling.v2.identity import (
    is_low_confidence_resolution,
    normalize_identity_key,
    resolve_strategy_identity,
    unresolved_reason_for_identity,
)


def test_explicit_template_id_resolves_exact_high_confidence() -> None:
    resolved = resolve_strategy_identity(
        template_id="tmpl-explicit-1",
        strategy_family="options_strategy",
        strategy_name="My Strategy",
    )

    assert resolved.template_id == "tmpl-explicit-1"
    assert resolved.resolution_method == "explicit_template_id"
    assert resolved.resolution_confidence == Decimal("1.0")
    assert resolved.ambiguous is False


def test_worker_template_id_resolves_with_high_confidence() -> None:
    resolved = resolve_strategy_identity(
        worker_template_id="worker-template-1",
        strategy_family="options_strategy",
    )

    assert resolved.template_id == "worker-template-1"
    assert resolved.resolution_method == "worker_template_id"
    assert resolved.resolution_confidence >= Decimal("0.95")
    assert resolved.ambiguous is False


def test_known_internal_source_maps_without_name_grouping() -> None:
    resolved = resolve_strategy_identity(
        source_system="option_strategy",
        strategy_family="options_strategy",
        strategy_name="Some Fancy Name",
    )

    assert resolved.template_id == "internal:option_strategy"
    assert resolved.resolution_method == "known_internal_source"
    assert resolved.resolution_confidence >= Decimal("0.90")
    assert resolved.ambiguous is False


def test_same_strategy_name_with_different_template_ids_remains_distinct() -> None:
    first = resolve_strategy_identity(template_id="tmpl-a", strategy_name="Shared Name")
    second = resolve_strategy_identity(template_id="tmpl-b", strategy_name="Shared Name")

    assert first.template_id == "tmpl-a"
    assert second.template_id == "tmpl-b"
    assert first.template_id != second.template_id


def test_missing_template_with_only_strategy_name_falls_back_to_legacy_name_and_ambiguous() -> None:
    resolved = resolve_strategy_identity(strategy_name="Scalper v2")

    assert resolved.template_id == "legacy-name:scalper-v2"
    assert resolved.resolution_method == "legacy_strategy_name"
    assert resolved.resolution_confidence <= Decimal("0.50")
    assert resolved.ambiguous is True


def test_scenario_key_becomes_variant_key() -> None:
    resolved = resolve_strategy_identity(template_id="tmpl-1", scenario_key="Scenario#One")

    assert resolved.variant_key == "scenario-one"


def test_config_hash_becomes_variant_key_when_scenario_missing() -> None:
    resolved = resolve_strategy_identity(template_id="tmpl-1", config_hash="ABCD_1234")

    assert resolved.variant_key == "abcd-1234"


def test_normalize_identity_key_strips_and_collapses_unsafe_characters() -> None:
    assert normalize_identity_key("  A/B\\C__D  ") == "a-b-c-d"
    assert normalize_identity_key("@@@") == "unknown"


def test_missing_template_id_is_low_confidence_and_unresolved_reason() -> None:
    resolved = resolve_strategy_identity(strategy_name="Legacy Scalper")

    assert is_low_confidence_resolution(resolved) is True
    assert unresolved_reason_for_identity(resolved) == "missing_template_id_strategy_name_only"
