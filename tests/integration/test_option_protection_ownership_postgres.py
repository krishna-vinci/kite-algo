"""Durable protection ownership on POSTGRESQL (B2.4 S1).

What is proved here is exactly what SQLite cannot prove: the primary key makes a
second owner row impossible, the compare-and-swap transfer is atomic under the
run's advisory lock, and the owner row is written/released in the SAME
transaction as the option run, its entry edge and its terminal status.

The disposable database lives on port 15433 only, and the migration round trip
below runs on its own scratch database that is created and dropped here.
"""

from __future__ import annotations

import json
import os
import threading
import uuid

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

OWNER = "app:owner"
ACCOUNT = "kite:paper-own"

# The entry scaffolding is the plan-binding suite's: an entry here IS
# ``resolve_plan_option_run`` over a frozen option-structure plan, and reusing
# its helpers keeps this file about ownership rather than about plan setup.
from tests.integration.test_option_plan_binding_postgres import (  # noqa: E402
    G1,
    _bind_run,
    _new_run_id,
    _resolve,
    _structure_legs,
    _strategy_and_plan,
)


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_own_{uuid.uuid4().hex[:10]}"
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


def _upgrade_to_head(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = dsn
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", "backend/alembic")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def pg():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    _upgrade_to_head(dsn)
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
        yield {"factory": factory, "dsn": dsn}
    finally:
        engine.dispose()
        _drop_db(name)


def _owner_store(factory):
    from backend.options.protection.ownership import OptionProtectionOwnerStore

    return OptionProtectionOwnerStore(session_factory=factory)


def _entry(pg) -> tuple[object, str, str, str]:
    """One entry plan resolved on POSTGRES: the owner claim happens inline.

    Returns ``(factory, option_run_id, owner_run_id, strategy_id)`` where
    ``owner_run_id`` is the worker run the entry hook claimed as owner.
    """

    factory = pg["factory"]
    legs = _structure_legs()
    run_id = _new_run_id()
    strategy_id, plan_id = _strategy_and_plan(
        factory, legs=legs, run_id=run_id, account=ACCOUNT
    )
    _bind_run(factory, run_id=run_id, strategy_id=strategy_id, account=ACCOUNT)
    target = _resolve(
        factory, plan_id, strategy_id=strategy_id, account=ACCOUNT, environment="paper"
    )
    # ``_resolve`` hardcodes the hosted worker run this entry is attributed to.
    return factory, str(target["option_run_id"]), "run-hosted-1", str(strategy_id)


def _owner_rows(factory, option_run_id: str) -> list[dict]:
    from sqlalchemy import text

    with factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT option_run_id, strategy_id, account_id, "
                    " execution_environment, owner_run_id, owner_epoch, "
                    " policy_version, policy, action_state, stage_digest, "
                    " state, released_at "
                    "FROM public.option_protection_owners "
                    "WHERE option_run_id = :r"
                ),
                {"r": option_run_id},
            ).mappings()
        ]


def _events(factory, option_run_id: str) -> list[dict]:
    from sqlalchemy import text

    with factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT event, owner_epoch, owner_run_id, detail "
                    "FROM public.option_protection_owner_events "
                    "WHERE option_run_id = :r "
                    "ORDER BY owner_epoch, event"
                ),
                {"r": option_run_id},
            ).mappings()
        ]


