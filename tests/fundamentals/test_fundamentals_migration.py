"""Contract tests for the fundamentals natural-key hardening migration."""

from importlib import import_module


def test_nullable_period_natural_key_migration(monkeypatch):
    migration = import_module(
        "backend.alembic.versions.20260905_000010_fundamentals_metric_natural_key"
    )
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    sql = "\n".join(statements).upper()
    assert migration.down_revision == "20260905_000009"
    assert "ROW_NUMBER() OVER" in sql
    assert "DELETE FROM PUBLIC.FUNDAMENTALS_METRICS" in sql
    assert "UNIQUE NULLS NOT DISTINCT" in sql
    for column in ("SYMBOL", "STATEMENT_SCOPE", "DATASET", "PERIOD_END", "METRIC_KEY"):
        assert column in sql

