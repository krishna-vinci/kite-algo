"""Supervisor lifecycle preparation on PostgreSQL: real concurrency.

SQLite serialises writers, so it cannot show whether the credential-handoff
markers and the token-reservation CAS hold under two supervisors racing. This
module creates a DISPOSABLE, uniquely named database, runs ``alembic upgrade
head`` (which now includes ``20260915_000020``'s handoff markers), asserts the
columns exist, and drops the database afterwards.

    HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q

Run in its own pytest invocation (other suites stub ``psycopg2``). Skipped when
no URL is configured.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg2  # real psycopg2 BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
    make_request,
)

PG_URL = os.environ.get("HOSTED_FOUNDATION_PG_URL") or os.environ.get(
    "ALERTS_TEST_DATABASE_URL", ""
)

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this PostgreSQL suite in its own "
        "pytest invocation",
        allow_module_level=True,
    )

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="no disposable PostgreSQL URL set; hosted supervisor PostgreSQL suite skipped",
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
    name = f"hosted_life_{uuid.uuid4().hex[:10]}"
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
def env(temp_db):
    url, name = temp_db
    engine = create_engine(url, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, engine
    engine.dispose()


def _seed_job(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"s-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )
    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    claimed = repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert claimed is not None
    return repo, job


def test_migration_adds_handoff_columns(env):
    _factory, engine = env
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'strategy_jobs'"
            )
        ).fetchall()
    columns = {row[0] for row in rows}
    assert {"handoff_at", "last_error"} <= columns


def test_concurrent_prepare_yields_one_credential(env):
    factory, _engine = env
    repo, job = _seed_job(factory)

    worker = FakeWorkerRepository()
    app = FastAPI()
    app.state.strategies_session_factory = factory
    app.state.algo_worker_repository = worker
    app.state.journal_service = StubJournalService()
    request = make_request(app)

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def _attempt():
        try:
            barrier.wait(timeout=10)
            results.append(
                asyncio.run(
                    hosted_lifecycle.prepare_launch(
                        request,
                        strategy_repo=SqlAlchemyStrategyRepository(factory),
                        worker_repo=worker,
                        job_id=job.id,
                        lease_owner="sup-A",
                        lease_epoch=1,
                        attempt=1,
                    )
                )
            )
        except hosted_lifecycle.HostedLifecycleError as exc:
            errors.append(exc.detail["rejection_reason"])

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Exactly one authorized attempt: one config, one credential, one run.
    assert len(results) == 1
    assert errors == ["HOSTED_PREPARE_INCOMPLETE"]
    assert len(worker.tokens) == 1
    assert len(worker.runs) == 1
    persisted = repo.get_job(OWNER, job.id)
    assert persisted.status == "running" and persisted.handoff_at is not None
    assert persisted.run_id == results[0]["run_id"]


def test_reservation_cas_admits_one_winner(env):
    factory, _engine = env
    repo, job = _seed_job(factory)

    outcomes = []
    barrier = threading.Barrier(2)

    def _reserve(token_id):
        barrier.wait(timeout=10)
        outcomes.append(
            repo.reserve_child_token(
                job.id,
                lease_owner="sup-A",
                expected_lease_epoch=1,
                expected_attempt=1,
                token_id=token_id,
            )
        )

    threads = [threading.Thread(target=_reserve, args=(f"worker_{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == [False, True]