class TestEntryClaim:
    """The creation hook: one active owner row, epoch 1, one claimed event."""

    def test_entry_creates_exactly_one_active_owner_row_at_epoch_one(self, pg):
        from backend.options.protection.ownership import (
            option_protection_policy_version,
        )

        factory, option_run_id, owner_run_id, strategy_id = _entry(pg)

        rows = _owner_rows(factory, option_run_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["state"] == "active"
        assert row["owner_run_id"] == owner_run_id
        assert int(row["owner_epoch"]) == 1
        assert row["action_state"] == "none"
        assert row["stage_digest"] is None
        assert row["released_at"] is None
        assert row["strategy_id"] == strategy_id
        assert row["account_id"] == ACCOUNT
        assert row["execution_environment"] == "paper"
        policy = row["policy"] if isinstance(row["policy"], dict) else json.loads(row["policy"])
        # The snapshot is the frozen plan's own inputs, and the version is the
        # digest of the decision inputs - not an integer.
        assert set(policy) >= {"structure_digest", "structure_id", "underlying", "expiry_policy"}
        assert row["policy_version"] == option_protection_policy_version(policy)
        assert len(str(row["policy_version"])) == 64

        events = _events(factory, option_run_id)
        assert [event["event"] for event in events] == ["claimed"]
        assert int(events[0]["owner_epoch"]) == 1
        assert events[0]["owner_run_id"] == owner_run_id

        # A reader on a run that genuinely has no owner row gets ``None`` (never
        # a fabricated "active"), while an unreadable row raises: that distinction
        # is the store's whole contract.
        store = _owner_store(factory)
        assert store.read(option_run_id) is not None
        assert store.read("opt_run_no_such_run") is None

    def test_a_run_without_a_worker_run_cannot_be_claimed(self, pg):
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.models import OptionRunCreateRequest
        from backend.options.protection.ownership import (
            OWNER_REQUIRED,
            OptionProtectionOwnerRefusal,
            option_protection_policy_snapshot,
        )
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        factory = pg["factory"]
        account = f"kite:ownless-{uuid.uuid4().hex[:6]}"
        strategy = SqlAlchemyStrategyRepository(factory).create_strategy(
            owner_id=OWNER,
            name=f"ownless-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=account,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        run = DurableOptionRunStore(session_factory=factory).create_run(
            OptionRunCreateRequest(
                strategy_name=str(strategy.id),
                product="NRML",
                legs=[
                    {
                        "tradingsymbol": "NIFTY26OCT25000CE",
                        "transaction_type": "SELL",
                        "quantity": 75,
                    }
                ],
                metadata={
                    "strategy_id": str(strategy.id),
                    "account_id": account,
                    "execution_environment": "paper",
                },
            )
        )
        store = _owner_store(factory)

        with pytest.raises(OptionProtectionOwnerRefusal) as ctx:
            store.claim(run, None, option_protection_policy_snapshot({}), None)

        assert ctx.value.reason_code == OWNER_REQUIRED
        assert _owner_rows(factory, run.strategy_run_id) == []


class TestTransfer:
    """The CAS: one winner, one epoch advance, a named conflict for the loser."""

    def test_two_concurrent_transfers_at_one_epoch_have_exactly_one_winner(self, pg):
        from backend.options.protection.ownership import (
            CONFLICT,
            OptionProtectionOwnerRefusal,
        )

        factory, option_run_id, owner_run_id, _strategy_id = _entry(pg)
        store = _owner_store(factory)
        observed = int(store.read(option_run_id)["owner_epoch"])
        assert observed == 1

        successors = [f"run-successor-{uuid.uuid4().hex[:8]}" for _ in range(2)]
        policies = [
            {"structure_digest": "d-1", "max_loss": {"max_loss_inr": 1000.0}},
            {"structure_digest": "d-1", "max_loss": {"max_loss_inr": 2000.0}},
        ]
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []

        def attempt(index: int) -> None:
            try:
                barrier.wait(timeout=15)
                epoch = store.transfer(
                    option_run_id,
                    successors[index],
                    observed,
                    policies[index],
                )
                results.append(("won", (successors[index], epoch)))
            except OptionProtectionOwnerRefusal as exc:
                results.append(("refused", exc.reason_code))
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertions below
                results.append(("error", repr(exc)))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(outcome for outcome, _ in results) == ["refused", "won"], results
        assert ("refused", CONFLICT) in results
        winner = next(value for outcome, value in results if outcome == "won")
        assert isinstance(winner, tuple)
        winning_owner, new_epoch = winner
        assert int(new_epoch) == observed + 1

        row = store.read(option_run_id)
        assert int(row["owner_epoch"]) == observed + 1
        assert row["owner_run_id"] == winning_owner
        assert row["state"] == "active"
        assert row["owner_run_id"] != owner_run_id

        events = _events(factory, option_run_id)
        names = [event["event"] for event in events]
        assert names.count("claimed") == 1
        # Exactly ONE epoch advance, so exactly one transfer committed - and the
        # winner's changed policy came with it, in the same CAS.
        assert names.count("transferred") == 1
        assert names.count("policy_changed") == 1
        transferred = next(event for event in events if event["event"] == "transferred")
        assert transferred["owner_run_id"] == winning_owner
        assert int(transferred["owner_epoch"]) == observed + 1

    def test_a_stale_observed_epoch_is_refused_by_name(self, pg):
        from backend.options.protection.ownership import (
            CONFLICT,
            OptionProtectionOwnerRefusal,
        )

        factory, option_run_id, _owner_run_id, _strategy_id = _entry(pg)
        store = _owner_store(factory)

        store.transfer(option_run_id, "run-first-successor", 1, {"structure_digest": "d"})
        with pytest.raises(OptionProtectionOwnerRefusal) as ctx:
            store.transfer(option_run_id, "run-second-successor", 1, {"structure_digest": "d"})

        assert ctx.value.reason_code == CONFLICT
        assert int(ctx.value.detail["observed_epoch"]) == 1
        assert int(ctx.value.detail["current_epoch"]) == 2
        row = store.read(option_run_id)
        assert int(row["owner_epoch"]) == 2
        assert row["owner_run_id"] == "run-first-successor"


class TestSingleOwnerRow:
    """The primary key is the rule: a second owner row is unrepresentable."""

    def test_a_second_owner_row_for_one_run_is_impossible(self, pg):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.protection.ownership import (
            CONFLICT,
            OptionProtectionOwnerRefusal,
            option_protection_policy_snapshot,
        )

        factory, option_run_id, owner_run_id, strategy_id = _entry(pg)
        store = _owner_store(factory)
        run = DurableOptionRunStore(session_factory=factory).get_run(option_run_id)

        with pytest.raises(OptionProtectionOwnerRefusal) as ctx:
            store.claim(
                run,
                "run-second-owner",
                option_protection_policy_snapshot({}),
                None,
            )

        assert ctx.value.reason_code == CONFLICT

        # ... and the PK refuses it at the database boundary too, not only in the
        # store's own read-before-write.
        with factory() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO public.option_protection_owners "
                        "(option_run_id, strategy_id, account_id, execution_environment, "
                        " owner_run_id, owner_epoch, policy_version, policy) "
                        "VALUES (:run, :sid, :account, 'paper', 'run-forced', 1, 'v', "
                        " CAST(:policy AS jsonb))"
                    ),
                    {
                        "run": option_run_id,
                        "sid": strategy_id,
                        "account": ACCOUNT,
                        "policy": json.dumps({}),
                    },
                )
            session.rollback()

        rows = _owner_rows(factory, option_run_id)
        assert len(rows) == 1
        assert rows[0]["owner_run_id"] == owner_run_id

    def test_an_active_row_must_name_an_owner(self, pg):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        factory, option_run_id, _owner_run_id, _strategy_id = _entry(pg)
        with factory() as session:
            with pytest.raises(IntegrityError):
                # ``ck_opo_owner_present``: an active row with no owner is not a
                # state the platform can hold, which is exactly why the claim
                # refuses a run that has no worker run.
                session.execute(
                    text(
                        "UPDATE public.option_protection_owners SET owner_run_id = NULL "
                        "WHERE option_run_id = :run"
                    ),
                    {"run": option_run_id},
                )
            session.rollback()
        assert _owner_rows(factory, option_run_id)[0]["owner_run_id"] is not None


