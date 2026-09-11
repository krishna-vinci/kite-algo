"""Screener-backed universes through the API, on real PostgreSQL.

Repairs and proves a shipped Phase 3 defect: the code has supported
``kind='screener'`` (dynamic universes fed by screener results) since Phase 3,
but migration ``20260909_000014`` declared
``CHECK (kind IN ('explicit','index','portfolio'))`` — and the ORM declared no
CHECK at all, so SQLite unit tests never noticed. Creating a screener-backed
universe therefore failed on real PostgreSQL.

Three installation lifecycles are exercised separately, because they prove
different things:

- **upgraded**  — an existing database migrated to head (the ALTER path);
- **fresh**    — a database migrated from ZERO by Alembic: the frozen baseline
  (``backend/alembic/baseline_schema.sql``) plus the whole chain. This is the
  path that defect D-2 blocked and that is now an executed assertion;
- **schema.sql** — a database built from the evolving reference DDL, which a
  fresh Alembic run does NOT independently verify.

Set the URLs to enable; each unset database is skipped with its reason rather
than silently passing:

    ALERTS_TEST_DATABASE_URL=...            (upgraded)
    ALERTS_FRESH_DATABASE_URL=...           (fresh from zero via Alembic)
    ALERTS_SCHEMA_DATABASE_URL=...          (built from backend/schema.sql)
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

UPGRADED_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")
FRESH_URL = os.environ.get("ALERTS_FRESH_DATABASE_URL", "")
SCHEMA_URL = os.environ.get("ALERTS_SCHEMA_DATABASE_URL", "")

PHASE4_TABLES = (
    "alert_breadth_state",
    "alert_breadth_triggers",
    "alert_session_counters",
    "alert_suppression_counters",
    "external_signal_producers",
    "external_signal_producer_credentials",
    "external_signal_values",
)


def _engine(url):
    return create_engine(url, poolclass=NullPool)


def _constraint_definition(engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'universes'::regclass AND contype = 'c'"
        )).fetchall()
    return " ".join(str(value) for (value,) in row)


def _assert_check_admits_screener(engine) -> None:
    """The constraint itself must admit 'screener' — not merely the insert."""
    definition = _constraint_definition(engine)
    assert "screener" in definition, (
        "universes_kind_check does not admit 'screener': " + definition
    )


def _assert_screener_universe_works(engine) -> None:
    """Create and resolve a screener-backed universe through the SQL surface.

    Exercises the same row shape the universe API writes, so a failure here is
    a genuine storage rejection rather than a test-fixture artifact.
    """
    import uuid

    universe_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO universes (id, owner_id, name, kind, source_config, enabled) "
            "VALUES (:id, 'owner-p4', :name, 'screener', "
            "        '{\"workflow\": \"p4-screener\", \"top_n\": 5}'::jsonb, true)"
        ), {"id": universe_id, "name": f"p4-screen-{universe_id[:8]}"})
        conn.execute(text(
            "INSERT INTO universe_revisions "
            "(id, universe_id, revision, expression, members, member_count, coverage) "
            "VALUES (:rid, :uid, 1, '{}'::jsonb, ARRAY['NSE:A','NSE:B'], 2, '{}'::jsonb)"
        ), {"rid": str(uuid.uuid4()), "uid": universe_id})
    with engine.connect() as conn:
        kind = conn.execute(text(
            "SELECT kind FROM universes WHERE id = :id"
        ), {"id": universe_id}).scalar()
        members = conn.execute(text(
            "SELECT member_count FROM universe_revisions WHERE universe_id = :id"
        ), {"id": universe_id}).scalar()
    assert kind == "screener"
    assert members == 2
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM universes WHERE id = :id"), {"id": universe_id})


@pytest.mark.skipif(
    not UPGRADED_URL, reason="ALERTS_TEST_DATABASE_URL not set; upgraded-lifecycle check skipped"
)
def test_upgraded_database_admits_screener_universes():
    """An EXISTING database migrated to head: the ALTER path fixes it."""
    engine = _engine(UPGRADED_URL)
    try:
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert head >= "20260911_000016", f"database is at {head}, not at Phase 4 head"
        _assert_check_admits_screener(engine)
        _assert_screener_universe_works(engine)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not FRESH_URL, reason="ALERTS_FRESH_DATABASE_URL not set; fresh-lifecycle check skipped"
)
def test_fresh_database_upgrade_to_head_creates_the_full_schema():
    """A database migrated from ZERO reaches head with everything present.

    This is the executed form of what used to be a recorded skip: the frozen
    baseline (``backend/alembic/baseline_schema.sql``) plus the migration chain
    must build a complete schema on an empty database. The defect it repairs
    was that the baseline executed the EVOLVING ``schema.sql``, which by then
    contained statements owned by later migrations — so the chain aborted at
    the first one and no from-zero install was possible.
    """
    engine = _engine(FRESH_URL)
    try:
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert head == "20260911_000016", f"fresh database is at {head}, not at Phase 4 head"

        with engine.connect() as conn:
            missing = [
                name for name in PHASE4_TABLES
                if conn.execute(
                    text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
                ).scalar() is None
            ]
            # The alerts-platform core tables come from migrations 000011/000012
            # and must exist too — a baseline that over-reached would have
            # created them early and the chain would have failed instead.
            core_missing = [
                name for name in (
                    "workflows", "workflow_revisions", "alert_subscriptions",
                    "signal_events", "deliveries", "evaluation_checkpoints",
                    "evaluation_ownership", "universes", "screener_run",
                )
                if conn.execute(
                    text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
                ).scalar() is None
            ]
        assert missing == [], f"Phase 4 tables missing from a from-zero install: {missing}"
        assert core_missing == [], f"core tables missing from a from-zero install: {core_missing}"
        _assert_check_admits_screener(engine)
        _assert_screener_universe_works(engine)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not SCHEMA_URL,
    reason="ALERTS_SCHEMA_DATABASE_URL not set; reference-DDL check skipped",
)
def test_schema_sql_database_admits_screener_universes():
    """A database built from ``backend/schema.sql`` (the from-scratch path).

    A fresh Alembic run does NOT verify this file, so it is checked separately.

    The file is NOT standalone: it is a delta that assumes the alerts-platform
    core tables exist (it ALTERs ``signal_events``, which only migration
    20260908_000011 creates), so applying it to an empty database still reports
    those few pre-existing failures before reaching the Phase 4 section. That is
    why the frozen baseline exists separately: migrations build a database from
    zero, and this file is the reference DDL for the platform tables. The
    assertions below check that the Phase 4 section itself landed correctly.
    """
    engine = _engine(SCHEMA_URL)
    try:
        with engine.connect() as conn:
            missing = [
                name for name in PHASE4_TABLES
                if conn.execute(
                    text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
                ).scalar() is None
            ]
        assert missing == [], (
            "backend/schema.sql did not create these Phase 4 tables: "
            f"{missing} (see the recorded pre-existing script failures)"
        )
        _assert_check_admits_screener(engine)
        _assert_screener_universe_works(engine)
    finally:
        engine.dispose()
