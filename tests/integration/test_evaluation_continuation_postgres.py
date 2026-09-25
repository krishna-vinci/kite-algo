"""Evaluation continuation on PostgreSQL: the automatic handover end to end.

Why PostgreSQL: continuation is only ever cleared by a CAS under the real
strategy-row -> book lock order with a VERSIONED barrier proof, and the audit
row's ``outcome='continuation'`` is a database constraint the migration owns.
SQLite cannot show any of that. This module creates a DISPOSABLE, uniquely named
database on the test server, runs ``alembic upgrade head`` against it, and drops
it afterwards. It never touches an existing database's data.

    CONTINUATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_evaluation_continuation_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no disposable PostgreSQL URL is set.

Note: ``hosted_lifecycle.release`` runs its database work through
``asyncio.to_thread``. In a sandbox that refuses thread-pool dispatch the plain
invocation hangs before any assertion; the assertions here do not depend on the
shim, and CI (or any normal host) runs it unmodified.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.strategies.repository import (  # noqa: E402
    SqlAlchemyStrategyRepository,
    StrategyFenceError,
)

PG_URL = os.environ.get("CONTINUATION_PG_URL") or os.environ.get(
    "ALERTS_TEST_DATABASE_URL", ""
)

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this PostgreSQL suite in its own invocation",
        allow_module_level=True,
    )

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="no disposable PostgreSQL URL set; evaluation-continuation suite skipped",
)

OWNER = "app:admin"
ACCOUNT = "kite:paper"
LEASE_OWNER = "sup:test"
#: ``strategy_position_projection.canonical_instrument_id`` is a UUID column.
INSTRUMENT_ID = "00000000-0000-0000-0000-0000000000aa"


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


def _migrated_url(dbname):
    return _sqlalchemy_url(dbname)


@pytest.fixture(scope="module")
def temp_db():
    admin = _parts().path.lstrip("/") or "postgres"
    name = f"continuation_{uuid.uuid4().hex[:10]}"
    conn = _connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()

    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _migrated_url(name)
    cfg = Config("backend/alembic.ini")
    try:
        command.upgrade(cfg, "head")
        yield _migrated_url(name)
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


@pytest.fixture(autouse=True)
def _isolated_account_state(factory):
    """Keep the module-scoped disposable database's account state per-test.

    Account-scoped tables are shared across a module-scoped database, so a
    divergence or an outstanding request left by one scenario would silently
    decide the next one.
    """
    yield
    with factory() as session:
        for statement in (
            "DELETE FROM strategy_reconciliation_state WHERE account_id = :a",
            "DELETE FROM hosted_execution_requests WHERE account_id = :a",
            "DELETE FROM strategy_position_projection WHERE account_id = :a",
            "DELETE FROM strategy_projection_state WHERE account_id = :a",
            # NOTE: ``strategy_execution_barrier_events`` is insert-only by
            # trigger, and the barrier is keyed per strategy, so neither needs
            # cleaning between scenarios.
            # ``strategy_run_bindings`` is insert-only by trigger and holds a
            # composite FK to the run, so a BOUND run cannot be deleted (and must
            # not be: it is immutable attribution history). Only unbound runs are
            # cleaned, which is every run the launch path creates here.
            "DELETE FROM public.algo_worker_runs r WHERE r.account_scope = :a "
            "AND NOT EXISTS (SELECT 1 FROM public.strategy_run_bindings b "
            " WHERE b.strategy_run_id = r.strategy_run_id)",
            "DELETE FROM public.algo_worker_tokens WHERE account_scope = :a",
        ):
            session.execute(text(statement), {"a": ACCOUNT})
        session.commit()


class _FakeWorkerRepo:
    """The runner-side repository, faked: no child session, no live token."""

    async def get_run(self, run_id):
        return None

    async def release_run_session(self, run_id, *, expected_nonce):
        return None

    async def revoke_token(self, token_id):
        return None


def _strategy(repo):
    return repo.create_strategy(
        owner_id=OWNER,
        name=f"strat-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="none",
    )


def _version(repo, strategy_id):
    return repo.create_version(
        strategy_id=strategy_id,
        source="print('hi')\n",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1, "trade": True},
        created_by=OWNER,
    )


def _launched_job(repo, factory, strategy, version, *, run_id="run-1"):
    """A queued job driven through the real launch CAS to ``running``."""
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
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert claimed is not None
    assert repo.reserve_child_token(
        job.id,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        token_id="tok-1",
    ) is True
    assert repo.record_child_run(
        job.id,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        token_id="tok-1",
        run_id=run_id,
    ) is True
    assert repo.mark_running_and_handoff(
        job.id,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
        run_id=run_id,
    ) is True
    # The release path revokes the child token; model that durable fact so the
    # authority axis reads ``revoked`` rather than ``uncertain``.
    with factory() as session:
        # The run the launch created. No protection policy is installed, so its
        # protection state is ``settled`` (nothing standing to preserve).
        session.execute(
            text(
                "INSERT INTO public.algo_worker_runs "
                "(strategy_run_id, token_id, template_id, account_scope, execution_mode, "
                " status, runtime_state_json) "
                "VALUES (:run, 'tok-1', 'tmpl-1', :a, 'paper', 'open', '{}'::jsonb) "
                "ON CONFLICT (strategy_run_id) DO NOTHING"
            ),
            {"a": ACCOUNT, "run": run_id},
        )
        session.execute(
            text(
                "INSERT INTO public.algo_worker_tokens "
                "(token_id, name, token_hash, account_scope, status) "
                "VALUES ('tok-1', 'child', 'hash-tok-1', :a, 'revoked') "
                "ON CONFLICT (token_id) DO UPDATE SET status = 'revoked'"
            ),
            {"a": ACCOUNT},
        )
        session.commit()
    return job


PLAN_ID = "00000000-0000-0000-0000-0000000000bb"
GENERATION_ID = "00000000-0000-0000-0000-0000000000cc"


def _seed_plan(factory, strategy_id):
    """The minimal proposal + frozen plan a durable request references."""
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO instrument_catalog_generations (id, status) "
                "VALUES (CAST(:g AS uuid), 'published') ON CONFLICT (id) DO NOTHING"
            ),
            {"g": GENERATION_ID},
        )
        session.execute(
            text(
                "INSERT INTO strategy_proposals "
                "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                " strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (CAST(:p AS uuid), :s, :a, :p, 'run_now', 'run-1', "
                " 'intent_bundle', '{}', 'sha', 'validated') "
                "ON CONFLICT (proposal_id) DO NOTHING"
            ),
            {"p": PLAN_ID, "s": str(strategy_id), "a": ACCOUNT},
        )
        session.execute(
            text(
                "INSERT INTO strategy_plans "
                "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                " logical_plan, resolved_plan, pinned_catalog_generation) "
                "VALUES (CAST(:p AS uuid), CAST(:p AS uuid), :s, :a, 'intent_bundle', "
                " 'hash', '{}', '{}', CAST(:g AS uuid)) "
                "ON CONFLICT (plan_id) DO NOTHING"
            ),
            {"p": PLAN_ID, "s": str(strategy_id), "a": ACCOUNT, "g": GENERATION_ID},
        )
        session.commit()


def _publish_held_book(factory, strategy_id, *, net_quantity=10):
    """Publish a non-empty attributed book under the production tables."""
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_projection_state "
                "(account_id, strategy_id, execution_environment, projection_version, "
                " last_rebuild_at, updated_at) "
                "VALUES (:a, :s, 'paper', 1, now(), now()) "
                "ON CONFLICT (account_id, strategy_id, execution_environment) DO UPDATE "
                "SET projection_version = EXCLUDED.projection_version, "
                "    last_rebuild_at = EXCLUDED.last_rebuild_at"
            ),
            {"a": ACCOUNT, "s": str(strategy_id)},
        )
        session.execute(
            text(
                "INSERT INTO strategy_position_projection "
                "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                " net_quantity, projection_version) "
                "VALUES (:a, :s, 'paper', 'canonical', :iid, CAST(:iid AS uuid), 'CNC', 100, "
                " 'NSE', 'RELIANCE', :qty, 1)"
            ),
            {
                "a": ACCOUNT,
                "s": str(strategy_id),
                "iid": INSTRUMENT_ID,
                "qty": int(net_quantity),
            },
        )
        session.commit()


def _release(repo, factory, job, *, completion, exit_code=None):
    return asyncio.run(
        hosted_lifecycle.release(
            strategy_repo=repo,
            worker_repo=_FakeWorkerRepo(),
            job_id=job.id,
            lease_owner=LEASE_OWNER,
            lease_epoch=1,
            attempt=1,
            completion=completion,
            exit_code=exit_code,
            session_factory=factory,
        )
    )


def _publish_flat_book(factory, strategy_id):
    """A PUBLISHED book with no legs: the equity projection is genuinely flat."""
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_projection_state "
                "(account_id, strategy_id, execution_environment, projection_version, "
                " last_rebuild_at, updated_at) "
                "VALUES (:a, :s, 'paper', 1, now(), now()) "
                "ON CONFLICT (account_id, strategy_id, execution_environment) DO UPDATE "
                "SET projection_version = EXCLUDED.projection_version, "
                "    last_rebuild_at = EXCLUDED.last_rebuild_at"
            ),
            {"a": ACCOUNT, "s": str(strategy_id)},
        )
        session.commit()


OPTION_LEG_ID = "dddddddd-0000-0000-0000-000000000001"


def _hold_option_structure(
    factory, strategy_id, *, run_id, status="entered", orders=None, pending_legs=None
):
    """One durable option run this strategy owns, reached through its OWN edge.

    This is the production shape: the strategy's bound worker run owns a frozen
    plan, the plan's ``option_structure`` edge names the durable option run, and
    the run carries the status (and any stage claims) under test.
    """
    option_run_id = f"opt_run_{uuid.uuid4().hex}"
    plan_id = str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    legs = [
        {
            "instrument_id": OPTION_LEG_ID,
            "tradingsymbol": "NIFTY26OCT25000CE",
            "transaction_type": "BUY",
            "quantity": 75,
            "metadata": {"instrument_id": OPTION_LEG_ID},
        }
    ]
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO instrument_catalog_generations (id, status) "
                "VALUES (CAST(:g AS uuid), 'published') ON CONFLICT (id) DO NOTHING"
            ),
            {"g": GENERATION_ID},
        )
        session.execute(
            text(
                "INSERT INTO public.option_run_states "
                "(strategy_run_id, strategy_name, product, status, legs, metadata, orders, "
                " pending_legs) "
                "VALUES (:run, 'options-b1', 'NRML', :status, CAST(:legs AS jsonb), "
                " CAST(:metadata AS jsonb), CAST(:orders AS jsonb), CAST(:pending AS jsonb))"
            ),
            {
                "run": option_run_id,
                "status": status,
                "legs": json.dumps(legs),
                "metadata": json.dumps({"strategy_id": str(strategy_id)}),
                "orders": json.dumps(list(orders or [])),
                "pending": json.dumps(list(pending_legs or [])),
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_proposals "
                "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                " strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (CAST(:p AS uuid), :s, :a, :p, 'run_now', :run, 'option_structure', "
                " '{}', 'sha', 'validated')"
            ),
            {
                "p": proposal_id,
                "s": str(strategy_id),
                "a": ACCOUNT,
                "run": run_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_plans "
                "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                " logical_plan, resolved_plan, pinned_catalog_generation) "
                "VALUES (CAST(:p AS uuid), CAST(:prop AS uuid), :s, :a, 'option_structure', "
                " 'hash', '{}', CAST(:resolved AS jsonb), CAST(:gen AS uuid))"
            ),
            {
                "p": plan_id,
                "prop": proposal_id,
                "s": str(strategy_id),
                "a": ACCOUNT,
                "gen": GENERATION_ID,
                "resolved": json.dumps(
                    {
                        "target_kind": "option_structure",
                        "product": "NRML",
                        "expiry_policy": "exit_before_cutoff",
                        "legs": legs,
                        "option_run": {"phase": "entry", "option_run_id": None},
                    }
                ),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_plan_option_runs "
                "(plan_id, option_run_id, strategy_id, account_id, execution_environment, phase) "
                "VALUES (CAST(:p AS uuid), :run, :s, :a, 'paper', 'entry')"
            ),
            {"p": plan_id, "run": option_run_id, "s": str(strategy_id), "a": ACCOUNT},
        )
        session.commit()
    return option_run_id


def _bind_worker_run(factory, *, run_id, strategy_id):
    """The strategy's immutable run binding (the snapshot's derivation root)."""
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_run_bindings "
                "(strategy_run_id, strategy_id, owner_id, account_id, execution_environment, "
                " bound_by, binding_source) "
                "VALUES (:run, :s, :owner, :a, 'paper', 'test', 'hosted_job')"
            ),
            {"run": run_id, "s": str(strategy_id), "owner": OWNER, "a": ACCOUNT},
        )
        session.commit()


# ---------------------------------------------------------------------------
# the healthy handover
# ---------------------------------------------------------------------------


def test_a_clean_finite_exit_clears_its_own_block_and_the_next_attempt_can_start(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["continuation"]["continued"] is True, response
    assert response["continuation"]["held"] is True, response
    assert response["replacement_blocked"] is False, response

    refreshed = repo.get_job_by_id(job.id)
    assert str(refreshed.status) == "stopped"
    assert refreshed.reconciled_at is not None
    assert refreshed.completion_state == "exited"

    audit = repo.list_reconciliations(job.id)
    assert audit and str(audit[0].outcome) == "continuation", [r.outcome for r in audit]
    assert str(audit[0].reason_code) == "CONTINUATION_ELIGIBLE"
    evidence = dict(audit[0].evidence_json or {})
    proof = dict(evidence.get("continuation_proof") or {})
    assert proof["held"] is True
    assert proof["exposure_state"] == "open"
    assert proof["predecessor"]["job_id"] == job.id
    assert proof["barrier_version"] is not None
    assert proof["projection_version"] == 1

    # And the whole point: no operator click is needed for the next evaluation.
    following = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    assert following is not None
    assert str(following.status) == "queued"


def test_a_held_option_structure_continues_as_held_never_flat(factory):
    """Phase B1 item 2: the options lane is its own axis.

    The equity projection is genuinely flat (an option structure writes no equity
    leg), so a verdict that only read that projection would call this book flat
    and settle it. The durable option run says otherwise: the structure is HELD,
    the attempt may continue, and the platform carries it forward as held.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    job = _launched_job(repo, factory, strategy, version, run_id=run_id)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_flat_book(factory, strategy.id)
    _bind_worker_run(factory, run_id=run_id, strategy_id=strategy.id)
    option_run_id = _hold_option_structure(
        factory, strategy.id, run_id=run_id, status="entered"
    )

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["continuation"]["continued"] is True, response
    assert response["continuation"]["held"] is True, response
    assert response["replacement_blocked"] is False, response
    audit = repo.list_reconciliations(job.id)
    proof = dict(dict(audit[0].evidence_json or {}).get("continuation_proof") or {})
    assert proof["held"] is True
    assert proof["exposure_state"] == "flat"
    assert proof["option_work_state"] == "held"
    assert proof["option_runs"] == [f"{option_run_id}=held"]


