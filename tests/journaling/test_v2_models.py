import pytest
from pydantic import ValidationError

from journaling.models import (
    JournalEpisode,
    JournalEpisodeStatus,
    JournalEnvironmentMode,
    JournalExecutionContext,
    JournalExecutionEnvironment,
    JournalExecutionIntent,
    JournalIntentChannel,
    JournalMetricSnapshot,
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
