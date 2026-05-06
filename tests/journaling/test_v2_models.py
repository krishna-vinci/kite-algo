from decimal import Decimal

import pytest
from pydantic import ValidationError

from journaling.models import (
    CostBreakdown,
    JournalEpisode,
    JournalEpisodeStatus,
    JournalEnvironmentMode,
    JournalExecutionContext,
    JournalExecutionEnvironment,
    JournalExecutionFact,
    JournalExecutionIntent,
    JournalIntentChannel,
    JournalMetricSnapshot,
    SourceType,
    JournalStrategyDeployment,
    JournalStrategyTemplate,
    JournalStrategyVariant,
)


def test_valid_paper_environment_model_accepts_required_fields() -> None:
    model = JournalExecutionEnvironment(
        mode=JournalEnvironmentMode.PAPER,
        account_scope="kite:paper-e2e",
        paper_account_key="kite:paper-e2e",
        environment_epoch=1,
    )

    assert model.mode == JournalEnvironmentMode.PAPER
    assert model.account_scope == "kite:paper-e2e"
    assert model.paper_account_key == "kite:paper-e2e"
    assert model.environment_epoch == 1


def test_environment_model_rejects_blank_account_scope() -> None:
    with pytest.raises(ValidationError):
        JournalExecutionEnvironment(
            mode=JournalEnvironmentMode.PAPER,
            account_scope="   ",
            paper_account_key="kite:paper-e2e",
            environment_epoch=1,
        )


def test_environment_model_rejects_epoch_zero() -> None:
    with pytest.raises(ValidationError):
        JournalExecutionEnvironment(
            mode=JournalEnvironmentMode.LIVE,
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
            environment_epoch=0,
        )


def test_valid_strategy_template_variant_deployment_context_chain() -> None:
    template = JournalStrategyTemplate(
        id="tmpl-1",
        strategy_family="options_strategy",
        template_key="template-alpha",
        display_name="Template Alpha",
    )
    variant = JournalStrategyVariant(
        id="var-1",
        template_id=template.id or "tmpl-1",
        variant_key="variant-a",
        display_name="Variant A",
    )
    deployment = JournalStrategyDeployment(
        id="dep-1",
        template_id=template.id or "tmpl-1",
        variant_id=variant.id,
        deployment_key="deploy-live-1",
        display_name="Live Deployment",
    )
    context = JournalExecutionContext(
        environment_id="env-1",
        source_system="algo_worker",
        external_run_id="run-1",
        strategy_template_id=template.id,
        strategy_variant_id=variant.id,
        strategy_deployment_id=deployment.id,
    )

    assert template.template_key == "template-alpha"
    assert variant.template_id == "tmpl-1"
    assert deployment.template_id == "tmpl-1"
    assert context.external_run_id == "run-1"


def test_strategy_template_rejects_blank_template_key() -> None:
    with pytest.raises(ValidationError):
        JournalStrategyTemplate(strategy_family="options_strategy", template_key="   ")


def test_execution_context_rejects_blank_external_run_id() -> None:
    with pytest.raises(ValidationError):
        JournalExecutionContext(
            environment_id="env-1",
            source_system="algo_worker",
            external_run_id=" ",
        )


def test_episode_rejects_zero_sequence() -> None:
    with pytest.raises(ValidationError):
        JournalEpisode(
            environment_id="env-1",
            execution_context_id="ctx-1",
            episode_seq=0,
        )


def test_execution_intent_idempotency_key_validation() -> None:
    with pytest.raises(ValidationError):
        JournalExecutionIntent(
            environment_id="env-1",
            idempotency_key="   ",
        )

    model = JournalExecutionIntent(environment_id="env-1", idempotency_key=None)
    assert model.idempotency_key is None


def test_cost_breakdown_derives_totals_when_zero() -> None:
    model = CostBreakdown(
        brokerage=Decimal("10.5"),
        exchange_txn_charge=Decimal("1.5"),
        stt=Decimal("2"),
        stamp_duty=Decimal("0.5"),
        sebi_charge=Decimal("0.1"),
        gst=Decimal("2.2"),
    )

    assert model.total_taxes == Decimal("4.8")
    assert model.total_charges == Decimal("16.8")


def test_execution_fact_accepts_nullable_itemized_cost_fields() -> None:
    model = JournalExecutionFact(
        run_id="run-1",
        source_type=SourceType.PAPER_TRADE,
        source_fact_key="fact-1",
        side="buy",
        quantity=1,
        price=Decimal("100"),
        brokerage=None,
        exchange_txn_charge=None,
        stt=None,
        stamp_duty=None,
        sebi_charge=None,
        gst=None,
        margin_required=None,
        charges_status=None,
    )

    assert model.brokerage is None
    assert model.exchange_txn_charge is None
    assert model.stt is None
    assert model.stamp_duty is None
    assert model.sebi_charge is None
    assert model.gst is None
    assert model.margin_required is None
    assert model.charges_status is None


def test_episode_notes_default_to_empty_string() -> None:
    model = JournalEpisode(
        environment_id="env-1",
        execution_context_id="ctx-1",
        episode_seq=1,
    )

    assert model.notes == ""


def test_v2_enums_serialize_as_strings_with_json_dump() -> None:
    env = JournalExecutionEnvironment(
        mode=JournalEnvironmentMode.PAPER,
        account_scope="kite:paper-e2e",
        paper_account_key="kite:paper-e2e",
        environment_epoch=1,
    )
    episode = JournalEpisode(
        environment_id="env-1",
        execution_context_id="ctx-1",
        episode_seq=1,
        status=JournalEpisodeStatus.OPEN,
    )
    intent = JournalExecutionIntent(
        environment_id="env-1",
        channel=JournalIntentChannel.ENTRY,
        idempotency_key="idem-1",
    )

    assert env.model_dump(mode="json")["mode"] == "paper"
    assert episode.model_dump(mode="json")["status"] == "open"
    assert intent.model_dump(mode="json")["channel"] == "entry"


def test_metric_snapshot_includes_v2_partition_defaults() -> None:
    snapshot = JournalMetricSnapshot(
        subject_type="strategy_template",
        subject_id="tmpl-1",
        window="since_inception",
        calc_version="journal_v2_metrics_v1",
    )

    assert snapshot.environment_id is None
    assert snapshot.identity_rule_version == "v1_legacy"
    assert snapshot.grouping_rule_version == "v1_legacy"