@pytest.mark.parametrize(
    "status,orders",
    [
        ("partial_entry", None),
        ("cleanup_required", None),
        ("exiting", None),
        # An unresolved protective exit stage keeps an ``entered`` run open.
        (
            "entered",
            [{"stage_digest": "protect-1", "attempt": 1, "state": "sending"}],
        ),
    ],
)
def test_option_work_in_flight_blocks_the_continuation(factory, status, orders):
    """Phase B1 item 2: in-flight option work is never cleared by a handover."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    job = _launched_job(repo, factory, strategy, version, run_id=run_id)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_flat_book(factory, strategy.id)
    _bind_worker_run(factory, run_id=run_id, strategy_id=strategy.id)
    _hold_option_structure(factory, strategy.id, run_id=run_id, status=status, orders=orders)

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert response["continuation"]["continued"] is False, response
    assert (
        response["continuation"]["reason_code"] == "CONTINUATION_OPTION_WORK_OUTSTANDING"
    ), response
    refreshed = repo.get_job_by_id(job.id)
    assert str(refreshed.status) == "recovery_required"
    assert refreshed.reconciled_at is None


def test_unreadable_option_runs_are_never_read_as_no_structure(factory, monkeypatch):
    """Unknown coverage refuses by name instead of handing over blind."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    job = _launched_job(repo, factory, strategy, version, run_id=run_id)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_flat_book(factory, strategy.id)
    _bind_worker_run(factory, run_id=run_id, strategy_id=strategy.id)

    # The discovery itself is unreadable for this account, so the axis is unknown.
    import backend.strategies.execution_snapshot as snapshot_module

    class _UnreadableDiscovery:
        def __init__(self, **_):
            pass

        def option_runs_for_scope(self, **_):
            raise RuntimeError("option_run_discovery_failed")

    monkeypatch.setattr(
        snapshot_module, "OwnedWorkSnapshotService", _UnreadableDiscovery
    )
    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["continuation"]["continued"] is False, response
    assert response["continuation"]["reason_code"] == "CONTINUATION_OPTION_WORK_UNKNOWN", response