class TestRelease:
    """Release belongs to the terminal status, in the same transaction."""

    def test_release_is_refused_while_the_run_is_not_terminal(self, pg):
        from backend.options.protection.ownership import (
            RELEASE_NOT_TERMINAL,
            OptionProtectionOwnerRefusal,
        )

        factory, option_run_id, owner_run_id, _strategy_id = _entry(pg)
        store = _owner_store(factory)

        # ``_entry`` leaves the run undecided: the entry executed nothing, so the
        # run is non-terminal and a release must refuse BY NAME.
        with pytest.raises(OptionProtectionOwnerRefusal) as ctx:
            store.release(option_run_id)

        assert ctx.value.reason_code == RELEASE_NOT_TERMINAL
        assert ctx.value.detail["run_status"] not in (None, "exited", "settled")
        row = store.read(option_run_id)
        assert row["state"] == "active"
        assert row["owner_run_id"] == owner_run_id
        assert int(row["owner_epoch"]) == 1
        assert [event["event"] for event in _events(factory, option_run_id)] == ["claimed"]

    def test_release_follows_the_terminal_write_and_is_idempotent(self, pg):
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.protection.ownership import (
            CONFLICT,
            OptionProtectionOwnerRefusal,
        )

        factory, option_run_id, _owner_run_id, _strategy_id = _entry(pg)
        store = _owner_store(factory)
        runs = DurableOptionRunStore(session_factory=factory)

        # The TERMINAL write is what releases: no separate call, and no window in
        # which the run is exited while still owned.
        run = runs.get_run(option_run_id)
        run.status = "exited"
        runs.save_run(run)

        released = store.read(option_run_id)
        assert released["state"] == "released"
        assert released["owner_run_id"] is None
        assert released["released_at"] is not None
        released_epoch = int(released["owner_epoch"])
        assert released_epoch == 2

        names = [event["event"] for event in _events(factory, option_run_id)]
        assert names.count("released") == 1

        # Idempotent: a repeated release, and a repeated terminal save, change
        # neither the row nor the log.
        again = store.release(option_run_id)
        assert again["state"] == "released"
        assert int(again["owner_epoch"]) == released_epoch
        runs.save_run(run)
        assert int(store.read(option_run_id)["owner_epoch"]) == released_epoch
        assert (
            [event["event"] for event in _events(factory, option_run_id)].count("released") == 1
        )

        # A released row cannot be transferred either: an epoch CAS on a
        # non-active row updates nothing and refuses by name.
        with pytest.raises(OptionProtectionOwnerRefusal) as ctx:
            store.transfer(option_run_id, "run-after-release", released_epoch, {})
        assert ctx.value.reason_code == CONFLICT


