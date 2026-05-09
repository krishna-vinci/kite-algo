from __future__ import annotations

from typing import Any, cast

from backend.algo_runtime.execution_attribution import build_execution_attribution
from backend.journaling.service import JournalService


class _FakeJournalV2Repository:
    def __init__(self) -> None:
        self._env_by_key: dict[tuple[Any, ...], str] = {}
        self._template_by_key: dict[str, str] = {}
        self._variant_by_key: dict[tuple[str, str], str] = {}
        self._deployment_by_key: dict[tuple[str, str], str] = {}
        self._context_by_key: dict[tuple[str, str, str], str] = {}

        self._env_seq = 0
        self._template_seq = 0
        self._variant_seq = 0
        self._deployment_seq = 0
        self._context_seq = 0

    def ensure_execution_environment(
        self,
        *,
        mode: str,
        account_scope: str,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int = 1,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        key = (mode, account_scope, broker_user_id or "", paper_account_key or "", environment_epoch)
        if key not in self._env_by_key:
            self._env_seq += 1
            self._env_by_key[key] = f"env-{self._env_seq}"
        return self._env_by_key[key]

    def ensure_strategy_template(
        self,
        *,
        template_key: str,
        strategy_family: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if template_key not in self._template_by_key:
            self._template_seq += 1
            self._template_by_key[template_key] = f"tmpl-ref-{self._template_seq}"
        return self._template_by_key[template_key]

    def ensure_strategy_variant(
        self,
        *,
        template_id: str,
        variant_key: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        key = (template_id, variant_key)
        if key not in self._variant_by_key:
            self._variant_seq += 1
            self._variant_by_key[key] = f"variant-{self._variant_seq}"
        return self._variant_by_key[key]

    def ensure_strategy_deployment(
        self,
        *,
        template_id: str,
        deployment_key: str,
        variant_id: str | None = None,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        key = (template_id, deployment_key)
        if key not in self._deployment_by_key:
            self._deployment_seq += 1
            self._deployment_by_key[key] = f"deploy-{self._deployment_seq}"
        return self._deployment_by_key[key]

    def ensure_execution_context(
        self,
        *,
        environment_id: str,
        source_system: str,
        external_run_id: str,
        template_id: str | None = None,
        variant_id: str | None = None,
        deployment_id: str | None = None,
        raw_identity: dict[str, Any] | None = None,
        resolved_identity: dict[str, Any] | None = None,
        resolution_method: str | None = None,
        resolution_confidence: Any | None = None,
        identity_rule_version: str = "journal_v2_identity_v1",
        status: str = "open",
        started_at: Any | None = None,
        ended_at: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        key = (environment_id, source_system, external_run_id)
        if key not in self._context_by_key:
            self._context_seq += 1
            self._context_by_key[key] = f"ctx-{self._context_seq}"
        return self._context_by_key[key]

    def create_unresolved_item(self, **_kwargs: Any) -> str:
        return "unresolved-1"


def test_worker_context_same_run_id_live_vs_paper_separates_environment_and_context() -> None:
    repository = _FakeJournalV2Repository()
    service = JournalService(repository=cast(Any, repository))

    live = service.ensure_v2_worker_context(
        execution_mode="live",
        account_scope="kite:AB1234",
        strategy_run_id="run-1",
        template_id="worker-template-1",
        strategy_name="Mean Reversion",
        strategy_family="indicator_strategy",
    )
    paper = service.ensure_v2_worker_context(
        execution_mode="paper",
        account_scope="kite:paper-a",
        strategy_run_id="run-1",
        template_id="worker-template-1",
        strategy_name="Mean Reversion",
        strategy_family="indicator_strategy",
    )

    assert live["environment_id"] != paper["environment_id"]
    assert live["execution_context_id"] != paper["execution_context_id"]


def test_same_strategy_name_with_different_template_ids_creates_distinct_templates() -> None:
    repository = _FakeJournalV2Repository()
    service = JournalService(repository=cast(Any, repository))

    first = service.ensure_v2_worker_context(
        execution_mode="paper",
        account_scope="kite:paper-a",
        strategy_run_id="run-a",
        template_id="template-a",
        strategy_name="Shared Name",
        strategy_family="indicator_strategy",
    )
    second = service.ensure_v2_worker_context(
        execution_mode="paper",
        account_scope="kite:paper-a",
        strategy_run_id="run-b",
        template_id="template-b",
        strategy_name="Shared Name",
        strategy_family="indicator_strategy",
    )

    assert first["template_id"] != second["template_id"]


def test_missing_template_with_name_only_returns_ambiguous_resolution() -> None:
    repository = _FakeJournalV2Repository()
    service = JournalService(repository=cast(Any, repository))

    result = service.ensure_v2_worker_context(
        execution_mode="paper",
        account_scope="kite:paper-a",
        strategy_run_id="run-name-only",
        strategy_name="Scalper v2",
        strategy_family="indicator_strategy",
    )

    assert result["ambiguous"] is True
    assert result["resolution_method"] == "legacy_strategy_name"
    assert result["template_id"] is None
    assert result["identity_rule_version"] == "journal_v2_identity_v1"
    assert result["grouping_rule_version"] == "journal_v2_grouping_v1"


def test_execution_attribution_preserves_v2_fields_and_protects_canonical_identity() -> None:
    payload = build_execution_attribution(
        execution_mode="paper",
        strategy_run_id="run-123",
        strategy_family="indicator_strategy",
        strategy_name="Mean Reversion",
        account_ref="kite:paper-a",
        entry_surface="algo_worker",
        source="algo_worker",
        idempotency_key="idem-123",
        metadata={
            "strategy_run_id": "evil-run",
            "execution_mode": "live",
            "account_ref": "kite:AB1234",
            "account_scope": "kite:AB1234",
            "template_id": "tmpl-meta",
            "scenario_key": "paper-scenario",
            "deployment_key": "deploy-paper",
        },
        extras={
            "worker_template_id": "worker-template-1",
            "scenario_name": "Paper Scenario",
            "config_hash": "abc123",
            "source_system": "algo_worker",
            "tags": ["worker", "paper"],
            "execution_mode": "live",
            "account_scope": "kite:AB1234",
            "strategy_run_id": "evil-run-2",
        },
    )

    assert payload["strategy_run_id"] == "run-123"
    assert payload["execution_mode"] == "paper"
    assert payload["account_ref"] == "kite:paper-a"
    assert payload["account_scope"] == "kite:paper-a"
    assert payload["metadata"]["strategy_run_id"] == "run-123"
    assert payload["metadata"]["execution_mode"] == "paper"
    assert payload["metadata"]["account_scope"] == "kite:paper-a"

    assert payload["template_id"] == "tmpl-meta"
    assert payload["worker_template_id"] == "worker-template-1"
    assert payload["scenario_key"] == "paper-scenario"
    assert payload["scenario_name"] == "Paper Scenario"
    assert payload["deployment_key"] == "deploy-paper"
    assert payload["config_hash"] == "abc123"
    assert payload["source_system"] == "algo_worker"
    assert payload["tags"] == ["worker", "paper"]