# ---------------------------------------------------------------------------
# everything else must stay blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completion", ["stop_requested", "timeout", None])
def test_a_stop_a_timeout_or_an_unreported_exit_never_clears_the_block(factory, completion):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)

    response = _release(repo, factory, job, completion=completion)

    assert response["replacement_blocked"] is True, response
    assert response["continuation"]["continued"] is False, response
    refreshed = repo.get_job_by_id(job.id)
    assert str(refreshed.status) == "recovery_required"
    assert refreshed.reconciled_at is None
    with pytest.raises(StrategyFenceError):
        repo.create_job(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={},
        )


@pytest.mark.parametrize("exit_code", [1, 2, -15, None])
def test_a_non_zero_or_unobserved_exit_never_clears_the_block(factory, exit_code):
    """A crashed or signalled child is not a finished finite evaluation.

    The trusted exit code travels with the report; only ``0`` clears.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)

    response = _release(repo, factory, job, completion="exited", exit_code=exit_code)

    assert response["replacement_blocked"] is True, response
    assert (
        response["continuation"]["reason_code"] == "CONTINUATION_NOT_NORMAL_COMPLETION"
    ), response
    assert repo.get_job_by_id(job.id).exit_code == exit_code


def test_a_stop_race_never_clears_the_block_even_with_a_clean_exit_code(factory):
    """The operator asked this attempt to stop; its exit 0 is not unattended.

    The unblock CAS revalidates ``desired_state`` inside the transaction, so the
    race is closed even though the runner reported ``exited``/``0``.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    assert repo.request_stop_active(job.id, owner_id=OWNER, expected_attempt=1, actor=OWNER) is True

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert (
        response["continuation"]["reason_code"] == "CONTINUATION_NOT_NORMAL_COMPLETION"
    ), response
    assert str(repo.get_job_by_id(job.id).status) == "recovery_required"


