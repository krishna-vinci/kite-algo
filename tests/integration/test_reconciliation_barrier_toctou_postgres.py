"""Reconciliation's settlement proof is validated INSIDE the unblock transaction.

The narrow TOCTOU this covers: the route collects evidence (and its quiescence
axis), then calls the repository's atomic unblock. Between those two moments a
work event can commit, so the repository must re-validate the proof under the
book's own advisory lock - the same lock work events take - in the transaction
that clears the block and writes the audit row.

Runs against a DISPOSABLE database on the local test server (port 15433) and
drops it afterwards. Skipped when no URL is configured.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

ACCOUNT = "kite:paper-toctou"
STRATEGY = "stg-toctou"
ENV = "paper"
OWNER = "app:owner"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_toctou_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, f"{PG_ADMIN.rpartition('/')[0]}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="module")
def pg():
    import psycopg2  # noqa: F401  real driver before any stubs
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    os.environ["DATABASE_URL"] = dsn
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", "backend/alembic")
    command.upgrade(cfg, "head")

    engine = create_engine(dsn, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                "VALUES (:sid, :owner, 'toctou', :account, 'active')"
            ),
            {"sid": STRATEGY, "owner": OWNER, "account": ACCOUNT},
        )
        session.commit()
    try:
        yield {"dsn": dsn, "factory": factory, "engine": engine}
    finally:
        engine.dispose()
        _drop_db(name)


def _job(factory) -> str:
    """A trade-capable paper attempt that reached recovery_required/cleanup-confirmed."""
    from backend.strategies import service as strategy_service
    from backend.strategies.repository import SqlAlchemyStrategyRepository
    from sqlalchemy import text

    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"toctou-{uuid.uuid4().hex[:6]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="def main(ctx):\n    return 0\n",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=True),
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
    with factory() as session:
        session.execute(
            text(
                "UPDATE strategy_jobs SET status = 'recovery_required', handoff_at = NOW(), "
                " run_id = 'run-toctou', lease_owner = 'sup-1', lease_epoch = 1, "
                " process_cleanup_state = 'confirmed', recovery_required_at = NOW(), "
                " desired_state = 'stopped' WHERE id = :id"
            ),
            {"id": job.id},
        )
        session.commit()
    return str(job.id)


def _book(factory, job_id: str):
    """The job's OWN persisted book: the repository derives it, tests must too."""
    from sqlalchemy import text

    with factory() as session:
        row = session.execute(
            text(
                "SELECT account_scope, strategy_id, execution_mode FROM public.strategy_jobs "
                "WHERE id = :id"
            ),
            {"id": job_id},
        ).first()
    return str(row.account_scope), str(row.strategy_id), str(row.execution_mode)


def _unblock(factory, barrier, job_id: str, *, version: int):
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    return repo.reconcile_with_audit(
        job_id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="confirmed",
        expected_run_id="run-toctou",
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"note": "toctou test"},
        actor_id=OWNER,
        settlement_barrier=barrier,
        barrier_account_id=ACCOUNT,
        barrier_strategy_id=STRATEGY,
        barrier_environment=ENV,
        expected_barrier_version=version,
        require_barrier_proof=True,
    )


def _state(factory, job_id: str):
    from sqlalchemy import text

    with factory() as session:
        job = session.execute(
            text("SELECT status, reconciled_at FROM public.strategy_jobs WHERE id = :id"),
            {"id": job_id},
        ).first()
        audits = session.execute(
            text(
                "SELECT outcome FROM strategy_job_reconciliations WHERE job_id = :id"
            ),
            {"id": job_id},
        ).scalars().all()
    return job, list(audits)


def test_work_committed_after_the_collect_refuses_the_unblock(pg):
    """The proof was current at collect time; new work invalidates it before CAS."""
    from backend.strategies.settlement import ExecutionBarrier

    factory = pg["factory"]
    barrier = ExecutionBarrier(session_factory=factory)
    job_id = _job(factory)
    account, strategy, env = _book(factory, job_id)
    assert barrier.record_proof(
        account_id=account, strategy_id=strategy, execution_environment=env
    ).recorded
    collected_version = barrier.state(
        account_id=account, strategy_id=strategy, execution_environment=env
    )["barrier_version"]

    # A committed work event lands between the collect and the repository call.
    barrier.record_work_event(
        account_id=account, strategy_id=strategy, execution_environment=env, event="work_created"
    )

    audit = _unblock(factory, barrier, job_id, version=collected_version)
    assert audit is None
    job, audits = _state(factory, job_id)
    assert job.status == "recovery_required" and job.reconciled_at is None
    assert "reconciled" not in audits


