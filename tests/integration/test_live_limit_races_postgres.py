"""Option fill-ledger idempotency under two live outcome writers.

The production lease normally keeps one writer per step, but an expired-lease
takeover can overlap a slow old owner with its replacement. The lane-owned ledger
must still be once-only. This suite owns its scratch PostgreSQL database through
the disposable test server's ADMIN DSN; it never touches ``kite_test``.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


PG_ADMIN = os.environ.get(
    "C12_LIMIT_RACE_PG_ADMIN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_limit_race_{uuid.uuid4().hex[:10]}"
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


@pytest.fixture
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    saved_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = dsn
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", dsn)
        cfg.set_main_option("script_location", "backend/alembic")
        command.upgrade(cfg, "head")
        engine = create_engine(dsn, poolclass=NullPool)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield {"dsn": dsn, "factory": factory}
        finally:
            engine.dispose()
    finally:
        if saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_url
        _drop_db(name)


def test_two_writers_record_the_same_option_fill_once(pg):
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest
    from backend.strategies.live_lane_ledger import LiveLaneLedger

    factory = pg["factory"]
    run_id = f"opt_run_{uuid.uuid4().hex}"
    plan_id = str(uuid.uuid4())
    store = DurableOptionRunStore(session_factory=factory)
    store.create_run(
        OptionRunCreateRequest(
            strategy_run_id=run_id,
            strategy_name="race-option",
            product="NRML",
            legs=[
                {
                    "leg_id": "leg-short",
                    "tradingsymbol": "NIFTY26OCT25000CE",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "lot_size": 75,
                }
            ],
        )
    )
    spec = SimpleNamespace(
        step_no=1,
        side="SELL",
        tradingsymbol="NIFTY26OCT25000CE",
        detail={
            "option": {"option_run_id": run_id, "run_leg_id": "leg-short"},
        },
    )

    class _Sequence:
        lane = "option_structure"

        @staticmethod
        def get_execution(_plan_id):
            return {
                "plan_id": plan_id,
                "lane": "option_structure",
                "strategy_id": "stg-race",
                "step_spec": [spec],
            }

    ledger = LiveLaneLedger(
        session_factory=factory,
        sequence=_Sequence(),
        option_runs=store,
        clock=lambda: datetime.now(timezone.utc),
    )
    start = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def writer():
        try:
            start.wait(timeout=5)
            results.append(
                ledger.record_confirmed_fill(
                    plan_id=plan_id, step_no=1, filled_total=75
                )
            )
        except BaseException as exc:  # surfaced below; a lost race must be visible
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    assert sorted(int(result["increment"]) for result in results) == [0, 75]
    run = store.get_run(run_id)
    assert len(run.trades) == 1
    assert int(run.trades[0]["quantity"]) == 75
    assert int(run.trades[0]["filled_total"]) == 75
    assert run.trades[0]["dedupe_key"] == f"{plan_id}:1:cumulative:75"


def test_a_later_ingested_fill_is_recovered_from_an_uncertain_claim(pg):
    """Uncertain work stays scannable, so the run ledger eventually gets the fill."""
    import asyncio

    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest
    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from backend.strategies.live_lane_ledger import LiveLaneLedger

    factory = pg["factory"]
    run_id = f"opt_run_{uuid.uuid4().hex}"
    plan_id = str(uuid.uuid4())
    store = DurableOptionRunStore(session_factory=factory)
    store.create_run(
        OptionRunCreateRequest(
            strategy_run_id=run_id,
            strategy_name="recovered-option",
            product="NRML",
            legs=[
                {
                    "leg_id": "leg-short",
                    "tradingsymbol": "NIFTY26OCT25000CE",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "lot_size": 75,
                }
            ],
        )
    )
    spec = SimpleNamespace(
        step_no=1,
        side="SELL",
        tradingsymbol="NIFTY26OCT25000CE",
        detail={"option": {"option_run_id": run_id, "run_leg_id": "leg-short"}},
    )

    class _Parent:
        lane = "option_structure"

        def __init__(self):
            self.legs = {}

        def get_execution(self, _plan_id):
            return {
                "plan_id": plan_id,
                "lane": "option_structure",
                "strategy_id": "stg-race",
                "step_spec": [spec],
            }

        def record_leg_outcome(self, *, plan_id, step_no, outcome, filled, ordered, **_kwargs):
            self.legs[(str(plan_id), int(step_no))] = {
                "outcome": str(outcome),
                "filled_quantity": int(filled),
                "ordered_quantity": int(ordered),
            }

        def leg_outcome(self, *, plan_id, step_no):
            return self.legs.get((str(plan_id), int(step_no)))

        def declare_leg_terminal(self, **_kwargs):
            return {"parent": False, "settled": False}

    parent = _Parent()
    row = {
        "submission_id": str(uuid.uuid4()),
        "plan_id": plan_id,
        "step_no": 1,
        "step_ref": f"live-plan:{plan_id}:step:1",
        "state": "uncertain",
        "strategy_id": "stg-race",
        "account_id": "kite:test",
        "broker_order_ids": ["O-LATE"],
        "delta": {"quantity": 75},
        "detail": {},
        "consumer_token": None,
        "consumer_until": None,
    }

    class _Submissions:
        def acquire_lease(self, *, plan_id, step_no, token, lease_seconds):
            if row["consumer_token"] not in (None, token):
                return None
            row["consumer_token"] = token
            row["consumer_until"] = datetime.now(timezone.utc) + timedelta(
                seconds=float(lease_seconds)
            )
            return dict(row)

        def release_lease(self, *, plan_id, step_no, token):
            if row["consumer_token"] == token:
                row["consumer_token"] = None
                row["consumer_until"] = None

        def record_outcome(
            self, *, plan_id, step_no, state, broker_order_ids=(), detail=None,
            consumer_token=None, **_kwargs,
        ):
            if consumer_token != row["consumer_token"]:
                return {}
            row["state"] = str(state)
            row["detail"] = {**row["detail"], **dict(detail or {})}
            return dict(row)

        def get(self, *, plan_id, step_no):
            return dict(row)

    ledger = LiveLaneLedger(
        session_factory=factory,
        sequence=parent,
        option_runs=store,
        clock=lambda: datetime.now(timezone.utc),
    )
    consumer = LiveOutcomeConsumer(
        session_factory=factory,
        sequence=parent,
        lane_ledger=ledger,
        submissions=_Submissions(),
    )
    consumer._bound_run = lambda _plan_id: run_id
    consumer._pending_rows = lambda: [dict(row)]
    consumer._parent_execution = lambda _plan_id: {"plan_id": plan_id}
    consumer._owned_orders = lambda _account, _run, _orders: {"O-LATE"}
    consumer._fills_by_order = lambda _account, owned: {order: 75 for order in owned}
    consumer._terminal_statuses = lambda _account, owned, _fills: {
        order: "CANCELLED" for order in owned
    }
    # The claim-to-ledger handoff is the boundary under test; publication and
    # barrier persistence have their own suites and are stubbed at that seam.
    def consume_through_lane(plan_id, *, filled, step_no):
        view = ledger.record_confirmed_fill(
            plan_id=plan_id, step_no=int(step_no), filled_total=int(filled)
        )
        if not view or not store.get_run(run_id).trades:
            raise RuntimeError(f"lane ledger did not record: {view}")
        parent.record_leg_outcome(
            plan_id=plan_id,
            step_no=step_no,
            outcome="filled",
            filled=filled,
            ordered=filled,
        )
        return True

    consumer._consume_reservation = consume_through_lane
    consumer._record_barrier_once = lambda **_kwargs: 1
    consumer._barrier_event_version = lambda **_kwargs: 1

    async def published(**_kwargs):
        return 1

    consumer._publish_attribution = published
    counts = asyncio.run(consumer.poll_once())

    assert counts["filled"] == 1, (counts, consumer.health(), parent.legs)
    claim = dict(row)
    assert claim["state"] == "filled"
    assert parent.legs[(plan_id, 1)]["outcome"] == "filled"
    assert len(store.get_run(run_id).trades) == 1
