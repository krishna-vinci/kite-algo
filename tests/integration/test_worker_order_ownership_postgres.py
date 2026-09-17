"""Worker order-ownership lookup on PostgreSQL: migration + real concurrency.

Why PostgreSQL: the ownership guard relies on partial unique indexes
(``worker_live_execution_links`` per-order/per-trade uniqueness), the
links-vs-intents precedence, and corruption detection — SQLite cannot show
these hold under concurrent writers. This module creates a DISPOSABLE, uniquely
named database on the test server, runs ``alembic upgrade head`` against it,
and drops it afterwards. It never touches an existing database's data.

    WORKER_OWNERSHIP_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_worker_order_ownership_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import asyncio
import os
import threading
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

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository  # noqa: E402
from backend.broker_api.orders.worker_execution_links import WorkerExecutionLinksStore  # noqa: E402

PG_URL = os.environ.get("WORKER_OWNERSHIP_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip("psycopg2 is stubbed in this process; run this suite in its own invocation", allow_module_level=True)
if not PG_URL:
    pytest.skip("WORKER_OWNERSHIP_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable", allow_module_level=True)


@pytest.fixture()
def disposable_db():
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(PG_URL)
    admin = create_engine(urlunsplit(parts._replace(path="/postgres")), pool_pre_ping=True)
    dbname = f"kite_own_{uuid.uuid4().hex[:12]}"
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()

    db_url = urlunsplit(parts._replace(path=f"/{dbname}"))
    engine = create_engine(db_url, pool_pre_ping=True)
    # ``backend/alembic/env.py`` unconditionally overrides ``sqlalchemy.url``
    # with ``get_database_url()`` (which reads DATABASE_URL and otherwise falls
    # back to the DB_* defaults). The disposable DSN must therefore be exported
    # for the upgrade and restored afterwards, or the migration would target the
    # ambient/.env database instead of the throwaway one.
    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    alembic_cfg = Config("backend/alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.set_main_option("script_location", "backend/alembic")
    try:
        command.upgrade(alembic_cfg, "head")
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()
        admin2 = create_engine(urlunsplit(parts._replace(path="/postgres")), pool_pre_ping=True)
        with admin2.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        admin2.dispose()


def _seed_order_link(session_factory, *, account, order_id, run_id):
    WorkerExecutionLinksStore(session_factory=session_factory).upsert_order_link(
        strategy_run_id=run_id, account_id=account, broker_order_id=order_id, client_order_ref=f"KA{order_id}"
    )


def _seed_intent(session_factory, *, account, order_id, run_id):
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO public.live_order_intents
                    (intent_id, client_order_ref, account_id, strategy_run_id, strategy_family,
                     strategy_name, execution_mode, entry_surface, broker_order_id, status)
                VALUES (:intent_id, :ref, :account, :run_id, 'f', 'n', 'live', 's', :order_id, 'placed')
                """
            ),
            {
                "intent_id": f"it-{uuid.uuid4().hex[:8]}",
                "ref": f"KAI{uuid.uuid4().hex[:6]}".upper(),
                "account": account,
                "run_id": run_id,
                "order_id": order_id,
            },
        )
        session.commit()


class TestOrderOwnershipPostgres:
    def test_migration_head_matches_expected(self, disposable_db):
        with disposable_db() as session:
            version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version >= "20260915_000024"

    def test_link_ownership_intent_fallback_and_unowned(self, disposable_db):
        repo = SqlAlchemyAlgoWorkerRepository(session_factory=disposable_db)
        _seed_order_link(disposable_db, account="kite:A1", order_id="OID-1", run_id="run-1")
        _seed_intent(disposable_db, account="kite:A1", order_id="OID-F", run_id="run-fallback")

        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A1", broker_order_id="OID-1")) == {
            "status": "owned", "strategy_run_id": "run-1", "source": "link",
        }
        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A1", broker_order_id="OID-F")) == {
            "status": "owned", "strategy_run_id": "run-fallback", "source": "intent",
        }
        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A1", broker_order_id="OID-MANUAL")) == {
            "status": "unowned", "strategy_run_id": None, "source": None,
        }
        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A2", broker_order_id="OID-1"))["status"] == "unowned"

    def test_link_intent_disagreement_is_conflict(self, disposable_db):
        _seed_order_link(disposable_db, account="kite:A1", order_id="OID-D", run_id="run-a")
        _seed_intent(disposable_db, account="kite:A1", order_id="OID-D", run_id="run-b")
        repo = SqlAlchemyAlgoWorkerRepository(session_factory=disposable_db)
        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A1", broker_order_id="OID-D"))["status"] == "conflict"

    def test_two_link_owners_is_conflict(self, disposable_db):
        store = WorkerExecutionLinksStore(session_factory=disposable_db)
        _seed_order_link(disposable_db, account="kite:A1", order_id="OID-C", run_id="run-a")
        # A trade row for the same broker order attributed to another run is
        # representable (per-trade uniqueness is on trade_id) and is corruption.
        store.upsert_trade_links_for_order(
            account_id="kite:A1", broker_order_id="OID-C", trades=[{"trade_id": "T-X"}],
        )
        with disposable_db() as session:
            session.execute(
                text(
                    "UPDATE public.worker_live_execution_links SET strategy_run_id='run-b' "
                    "WHERE account_id='kite:A1' AND broker_order_id='OID-C' AND trade_id='T-X'"
                )
            )
            session.commit()
        repo = SqlAlchemyAlgoWorkerRepository(session_factory=disposable_db)
        assert asyncio.run(repo.get_live_order_ownership(account_id="kite:A1", broker_order_id="OID-C"))["status"] == "conflict"

    def test_concurrent_order_link_upserts_yield_single_owner(self, disposable_db):
        """Concurrent link upserts must leave exactly one durable owner.

        ``upsert_order_link`` is check-then-act (UPDATE, then INSERT when no row
        matched), so racing writers can raise a unique-violation that the
        placement path logs and swallows (``broker_api/orders/service.py``).
        That transient error is pre-existing behavior outside this slice. What
        the ownership guard depends on is the resulting durable state: the
        partial unique index admits exactly one ``trade_id IS NULL`` owner, so
        the lookup can never resolve a raced order to two owners or to a
        conflict. This test pins that invariant.
        """
        store = WorkerExecutionLinksStore(session_factory=disposable_db)
        errors = []

        def worker(run_id):
            try:
                store.upsert_order_link(
                    strategy_run_id=run_id, account_id="kite:C1", broker_order_id="OID-C", client_order_ref="KAC"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"run-{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Any failure must be the known race on the unique order index, never a
        # silent second owner.
        for exc in errors:
            assert "idx_worker_exec_links_order" in str(exc), f"unexpected concurrent failure: {exc!r}"

        repo = SqlAlchemyAlgoWorkerRepository(session_factory=disposable_db)
        ownership = asyncio.run(repo.get_live_order_ownership(account_id="kite:C1", broker_order_id="OID-C"))
        assert ownership["status"] == "owned"
        assert ownership["source"] == "link"
        assert ownership["strategy_run_id"].startswith("run-")
        with disposable_db() as session:
            count = session.execute(
                text(
                    "SELECT COUNT(*) FROM public.worker_live_execution_links "
                    "WHERE account_id='kite:C1' AND broker_order_id='OID-C' AND trade_id IS NULL"
                )
            ).scalar()
        assert count == 1