def test_a_stale_completion_report_cannot_launder_an_unsafe_outcome(factory):
    """Write-once, authority-fenced: an unsafe report can never become clean."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)

    assert repo.report_completion(
        job.id,
        completion_state="stop_requested",
        exit_code=None,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
    ) is True
    # A later "clean" report for the SAME attempt is refused, whatever it claims.
    assert repo.report_completion(
        job.id,
        completion_state="exited",
        exit_code=0,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
    ) is False
    # And a stale authority (wrong epoch/owner) cannot report at all.
    assert repo.report_completion(
        job.id,
        completion_state="exited",
        exit_code=0,
        lease_owner="sup:other",
        expected_lease_epoch=1,
        expected_attempt=1,
    ) is False
    refreshed = repo.get_job_by_id(job.id)
    assert refreshed.completion_state == "stop_requested"
    assert refreshed.exit_code is None


def test_unknown_process_cleanup_never_clears_the_block(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    # The runner never confirmed the process group is gone.
    _publish_held_book(factory, strategy.id, net_quantity=10)

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert (
        response["continuation"]["reason_code"] == "CONTINUATION_PROCESS_CLEANUP_UNKNOWN"
    ), response
    assert str(repo.get_job_by_id(job.id).status) == "recovery_required"


def test_an_unreconciled_exposure_divergence_never_clears_the_block(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    _seed_plan(factory, strategy.id)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_reconciliation_state "
                "(account_id, instrument_token, exchange, tradingsymbol, product, "
                " divergence_class, broker_quantity, attributed_quantity, manual_quantity, "
                " residual_quantity) "
                "VALUES (:a, 100, 'NSE', 'RELIANCE', 'CNC', 'unexplained', 12, 10, 0, 2)"
            ),
            {"a": ACCOUNT},
        )
        session.commit()

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert (
        response["continuation"]["reason_code"] == "CONTINUATION_RECONCILIATION_DIVERGENCE"
    ), response


def test_an_outstanding_execution_request_never_clears_the_block(factory):
    """A request still awaiting a decision means the evaluation is not finished."""
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO hosted_execution_requests "
                "(request_id, owner_id, strategy_id, canonical_strategy_id, account_id, "
                " execution_environment, strategy_run_id, job_id, version_id, source_sha256, "
                " policy_hash, plan_id, plan_hash, authorization_mode, status, idempotency_key, "
                " request_hash) "
                "VALUES ('req-1', :o, :s, :s, :a, 'paper', 'run-1', :j, :v, :sha, 'ph', "
                " CAST(:plan AS uuid), 'planhash', 'approval_based', 'awaiting_approval', "
                " 'key-1', 'rhash')"
            ),
            {
                "o": OWNER,
                "s": str(strategy.id),
                "a": ACCOUNT,
                "j": job.id,
                "v": version.id,
                "sha": "b" * 64,
                "plan": PLAN_ID,
            },
        )
        session.commit()

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert response["continuation"]["reason_code"] == "CONTINUATION_APPROVAL_OUTSTANDING", response


def _seed_request(factory, strategy, version, job, *, status):
    _seed_plan(factory, strategy.id)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO hosted_execution_requests "
                "(request_id, owner_id, strategy_id, canonical_strategy_id, account_id, "
                " execution_environment, strategy_run_id, job_id, version_id, source_sha256, "
                " policy_hash, plan_id, plan_hash, authorization_mode, status, idempotency_key, "
                " request_hash) "
                "VALUES ('req-1', :o, :s, :s, :a, 'paper', 'run-1', :j, :v, :sha, 'ph', "
                " CAST(:plan AS uuid), 'planhash', 'approval_based', :status, 'key-1', 'rhash')"
            ),
            {
                "o": OWNER,
                "s": str(strategy.id),
                "a": ACCOUNT,
                "j": job.id,
                "v": version.id,
                "sha": "b" * 64,
                "plan": PLAN_ID,
                "status": status,
            },
        )
        session.commit()


def _seed_active_approval(factory, strategy):
    with factory() as session:
        # The approval FKs a real reservation. It is ``released`` on purpose: it
        # must exist as a record without holding any capacity, so the test proves
        # the APPROVAL does not block rather than piggy-backing on a released
        # reservation's lack of capacity.
        session.execute(
            text(
                "INSERT INTO strategy_reservations "
                "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                " execution_environment, status, reserved_notional_inr, valid_until, "
                " created_at) "
                "VALUES (CAST(:res AS uuid), CAST(:plan AS uuid), :s, :a, 'eval-1', 'paper', "
                " 'released', 1000, now() + interval '1 day', now() - interval '1 hour') "
                "ON CONFLICT (reservation_id) DO NOTHING"
            ),
            {
                "res": "00000000-0000-0000-0000-0000000000ee",
                "plan": PLAN_ID,
                "s": str(strategy.id),
                "a": ACCOUNT,
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_approvals "
                "(approval_id, plan_id, strategy_id, account_id, reservation_id, plan_hash, "
                " exposure_snapshot_version, reconciliation_version, catalog_generation, "
                " actor_id, status, valid_from, valid_until) "
                "VALUES (CAST(:approval AS uuid), CAST(:plan AS uuid), :s, :a, CAST(:res AS uuid), "
                " 'planhash', 1, 1, CAST(:gen AS uuid), 'app:admin', 'active', now(), "
                " now() + interval '1 day') "
                "ON CONFLICT (approval_id) DO NOTHING"
            ),
            {
                "approval": "00000000-0000-0000-0000-0000000000dd",
                "res": "00000000-0000-0000-0000-0000000000ee",
                "plan": PLAN_ID,
                "s": str(strategy.id),
                "a": ACCOUNT,
                "gen": GENERATION_ID,
            },
        )
        session.commit()


def test_a_standing_active_approval_does_not_block_a_finished_evaluation(factory):
    """An ``active`` approval is a RECORD, not unfinished work.

    It legitimately remains active after its plan executed, so refusing on it
    alone would block a healthy recurring strategy forever.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    _seed_active_approval(factory, strategy)
    _seed_request(factory, strategy, version, job, status="executed")

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["continuation"]["continued"] is True, response


