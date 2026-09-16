"""Hosted-strategy foundation on PostgreSQL: migration + real concurrency.

Why PostgreSQL: SQLite serialises writers, so it cannot show whether the
version-numbering lock, the lease-epoch CAS, the composite-identity foreign keys,
and the create-vs-recovery serialisation actually hold under concurrent writers.
This module creates a DISPOSABLE, uniquely named database on the test server,
runs ``alembic upgrade head`` against it, and drops it afterwards. It never
touches an existing database's data.

    HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_hosted_strategy_foundation_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when neither ``HOSTED_FOUNDATION_PG_URL`` nor
``ALERTS_TEST_DATABASE_URL`` is set, so CI without PostgreSQL does not report a
fabricated pass.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg2  # real psycopg2 must be imported BEFORE the stubs, or the
# dependency stubber would replace it with a no-op and the disposable database
# could never be created.
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.strategies.repository import (  # noqa: E402
    SqlAlchemyStrategyRepository,
    StrategyDisabled,
    StrategyFenceError,
)

PG_URL = os.environ.get("HOSTED_FOUNDATION_PG_URL") or os.environ.get(
    "ALERTS_TEST_DATABASE_URL", ""
)

# The dependency stubber replaces ``psycopg2`` with a no-op when a browser/API
# test module is collected first. Rather than fail confusingly, skip with the
# reason — this suite, like the other PostgreSQL suites, must run in its own
# pytest invocation (see the module docstring).
if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process (another suite imported the test "
        "stubs first); run this PostgreSQL suite in its own pytest invocation",
        allow_module_level=True,
    )

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="no disposable PostgreSQL URL set; hosted-strategy PostgreSQL suite skipped",
)

NEW_TABLES = (
    "hosted_strategies",
    "hosted_strategy_versions",
    "hosted_strategy_schedules",
    "strategy_jobs",
)

OWNER = "app:admin"


def _parts():
    return urlsplit(PG_URL)


def _connect(dbname):
    parts = _parts()
    return psycopg2.connect(
        host=parts.hostname,
        port=parts.port or 5432,
        user=parts.username,
        password=parts.password,
        dbname=dbname,
    )


def _sqlalchemy_url(dbname):
    parts = _parts()
    return (
        f"postgresql+psycopg2://{parts.username}:{parts.password}"
        f"@{parts.hostname}:{parts.port or 5432}/{dbname}"
    )


def _admin_dbname():
    return _parts().path.lstrip("/") or "postgres"


@pytest.fixture(scope="module")
def temp_db():
    admin = _admin_dbname()
    name = f"hosted_fnd_{uuid.uuid4().hex[:10]}"
    conn = _connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()

    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _sqlalchemy_url(name)
    cfg = Config("backend/alembic.ini")
    try:
        command.upgrade(cfg, "head")
        yield _sqlalchemy_url(name), name
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
        conn = _connect(admin)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.close()


@pytest.fixture()
def factory(temp_db):
    url, _ = temp_db
    engine = create_engine(url, poolclass=NullPool)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _strategy(repo, name=None):
    # Unique per call: the module-scoped temp database is shared across tests.
    return repo.create_strategy(
        owner_id=OWNER,
        name=name or f"strat-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )


def _version(repo, strategy_id, source="print('hi')\n"):
    return repo.create_version(
        strategy_id=strategy_id,
        source=source,
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )


def _job(repo, strategy, version, attempt=1):
    return repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
        attempt=attempt,
    )


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migration_creates_foundation_tables(temp_db):
    url, _ = temp_db
    engine = create_engine(url, poolclass=NullPool)
    try:
        inspector = inspect(engine)
        for table in NEW_TABLES:
            assert inspector.has_table(table, schema="public"), table
    finally:
        engine.dispose()


def test_migration_constraints_reject_invalid_rows(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)

    # execution_mode CHECK rejects 'live'.
    with pytest.raises(Exception):
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_jobs (id, strategy_id, version_id, owner_id, "
                    "account_scope, job_kind, execution_mode, params_snapshot, "
                    "capabilities_snapshot, policy_snapshot, max_duration_s, progress_deadline_s) "
                    "VALUES ('bad', :sid, :vid, 'app:admin', 'kite:paper', 'finite', 'live', "
                    "'{}', '{}', '{}', 600, 60)"
                ),
                {"sid": strategy.id, "vid": version.id},
            )
            session.commit()

    # A weekly schedule requires a weekday.
    with pytest.raises(Exception):
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_schedules (id, strategy_id, version_id, owner_id, "
                    "account_scope, execution_mode, job_kind, max_duration_s, progress_deadline_s, "
                    "schedule_kind, at_time) "
                    "VALUES ('bad', :sid, :vid, 'app:admin', 'kite:paper', 'paper', 'finite', "
                    "600, 60, 'weekly', '09:15')"
                ),
                {"sid": strategy.id, "vid": version.id},
            )
            session.commit()


def test_composite_foreign_keys_reject_mismatched_identity(factory):
    """A job cannot reference another strategy's version or a wrong owner."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy_a = _strategy(repo)
    strategy_b = _strategy(repo)
    version_a = _version(repo, strategy_a.id)
    version_b = _version(repo, strategy_b.id)

    with pytest.raises(Exception):  # (version_id, strategy_id) FK
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_jobs (id, strategy_id, version_id, owner_id, "
                    "account_scope, job_kind, execution_mode, params_snapshot, "
                    "capabilities_snapshot, policy_snapshot, max_duration_s, progress_deadline_s) "
                    "VALUES ('m1', :sid, :vid, 'app:admin', 'kite:paper', 'finite', 'paper', "
                    "'{}', '{}', '{}', 600, 60)"
                ),
                {"sid": strategy_a.id, "vid": version_b.id},
            )
            session.commit()

    with pytest.raises(Exception):  # (strategy_id, owner_id) FK
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_jobs (id, strategy_id, version_id, owner_id, "
                    "account_scope, job_kind, execution_mode, params_snapshot, "
                    "capabilities_snapshot, policy_snapshot, max_duration_s, progress_deadline_s) "
                    "VALUES ('m2', :sid, :vid, 'app:other', 'kite:paper', 'finite', 'paper', "
                    "'{}', '{}', '{}', 600, 60)"
                ),
                {"sid": strategy_a.id, "vid": version_a.id},
            )
            session.commit()


