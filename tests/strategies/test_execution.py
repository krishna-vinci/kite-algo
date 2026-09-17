"""Paper plan execution (Project 6): the event trail, the executor and bundles.

The executor is the first consumer of a frozen plan, so its contract is pinned
here before anything submits an order (D-2): the precondition chain fails
closed with named refusals in a fixed order, a zero-delta step records
``no_op`` without touching the paper runtime, a submission carries the BOUND
run's attribution so G1's paper fold attributes the fills, every transition
lands in the append-only event trail, and the reservation is consumed by the
fill (Phase 4 lifecycle) rather than by words.

SQLite runs with the established ``public.`` ATTACH fixture; the insert-only
trigger and the advisory-lock serialization are proved on PostgreSQL in
``tests/integration/test_paper_execution_postgres.py``.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the strategy tables

G1 = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

ACCOUNT = "kite:paper-a"
STRATEGY = "stg-A"
OWNER = "app:o"
RUN_ID = "run-1"
INST_ID = "aaaaaaaa-0000-0000-0000-000000000001"

SINGLE_LEGS = [
    {
        "instrument_id": INST_ID,
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "broker_exchange": "NSE",
        "broker_symbol": "RELIANCE",
        "broker_token": 738561,
        "product": "CNC",
        "signed_quantity": 100,
        "reference_price": 1500.0,
    }
]


def _resolved_plan(plan_kind="single_instrument", legs=None):
    return {
        "target_kind": plan_kind,
        "catalog_generation": G1,
        "legs": list(SINGLE_LEGS if legs is None else legs),
    }


class ExecutionTestCase(unittest.TestCase):
    """Shared SQLite fixture: strategy tables + the plan execution event trail."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _attach_public(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
                )
                """
            )
            # The catalog record carries lot_size when the instrument has lots;
            # a missing row or NULL means the pinned catalog provides no lots.
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_records (
                    instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active',
                    lot_size INTEGER,
                    current_generation_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_broker_mappings (
                    mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT,
                    broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER,
                    valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER
                )
                """
            )
            dbapi_connection.commit()

        from backend.strategies.attribution_models import (
            Strategy,
            StrategyPlan,
            StrategyPlanExecutionEvent,
            StrategyPositionProjection,
            StrategyProposal,
            StrategyRunBinding,
        )

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyRunBinding.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyPlanExecutionEvent.__table__,
                StrategyPositionProjection.__table__,
            ],
        )
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    # ------------------------------------------------------------------ seeds

    def seed_strategy(self, *, sid=STRATEGY, account=ACCOUNT, owner=OWNER):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES (:sid, :owner, 'A', :account, 'active')"
                ),
                {"sid": sid, "owner": owner, "account": account},
            )
            session.commit()

    def seed_validated_plan(
        self,
        plan_id="plan-1",
        *,
        sid=STRATEGY,
        account=ACCOUNT,
        run_id=RUN_ID,
        plan_kind="single_instrument",
        legs=None,
        evaluation_id=None,
    ):
        """A validated proposal envelope + its frozen plan (the P3 output)."""
        proposal_id = f"prop-{plan_id}"
        resolved = _resolved_plan(plan_kind, legs)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, :sid, :account, :eval, 'run_now', :run, :kind, '{}', 'sha', 'validated')"
                ),
                {
                    "pid": proposal_id,
                    "sid": sid,
                    "account": account,
                    "eval": evaluation_id or f"eval-{plan_id}",
                    "run": run_id,
                    "kind": plan_kind,
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:pid, :prop, :sid, :account, :kind, 'h', '{}', :resolved, :gen)"
                ),
                {
                    "pid": plan_id,
                    "prop": proposal_id,
                    "sid": sid,
                    "account": account,
                    "kind": plan_kind,
                    "resolved": _resolved_json(resolved),
                    "gen": G1,
                },
            )
            session.commit()
        return plan_id

    def seed_binding(self, *, run_id=RUN_ID, sid=STRATEGY, account=ACCOUNT, env="paper"):
        from backend.strategies.attribution import SqlAttributionStore

        SqlAttributionStore(session_factory=self.factory).bind_run(
            strategy_run_id=run_id,
            strategy_id=sid,
            owner_id=OWNER,
            account_id=account,
            execution_environment=env,
            bound_by="test",
            binding_source="hosted_job",
        )

    def seed_book(self, *, token=738561, product="CNC", qty=0, sid=STRATEGY, account=ACCOUNT):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " product, canonical_instrument_id, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES (:account, :sid, 'paper', 'canonical', :inst, :product, :inst, "
                    " :token, 'NSE', 'RELIANCE', :qty, 1)"
                ),
                {
                    "account": account,
                    "sid": sid,
                    "inst": INST_ID,
                    "product": product,
                    "token": token,
                    "qty": qty,
                },
            )
            session.commit()

    def seed_lot_size(self, lot_size, *, instrument_id=INST_ID):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, lot_size, "
                    " current_generation_id) "
                    "VALUES (:iid, 'NSE', 'RELIANCE', 'active', :lot, :gen)"
                ),
                {"iid": instrument_id, "lot": lot_size, "gen": G1},
            )
            session.commit()

    # ------------------------------------------------------------------ reads

    def events(self, plan_id, *, step_no=None):
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        with self.factory() as session:
            query = session.query(StrategyPlanExecutionEvent).filter(
                StrategyPlanExecutionEvent.plan_id == plan_id
            )
            if step_no is not None:
                query = query.filter(StrategyPlanExecutionEvent.step_no == step_no)
            return [
                {
                    "step_no": row.step_no,
                    "event": row.event,
                    "paper_order_id": row.paper_order_id,
                    "filled_quantity": row.filled_quantity,
                    "refusal_reason": row.refusal_reason,
                    "actor_id": row.actor_id,
                    "detail": dict(row.detail or {}),
                }
                for row in query.order_by(StrategyPlanExecutionEvent.created_at).all()
            ]


