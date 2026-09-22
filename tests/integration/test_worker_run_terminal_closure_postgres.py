"""Reconciliation closes the LINKED worker run in the SAME transaction.

R3: stopping a child is not the same as closing an exposed trading run. Once the
unblock is justified (proof + flatness + revoked authority + confirmed cleanup),
the linked hosted worker run must be closed with its closed-status semantics -
and it must commit atomically with the unblock, because "replacement cleared
while the run is still open" is exactly the state this guards against.

Runs against a DISPOSABLE database on the local test server (port 15433) and
drops it afterwards. Never the production database.
"""

from __future__ import annotations

import os
import uuid

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

ACCOUNT = "kite:paper-close"
OWNER = "app:owner"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_close_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    base = PG_ADMIN.rpartition("/")[0]
    return name, f"{base}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="module")
def pg():
    import psycopg2  # noqa: F401  the real driver must load before any stub
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
    try:
        yield {"dsn": dsn, "factory": factory, "engine": engine}
    finally:
        engine.dispose()
        _drop_db(name)


def _job(factory, *, run_id: str) -> str:
    from sqlalchemy import text

    from backend.strategies import service as strategy_service
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"close-{uuid.uuid4().hex[:6]}",
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
                " run_id = :run_id, lease_owner = 'sup-1', lease_epoch = 1, "
                " process_cleanup_state = 'confirmed', recovery_required_at = NOW(), "
                " desired_state = 'stopped' WHERE id = :id"
            ),
            {"id": job.id, "run_id": run_id},
        )
        # The run the attempt was handed to: open, and (without a close) it stays
        # open - which is the state the unblock must not leave behind.
        session.execute(
            text(
                "INSERT INTO public.algo_worker_runs "
                "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
                "VALUES (:run_id, 'tok-1', 'tpl-1', :account, 'paper', 'open')"
            ),
            {"run_id": run_id, "account": ACCOUNT},
        )
        session.commit()
    return str(job.id)


def _unblock(factory, job_id: str, *, run_id: str | None, close: bool):
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    return repo.reconcile_with_audit(
        job_id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="confirmed",
        expected_run_id=run_id,
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"note": "closure test"},
        actor_id=OWNER,
        close_worker_run=close,
        worker_run_id=run_id,
    )


def _snapshot(factory, job_id: str, run_id: str):
    from sqlalchemy import text

    with factory() as session:
        job = session.execute(
            text("SELECT status, reconciled_at FROM public.strategy_jobs WHERE id = :id"),
            {"id": job_id},
        ).first()
        run = session.execute(
            text(
                "SELECT status, closed_at FROM public.algo_worker_runs "
                "WHERE strategy_run_id = :r"
            ),
            {"r": run_id},
        ).first()
        audits = (
            session.execute(
                text("SELECT outcome FROM strategy_job_reconciliations WHERE job_id = :id"),
                {"id": job_id},
            )
            .scalars()
            .all()
        )
    return job, run, list(audits)


def test_the_linked_run_is_closed_atomically_with_the_unblock(pg):
    factory = pg["factory"]
    run_id = f"run-close-{uuid.uuid4().hex[:8]}"
    job_id = _job(factory, run_id=run_id)

    audit = _unblock(factory, job_id, run_id=run_id, close=True)

    assert audit is not None
    job, run, audits = _snapshot(factory, job_id, run_id)
    assert job.status == "stopped" and job.reconciled_at is not None
    assert run.status == "closed" and run.closed_at is not None
    assert audits == ["reconciled"]
    assert audit.evidence_json["linked_worker_run_closed"] == run_id


def test_an_unknown_linked_run_refuses_the_whole_unblock(pg):
    factory = pg["factory"]
    run_id = f"run-close-{uuid.uuid4().hex[:8]}"
    job_id = _job(factory, run_id=run_id)

    # The job names a run that is not there: the closure cannot be confirmed, so
    # replacement must stay blocked and no audit row may be written.
    audit = _unblock(factory, job_id, run_id="run-does-not-exist", close=True)

    assert audit is None
    job, run, audits = _snapshot(factory, job_id, run_id)
    assert job.status == "recovery_required" and job.reconciled_at is None
    assert run.status == "open"
    assert audits == []


def test_a_linked_close_without_a_run_id_refuses_rather_than_unblocking(pg):
    factory = pg["factory"]
    run_id = f"run-close-{uuid.uuid4().hex[:8]}"
    job_id = _job(factory, run_id=run_id)

    # Same transaction, but the caller names no run: refuse (no partial unblock).
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    audit = repo.reconcile_with_audit(
        job_id,
        owner_id=OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        expected_process_cleanup_state="confirmed",
        expected_run_id=run_id,
        reason_code="TRADING_SETTLED_FLAT",
        evidence={"note": "no run id"},
        actor_id=OWNER,
        close_worker_run=True,
        worker_run_id=None,
    )
    assert audit is None
    job, run, audits = _snapshot(factory, job_id, run_id)
    assert job.status == "recovery_required"
    assert run.status == "open"
    assert audits == []


def test_data_only_compatibility_leaves_the_run_untouched(pg):
    factory = pg["factory"]
    run_id = f"run-close-{uuid.uuid4().hex[:8]}"
    job_id = _job(factory, run_id=run_id)

    # close_worker_run=False is the data-only/unlaunched path: unchanged behavior.
    audit = _unblock(factory, job_id, run_id=run_id, close=False)

    assert audit is not None
    job, run, audits = _snapshot(factory, job_id, run_id)
    assert job.status == "stopped"
    assert run.status == "open" and run.closed_at is None
