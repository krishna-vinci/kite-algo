"""Proposals and frozen plans on PostgreSQL: immutability, races, pinning.

Why PostgreSQL: SQLite cannot prove the insert-only triggers, the composite
account FK, the UNIQUE (strategy_id, evaluation_id) race, or the fact that a
pinned read genuinely diverges from the current-only view. Every test runs
against a DISPOSABLE, uniquely named database created on the test server,
upgraded with ``alembic upgrade head`` and dropped afterwards. No existing
database is ever touched.

    PROPOSALS_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_proposals_plans_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.proposals import (  # noqa: E402
    ProposalConflict,
    ProposalStore,
    ProposalSubmission,
    plan_invalidation_state,
)

PG_URL = os.environ.get("PROPOSALS_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "PROPOSALS_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# disposable database
# ---------------------------------------------------------------------------


def _url_for(dbname: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    return urlunsplit(urlsplit(PG_URL)._replace(path=f"/{dbname}"))


def _admin_engine():
    return create_engine(_url_for("postgres"), pool_pre_ping=True)


def _drop_database(dbname: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _upgrade(db_url: str, revision: str = "head") -> None:
    """alembic/env.py overrides sqlalchemy.url with get_database_url(), so the
    disposable DSN must be exported for the duration of the upgrade."""
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", "backend/alembic")
    try:
        command.upgrade(cfg, revision)
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


class _PgTestCase(unittest.TestCase):
    """Per-test disposable database, dropped on the way out."""

    def setUp(self):
        self._created: list = []

    def tearDown(self):
        for dbname in self._created:
            _drop_database(dbname)
        self._created = []

    def make_db(self, revision: str = "head"):
        dbname = f"kite_prop_{uuid.uuid4().hex[:12]}"
        admin = _admin_engine()
        try:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            admin.dispose()
        self._created.append(dbname)
        db_url = _url_for(dbname)
        engine = create_engine(db_url, pool_pre_ping=True)
        _upgrade(db_url, revision)
        self.addCleanup(engine.dispose)
        return sessionmaker(bind=engine)


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def _rows(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).fetchall()


# -- seeding -----------------------------------------------------------------

G1 = "11111111-1111-1111-1111-111111111111"
G2 = "22222222-2222-2222-2222-222222222222"
G3 = "33333333-3333-3333-3333-333333333333"
INST_OLD = "aaaaaaaa-0000-0000-0000-000000000001"
INST_NEW = "bbbbbbbb-0000-0000-0000-000000000002"
T1 = "2026-09-01T00:00:00+00:00"
T2 = "2026-09-10T00:00:00+00:00"
T3 = "2026-09-15T00:00:00+00:00"


def seed_generation(sf, gid, published_at):
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:id, 'published', :published_at)",
        {"id": gid, "published_at": published_at},
    )


def seed_record(sf, instrument_id, *, symbol, lifecycle="active", generation=G1):
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_records "
        "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
        " current_generation_id) "
        "VALUES (:id, :identity, :public, 'NSE', :symbol, :lifecycle, :generation)",
        {
            "id": instrument_id,
            "identity": f"identity-{instrument_id}",
            "public": f"NSE:{symbol}",
            "symbol": symbol,
            "lifecycle": lifecycle,
            "generation": generation,
        },
    )


def seed_mapping(sf, instrument_id, *, token, symbol, valid_from, valid_to=None, is_current=True):
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, valid_to_generation, is_current) "
        "VALUES (:iid, 'kite', 'NSE', :symbol, :token, :vf, :vt, :current)",
        {
            "iid": instrument_id,
            "symbol": symbol,
            "token": token,
            "vf": valid_from,
            "vt": valid_to,
            "current": is_current,
        },
    )


def seed_strategy(sf, *, sid="stg-A", owner="app:o", account="kite:A"):
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, :name, :account, 'active')",
        {"sid": sid, "owner": owner, "name": f"Strategy {sid}", "account": account},
    )


def submission(**overrides) -> ProposalSubmission:
    values = {
        "strategy_id": "stg-A",
        "account_id": "kite:A",
        "evaluation_id": "eval-1",
        "evaluation_kind": "run_now",
        "job_id": None,
        "strategy_run_id": "run-1",
        "target_kind": "single_instrument",
        "payload": {
            "instrument_token": 100,
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "product": "CNC",
            "target_quantity": 10,
        },
    }
    values.update(overrides)
    return ProposalSubmission(**values)


# ---------------------------------------------------------------------------
# 1. migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_migration_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") == "20260917_000027"
        tables = {
            row[0]
            for row in _rows(
                sf,
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN "
                "('strategy_proposals','strategy_plans','strategy_proposal_journal')",
            )
        }
        assert len(tables) == 3
        constraints = {
            row[0]
            for row in _rows(
                sf,
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('uq_proposals_strategy_evaluation','uq_plans_proposal','ck_proposals_status',"
                "'ck_proposals_scheduled_requires_job','ck_plans_target_weights_scope',"
                "'strategy_proposals_strategy_id_account_id_fkey',"
                "'strategy_plans_pinned_catalog_generation_fkey')",
            )
        }
        assert len(constraints) == 7
        triggers = {
            row[0]
            for row in _rows(
                sf,
                "SELECT tgname FROM pg_trigger WHERE tgname IN "
                "('trg_strategy_proposals_immutable','trg_strategy_plans_immutable',"
                "'trg_strategy_proposal_journal_immutable')",
            )
        }
        assert len(triggers) == 3

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000026")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-g1', 'app:o', 'G1 strategy', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") == "20260917_000027"
        # Pre-existing data survives; the three new tables start empty.
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-g1'") == "G1 strategy"
        for table in ("strategy_proposals", "strategy_plans", "strategy_proposal_journal"):
            assert _scalar(sf, f"SELECT COUNT(*) FROM public.{table}") == 0


# ---------------------------------------------------------------------------
# 2/3. immutability and the composite FK
# ---------------------------------------------------------------------------


class TestImmutability(_PgTestCase):
    def _seed_plan(self, sf):
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        seed_strategy(sf)
        ProposalStore(session_factory=sf).submit(submission())
        return ProposalStore(session_factory=sf).find_by_evaluation(
            strategy_id="stg-A", evaluation_id="eval-1"
        )

    def test_insert_only_triggers_on_all_three_tables(self):
        sf = self.make_db()
        self._seed_plan(sf)
        for statement in (
            "UPDATE public.strategy_proposals SET status = 'refused'",
            "DELETE FROM public.strategy_proposals",
            "UPDATE public.strategy_plans SET plan_hash = 'x'",
            "DELETE FROM public.strategy_plans",
            "UPDATE public.strategy_proposal_journal SET event = 'conflict'",
            "DELETE FROM public.strategy_proposal_journal",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "immutable" in str(exc.value).lower() or "append-only" in str(exc.value).lower()

    def test_proposal_composite_fk_refuses_account_disagreement(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        seed_strategy(sf, sid="stg-A", account="kite:A")

        # A proposal claiming an account its strategy does not hold is refused by
        # the DATABASE, not by application discipline — the store cannot write it.
        with pytest.raises(Exception):
            ProposalStore(session_factory=sf).submit(
                submission(strategy_id="stg-A", account_id="kite:OTHER")
            )
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_proposals") == 0

        # The raw insert is refused the same way, so nothing can route around the
        # store and write a drifting envelope.
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_proposals "
                "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                " strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (gen_random_uuid(), 'stg-A', 'kite:OTHER', 'eval-x', 'run_now', 'run-x', "
                " 'single_instrument', '{}'::jsonb, 'sha', 'received')",
            )

        # A plan cannot outlive its envelope's account agreement either.
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_plans "
                "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                " logical_plan, resolved_plan, pinned_catalog_generation) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), 'stg-A', 'kite:OTHER', "
                " 'single_instrument', 'h', '{}'::jsonb, '{}'::jsonb, :gen)",
                {"gen": G1},
            )

    def test_scheduled_occurrence_without_job_is_refused_by_check(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_strategy(sf)
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_proposals "
                "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                " strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (gen_random_uuid(), 'stg-A', 'kite:A', 'eval-s', 'scheduled_occurrence', "
                " 'run-1', 'single_instrument', '{}'::jsonb, 'sha', 'received')",
            )


# ---------------------------------------------------------------------------
# 4. the concurrent same-evaluation race
# ---------------------------------------------------------------------------


class TestConcurrency(_PgTestCase):
    def _seed(self, sf):
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        seed_strategy(sf)

    def test_concurrent_same_evaluation_yields_exactly_one_envelope(self):
        sf = self.make_db()
        self._seed(sf)
        store = ProposalStore(session_factory=sf)
        outcomes: list = []
        barrier = threading.Barrier(2)

        def attempt(sub):
            try:
                barrier.wait(timeout=30)
                outcomes.append(("ok", store.submit(sub)))
            except ProposalConflict as exc:
                outcomes.append(("conflict", exc.reason_code))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [
            threading.Thread(target=attempt, args=(submission(),)) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        errors = [item for item in outcomes if item[0] == "error"]
        assert not errors, errors
        proposal_ids = {
            item[1]["proposal_id"] for item in outcomes if item[0] == "ok"
        }
        # Both callers converge on ONE envelope, and never a duplicate.
        assert len(proposal_ids) == 1, outcomes
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_proposals") == 1
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_plans") == 1
        idempotent_flags = sorted(
            item[1]["idempotent"] for item in outcomes if item[0] == "ok"
        )
        assert idempotent_flags == [False, True], outcomes

    def test_concurrent_different_payload_resolves_to_conflict(self):
        sf = self.make_db()
        self._seed(sf)
        store = ProposalStore(session_factory=sf)
        outcomes: list = []
        barrier = threading.Barrier(2)

        def attempt(sub):
            try:
                barrier.wait(timeout=30)
                outcomes.append(("ok", store.submit(sub)))
            except ProposalConflict as exc:
                outcomes.append(("conflict", exc.reason_code))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [
            threading.Thread(target=attempt, args=(submission(),)),
            threading.Thread(
                target=attempt,
                args=(
                    submission(
                        payload={
                            "instrument_token": 100,
                            "exchange": "NSE",
                            "tradingsymbol": "RELIANCE",
                            "product": "CNC",
                            "target_quantity": 999,
                        }
                    ),
                ),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        errors = [item for item in outcomes if item[0] == "error"]
        assert not errors, errors
        # One wins, the other is a conflict — never a second envelope.
        assert sorted(item[0] for item in outcomes) == ["conflict", "ok"], outcomes
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_proposals") == 1
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM public.strategy_proposal_journal WHERE event='conflict'",
        ) == 1


# ---------------------------------------------------------------------------
# 5. pinning
# ---------------------------------------------------------------------------


class TestPinning(_PgTestCase):
    def _seed_remapped(self, sf):
        seed_generation(sf, G1, T1)
        seed_generation(sf, G2, T2)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_record(sf, INST_NEW, symbol="RELIANCE", generation=G2)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1, valid_to=G2,
                     is_current=False)
        seed_mapping(sf, INST_NEW, token=100, symbol="RELIANCE", valid_from=G2, is_current=True)
        seed_strategy(sf)

    def test_pinned_read_diverges_from_current_view(self):
        sf = self.make_db()
        self._seed_remapped(sf)

        # The current view knows only the newer mapping.
        current = _rows(
            sf,
            "SELECT instrument_id FROM public.instrument_catalog_published_v "
            "WHERE broker='kite' AND broker_token=100",
        )
        assert [str(row[0]) for row in current] == [INST_NEW]

        # A plan submitted with no catalog_generation pins what is published now.
        now = ProposalStore(session_factory=sf).submit(submission(evaluation_id="eval-now"))
        assert now["plan"]["pinned_catalog_generation"] == str(G2)
        assert now["plan"]["resolved_plan"]["legs"][0]["instrument_id"] == INST_NEW

        # A plan submitted against G1 resolves the OLD instrument, which the
        # current view no longer knows about at all.
        pinned = ProposalStore(session_factory=sf).submit(
            submission(
                evaluation_id="eval-pinned",
                payload={
                    "instrument_token": 100,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "product": "CNC",
                    "target_quantity": 10,
                    "catalog_generation": G1,
                },
            )
        )
        assert pinned["plan"]["pinned_catalog_generation"] == str(G1)
        assert pinned["plan"]["resolved_plan"]["legs"][0]["instrument_id"] == INST_OLD
        # Two submissions, two genuinely different frozen plans.
        assert pinned["plan"]["plan_hash"] != now["plan"]["plan_hash"]

    def test_unpublished_generation_refused(self):
        sf = self.make_db()
        self._seed_remapped(sf)
        staging = "99999999-9999-9999-9999-999999999999"
        _exec(
            sf,
            "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
            "VALUES (:id, 'staging', NULL)",
            {"id": staging},
        )
        result = ProposalStore(session_factory=sf).submit(
            submission(
                evaluation_id="eval-staging",
                payload={
                    "instrument_token": 100,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "product": "CNC",
                    "target_quantity": 1,
                    "catalog_generation": staging,
                },
            )
        )
        assert result["status"] == "refused"
        assert _scalar(
            sf, "SELECT reason_code FROM public.strategy_proposal_journal WHERE event='validation_refused'"
        ) == "CATALOG_GENERATION_NOT_PUBLISHED"


# ---------------------------------------------------------------------------
# 6. derived invalidation
# ---------------------------------------------------------------------------


class TestInvalidation(_PgTestCase):
    def _plan(self, sf, *, generation=G1, token=100, symbol="RELIANCE"):
        seed_strategy(sf)
        result = ProposalStore(session_factory=sf).submit(
            submission(
                payload={
                    "instrument_token": token,
                    "exchange": "NSE",
                    "tradingsymbol": symbol,
                    "product": "CNC",
                    "target_quantity": 10,
                    "catalog_generation": generation,
                }
            )
        )
        assert result["status"] == "validated", result
        return result["plan"]

    def test_unrelated_newer_generation_leaves_the_plan_valid(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        plan = self._plan(sf, generation=G1)

        # A newer generation that touches an entirely different instrument.
        seed_generation(sf, G2, T2)
        seed_record(sf, INST_NEW, symbol="INFY", generation=G2)
        seed_mapping(sf, INST_NEW, token=200, symbol="INFY", valid_from=G2)

        state = plan_invalidation_state(plan, session_factory=sf)
        assert state["state"] == "valid"
        assert state["reason"] == "CATALOG_GENERATION_UNRELATED"
        assert state["pinned_catalog_generation"] == str(G1)
        assert state["current_catalog_generation"] == str(G2)

    def test_remapped_pinned_instrument_invalidates(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        plan = self._plan(sf, generation=G1)

        # A newer generation re-maps the SAME coordinate to a different instrument.
        # The publisher closes the old mapping first: only one current mapping may
        # exist per (broker, broker_token).
        seed_generation(sf, G2, T2)
        seed_record(sf, INST_NEW, symbol="RELIANCE", generation=G2)
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = FALSE, valid_to_generation = :g2 "
            "WHERE instrument_id = :old",
            {"g2": G2, "old": INST_OLD},
        )
        seed_mapping(sf, INST_NEW, token=100, symbol="RELIANCE", valid_from=G2)

        state = plan_invalidation_state(plan, session_factory=sf)
        assert state["state"] == "invalidated"
        assert state["reason"] == "PINNED_INSTRUMENT_CHANGED"
        assert [change["reason"] for change in state["changes"]] == ["COORDINATE_REMAPPED"]
        assert state["changes"][0]["current_instrument_id"] == str(INST_NEW)

    def test_lifecycle_retired_pinned_instrument_invalidates(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        plan = self._plan(sf, generation=G1)

        # A newer generation publishes; the pinned listing is retired and its
        # mapping closed, which is how a retired contract actually leaves the book.
        seed_generation(sf, G2, T2)
        seed_record(sf, INST_NEW, symbol="INFY", generation=G2)
        seed_mapping(sf, INST_NEW, token=200, symbol="INFY", valid_from=G2)
        _exec(
            sf,
            "UPDATE public.instrument_catalog_records SET lifecycle_status = 'retired' "
            "WHERE instrument_id = :old",
            {"old": INST_OLD},
        )
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = FALSE, valid_to_generation = :g2 "
            "WHERE instrument_id = :old",
            {"g2": G2, "old": INST_OLD},
        )

        state = plan_invalidation_state(plan, session_factory=sf)
        assert state["state"] == "invalidated"
        assert [change["reason"] for change in state["changes"]] == ["COORDINATE_UNMAPPED"]

        # A record that is retired but still mapped (the publisher has not closed
        # the mapping yet) is invalidated for the lifecycle reason instead — either
        # way the plan no longer means what it meant.
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = TRUE, valid_to_generation = NULL "
            "WHERE instrument_id = :old",
            {"old": INST_OLD},
        )
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = FALSE, valid_to_generation = :g2 "
            "WHERE instrument_id = :new",
            {"g2": G2, "new": INST_NEW},
        )
        lifecycle_state = plan_invalidation_state(plan, session_factory=sf)
        assert lifecycle_state["state"] == "invalidated"
        assert [change["reason"] for change in lifecycle_state["changes"]] == ["INSTRUMENT_NOT_ACTIVE"]

    def test_plan_is_never_mutated_by_invalidation(self):
        sf = self.make_db()
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        plan = self._plan(sf, generation=G1)
        seed_generation(sf, G2, T2)
        seed_record(sf, INST_NEW, symbol="RELIANCE", generation=G2)
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = FALSE, valid_to_generation = :g2 "
            "WHERE instrument_id = :old",
            {"g2": G2, "old": INST_OLD},
        )
        seed_mapping(sf, INST_NEW, token=100, symbol="RELIANCE", valid_from=G2)

        assert plan_invalidation_state(plan, session_factory=sf)["state"] == "invalidated"
        # The artifact is untouched: same hash, same resolution, still queryable.
        stored = ProposalStore(session_factory=sf).plan_for_proposal(plan["proposal_id"])
        assert stored["plan_hash"] == plan["plan_hash"]
        assert stored["resolved_plan"] == plan["resolved_plan"]
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_plans") == 1


# ---------------------------------------------------------------------------
# 7. journal completeness
# ---------------------------------------------------------------------------


class TestJournal(_PgTestCase):
    def _seed(self, sf):
        seed_generation(sf, G1, T1)
        seed_record(sf, INST_OLD, symbol="RELIANCE", generation=G1)
        seed_mapping(sf, INST_OLD, token=100, symbol="RELIANCE", valid_from=G1)
        seed_strategy(sf)

    def _events(self, sf):
        return [
            (str(row[0]), str(row[1] or ""))
            for row in _rows(
                sf,
                "SELECT event, reason_code FROM public.strategy_proposal_journal "
                "ORDER BY created_at, event",
            )
        ]

    def test_trails_for_every_outcome(self):
        sf = self.make_db()
        self._seed(sf)
        store = ProposalStore(session_factory=sf)
        store.submit(submission(evaluation_id="eval-ok"))
        store.submit(submission(evaluation_id="eval-ok"))  # idempotent retry
        with pytest.raises(ProposalConflict):
            store.submit(
                submission(
                    evaluation_id="eval-ok",
                    payload={
                        "instrument_token": 100,
                        "exchange": "NSE",
                        "tradingsymbol": "RELIANCE",
                        "product": "CNC",
                        "target_quantity": 42,
                    },
                )
            )
        store.submit(
            submission(
                evaluation_id="eval-bad",
                payload={
                    "instrument_token": 424242,
                    "exchange": "NSE",
                    "tradingsymbol": "MISSING",
                    "product": "CNC",
                    "target_quantity": 1,
                },
            )
        )

        events = self._events(sf)
        names = [event for event, _ in events]
        assert names == [
            "received",
            "plan_created",
            "idempotent_retry",
            "conflict",
            "received",
            "validation_refused",
        ], events
        assert ("conflict", "PROPOSAL_EVALUATION_CONFLICT") in events
        assert ("validation_refused", "INSTRUMENT_UNRESOLVED") in events

    def test_every_evaluation_has_its_own_trail(self):
        sf = self.make_db()
        self._seed(sf)
        store = ProposalStore(session_factory=sf)
        for index in range(3):
            store.submit(
                submission(
                    evaluation_id=f"eval-{index}",
                    evaluation_kind="scheduled_occurrence",
                    job_id="job-continuous",
                )
            )
        # One continuous job, three evaluations, three envelopes — and nothing in
        # the contract caps evaluations per job.
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_proposals") == 3
        assert _scalar(
            sf, "SELECT COUNT(DISTINCT proposal_id) FROM public.strategy_proposal_journal"
        ) == 3
