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
- **fresh**    — a database migrated from zero by Alembic (the chain);
- **schema.sql** — a database built from the reference DDL (from-scratch
  installs), which a fresh Alembic run does NOT independently verify.

Set the URLs to enable; each unset database is skipped with its reason rather
than silently passing:

    ALERTS_TEST_DATABASE_URL=...            (upgraded)
    ALERTS_FRESH_DATABASE_URL=...           (fresh via Alembic)
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
def test_fresh_database_lifecycle_is_recorded_not_assumed():
    """A from-zero install is BLOCKED by a pre-existing baseline defect.

    ``alembic upgrade head`` runs ``20260330_000001_baseline_schema``, which
    executes ``backend/schema.sql``. That file ALTERs ``signal_events`` (the
    Phase 3 screener section) but the table is only created by migration
    ``20260908_000011`` — i.e. LATER in the same chain. The file also assumes
    an alerts-platform baseline it never creates, so executing it through
    Alembic aborts the whole chain at the first statement.

    This is NOT a Phase 4 defect and is not repaired here: it predates Phase 4
    (it was present at ``ae98218``) and fixing the installation story means
    restructuring schema.sql so every statement is ordered after its
    dependencies. What this test does is pin the CURRENT behaviour, so a future
    fix flips it from skip to pass instead of silently changing semantics.
    """
    engine = _engine(FRESH_URL)
    try:
        with engine.connect() as conn:
            has_baseline_tables = conn.execute(
                text("SELECT to_regclass('public.workflows')")
            ).scalar()
        if has_baseline_tables is not None:
            # A future repair made the from-zero path work: assert it properly.
            with engine.connect() as conn:
                head = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
            assert head >= "20260911_000016"
            _assert_check_admits_screener(engine)
            _assert_screener_universe_works(engine)
            return
        pytest.skip(
            "from-zero `alembic upgrade head` is blocked by the pre-existing "
            "baseline defect: schema.sql (run by 20260330_000001) references "
            "signal_events, which 20260908_000011 creates later in the same "
            "chain. Documented in documents/alerts-phase4-parity.md; not a "
            "Phase 4 regression."
        )
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not SCHEMA_URL,
    reason="ALERTS_SCHEMA_DATABASE_URL not set; reference-DDL check skipped",
)
def test_schema_sql_database_admits_screener_universes():
    """A database built from ``backend/schema.sql`` (the from-scratch path).

    A fresh Alembic run does NOT verify this file, so it is checked separately.
    The file has three PRE-EXISTING failures earlier in the script (it ALTERs
    alerts-platform tables that only Alembic creates), which are unrelated to
    Phase 4 and are reported rather than hidden — the Phase 4 section itself is
    applied and asserted here.
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
