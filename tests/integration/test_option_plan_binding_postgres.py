"""The plan↔option-run edge on POSTGRESQL (the production database).

The SQLite fixture mirrors the table, but the real constraints - the unique
``plan_id``, the partial-unique ENTRY edge, the FK to ``option_run_states`` and
the composite FK to ``strategies(id, account_scope)`` - are enforced by
PostgreSQL and proved here, on a disposable database (port 15433).
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

OWNER = "app:owner"
ACCOUNT = "kite:paper-bind"
G1 = "11111111-1111-1111-1111-111111111111"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_bind_{uuid.uuid4().hex[:10]}"
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
    import psycopg2  # noqa: F401
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
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:id, 'published', NOW())"
            ),
            {"id": G1},
        )
        session.commit()
    try:
        yield {"factory": factory}
    finally:
        engine.dispose()
        _drop_db(name)


def _structure_legs():
    """Two frozen option legs exactly as the option_structure compiler emits them."""
    return [
        {
            "instrument_id": str(uuid.uuid4()),
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26OCT25000CE",
            "broker_exchange": "NFO",
            "broker_symbol": "NIFTY26OCT25000CE",
            "broker_token": 7001,
            "product": "NRML",
            "instrument_type": "CE",
            "option_type": "CE",
            "strike": 25000.0,
            "expiry": "2026-10-29",
            "lot_size": 75,
            "ratio": 1,
            "side": "SELL",
            "quantity": 75,
            "signed_quantity": -75,
            "reference_price": 100.0,
        },
        {
            "instrument_id": str(uuid.uuid4()),
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26OCT30000CE",
            "broker_exchange": "NFO",
            "broker_symbol": "NIFTY26OCT30000CE",
            "broker_token": 7002,
            "product": "NRML",
            "instrument_type": "CE",
            "option_type": "CE",
            "strike": 30000.0,
            "expiry": "2026-10-29",
            "lot_size": 75,
            "ratio": 1,
            "side": "BUY",
            "quantity": 75,
            "signed_quantity": 75,
            "reference_price": 80.0,
        },
    ]


def _strategy_and_plan(factory, *, legs=None, phase="entry") -> tuple[str, str]:
    import json

    from sqlalchemy import text

    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"bind-{uuid.uuid4().hex[:6]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    strategy_id = str(strategy.id)
    plan_id = str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                " 'run-bind', 'option_structure', '{}', 'sha', 'validated')"
            ),
            {
                "pid": proposal_id,
                "sid": strategy_id,
                "account": ACCOUNT,
                "eval": f"eval-{plan_id}",
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation) "
                "VALUES (:pid, :prop, :sid, :account, 'option_structure', 'h', '{}', :resolved, :gen)"
            ),
            {
                "pid": plan_id,
                "prop": proposal_id,
                "sid": strategy_id,
                "account": ACCOUNT,
                "gen": G1,
                "resolved": json.dumps(
                    {
                        "target_kind": "option_structure",
                        "product": "NRML",
                        "expiry_policy": "exit_before_cutoff",
                        "legs": list(legs if legs is not None else _structure_legs()),
                        "option_run": {"phase": phase, "option_run_id": None},
                    }
                ),
            },
        )
        session.commit()
    return strategy_id, plan_id


def _option_run(factory, *, name: str = "structure", leg_symbol: str = "NIFTY26OCT25000CE") -> str:
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest

    run = DurableOptionRunStore(session_factory=factory).create_run(
        OptionRunCreateRequest(
            strategy_name=name,
            product="NRML",
            legs=[
                {
                    "tradingsymbol": leg_symbol,
                    "transaction_type": "SELL",
                    "quantity": 75,
                }
            ],
            metadata={"source": "pg-binding-test"},
        )
    )
    return run.strategy_run_id


def _store(factory):
    from backend.options.execution.plan_binding import PlanOptionRunBindingStore

    return PlanOptionRunBindingStore(session_factory=factory)


def test_binding_is_idempotent_and_a_changed_identity_conflicts(pg):
    from backend.options.execution.plan_binding import PlanBindingConflict

    factory = pg["factory"]
    strategy_id, plan_id = _strategy_and_plan(factory)
    run_id = _option_run(factory)
    store = _store(factory)

    first = store.bind(
        plan_id=plan_id,
        option_run_id=run_id,
        strategy_id=strategy_id,
        account_id=ACCOUNT,
        execution_environment="paper",
        phase="entry",
        worker_run_id="run-hosted-1",
    )
    # A retry returns the SAME binding (never a second run).
    again = store.bind(
        plan_id=plan_id,
        option_run_id=run_id,
        strategy_id=strategy_id,
        account_id=ACCOUNT,
        execution_environment="paper",
        phase="entry",
        worker_run_id="run-hosted-2",
    )
    assert again == first
    assert store.list_for_run(run_id) == [first]

    # Re-pointing the same plan at another run is a conflict, not a rebind.
    other_run = _option_run(factory, leg_symbol="NIFTY26OCT30000CE")
    with pytest.raises(PlanBindingConflict) as ctx:
        store.bind(
            plan_id=plan_id,
            option_run_id=other_run,
            strategy_id=strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase="entry",
        )
    assert ctx.value.reason_code == "OPTION_PLAN_BINDING_CONFLICT"
    assert store.get(plan_id)["option_run_id"] == run_id


def test_at_most_one_entry_plan_per_run_but_many_exit_plans(pg):
    from sqlalchemy.exc import IntegrityError

    factory = pg["factory"]
    strategy_id, plan_id = _strategy_and_plan(factory)
    _strategy_b, other_plan = _strategy_and_plan(factory)
    run_id = _option_run(factory)
    store = _store(factory)

    store.bind(
        plan_id=plan_id,
        option_run_id=run_id,
        strategy_id=strategy_id,
        account_id=ACCOUNT,
        execution_environment="paper",
        phase="entry",
    )
    # A second ENTRY plan for the same run violates the partial unique index.
    with pytest.raises(IntegrityError):
        store.bind(
            plan_id=other_plan,
            option_run_id=run_id,
            strategy_id=strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase="entry",
        )
    # EXIT plans are deliberately many: closing a structure is not one plan.
    store.bind(
        plan_id=other_plan,
        option_run_id=run_id,
        strategy_id=strategy_id,
        account_id=ACCOUNT,
        execution_environment="paper",
        phase="exit",
    )
    phases = sorted(row["phase"] for row in store.list_for_run(run_id))
    assert phases == ["entry", "exit"]


def test_the_edge_requires_a_real_plan_and_a_real_run(pg):
    from sqlalchemy.exc import IntegrityError

    factory = pg["factory"]
    strategy_id, plan_id = _strategy_and_plan(factory)
    store = _store(factory)

    # No such option run: the FK to option_run_states refuses.
    with pytest.raises(IntegrityError):
        store.bind(
            plan_id=plan_id,
            option_run_id="opt_run_does_not_exist",
            strategy_id=strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase="entry",
        )
    # No such plan: the FK to strategy_plans refuses.
    run_id = _option_run(factory)
    with pytest.raises(IntegrityError):
        store.bind(
            plan_id=str(uuid.uuid4()),
            option_run_id=run_id,
            strategy_id=strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase="entry",
        )


def test_concurrent_resolution_of_one_plan_yields_exactly_one_run(pg):
    """Two instances resolving the same plan must not create two runs.

    The real ``DurableOptionRunStore`` is used (not the binding store alone),
    because the failure mode is exactly the orphan run the two-commit shape
    produced.
    """
    import threading

    from sqlalchemy import text

    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.plan_binding import (
        PlanOptionRunBindingStore,
        resolve_plan_option_run,
    )

    factory = pg["factory"]
    strategy_id, plan_id = _strategy_and_plan(factory)
    with factory() as session:
        resolved = session.execute(
            text("SELECT resolved_plan FROM strategy_plans WHERE plan_id = :p"),
            {"p": plan_id},
        ).scalar()
    plan = {
        "plan_id": plan_id,
        "strategy_id": strategy_id,
        "account_id": ACCOUNT,
        "plan_kind": "option_structure",
        "resolved_plan": resolved if isinstance(resolved, dict) else __import__("json").loads(resolved),
    }

    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def _resolve():
        try:
            barrier.wait(timeout=10)
            target = resolve_plan_option_run(
                plan,
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                execution_environment="paper",
                worker_run_id="run-hosted-1",
                binding_store=PlanOptionRunBindingStore(session_factory=factory),
                run_store=DurableOptionRunStore(session_factory=factory),
            )
            results.append(target["option_run_id"])
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(repr(exc))

    threads = [threading.Thread(target=_resolve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(set(results)) == 1, results
    with factory() as session:
        # Scoped to THIS strategy: other tests in this module share the database.
        runs = session.execute(
            text(
                "SELECT COUNT(*) FROM public.option_run_states r "
                "JOIN public.strategy_plan_option_runs b "
                " ON b.option_run_id = r.strategy_run_id WHERE b.strategy_id = :sid"
            ),
            {"sid": strategy_id},
        ).scalar()
        bindings = session.execute(
            text(
                "SELECT COUNT(*) FROM public.strategy_plan_option_runs "
                "WHERE strategy_id = :sid"
            ),
            {"sid": strategy_id},
        ).scalar()
    assert int(runs) == 1
    assert int(bindings) == 1

    # A later retry (a new instance) returns that SAME run.
    retry = resolve_plan_option_run(
        plan,
        strategy_id=strategy_id,
        account_id=ACCOUNT,
        execution_environment="paper",
        worker_run_id="run-hosted-2",
        binding_store=PlanOptionRunBindingStore(session_factory=factory),
        run_store=DurableOptionRunStore(session_factory=factory),
    )
    assert retry["option_run_id"] == results[0]