# ---------------------------------------------------------------------------
# real concurrency
# ---------------------------------------------------------------------------


def test_concurrent_version_numbering_is_transactional(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait()
        try:
            version = repo.create_version(
                strategy_id=strategy.id,
                source="print('x')\n",
                source_sha256="b" * 64,
                parameters_schema={},
                capabilities_snapshot={},
                created_by=OWNER,
            )
            results.append(("ok", version.version))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            results.append(("err", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(v for tag, v in results if tag == "ok") == [1, 2], results
    assert all(tag == "ok" for tag, _ in results), results


def test_concurrent_lease_claim_has_one_winner(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def claim(tag):
        barrier.wait()
        claimed = repo.claim_job(
            job.id,
            lease_owner=tag,
            expected_lease_epoch=0,
            expected_attempt=1,
            lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        outcomes.append(claimed is not None)

    threads = [threading.Thread(target=claim, args=(f"sup-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(outcomes) == 1, outcomes
    assert repo.get_job(OWNER, job.id).lease_epoch == 1


def test_concurrent_replacement_after_recovery_has_one_winner(factory):
    """After reconciliation, two racing create_job calls produce one winner."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    assert repo.reconcile_recovery(
        job.id, owner_id=OWNER, expected_lease_epoch=1, expected_attempt=1
    )

    barrier = threading.Barrier(2)
    results: list = []

    def create():
        barrier.wait()
        try:
            new_job = _job(repo, strategy, version, attempt=2)
            results.append(("ok", new_job.id))
        except StrategyFenceError:
            results.append(("blocked", None))
        except Exception as exc:  # noqa: BLE001 - recorded
            results.append(("err", type(exc).__name__))

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(tag for tag, _ in results) == ["blocked", "ok"], results


def test_disable_then_claim_is_refused(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    repo.update_strategy(OWNER, strategy.id, status="disabled")
    with pytest.raises(StrategyDisabled):
        repo.claim_job(
            job.id,
            lease_owner="sup-A",
            expected_lease_epoch=0,
            expected_attempt=1,
            lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_disable_and_claim_serialise_under_the_parent_lock(factory):
    """Disable and claim share the strategy-row lock: no deadlock, consistent end.

    Either the claim won the lock first (job now starting, which disable does not
    stop) or disable won (claim refused). The strategy ends disabled either way.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def disable():
        barrier.wait()
        repo.update_strategy(OWNER, strategy.id, status="disabled")
        outcomes.append("disabled")

    def claim():
        barrier.wait()
        try:
            claimed = repo.claim_job(
                job.id,
                lease_owner="sup-A",
                expected_lease_epoch=0,
                expected_attempt=1,
                lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            outcomes.append("claimed" if claimed is not None else "queued")
        except StrategyDisabled:
            outcomes.append("refused")

    threads = [threading.Thread(target=disable), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()  # both must finish: no deadlock

    assert "disabled" in outcomes, outcomes
    assert set(outcomes) & {"claimed", "queued", "refused"}, outcomes
    final = repo.get_strategy(OWNER, strategy.id)
    assert final.status == "disabled"


# ---------------------------------------------------------------------------
# downgrade (must run last: drops this module's tables)
# ---------------------------------------------------------------------------


def test_migration_downgrade_is_clean(temp_db):
    url, _ = temp_db
    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.downgrade(Config("backend/alembic.ini"), "20260912_000018")
    finally:
        if original_url is not None:
            os.environ["DATABASE_URL"] = original_url
    engine = create_engine(url, poolclass=NullPool)
    try:
        inspector = inspect(engine)
        for table in NEW_TABLES:
            assert not inspector.has_table(table, schema="public"), table
    finally:
        engine.dispose()