def test_wrong_book_is_refused(pg):
    """A proof for another book never unblocks this one."""
    from backend.strategies.settlement import ExecutionBarrier

    factory = pg["factory"]
    barrier = ExecutionBarrier(session_factory=factory)
    job_id = _job(factory)
    assert barrier.record_proof(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
    ).recorded
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    audit = repo.reconcile_with_audit(
        job_id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="confirmed",
        expected_run_id="run-toctou",
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"note": "wrong book"},
        actor_id=OWNER,
        settlement_barrier=barrier,
        barrier_account_id=ACCOUNT,
        barrier_strategy_id="stg-other",
        barrier_environment=ENV,
        expected_barrier_version=0,
        require_barrier_proof=True,
    )
    assert audit is None
    job, audits = _state(factory, job_id)
    assert job.status == "recovery_required" and "reconciled" not in audits


def test_a_valid_proof_for_another_book_cannot_unblock(pg):
    """Caller-supplied book args are ignored: the book comes from the job row.

    The decoy book holds a VALID proof at its own version, so this fails if the
    repository trusts the arguments instead of the persisted identity.
    """
    from backend.strategies.settlement import ExecutionBarrier

    factory = pg["factory"]
    barrier = ExecutionBarrier(session_factory=factory)
    decoy_strategy = f"stg-decoy-{uuid.uuid4().hex[:6]}"
    barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=decoy_strategy, execution_environment=ENV,
        event="work_created",
    )
    assert barrier.record_proof(
        account_id=ACCOUNT, strategy_id=decoy_strategy, execution_environment=ENV
    ).recorded
    decoy_state = barrier.state(
        account_id=ACCOUNT, strategy_id=decoy_strategy, execution_environment=ENV
    )
    assert decoy_state["proof_valid"] and decoy_state["barrier_version"] == 1

    job_id = _job(factory)  # its OWN book has no proof at all
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    audit = repo.reconcile_with_audit(
        job_id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="confirmed",
        expected_run_id="run-toctou",
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"note": "decoy book"},
        actor_id=OWNER,
        settlement_barrier=barrier,
        barrier_account_id=ACCOUNT,
        barrier_strategy_id=decoy_strategy,
        barrier_environment=ENV,
        expected_barrier_version=1,
        require_barrier_proof=True,
    )
    assert audit is None
    job, audits = _state(factory, job_id)
    assert job.status == "recovery_required" and "reconciled" not in audits


def test_a_current_proof_still_unblocks(pg):
    """Positive control: a CURRENT proof for the job's own book unblocks."""
    from backend.strategies.settlement import ExecutionBarrier

    factory = pg["factory"]
    barrier = ExecutionBarrier(session_factory=factory)
    job_id = _job(factory)
    account, strategy, env = _book(factory, job_id)
    barrier.record_work_event(
        account_id=account, strategy_id=strategy, execution_environment=env, event="work_created"
    )
    barrier.record_work_event(
        account_id=account, strategy_id=strategy, execution_environment=env, event="work_resolved"
    )
    assert barrier.record_proof(
        account_id=account, strategy_id=strategy, execution_environment=env
    ).recorded
    version = barrier.state(
        account_id=account, strategy_id=strategy, execution_environment=env
    )["barrier_version"]

    audit = _unblock(factory, barrier, job_id, version=version)
    assert audit is not None
    job, audits = _state(factory, job_id)
    assert job.status == "stopped" and job.reconciled_at is not None
    assert audits == ["reconciled"]


def test_a_concurrent_work_writer_serializes_against_the_unblock(pg):
    """A writer holding the book lock forces the unblock to re-check and refuse."""
    from sqlalchemy import text

    from backend.strategies.settlement import ExecutionBarrier

    factory = pg["factory"]
    barrier = ExecutionBarrier(session_factory=factory)
    job_id = _job(factory)
    account, strategy, env = _book(factory, job_id)
    assert barrier.record_proof(
        account_id=account, strategy_id=strategy, execution_environment=env
    ).recorded
    version = barrier.state(
        account_id=account, strategy_id=strategy, execution_environment=env
    )["barrier_version"]

    # The writer takes the book lock first and bumps the version without
    # committing yet: the unblock must WAIT, then see the new version.
    engine = pg["engine"]
    writer = engine.connect()
    trans = writer.begin()
    writer.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"barrier:{account}:{strategy}:{env}"},
    )
    result: dict = {}

    def _run():
        result["audit"] = _unblock(factory, barrier, job_id, version=version)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(1.0)
    assert thread.is_alive(), "the unblock did not serialize against the book lock"

    writer.execute(
        text(
            "UPDATE strategy_execution_barriers SET barrier_version = barrier_version + 1 "
            "WHERE account_id = :a AND strategy_id = :s AND execution_environment = :e"
        ),
        {"a": account, "s": strategy, "e": env},
    )
    trans.commit()
    writer.close()

    thread.join(timeout=30)
    assert not thread.is_alive(), "the unblock never completed after the writer released"
    assert result.get("audit") is None
    job, audits = _state(factory, job_id)
    assert job.status == "recovery_required" and "reconciled" not in audits
