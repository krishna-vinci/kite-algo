"""Run-scoped notifications on PostgreSQL: atomic publication + idempotency race.

Disposable, uniquely named database; ``alembic upgrade head`` includes the
run-scoped ``signal_events`` columns (``20260915_000022``). Dropped afterwards.
Run in its own pytest invocation (other suites stub ``psycopg2``).
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import psycopg2  # real psycopg2 BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.notifications.repository import (  # noqa: E402
    Delivery,
    RunNotificationError,
    SqlAlchemyNotificationRepository,
)
from backend.workflows.repository import SignalEvent  # noqa: E402

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
    reason="no disposable PostgreSQL URL set; run-scoped notification PostgreSQL suite skipped",
)

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
OWNER = "app:admin"


def _parts():
    return urlsplit(PG_URL)


def _connect(dbname):
    parts = _parts()
    return psycopg2.connect(
        host=parts.hostname, port=parts.port or 5432, user=parts.username, password=parts.password, dbname=dbname
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
    name = f"run_notify_{uuid.uuid4().hex[:10]}"
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
        yield _sqlalchemy_url(name)
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
    engine = create_engine(temp_db, poolclass=NullPool)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _repo(factory):
    return SqlAlchemyNotificationRepository(factory)


def test_columns_present(factory):
    with factory() as session:
        rows = session.execute(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'signal_events'"
            )
        ).fetchall()
    columns = {row[0] for row in rows}
    assert {"source_kind", "owner_id", "run_id"} <= columns


def test_concurrent_enqueue_same_key_creates_one_event(factory):
    repo = _repo(factory)
    repo.upsert_channel(OWNER, "ops", "ntfy", {"url": "https://example.invalid"}, None, True)

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def _enqueue():
        try:
            barrier.wait(timeout=10)
            results.append(
                _repo(factory).enqueue_run_notification(
                    owner_id=OWNER,
                    run_id="run_1",
                    channel_names=["ops"],
                    text="hello",
                    idempotency_key="race-key-1",
                    occurred_at=NOW,
                )
            )
        except RunNotificationError as exc:  # pragma: no cover
            errors.append(exc.code)

    threads = [threading.Thread(target=_enqueue) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(result["status"] for result in results) == ["accepted", "deduped"]
    with factory() as session:
        events = list(session.execute(select(SignalEvent)).scalars().all())
        deliveries = list(session.execute(select(Delivery)).scalars().all())
    assert len(events) == 1
    assert len(deliveries) == 1
    assert events[0].source_kind == "strategy_run" and events[0].run_id == "run_1"


def test_conflicting_content_after_commit_is_rejected(factory):
    repo = _repo(factory)
    repo.upsert_channel(OWNER, "ops", "ntfy", {"url": "https://example.invalid"}, None, True)
    repo.enqueue_run_notification(
        owner_id=OWNER, run_id="run_2", channel_names=["ops"], text="one", idempotency_key="key-1", occurred_at=NOW
    )
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_2", channel_names=["ops"], text="two", idempotency_key="key-1", occurred_at=NOW
        )
    assert exc.value.code == "idempotency_conflict"
    with factory() as session:
        events = [e for e in session.execute(select(SignalEvent)).scalars().all() if e.run_id == "run_2"]
    assert len(events) == 1
