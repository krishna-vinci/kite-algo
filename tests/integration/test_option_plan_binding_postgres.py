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


def _strategy_and_plan(
    factory,
    *,
    legs=None,
    phase="entry",
    run_id="run-bind",
    strategy_id=None,
    account=ACCOUNT,
    plan_id=None,
    reference=None,
    resolved_extra=None,
) -> tuple[str, str]:
    import json

    from sqlalchemy import text

    from backend.strategies.repository import SqlAlchemyStrategyRepository

    if strategy_id is None:
        repo = SqlAlchemyStrategyRepository(factory)
        strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"bind-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=account,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        strategy_id = str(strategy.id)
    plan_id = plan_id or str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                " :run, 'option_structure', '{}', 'sha', 'validated')"
            ),
            {
                "pid": proposal_id,
                "sid": strategy_id,
                "account": account,
                "eval": f"eval-{plan_id}",
                "run": run_id,
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
                "account": account,
                "gen": G1,
                "resolved": json.dumps(
                    {
                        "target_kind": "option_structure",
                        "product": "NRML",
                        "expiry_policy": "exit_before_cutoff",
                        "legs": list(legs if legs is not None else _structure_legs()),
                        "option_run": {
                            "phase": phase,
                            "option_run_id": None if reference is None else str(reference),
                        },
                        **dict(resolved_extra or {}),
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


def _new_run_id() -> str:
    """A fresh worker-run id: the module shares ONE disposable database."""
    return f"run-{uuid.uuid4().hex[:12]}"


def _worker_run(factory, *, run_id: str, account: str = ACCOUNT) -> None:
    """The worker-run row a strategy binding's composite FK points at."""
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.algo_worker_runs "
                "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
                "VALUES (:run, 'tok-1', 'hosted:test', :account, 'paper', 'open')"
            ),
            {"run": run_id, "account": account},
        )
        session.commit()


def _bind_run(factory, *, run_id: str, strategy_id: str, account: str = ACCOUNT) -> None:
    from backend.strategies.attribution import SqlAttributionStore

    _worker_run(factory, run_id=run_id, account=account)
    SqlAttributionStore(session_factory=factory).bind_run(
        strategy_run_id=run_id,
        strategy_id=strategy_id,
        owner_id=OWNER,
        account_id=account,
        execution_environment="paper",
        bound_by="test",
        binding_source="hosted_job",
    )


def _plan_for(
    factory,
    strategy_id: str,
    *,
    legs,
    phase="entry",
    run_id="run-bind",
    reference=None,
):
    """A second frozen plan OF THE SAME strategy (a restarted evaluation)."""
    _, plan_id = _strategy_and_plan(
        factory,
        legs=legs,
        phase=phase,
        run_id=run_id,
        strategy_id=strategy_id,
        reference=reference,
    )
    return plan_id


def _resolve(factory, plan_id: str, *, strategy_id: str, account: str = ACCOUNT, environment: str = "paper"):
    import json

    from sqlalchemy import text

    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.plan_binding import (
        PlanOptionRunBindingStore,
        resolve_plan_option_run,
    )

    with factory() as session:
        resolved = session.execute(
            text("SELECT resolved_plan FROM strategy_plans WHERE plan_id = :p"),
            {"p": plan_id},
        ).scalar()
    plan = {
        "plan_id": plan_id,
        "strategy_id": strategy_id,
        "account_id": account,
        "plan_kind": "option_structure",
        "resolved_plan": resolved if isinstance(resolved, dict) else json.loads(resolved),
    }
    return resolve_plan_option_run(
        plan,
        strategy_id=strategy_id,
        account_id=account,
        execution_environment=environment,
        worker_run_id="run-hosted-1",
        binding_store=PlanOptionRunBindingStore(session_factory=factory),
        run_store=DurableOptionRunStore(session_factory=factory),
    )


class TestDuplicateStructureAdmission:
    """Phase B1 item 1: a strategy never opens an equivalent structure twice."""

    def _enter(self, pg):
        factory = pg["factory"]
        legs = _structure_legs()
        run_id = _new_run_id()
        strategy_id, plan_id = _strategy_and_plan(factory, legs=legs, run_id=run_id)
        _bind_run(factory, run_id=run_id, strategy_id=strategy_id)
        target = _resolve(factory, plan_id, strategy_id=strategy_id)
        return factory, strategy_id, plan_id, legs, target

    def _second_attempt(self, factory, strategy_id: str, *, legs):
        """A restarted evaluation: its OWN bound worker run and frozen plan."""
        run_id = _new_run_id()
        _bind_run(factory, run_id=run_id, strategy_id=strategy_id)
        return _plan_for(factory, strategy_id, legs=legs, run_id=run_id)

    def _entry_edge_count(self, factory, strategy_id: str) -> int:
        """How many runs this strategy's edges reach (one entry edge each here)."""
        from sqlalchemy import text

        with factory() as session:
            return int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM public.strategy_plan_option_runs "
                        "WHERE strategy_id = :sid"
                    ),
                    {"sid": strategy_id},
                ).scalar()
                or 0
            )

    def test_an_equivalent_entry_is_refused_and_no_second_run_is_created(self, pg):
        from backend.options.execution.plan_binding import PlanBindingRefusal

        factory, strategy_id, _plan_id, legs, target = self._enter(pg)
        held_run_id = target["option_run_id"]
        second_plan = self._second_attempt(factory, strategy_id, legs=legs)

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, second_plan, strategy_id=strategy_id)

        assert ctx.value.reason_code == "OPTION_STRUCTURE_ALREADY_OPEN"
        assert ctx.value.detail["option_run_id"] == held_run_id
        assert ctx.value.detail["option_run_status"] == "created"
        assert self._entry_edge_count(factory, strategy_id) == 1
        assert _store(factory).get(second_plan) is None

    def test_a_finished_structure_does_not_block_a_new_entry(self, pg):
        from sqlalchemy import text

        factory, strategy_id, _plan_id, legs, target = self._enter(pg)
        with factory() as session:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET status = 'exited' "
                    "WHERE strategy_run_id = :r"
                ),
                {"r": target["option_run_id"]},
            )
            session.commit()
        second_plan = self._second_attempt(factory, strategy_id, legs=legs)

        resolved = _resolve(factory, second_plan, strategy_id=strategy_id)

        assert resolved["option_run_id"] != target["option_run_id"]
        assert self._entry_edge_count(factory, strategy_id) == 2

    def test_a_different_structure_is_not_a_duplicate(self, pg):
        from sqlalchemy import text

        factory, strategy_id, _plan_id, _legs, target = self._enter(pg)
        # A cleanly ENTERED structure is the one non-terminal state that leaves a
        # DIFFERENT structure admissible (an unresolved one refuses instead).
        with factory() as session:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET status = 'entered' "
                    "WHERE strategy_run_id = :r"
                ),
                {"r": target["option_run_id"]},
            )
            session.commit()
        other_legs = _structure_legs()
        other_legs[0]["tradingsymbol"] = "NIFTY26OCT22500CE"
        other_legs[0]["strike"] = 22500.0
        other_legs[1]["tradingsymbol"] = "NIFTY26OCT27500CE"
        other_legs[1]["strike"] = 27500.0
        second_plan = self._second_attempt(factory, strategy_id, legs=other_legs)

        resolved = _resolve(factory, second_plan, strategy_id=strategy_id)

        assert resolved["option_run_id"] != target["option_run_id"]
        assert self._entry_edge_count(factory, strategy_id) == 2

    def test_a_different_structure_while_a_run_is_unresolved_refuses(self, pg):
        """ANY unresolved run of this strategy blocks a new entry, twin or not."""
        from backend.options.execution.plan_binding import PlanBindingRefusal

        factory, strategy_id, _plan_id, _legs, target = self._enter(pg)
        other_legs = _structure_legs()
        other_legs[0]["tradingsymbol"] = "NIFTY26OCT22500CE"
        other_legs[0]["strike"] = 22500.0
        other_legs[1]["tradingsymbol"] = "NIFTY26OCT27500CE"
        other_legs[1]["strike"] = 27500.0
        second_plan = self._second_attempt(factory, strategy_id, legs=other_legs)

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, second_plan, strategy_id=strategy_id)

        assert ctx.value.reason_code == "OPTION_STRUCTURE_UNRESOLVED"
        assert ctx.value.detail["option_run_id"] == target["option_run_id"]
        assert ctx.value.detail["status"] == "created"
        assert ctx.value.detail["plan_id"] == second_plan
        assert self._entry_edge_count(factory, strategy_id) == 1
        assert _store(factory).get(second_plan) is None

    def test_unknown_discovery_refuses_rather_than_reading_no_runs(self, pg):
        """A scope-mismatched edge makes the read UNKNOWN, and unknown refuses."""
        import json

        from sqlalchemy import text

        from backend.options.execution.plan_binding import PlanBindingRefusal

        factory, strategy_id, _plan_id, legs, target = self._enter(pg)
        # A second strategy, with its OWN account, whose identity is recorded on
        # an edge that points at THIS strategy's plan. That is a data mismatch,
        # and the snapshot reports the whole read as unknown rather than dropping
        # the row.
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        created = SqlAlchemyStrategyRepository(factory).create_strategy(
            owner_id=OWNER,
            name=f"bind-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope="kite:OTHER",
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        other_strategy = str(created.id)
        mismatched_run = _option_run(factory, name="foreign")
        mismatched_plan = self._second_attempt(factory, strategy_id, legs=legs)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.strategy_plan_option_runs "
                    "(plan_id, option_run_id, strategy_id, account_id, "
                    " execution_environment, phase) "
                    "VALUES (:plan, :run, :sid, 'kite:OTHER', 'paper', 'exit')"
                ),
                {
                    "plan": mismatched_plan,
                    "run": mismatched_run,
                    "sid": other_strategy,
                },
            )
            session.commit()
        second_plan = self._second_attempt(factory, strategy_id, legs=legs)

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, second_plan, strategy_id=strategy_id)

        assert ctx.value.reason_code == "OPTION_STRUCTURE_DISCOVERY_UNKNOWN"
        assert ctx.value.detail["reason"] == "option_run_scope_mismatch"
        assert _store(factory).get(second_plan) is None