def test_a_dispatch_unresolved_request_blocks_even_though_it_is_a_final_state(factory):
    """``dispatch_unresolved`` is a final REQUEST state with an UNKNOWN outcome.

    A quiet proof must never be built on it.
    """
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    _seed_request(factory, strategy, version, job, status="dispatch_unresolved")

    response = _release(repo, factory, job, completion="exited", exit_code=0)

    assert response["replacement_blocked"] is True, response
    assert response["continuation"]["reason_code"] == "CONTINUATION_APPROVAL_OUTSTANDING", response


def test_protection_ownership_is_run_scoped_so_two_runs_are_two_owners(factory):
    """The PRODUCTION protection reader is per RUN, not per strategy.

    ``_list_protection_enabled_runs`` selects every run whose own status is
    ``open``/``exiting`` AND whose ``runtime_state.backend_protection.enabled`` is
    true. That is why leaving a predecessor run open while a successor run exists
    is NOT strategy-level ownership continuity: it is two owners. Continuation
    refuses a protected attempt by name for exactly this reason.
    """
    import json

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository

    _strategy(SqlAlchemyStrategyRepository(factory))
    with factory() as session:
        for run_id, enabled in (("run-1", True), ("run-2", True), ("run-3", False)):
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs "
                    "(strategy_run_id, token_id, template_id, account_scope, execution_mode, "
                    " status, runtime_state_json) "
                    "VALUES (:r, 'tok', 't', :a, 'paper', 'open', CAST(:p AS jsonb))"
                ),
                {
                    "r": run_id,
                    "a": ACCOUNT,
                    "p": json.dumps({"backend_protection": {"enabled": enabled}}),
                },
            )
        session.commit()

    worker = SqlAlchemyAlgoWorkerRepository(session_factory=factory)
    owned = {
        str(row["strategy_run_id"])
        for row in worker._list_protection_enabled_runs_sync()
        if str(row.get("account_scope") or "") == ACCOUNT
    }
    assert owned == {"run-1", "run-2"}, owned
    assert "run-3" not in owned