def _resolved_json(resolved):
    import json

    return json.dumps(resolved)


# ---------------------------------------------------------------------------
# Task 1: the event trail schema (D-3)
# ---------------------------------------------------------------------------


class ExecutionEventSchemaTests(ExecutionTestCase):
    def test_event_trail_table_exists_with_the_sketch_columns(self):
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        columns = {column.name for column in StrategyPlanExecutionEvent.__table__.columns}
        self.assertEqual(
            columns,
            {
                "id",
                "plan_id",
                "step_no",
                "event",
                "paper_order_id",
                "filled_quantity",
                "refusal_reason",
                "actor_id",
                "detail",
                "created_at",
            },
        )

    def test_event_vocabulary_is_the_sketch(self):
        from backend.strategies.attribution_models import PLAN_EXECUTION_EVENTS

        self.assertEqual(
            PLAN_EXECUTION_EVENTS, ("submitted", "filled", "rejected", "failed", "no_op")
        )

    def test_an_event_references_a_real_plan(self):
        self.seed_strategy()
        self.seed_validated_plan()
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        with self.factory() as session:
            session.add(
                StrategyPlanExecutionEvent(
                    id=str(uuid.uuid4()),
                    plan_id="plan-1",
                    step_no=1,
                    event="submitted",
                    actor_id=OWNER,
                    detail={"quantity": 100},
                )
            )
            session.commit()
        trail = self.events("plan-1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["event"], "submitted")

    def test_an_event_for_an_unknown_plan_is_refused(self):
        from backend.strategies.attribution_models import StrategyPlanExecutionEvent

        with self.factory() as session:
            session.add(
                StrategyPlanExecutionEvent(
                    id=str(uuid.uuid4()),
                    plan_id="plan-missing",
                    step_no=1,
                    event="submitted",
                    actor_id=OWNER,
                    detail={},
                )
            )
            with self.assertRaises(Exception):
                session.commit()

    def test_bundle_vocabulary_is_admitted_for_proposals_and_plans(self):
        """``intent_bundle`` plans are storable (D-6): the vocabulary is widened,
        never narrowed — every previously valid row stays valid."""
        self.seed_strategy()
        self.seed_validated_plan(
            "plan-bundle", plan_kind="intent_bundle", legs=[dict(SINGLE_LEGS[0])]
        )
        with self.factory() as session:
            target_kind = session.execute(
                text("SELECT target_kind FROM strategy_proposals WHERE proposal_id = 'prop-plan-bundle'")
            ).scalar()
            plan_kind = session.execute(
                text("SELECT plan_kind FROM strategy_plans WHERE plan_id = 'plan-bundle'")
            ).scalar()
        self.assertEqual(target_kind, "intent_bundle")
        self.assertEqual(plan_kind, "intent_bundle")


if __name__ == "__main__":
    unittest.main()
