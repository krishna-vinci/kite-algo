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
from backend.strategies.repository import (  # noqa: E402
    SqlAlchemyStrategyRepository,
    StrategyConflict,
    StrategyFenceError,
    StrategyIdempotencyConflict,
)
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
        capabilities_snapshot={
            "schema_version": 2,
            "capabilities": {"trade": True, "notify": False, "data": True},
        },
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


def _prepare_context(factory, repo, job):
    worker = FakeWorkerRepository()
    app = FastAPI()
    app.state.strategies_session_factory = factory
    app.state.algo_worker_repository = worker
    app.state.journal_service = StubJournalService()
    request = make_request(app)
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
    return worker, request


def test_launched_release_blocks_replacement_pg(env):
    factory, _engine = env
    repo, job = _seed_job(factory)
    worker, _request = _prepare_context(factory, repo, job)

    result = asyncio.run(
        hosted_lifecycle.release(
            strategy_repo=SqlAlchemyStrategyRepository(factory),
            worker_repo=worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=1,
            attempt=1,
        )
    )
    assert result["status"] == "recovery_required"
    assert result["replacement_blocked"] is True

    # The replacement block survives: create_job is refused in its own txn.
    with pytest.raises(StrategyFenceError):
        repo.create_job(
            strategy_id=job.strategy_id,
            version_id=job.version_id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={},
            attempt=2,
        )


def test_racing_recover_fences_once(env):
    factory, engine = env
    repo, job = _seed_job(factory)
    _prepare_context(factory, repo, job)

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE strategy_jobs SET lease_until = now() - interval '1 minute' WHERE id = :id"),
            {"id": job.id},
        )

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def _recover():
        try:
            barrier.wait(timeout=10)
            results.append(
                asyncio.run(
                    hosted_lifecycle.expire(
                        strategy_repo=SqlAlchemyStrategyRepository(factory),
                        worker_repo=FakeWorkerRepository(),
                        job_id=job.id,
                        lease_owner="sup-A",
                        lease_epoch=1,
                        attempt=1,
                    )
                )
            )
        except hosted_lifecycle.HostedLifecycleError as exc:
            errors.append(exc.detail["rejection_reason"])

    threads = [threading.Thread(target=_recover) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Exactly one durable fence; the loser is refused (already fenced or CAS lost).
    assert len(results) == 1
    assert len(errors) == 1
    assert repo.get_job(OWNER, job.id).status == "recovery_required"


def test_concurrent_reconcile_and_replacement(env):
    factory, _engine = env
    repo, job = _seed_job(factory)
    assert repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )

    # Replacement is blocked while unreconciled.
    with pytest.raises(StrategyFenceError):
        repo.create_job(
            strategy_id=job.strategy_id,
            version_id=job.version_id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={},
            attempt=2,
        )

    outcomes = []
    barrier = threading.Barrier(2)

    def _reconcile():
        barrier.wait(timeout=10)
        outcomes.append(
            repo.reconcile_recovery(
                job.id, owner_id=OWNER, expected_lease_epoch=1, expected_attempt=1
            )
        )

    threads = [threading.Thread(target=_reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Exactly one reconciliation clears the block; the other CAS loses.
    assert sorted(outcomes) == [False, True]
    assert repo.get_job(OWNER, job.id).status == "stopped"
    new_job = repo.create_job(
        strategy_id=job.strategy_id,
        version_id=job.version_id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
        attempt=2,
    )
    assert new_job.id != job.id


def test_reconciliation_audit_is_append_only(env):
    factory, _engine = env
    repo, job = _seed_job(factory)
    assert repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    repo.record_reconciliation(
        job_id=job.id,
        strategy_id=job.strategy_id,
        owner_id=OWNER,
        attempt=1,
        run_id=job.run_id,
        outcome="blocked",
        reason_code="OPEN_EXPOSURE",
        evidence={"exposure_state": "open"},
        actor_id=OWNER,
    )
    repo.record_reconciliation(
        job_id=job.id,
        strategy_id=job.strategy_id,
        owner_id=OWNER,
        attempt=1,
        run_id=job.run_id,
        outcome="reconciled",
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"exposure_state": "flat"},
        actor_id=OWNER,
    )
    history = repo.list_reconciliations(job.id)
    assert [row.outcome for row in history] == ["reconciled", "blocked"]  # newest first
    assert {row.actor_id for row in history} == {OWNER}


def test_reconcile_with_audit_is_atomic_and_serialized(env):
    factory, _engine = env
    repo, job = _seed_job(factory)
    assert repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    # Confirmed cleanup evidence on the job for the CAS.
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET process_cleanup_state = 'confirmed' WHERE id = :id"),
            {"id": job.id},
        )
        session.commit()

    results = []
    errors = []
    barrier = threading.Barrier(3)

    def _reconcile():
        barrier.wait(timeout=10)
        try:
            results.append(
                repo.reconcile_with_audit(
                    job.id,
                    owner_id=OWNER,
                    expected_lease_epoch=1,
                    expected_attempt=1,
                    expected_process_cleanup_state="confirmed",
                    expected_run_id=job.run_id,
                    reason_code="TRADING_SETTLED_FLAT",
                    evidence={"exposure_state": "flat"},
                    actor_id=OWNER,
                )
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def _replace():
        barrier.wait(timeout=10)
        try:
            repo.create_job(
                strategy_id=job.strategy_id,
                version_id=job.version_id,
                owner_id=OWNER,
                job_kind="finite",
                execution_mode="paper",
                params={},
                attempt=2,
            )
        except StrategyFenceError:
            pass

    threads = [threading.Thread(target=_reconcile), threading.Thread(target=_reconcile), threading.Thread(target=_replace)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    winners = [row for row in results if row is not None]
    assert len(winners) == 1  # exactly one reconciliation clears the block
    # Atomic: the block is cleared AND exactly one audit row was written.
    assert repo.get_job(OWNER, job.id).status == "stopped"
    audit_rows = [row for row in repo.list_reconciliations(job.id) if row.outcome == "reconciled"]
    assert len(audit_rows) == 1


def test_reconcile_with_audit_rejects_changed_cleanup_evidence(env):
    factory, _engine = env
    repo, job = _seed_job(factory)
    assert repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET process_cleanup_state = 'confirmed' WHERE id = :id"),
            {"id": job.id},
        )
        session.commit()
    # Assessment saw 'unresolved' but the durable row says 'confirmed' → refuse.
    result = repo.reconcile_with_audit(
        job.id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="unresolved",
        expected_run_id=job.run_id,
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"process_cleanup_state": "unresolved"},
        actor_id=OWNER,
    )
    assert result is None
    assert repo.get_job(OWNER, job.id).status == "recovery_required"
    assert repo.list_reconciliations(job.id) == []