def test_the_shared_run_now_path_finishes_the_proof_after_a_restart(factory):
    """A host restart between the fence and the proof must not strand the block.

    The durable ``completion_state`` written by the release path is the marker
    that lets the shared Run now path finish the continuation later, with no
    operator reconciliation.
    """
    from backend.strategies.continuation import COMPLETION_UNKNOWN, ContinuationService

    repo = SqlAlchemyStrategyRepository(factory)
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _launched_job(repo, factory, strategy, version)
    repo.report_process_cleanup(job.id, state="confirmed", actor=LEASE_OWNER, expected_attempt=1)
    _publish_held_book(factory, strategy.id, net_quantity=10)
    # The runner reported a clean exit, then the host died before the proof.
    assert repo.report_completion(
        job.id,
        completion_state="exited",
        exit_code=0,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
    ) is True
    assert repo.mark_recovery_required(
        job.id,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
    ) is True
    assert str(repo.get_job_by_id(job.id).status) == "recovery_required"

    service = ContinuationService(session_factory=factory, repository=repo)
    result = service.attempt(
        owner_id=OWNER,
        strategy_id=strategy.id,
        completion_state=COMPLETION_UNKNOWN,
        actor_id="host:run_now:test",
    )

    assert result["continued"] is True, result
    assert result["held"] is True
    assert str(repo.get_job_by_id(job.id).status) == "stopped"
    following = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    assert following is not None