def test_migration_round_trip_on_a_scratch_database():
    """000047 applies, reverses, and re-applies on a disposable database."""

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    previous = os.environ.get("DATABASE_URL")
    try:
        _upgrade_to_head(dsn)
        engine = create_engine(dsn, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                owners = {
                    str(row[0])
                    for row in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'public.option_protection_owners'::regclass"
                        )
                    )
                }
                assert "ck_opo_owner_present" in owners
                assert "ck_opo_environment" in owners
                assert "fk_opo_strategy" in owners
                events = {
                    str(row[0])
                    for row in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = "
                            "'public.option_protection_owner_events'::regclass"
                        )
                    )
                }
                assert any(
                    constraint.startswith("option_protection_owner_events_event")
                    or constraint == "ck_opo_event"
                    for constraint in events
                ), events

            cfg = Config("backend/alembic.ini")
            cfg.set_main_option("sqlalchemy.url", dsn)
            cfg.set_main_option("script_location", "backend/alembic")
            command.downgrade(cfg, "20260925_000046")
            with engine.connect() as conn:
                remaining = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name IN "
                        "('option_protection_owners', 'option_protection_owner_events')"
                    )
                ).scalar()
            assert int(remaining or 0) == 0

            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                restored = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name IN "
                        "('option_protection_owners', 'option_protection_owner_events')"
                    )
                ).scalar()
            assert int(restored or 0) == 2
        finally:
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        _drop_db(name)