class TestExitOwnershipPreserved:
    """Phase B1 item 4: the caller's option-run id stays a LOOKUP KEY."""

    def _entry(self, pg):
        """One bound, entered structure: (factory, strategy_id, entry_legs, run_id)."""
        factory = pg["factory"]
        entry_legs = _structure_legs()
        run_id = _new_run_id()
        strategy_id, entry_plan = _strategy_and_plan(
            factory, legs=entry_legs, run_id=run_id
        )
        _bind_run(factory, run_id=run_id, strategy_id=strategy_id)
        target = _resolve(factory, entry_plan, strategy_id=strategy_id)
        return factory, strategy_id, entry_legs, target["option_run_id"]

    def _closing_plan(
        self, factory, strategy_id: str, *, entry_legs, reference, **leg_overrides
    ):
        """A closing plan for the same structure: same legs, opposite direction."""
        exit_legs = [dict(leg) for leg in entry_legs]
        for leg in exit_legs:
            leg["side"] = "BUY" if str(leg["side"]).upper() == "SELL" else "SELL"
        if leg_overrides:
            exit_legs[0].update(leg_overrides)
        run_id = _new_run_id()
        _bind_run(factory, run_id=run_id, strategy_id=strategy_id)
        return _plan_for(
            factory,
            strategy_id,
            legs=exit_legs,
            phase="exit",
            run_id=run_id,
            reference=reference,
        )

    def test_an_exit_may_only_close_the_structure_its_own_scope_bound(self, pg):
        factory, strategy_id, entry_legs, run_id = self._entry(pg)
        exit_plan = self._closing_plan(
            factory, strategy_id, entry_legs=entry_legs, reference=run_id
        )

        # The positive path: same scope, same structure, opposite direction.
        resolved = _resolve(factory, exit_plan, strategy_id=strategy_id)
        assert resolved["phase"] == "exit"
        assert resolved["option_run_id"] == run_id

    def test_a_foreign_scope_cannot_close_this_structure(self, pg):
        from backend.options.execution.plan_binding import PlanBindingRefusal

        factory, strategy_id, entry_legs, run_id = self._entry(pg)
        exit_plan = self._closing_plan(
            factory, strategy_id, entry_legs=entry_legs, reference=run_id
        )

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, exit_plan, strategy_id=strategy_id, account="kite:OTHER")
        assert ctx.value.reason_code == "OPTION_EXIT_SCOPE_MISMATCH"

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, exit_plan, strategy_id=strategy_id, environment="live")
        assert ctx.value.reason_code == "OPTION_EXIT_SCOPE_MISMATCH"
        assert _store(factory).get(exit_plan) is None

    def test_an_exit_leg_that_is_not_in_the_bound_run_is_refused(self, pg):
        from backend.options.execution.plan_binding import PlanBindingRefusal

        factory, strategy_id, entry_legs, run_id = self._entry(pg)
        # A leg the bound run never held.
        exit_plan = self._closing_plan(
            factory,
            strategy_id,
            entry_legs=entry_legs,
            reference=run_id,
            instrument_id=str(uuid.uuid4()),
            tradingsymbol="NIFTY26OCT24000CE",
        )

        with pytest.raises(PlanBindingRefusal) as ctx:
            _resolve(factory, exit_plan, strategy_id=strategy_id)

        assert ctx.value.reason_code == "OPTION_EXIT_LEG_MISMATCH"
        assert _store(factory).get(exit_plan) is None


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