def test_concurrent_run_now_same_idempotency_key_creates_one_job(env):
    factory, _engine = env
    repo, claimed_job = _seed_job(factory)
    # _seed_job already created a queued+claimed job; use a fresh strategy.
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"rn-{uuid.uuid4().hex[:8]}",
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
        capabilities_snapshot={"schema_version": 2, "capabilities": {"trade": True, "notify": False, "data": True}},
        created_by=OWNER,
    )
    occurrence = f"manual:{strategy.id}:race-key"
    errors = []
    created = []

    def _create():
        try:
            created.append(
                repo.create_job(
                    strategy_id=strategy.id,
                    version_id=version.id,
                    owner_id=OWNER,
                    job_kind="finite",
                    execution_mode="paper",
                    params={},
                    occurrence_key=occurrence,
                )
            )
        except StrategyConflict:
            errors.append("conflict")

    threads = [threading.Thread(target=_create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Exactly one job exists; a retry returns the same job (no duplicate).
    assert not errors
    assert len({job.id for job in created}) == 1
    existing = repo.get_job_by_occurrence_key(occurrence)
    assert existing is not None and existing.id == created[0].id


def test_stop_request_retains_authority_for_cleanup(env):
    factory, _engine = env
    repo, job = _seed_job(factory)  # claimed -> starting
    assert repo.request_stop_active(job.id, owner_id=OWNER, expected_attempt=1, actor=OWNER) is True
    persisted = repo.get_job(OWNER, job.id)
    assert persisted.desired_state == "stopped" and persisted.status == "starting"
    assert persisted.lease_owner == "sup-A" and persisted.lease_until is not None
    # The supervisor can still complete its authorized terminal transition.
    assert (
        repo.mark_recovery_required(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1)
        is True
    )


def test_stop_queued_job_without_launch(env):
    factory, _engine = env
    repo, claimed = _seed_job(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"q-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="none",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 2, "capabilities": {"trade": True, "notify": False, "data": True}},
        created_by=OWNER,
    )
    job = repo.create_job(
        strategy_id=strategy.id, version_id=version.id, owner_id=OWNER, job_kind="finite",
        execution_mode="paper", params={},
    )
    assert repo.stop_queued_job(job.id, owner_id=OWNER, expected_attempt=1, actor=OWNER) is True
    persisted = repo.get_job(OWNER, job.id)
    assert persisted.status == "stopped" and persisted.desired_state == "stopped"


def _fresh_strategy_version(repo, *, prefix):
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"{prefix}-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="none",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object", "properties": {"lots": {"type": "integer"}}, "required": ["lots"]},
        capabilities_snapshot={"schema_version": 2, "capabilities": {"trade": True, "notify": False, "data": True}},
        created_by=OWNER,
    )
    return strategy, version


def test_create_job_same_key_different_request_conflicts(env):
    factory, _engine = env
    repo, _claimed = _seed_job(factory)
    strategy, version = _fresh_strategy_version(repo, prefix="idem")
    occurrence = f"manual:{strategy.id}:key-1"
    repo.create_job(
        strategy_id=strategy.id, version_id=version.id, owner_id=OWNER, job_kind="finite",
        execution_mode="paper", params={"lots": 1}, occurrence_key=occurrence,
    )
    with pytest.raises(StrategyIdempotencyConflict):
        repo.create_job(
            strategy_id=strategy.id, version_id=version.id, owner_id=OWNER, job_kind="finite",
            execution_mode="paper", params={"lots": 2}, occurrence_key=occurrence,
        )


def test_concurrent_log_ingestion_preserves_sequence_and_cap(env):
    factory, _engine = env
    repo, job = _seed_job(factory)  # starting
    chunk = "z" * (8 * 1024)
    errors = []

    def _ingest():
        try:
            for _ in range(10):  # 10 x 8 KiB = 80 KiB per thread
                repo.append_job_log(
                    job.id, attempt=1, chunks=[chunk], max_total_bytes=256 * 1024
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_ingest) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    logs = repo.list_job_logs(job.id, limit=500)
    seqs = [row.seq for row in logs]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert repo.job_log_byte_count(job.id) == 20 * 8 * 1024
    assert repo.job_log_byte_count(job.id) <= 256 * 1024
