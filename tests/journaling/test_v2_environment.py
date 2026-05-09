import re
from pathlib import Path


SCHEMA_SQL = Path("backend/schema.sql").read_text()


def _assert_has(pattern: str) -> None:
    assert re.search(pattern, SCHEMA_SQL, flags=re.IGNORECASE | re.DOTALL), pattern


def test_v2_required_table_names_exist() -> None:
    required_tables = [
        "journal_execution_environments",
        "journal_strategy_templates",
        "journal_strategy_variants",
        "journal_strategy_deployments",
        "journal_execution_contexts",
        "journal_episodes",
        "journal_episode_legs",
        "journal_execution_intents",
    ]
    for table_name in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS public.{table_name}" in SCHEMA_SQL


def test_environment_mode_check_includes_live_paper_dry_run_preview() -> None:
    _assert_has(
        r"journal_execution_environments\s*\(.*?"
        r"mode\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*mode\s+IN\s*\(\s*'live'\s*,\s*'paper'\s*,\s*'dry_run_preview'\s*\)\s*\)"
    )


def test_environment_identity_uses_expression_unique_index_with_coalesce() -> None:
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_execution_environments_identity\s+"
        r"ON\s+public\.journal_execution_environments\s*\(\s*"
        r"mode\s*,\s*account_scope\s*,\s*COALESCE\(broker_user_id\s*,\s*''\)\s*,\s*COALESCE\(paper_account_key\s*,\s*''\)\s*,\s*environment_epoch\s*\)"
    )


def test_context_episode_intent_uniqueness_indexes_exist() -> None:
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_execution_contexts_environment_source_external\s+"
        r"ON\s+public\.journal_execution_contexts\s*\(\s*environment_id\s*,\s*source_system\s*,\s*external_run_id\s*\)"
    )
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_episodes_context_seq\s+"
        r"ON\s+public\.journal_episodes\s*\(\s*execution_context_id\s*,\s*episode_seq\s*\)"
    )
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_execution_intents_environment_idempotency\s+"
        r"ON\s+public\.journal_execution_intents\s*\(\s*environment_id\s*,\s*idempotency_key\s*\)\s*"
        r"WHERE\s+idempotency_key\s+IS\s+NOT\s+NULL"
    )


def test_journal_execution_facts_v2_columns_added() -> None:
    _assert_has(r"ALTER\s+TABLE\s+public\.journal_execution_facts\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+environment_id\s+UUID")
    _assert_has(r"ALTER\s+TABLE\s+public\.journal_execution_facts\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+episode_id\s+UUID")
    _assert_has(r"ALTER\s+TABLE\s+public\.journal_execution_facts\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+intent_id\s+UUID")
    _assert_has(r"ALTER\s+TABLE\s+public\.journal_execution_facts\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+position_effect\s+TEXT")
    _assert_has(
        r"journal_execution_facts_position_effect_chk.*?position_effect\s+IS\s+NULL\s+OR\s+position_effect\s+IN\s*\(\s*'open'\s*,\s*'add'\s*,\s*'reduce'\s*,\s*'close'\s*,\s*'flip'\s*\)"
    )


def test_metric_snapshot_v2_rule_version_columns_added() -> None:
    _assert_has(r"ALTER\s+TABLE\s+public\.journal_metric_snapshots\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+environment_id\s+UUID")
    _assert_has(
        r"ALTER\s+TABLE\s+public\.journal_metric_snapshots\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+identity_rule_version\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'v1_legacy'"
    )
    _assert_has(
        r"ALTER\s+TABLE\s+public\.journal_metric_snapshots\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+grouping_rule_version\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'v1_legacy'"
    )
    _assert_has(r"DROP\s+INDEX\s+IF\s+EXISTS\s+public\.ux_journal_metric_snapshots_subject_window_version")
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_metric_snapshots_legacy_subject_window_version\s+"
        r"ON\s+public\.journal_metric_snapshots\s*\(\s*subject_type\s*,\s*subject_id\s*,\s*time_window\s*,\s*calc_version\s*\)\s*"
        r"WHERE\s+environment_id\s+IS\s+NULL"
    )
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_metric_snapshots_v2_environment_subject_window_version\s+"
        r"ON\s+public\.journal_metric_snapshots\s*\(\s*"
        r"environment_id\s*,\s*subject_type\s*,\s*subject_id\s*,\s*time_window\s*,\s*calc_version\s*,\s*identity_rule_version\s*,\s*grouping_rule_version\s*\)\s*"
        r"WHERE\s+environment_id\s+IS\s+NOT\s+NULL"
    )


def test_source_links_allow_paper_strategy_run() -> None:
    _assert_has(r"journal_source_links_source_type_chk")
    _assert_has(r"'paper_strategy_run'")


def test_v2_lookup_indexes_exist() -> None:
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_execution_environments_mode_scope\s+"
        r"ON\s+public\.journal_execution_environments\s*\(\s*mode\s*,\s*account_scope\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_episodes_environment_status_opened\s+"
        r"ON\s+public\.journal_episodes\s*\(\s*environment_id\s*,\s*status\s*,\s*opened_at\s+DESC\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_execution_facts_environment_episode_fill_time\s+"
        r"ON\s+public\.journal_execution_facts\s*\(\s*environment_id\s*,\s*episode_id\s*,\s*fill_timestamp\s+DESC\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_metric_snapshots_environment_subject_window_version\s+"
        r"ON\s+public\.journal_metric_snapshots\s*\(\s*"
        r"environment_id\s*,\s*subject_type\s*,\s*subject_id\s*,\s*time_window\s*,\s*calc_version\s*,\s*identity_rule_version\s*,\s*grouping_rule_version\s*\)"
    )
