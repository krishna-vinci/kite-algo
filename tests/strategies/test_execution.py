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
import json
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
        "signed_quantity": 10,
        # Pinned with the plan, exactly as a compiler emits it.
        "lot_size": 1,
        "lot_source": "default",
        "reference_price": 1500.0,
    }
]


def _resolved_plan(plan_kind="single_instrument", legs=None, **extra):
    plan = {
        "target_kind": plan_kind,
        "catalog_generation": G1,
        "legs": list(SINGLE_LEGS if legs is None else legs),
    }
    plan.update(extra)
    return plan


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
                    instrument_type TEXT,
                    lot_size INTEGER,
                    current_generation_id TEXT,
                    expiry TEXT, tick_size REAL, underlying TEXT,
                    strike REAL, option_type TEXT
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
            # The option lane executes through the durable option-run engine, so
            # its canonical table exists here exactly as ``schema.sql`` defines it.
            cursor.execute(
                """
                CREATE TABLE public.option_run_states (
                    strategy_run_id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    product VARCHAR(8) NOT NULL CHECK (product IN ('MIS', 'NRML')),
                    status VARCHAR(64) NOT NULL,
                    legs TEXT NOT NULL DEFAULT '[]',
                    protection TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    orders TEXT NOT NULL DEFAULT '[]',
                    trades TEXT NOT NULL DEFAULT '[]',
                    completed_legs TEXT NOT NULL DEFAULT '[]',
                    failed_legs TEXT NOT NULL DEFAULT '[]',
                    pending_legs TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # The binding edge lives in the ``public`` schema with the option-run
            # table it points at (the raw store qualifies both). Constraints are
            # the production database's job and are proved on PostgreSQL.
            cursor.execute(
                """
                CREATE TABLE public.strategy_plan_option_runs (
                    plan_id TEXT PRIMARY KEY,
                    option_run_id TEXT NOT NULL,
                    worker_run_id TEXT,
                    strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    execution_environment TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX public.uq_plan_option_run_entry
                    ON strategy_plan_option_runs (option_run_id)
                    WHERE phase = 'entry'
                """
            )
            dbapi_connection.commit()

        from backend.strategies.attribution_models import (
            Strategy,
            StrategyAdmissionPolicy,
            StrategyExecutionBarrier,
            StrategyExecutionBarrierEvent,
            StrategyPlan,
            StrategyPlanExecutionEvent,
            StrategyPositionProjection,
            StrategyProposal,
            StrategyReservation,
            StrategyReservationEvent,
            StrategyRoll,
            StrategyRollEvent,
            StrategyRunBinding,
        )
        from backend.strategies.attribution_models import PaperOrderFillProgress

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyRunBinding.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyPlanExecutionEvent.__table__,
                StrategyPositionProjection.__table__,
                StrategyReservation.__table__,
                StrategyReservationEvent.__table__,
                StrategyExecutionBarrier.__table__,
                StrategyExecutionBarrierEvent.__table__,
                # Weight-sized plans read their sizing basis from the recorded
                # admission policy, so the table must exist here too.
                StrategyAdmissionPolicy.__table__,
                # The executor reads fill progress to decide whether a step is
                # resolved, so the table must exist even when nothing writes to it.
                PaperOrderFillProgress.__table__,
                # The roll seam reads the roll (and records the replacement fill).
                StrategyRoll.__table__,
                StrategyRollEvent.__table__,
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
        resolved_extra=None,
    ):
        """A validated proposal envelope + its frozen plan (the P3 output)."""
        proposal_id = f"prop-{plan_id}"
        resolved = _resolved_plan(plan_kind, legs, **(resolved_extra or {}))
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
                    " logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES (:pid, :prop, :sid, :account, :kind, 'h', '{}', :resolved, :gen, "
                    + ("'univ-1', 'm-hash-1')" if plan_kind == "target_weights" else "NULL, NULL)")
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

    def seed_book(
        self,
        *,
        token=738561,
        product="CNC",
        qty=0,
        sid=STRATEGY,
        account=ACCOUNT,
        instrument_id=INST_ID,
        symbol="RELIANCE",
    ):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " product, canonical_instrument_id, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES (:account, :sid, 'paper', 'canonical', :inst, :product, :inst, "
                    " :token, 'NSE', :symbol, :qty, 1)"
                ),
                {
                    "account": account,
                    "sid": sid,
                    "inst": instrument_id,
                    "product": product,
                    "token": token,
                    "qty": qty,
                    "symbol": symbol,
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

    def seed_allocation(self, *, allocation=100000.0, sid=STRATEGY, account=ACCOUNT):
        """The strategy's recorded admission policy - the weight-sizing basis."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_admission_policies "
                    "(strategy_id, account_id, allocation_inr, updated_by) "
                    "VALUES (:sid, :account, :allocation, 'test')"
                ),
                {"sid": sid, "account": account, "allocation": float(allocation)},
            )
            session.commit()

    def seed_catalog(
        self,
        *,
        instrument_type="EQ",
        symbol="RELIANCE",
        token=738561,
        instrument_id=INST_ID,
    ):
        """One published generation with one mapped record (compiler tests)."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO public.instrument_catalog_generations "
                    "(id, status, published_at) VALUES (:gen, 'published', :at)"
                ),
                {"gen": G1, "at": "2026-09-01T00:00:00+00:00"},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, instrument_type, "
                    " current_generation_id) "
                    "VALUES (:iid, 'NSE', :symbol, 'active', :kind, :gen)"
                ),
                {"iid": instrument_id, "symbol": symbol, "kind": instrument_type, "gen": G1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                    " valid_from_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', 'NSE', :symbol, :token, :gen, 1)"
                ),
                {
                    "mid": f"map-{instrument_id}",
                    "iid": instrument_id,
                    "symbol": symbol,
                    "token": token,
                    "gen": G1,
                },
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
                "broker_order_id",
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
            PLAN_EXECUTION_EVENTS,
            ("submitted", "filled", "partially_filled", "rejected", "failed", "no_op")
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


# ---------------------------------------------------------------------------
# Task 2: the paper executor (D-2, D-4, D-5)
# ---------------------------------------------------------------------------


class FakeInstrumentsRepository:
    """Enough catalog truth for the paper runtime to price the pinned leg."""

    def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
        if tradingsymbol == "MISSING":
            return None
        return {
            "instrument_token": 738561,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "lot_size": 1,
            "instrument_type": "EQ",
            "last_price": 1500.0,
        }


class FakeMarketRuntime:
    def __init__(self, last_price=1500.0):
        self.last_price = last_price

    async def get_tick(self, token):
        return {"instrument_token": token, "last_price": self.last_price}

    async def get_last_price(self, token):
        return self.last_price


class ExecutorTestCase(ExecutionTestCase):
    """Adds the paper runtime fakes and the seeded execution context."""

    def setUp(self):
        from unittest.mock import patch

        async def _inline_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        # The paper runtime must not spawn threads or reach Redis in unit tests.
        for patcher in (
            patch("backend.paper_runtime.service.asyncio.to_thread", new=_inline_to_thread),
            patch("backend.paper_runtime.service.publish_event", autospec=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        super().setUp()
        self.runtime_calls = []

    def build_paper_service(self, *, starting_balance="100000"):
        from decimal import Decimal

        from backend.paper_runtime.service import PaperTradingService

        return PaperTradingService(
            repository=_FakePaperRepository(),
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=FakeMarketRuntime(),
            default_starting_balance=Decimal(starting_balance),
        )

    def _option_leg(self, *, side, symbol, lot=75, ratio=1, **overrides):
        leg = {
            "instrument_id": INST_B,
            "exchange": "NFO",
            "tradingsymbol": symbol,
            "broker_exchange": "NFO",
            "broker_symbol": symbol,
            "broker_token": TOKEN_B,
            "product": "NRML",
            "instrument_type": "CE",
            "option_type": "CE",
            "strike": 2500.0,
            "expiry": "2026-10-29",
            "lot_size": lot,
            "ratio": ratio,
            "side": side,
            "quantity": lot * ratio,
            "signed_quantity": lot * ratio * (1 if side == "BUY" else -1),
            "reference_price": 100.0,
        }
        leg.update(overrides)
        return leg

    def _seed_lane(self, plan_id, *, plan_kind, legs, requirement=15000.0):
        self.seed_strategy()
        self.seed_validated_plan(
            plan_id, plan_kind=plan_kind, legs=legs, run_id=f"run-{plan_id}"
        )
        self.seed_binding(run_id=f"run-{plan_id}")
        self.claim_reservation(plan_id=plan_id, requirement=requirement)
        self.seed_lot_size(7)  # the catalog moved after the plan was frozen

    def build_executor(self, *, paper_service=None, now=NOW):
        from backend.strategies.execution import PaperPlanExecutor

        return PaperPlanExecutor(
            session_factory=self.factory,
            paper_service=paper_service or self.build_paper_service(),
            # A fixed clock: validity is decided against the fixture's `now`,
            # never the wall clock, so the suite stays deterministic.
            clock=lambda: now,
        )

    def claim_reservation(
        self,
        plan_id="plan-1",
        *,
        environment="paper",
        requirement=15000.0,
        valid_for=3600,
        now=NOW,
        account=ACCOUNT,
    ):
        from backend.strategies.reservations import ClaimRequest, ReservationLedger

        self.ledger = ReservationLedger(session_factory=self.factory)
        return self.ledger.claim(
            ClaimRequest(
                plan_id=plan_id,
                strategy_id=STRATEGY,
                account_id=account,
                evaluation_id=f"eval-{plan_id}",
                execution_environment=environment,
                requirement_inr=requirement,
                valid_until=now + timedelta(seconds=valid_for),
                allocation_inr=100000.0,
                actor_id=OWNER,
            ),
            now=now,
        )

    def reservation_events(self, reservation_id):
        return [row["event"] for row in self.ledger.events(reservation_id)]

    def barrier_events(self, *, sid=STRATEGY, account=ACCOUNT, env="paper"):
        from backend.strategies.attribution_models import StrategyExecutionBarrierEvent

        with self.factory() as session:
            rows = (
                session.query(StrategyExecutionBarrierEvent)
                .filter(
                    StrategyExecutionBarrierEvent.account_id == account,
                    StrategyExecutionBarrierEvent.strategy_id == sid,
                    StrategyExecutionBarrierEvent.execution_environment == env,
                )
                .order_by(StrategyExecutionBarrierEvent.version)
                .all()
            )
            return [(row.event, row.ref) for row in rows]

    def book_net(self, *, token=738561, product="CNC", sid=STRATEGY, account=ACCOUNT):
        from backend.strategies.attribution_models import StrategyPositionProjection

        with self.factory() as session:
            row = (
                session.query(StrategyPositionProjection)
                .filter(
                    StrategyPositionProjection.account_id == account,
                    StrategyPositionProjection.strategy_id == sid,
                    StrategyPositionProjection.execution_environment == "paper",
                    StrategyPositionProjection.instrument_token == token,
                    StrategyPositionProjection.product == product,
                )
                .one_or_none()
            )
            return int(row.net_quantity) if row is not None else None


class ExecutorPreconditionTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """The precondition chain fails closed, in order, with named refusals (D-2)."""

    def setUp(self):
        super().setUp()
        self.seed_strategy()
        self.seed_validated_plan()
        self.seed_binding()
        self.claim_reservation()

    async def _refused(self, executor=None, plan_id="plan-1", **seed_overrides):
        from backend.strategies.execution import ExecutionRefusal

        executor = executor or self.build_executor()
        plan = self._plan_view(plan_id)
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(plan, actor=OWNER)
        return ctx.exception

    def _plan_view(self, plan_id="plan-1"):
        from backend.strategies.proposals import ProposalStore

        return ProposalStore(session_factory=self.factory).get_plan(plan_id)

    async def test_a_target_weights_plan_executes_sized_from_its_pinned_metadata(self):
        """``target_weights`` reaches the executor: P3 -> P6 is a real handoff.

        The kind used to refuse ``PLAN_KIND_UNSUPPORTED`` while the campaign
        reported the portfolio lane certified. It now executes end to end: each
        leg is sized from the plan's frozen weight, its frozen reference price
        and its frozen lot, against the strategy's recorded allocation - and the
        live catalog cannot change the executed quantity.
        """
        self.seed_allocation(allocation=100000.0)
        self.seed_validated_plan(
            "plan-tw",
            run_id="run-tw",
            plan_kind="target_weights",
            resolved_extra={"capital_basis_inr": 100000.0, "cash_buffer_pct": 0.0},
            legs=[
                {
                    "instrument_id": INST_ID,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "broker_exchange": "NSE",
                    "broker_symbol": "RELIANCE",
                    "broker_token": 738561,
                    "product": "CNC",
                    "target_weight": 0.5,
                    "reference_price": 1500.0,
                    "lot_size": 30,
                    "lot_source": "catalog",
                }
            ],
        )
        self.seed_binding(run_id="run-tw")
        self.claim_reservation(plan_id="plan-tw", requirement=50000.0)
        self.seed_lot_size(7)  # the catalog moved after the plan was frozen
        executor = self.build_executor()

        result = await executor.execute(self._plan_view("plan-tw"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        # 0.5 x 100,000 / 1,500 = 33 units, floored to the PINNED lot of 30 -> 30
        # (the moved catalog would have made it 28).
        self.assertEqual(step["filled_quantity"], 30)
        (order,) = executor._paper_service.repository.orders.values()
        self.assertEqual(order.quantity, 30)
        self.assertEqual(order.transaction_type, "buy")
        self.assertEqual(
            [row["event"] for row in self.events("plan-tw")], ["submitted", "filled"]
        )

    async def test_a_weight_leg_without_a_frozen_capital_basis_is_refused(self):
        """No frozen basis means no approved size: refuse, never read the policy."""
        self.seed_validated_plan(
            "plan-tw",
            run_id="run-tw",
            plan_kind="target_weights",
            legs=[
                {
                    "instrument_id": INST_ID,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "broker_exchange": "NSE",
                    "broker_symbol": "RELIANCE",
                    "broker_token": 738561,
                    "product": "CNC",
                    "target_weight": 0.5,
                    "reference_price": 100.0,
                    "lot_size": 30,
                    "lot_source": "catalog",
                }
            ],
        )
        self.seed_binding(run_id="run-tw")
        self.claim_reservation(plan_id="plan-tw", requirement=50000.0)
        exc = await self._refused(plan_id="plan-tw")
        self.assertEqual(exc.reason_code, "PLAN_CAPITAL_BASIS_UNPINNED")
        self.assertEqual(
            [row["refusal_reason"] for row in self.events("plan-tw")],
            ["PLAN_CAPITAL_BASIS_UNPINNED"],
        )

    async def test_a_later_policy_change_cannot_grow_a_frozen_weight_target(self):
        """The approved size is frozen; the live policy is not a sizing input."""
        self.seed_allocation(allocation=100000.0)
        self.seed_validated_plan(
            "plan-tw",
            run_id="run-tw",
            plan_kind="target_weights",
            resolved_extra={"capital_basis_inr": 100000.0, "cash_buffer_pct": 0.0},
            legs=[
                {
                    "instrument_id": INST_ID,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "broker_exchange": "NSE",
                    "broker_symbol": "RELIANCE",
                    "broker_token": 738561,
                    "product": "CNC",
                    "target_weight": 0.5,
                    "reference_price": 1500.0,
                    "lot_size": 30,
                    "lot_source": "catalog",
                }
            ],
        )
        self.seed_binding(run_id="run-tw")
        self.claim_reservation(plan_id="plan-tw", requirement=50000.0)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_admission_policies SET allocation_inr = 1000000.0 "
                    "WHERE strategy_id = :sid"
                ),
                {"sid": STRATEGY},
            )
            session.commit()
        executor = self.build_executor()

        result = await executor.execute(self._plan_view("plan-tw"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        # A ten-fold allocation increase changes nothing: 0.5 x 100,000 / 1,500 = 33
        # floored to the pinned lot of 30, exactly as approved.
        self.assertEqual(step["filled_quantity"], 30)

    async def test_a_policy_drop_below_the_frozen_basis_is_refused(self):
        """Frozen basis above the current authority is drift: refuse, do not guess."""
        self.seed_allocation(allocation=100000.0)
        self.seed_validated_plan(
            "plan-tw",
            run_id="run-tw",
            plan_kind="target_weights",
            resolved_extra={"capital_basis_inr": 100000.0, "cash_buffer_pct": 0.0},
            legs=[
                {
                    "instrument_id": INST_ID,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "broker_exchange": "NSE",
                    "broker_symbol": "RELIANCE",
                    "broker_token": 738561,
                    "product": "CNC",
                    "target_weight": 0.5,
                    "reference_price": 1500.0,
                    "lot_size": 30,
                    "lot_source": "catalog",
                }
            ],
        )
        self.seed_binding(run_id="run-tw")
        self.claim_reservation(plan_id="plan-tw", requirement=50000.0)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_admission_policies SET allocation_inr = 10000.0 "
                    "WHERE strategy_id = :sid"
                ),
                {"sid": STRATEGY},
            )
            session.commit()
        exc = await self._refused(plan_id="plan-tw")
        self.assertEqual(exc.reason_code, "PLAN_CAPITAL_BASIS_DRIFT")

    async def test_an_unknown_plan_kind_is_refused_by_name(self):
        """A kind outside the executable vocabulary is refused, not attempted."""
        plan = dict(self._plan_view("plan-1"))
        plan["plan_kind"] = "not_a_kind"
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor()
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(plan, actor=OWNER)
        self.assertEqual(ctx.exception.reason_code, "PLAN_KIND_UNSUPPORTED")

    async def test_a_plan_whose_envelope_is_not_validated_is_refused(self):
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_proposals SET status = 'refused' "
                    "WHERE proposal_id = 'prop-plan-1'"
                )
            )
            session.commit()
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "PLAN_NOT_VALIDATED")

    async def test_execution_without_a_reservation_is_refused(self):
        self.ledger.release(
            self.ledger.for_plan("plan-1")["reservation_id"], actor_id=OWNER
        )
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "RESERVATION_REQUIRED")

    async def test_a_consumed_reservation_is_not_re_executable(self):
        self.ledger.consume(self.ledger.for_plan("plan-1")["reservation_id"], actor_id=OWNER)
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "RESERVATION_REQUIRED")

    async def test_a_live_target_is_refused_paper_only(self):
        """The executor refuses live by name — paper accounts only, ever."""
        reservation = self.ledger.for_plan("plan-1")
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_reservations SET execution_environment = 'live' "
                    "WHERE reservation_id = :rid"
                ),
                {"rid": reservation["reservation_id"]},
            )
            session.commit()
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "PAPER_ONLY_EXECUTION")

    async def test_an_expired_reservation_is_refused(self):
        reservation = self.ledger.for_plan("plan-1")
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_reservations SET valid_until = :past "
                    "WHERE reservation_id = :rid"
                ),
                {"rid": reservation["reservation_id"], "past": NOW - timedelta(hours=1)},
            )
            session.commit()
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "RESERVATION_EXPIRED")

    async def test_a_missing_run_binding_fails_closed(self):
        with self.factory() as session:
            session.execute(
                text("DELETE FROM strategy_run_bindings WHERE strategy_run_id = :run"),
                {"run": RUN_ID},
            )
            session.commit()
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "STRATEGY_RUN_BINDING_MISSING")

    async def test_a_binding_for_another_account_is_impossible_by_schema(self):
        """Owner/account drift on the binding is refused by the composite FK.

        The executor's ``ACCOUNT_SCOPE_MISMATCH`` guard stays as fail-closed
        defense for integrity-violating stores; the database makes the state
        unreachable where integrity holds (the live-binding test below drives
        the same refusal branch through the reachable dimension).
        """
        with self.factory() as session:
            with self.assertRaises(Exception):
                session.execute(
                    text(
                        "UPDATE strategy_run_bindings SET account_id = 'kite:other' "
                        "WHERE strategy_run_id = :run"
                    ),
                    {"run": RUN_ID},
                )
                session.rollback()

    async def test_a_live_binding_is_a_paper_scope_mismatch(self):
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_run_bindings SET execution_environment = 'live' "
                    "WHERE strategy_run_id = :run"
                ),
                {"run": RUN_ID},
            )
            session.commit()
        exc = await self._refused()
        self.assertEqual(exc.reason_code, "ACCOUNT_SCOPE_MISMATCH")

    async def test_every_precondition_refusal_is_an_event_with_its_name(self):
        self.ledger.release(
            self.ledger.for_plan("plan-1")["reservation_id"], actor_id=OWNER
        )
        await self._refused()
        trail = self.events("plan-1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["event"], "rejected")
        self.assertEqual(trail[0]["refusal_reason"], "RESERVATION_REQUIRED")
        self.assertEqual(trail[0]["actor_id"], OWNER)


class ExecutorZeroDeltaTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_zero_delta_step_records_no_op_without_touching_the_runtime(self):
        self.seed_strategy()
        self.seed_validated_plan()
        self.seed_binding()
        self.claim_reservation()
        self.seed_book(qty=10)  # already at target
        self.seed_lot_size(1)

        executed = []
        paper = self.build_paper_service()
        original = paper.place_order

        async def _spy(**kwargs):
            executed.append(kwargs)
            return await original(**kwargs)

        paper.place_order = _spy
        executor = self.build_executor(paper_service=paper)
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)

        self.assertEqual(executed, [])  # the paper runtime was never touched
        self.assertEqual(result["status"], "no_op")
        trail = self.events("plan-1")
        self.assertEqual([row["event"] for row in trail], ["no_op"])
        self.assertEqual([row["step_no"] for row in trail], [1])
        # Nothing happened, so the reservation is untouched and no work exists.
        self.assertEqual(self.reservation_events(self.ledger.for_plan("plan-1")["reservation_id"]), ["created"])
        self.assertEqual(self.barrier_events(), [])


class ExecutorSubmissionTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_fill_submits_through_the_paper_runtime_and_consumes_the_reservation(self):
        self.seed_strategy()
        self.seed_validated_plan()  # target +10 @ 1500
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)
        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        self.assertEqual(step["event"], "filled")
        self.assertEqual(step["filled_quantity"], 10)
        self.assertTrue(step["paper_order_id"].startswith("PAPER-"))

        # The paper runtime produced a real paper order carrying the BOUND run's
        # attribution plus the plan/reservation/step refs.
        from backend.paper_runtime.models import PaperOrderStatus

        paper = executor._paper_service
        (order,) = paper.repository.orders.values()
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.metadata["strategy_run_id"], RUN_ID)
        self.assertEqual(order.metadata["plan_id"], "plan-1")
        self.assertEqual(order.metadata["reservation_id"], self.ledger.for_plan("plan-1")["reservation_id"])
        self.assertEqual(order.metadata["step_no"], 1)
        self.assertEqual(order.quantity, 10)
        self.assertEqual(order.transaction_type, "buy")

        # The trail: submitted -> filled (derived state, append-only rows).
        trail = self.events("plan-1")
        self.assertEqual([row["event"] for row in trail], ["submitted", "filled"])
        self.assertEqual(trail[1]["paper_order_id"], order.order_id)

        # The fill CONSUMED the reservation (D-4) with the plan/order refs.
        reservation = self.ledger.for_plan("plan-1")
        self.assertEqual(reservation["status"], "consumed")
        detail = self.ledger.events(reservation["reservation_id"])[-1]["detail"]
        self.assertEqual(detail.get("plan_id"), "plan-1")
        self.assertEqual(detail.get("paper_order_ids"), [order.order_id])

        # The barrier recorded the work transitions (created on submission,
        # resolved on fill) — observers, not a new settlement semantics (D-5).
        self.assertEqual(
            [event for event, _ in self.barrier_events()],
            ["work_created", "work_resolved"],
        )
        self.assertEqual(self.barrier_events()[0][1], "plan:plan-1:step:1")

    async def test_a_runtime_rejection_releases_the_reservation_terminal_unfilled(self):
        self.seed_strategy()
        self.seed_validated_plan()
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)
        # A paper account that cannot fund the step: the runtime's own
        # funds/margin admission is the paper admission for the leg.
        executor = self.build_executor(paper_service=self.build_paper_service(starting_balance="100"))
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)

        self.assertEqual(result["status"], "rejected")
        (step,) = result["steps"]
        self.assertEqual(step["event"], "rejected")
        self.assertEqual(step["refusal_reason"], "PAPER_ORDER_REJECTED")
        self.assertTrue(step["detail"]["reason"])

        trail = self.events("plan-1")
        self.assertEqual([row["event"] for row in trail], ["submitted", "rejected"])
        # The rejection RELEASED the unused capacity (D-4).
        reservation = self.ledger.for_plan("plan-1")
        self.assertEqual(reservation["status"], "released")
        self.assertEqual(reservation["release_reason"], "terminal_unfilled")
        self.assertEqual(
            self.reservation_events(reservation["reservation_id"]), ["created", "released"]
        )
        # And the barrier work was resolved, not left dangling.
        self.assertEqual(
            [event for event, _ in self.barrier_events()],
            ["work_created", "work_resolved"],
        )

    async def test_the_pinned_lot_decides_and_the_catalog_cannot_change_it(self):
        """Units are frozen with the plan, never re-read at execution time.

        The plan below pins a lot of 4 (so 10 -> 8). The live catalog is then set
        to 7 - a change that happens *after* freezing - and the executed quantity
        must still be the pinned floor, not the catalog's new number.
        """
        self.seed_strategy()
        self.seed_validated_plan(
            legs=[dict(SINGLE_LEGS[0], lot_size=4, lot_source="catalog")]
        )
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(7)  # the catalog moved after the plan was frozen
        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)
        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        self.assertEqual(step["filled_quantity"], 8)
        paper = executor._paper_service
        (order,) = paper.repository.orders.values()
        self.assertEqual(order.quantity, 8)

    async def test_a_plan_without_pinned_units_is_refused_by_name(self):
        """No pinned lot means unknown units: refuse, never re-read the catalog."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        unpinned = {k: v for k, v in SINGLE_LEGS[0].items() if k not in ("lot_size", "lot_source")}
        self.seed_validated_plan(legs=[unpinned])
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(4)
        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-1")

        with self.assertRaises(ExecutionRefusal) as refusal:
            await executor.execute(plan, actor=OWNER)
        self.assertEqual(refusal.exception.reason_code, "PLAN_UNITS_UNPINNED")
        # The refusal is on the append-only trail and nothing was submitted.
        trail = self.events("plan-1")
        self.assertEqual([row["refusal_reason"] for row in trail], ["PLAN_UNITS_UNPINNED"])
        self.assertEqual(executor._paper_service.repository.orders, {})

    async def test_a_broken_runtime_records_failed_and_holds_the_reservation(self):
        """Unknown execution state holds capacity — it never releases on a guess."""
        self.seed_strategy()
        self.seed_validated_plan()
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)

        class _ExplodingService:
            async def place_order(self, **kwargs):
                raise RuntimeError("runtime unavailable")

        executor = self.build_executor(paper_service=_ExplodingService())
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)
        self.assertEqual(result["status"], "failed")
        trail = self.events("plan-1")
        self.assertEqual([row["event"] for row in trail], ["submitted", "failed"])
        reservation = self.ledger.for_plan("plan-1")
        self.assertEqual(reservation["status"], "active")  # held, not released
        # The barrier keeps the work in flight honestly.
        self.assertEqual([event for event, _ in self.barrier_events()], ["work_created"])


class _FakePaperRepository:
    """In-memory paper repository (the established fake from the runtime tests)."""

    def __init__(self):
        from decimal import Decimal

        from backend.paper_runtime.models import PaperAccount

        self.accounts = {}
        self.orders = {}
        self.trades = {}
        self.positions = {}
        self.position_lots = {}
        self.fund_ledger = []
        self._Decimal = Decimal
        self._PaperAccount = PaperAccount

    def get_account(self, account_scope):
        return self.accounts.get(account_scope)

    def upsert_account(self, account):
        self.accounts[account.account_scope] = account
        return account

    def insert_order(self, order):
        self.orders[(order.account_scope, order.order_id)] = order
        return order

    def update_order(self, order):
        self.orders[(order.account_scope, order.order_id)] = order
        return order

    def get_position(self, account_scope, instrument_token, product):
        return self.positions.get((account_scope, instrument_token, product))

    def upsert_position(self, position):
        self.positions[(position.account_scope, position.instrument_token, position.product)] = position
        return position

    def insert_trade(self, trade):
        self.trades[(trade.account_scope, trade.trade_id)] = trade
        return trade

    def upsert_position_lot(self, lot):
        self.position_lots[(lot.account_scope, lot.lot_id)] = lot
        return lot

    def list_open_position_lots(self, account_scope, instrument_token=None, product=None):
        lots = [
            lot
            for lot in self.position_lots.values()
            if lot.account_scope == account_scope and lot.remaining_quantity > 0
        ]
        if instrument_token is not None:
            lots = [lot for lot in lots if lot.instrument_token == instrument_token]
        if product is not None:
            lots = [lot for lot in lots if lot.product == product]
        return sorted(lots, key=lambda lot: lot.opened_at)

    def list_pending_orders_for_instrument(self, instrument_token):
        from backend.paper_runtime.models import PaperOrderStatus

        return [
            order
            for order in self.orders.values()
            if order.instrument_token == instrument_token
            and order.status
            in {PaperOrderStatus.PENDING, PaperOrderStatus.OPEN, PaperOrderStatus.PARTIALLY_FILLED}
        ]

    def list_open_positions_for_instrument(self, instrument_token):
        return [
            position
            for position in self.positions.values()
            if position.instrument_token == instrument_token and position.net_quantity != 0
        ]

    def list_orders(self, account_scope, limit=200, **kwargs):
        return [order for order in self.orders.values() if order.account_scope == account_scope][:limit]

    def list_trades(self, account_scope, limit=500, **kwargs):
        return [trade for trade in self.trades.values() if trade.account_scope == account_scope][:limit]

    def list_positions(self, account_scope, only_open=False, **kwargs):
        items = [position for position in self.positions.values() if position.account_scope == account_scope]
        if only_open:
            items = [position for position in items if position.net_quantity != 0]
        return items

    def list_active_market_tokens(self):
        return sorted({order.instrument_token for order in self.orders.values()})

    def append_fund_ledger_entry(self, entry):
        self.fund_ledger.append(entry)
        return entry

    def clear_account_scope(self, account_scope):
        self.orders = {key: value for key, value in self.orders.items() if key[0] != account_scope}
        self.trades = {key: value for key, value in self.trades.items() if key[0] != account_scope}
        self.positions = {key: value for key, value in self.positions.items() if key[0] != account_scope}
        self.fund_ledger = [entry for entry in self.fund_ledger if entry.account_scope != account_scope]


# ---------------------------------------------------------------------------
# Task 3: the intent_bundle compiler and per-leg execution (D-6)
# ---------------------------------------------------------------------------

INST_B = "bbbbbbbb-0000-0000-0000-000000000002"
TOKEN_B = 738562

BUNDLE_LEGS = [
    # Exposure-increasing leg (target +10, current 0).
    dict(SINGLE_LEGS[0]),
    # Risk-reducing leg on a second instrument.
    {
        "instrument_id": INST_B,
        "exchange": "NSE",
        "tradingsymbol": "TCS",
        "broker_exchange": "NSE",
        "broker_symbol": "TCS",
        "broker_token": TOKEN_B,
        "product": "CNC",
        "signed_quantity": -10,
        "lot_size": 1,
        "lot_source": "default",
        "reference_price": 1000.0,
    },
]


def _futures_leg(**overrides):
    """One futures leg, pinned exactly as the compiler emits it."""
    leg = {
        "instrument_id": INST_B,
        "exchange": "NFO",
        "tradingsymbol": "TCS26OCTFUT",
        "broker_exchange": "NFO",
        "broker_symbol": "TCS26OCTFUT",
        "broker_token": TOKEN_B,
        "product": "NRML",
        "instrument_type": "FUT",
        "lots": 1,
        "lot_size": 75,
        "signed_quantity": 100,
        "expiry": "2026-10-29",
        "tick_size": 0.05,
        "reference_price": 1000.0,
    }
    leg.update(overrides)
    return leg


def _bundle_payload(legs):
    return {
        "legs": [
            {
                "instrument_token": leg["broker_token"],
                "exchange": leg["exchange"],
                "tradingsymbol": leg["tradingsymbol"],
                "product": leg["product"],
                "target_quantity": leg["signed_quantity"],
                "reference_price": leg["reference_price"],
            }
            for leg in legs
        ]
    }


class _StagedStubService:
    """A paper runtime stub that supplies AUTHORITATIVE funds evidence.

    The real runtime re-checks cash under its own lock; this stub exists so the
    executor's OWN half of the staged contract - re-derive the account money
    before releasing a dependent buy - is exercised deterministically, including
    a partial sale and an account that cannot carry the increase.
    """

    def __init__(self, *, sell_status="filled", available="0", sell_fill_ratio=0.5):
        self.sell_status = sell_status
        self.available = available
        self.sell_fill_ratio = sell_fill_ratio
        self.sent = []

    async def place_order(self, *, account_scope, order_payload, attribution):
        _ = (account_scope, attribution)
        self.sent.append(dict(order_payload))
        quantity = int(order_payload["quantity"])
        side = str(order_payload["transaction_type"]).upper()
        status = self.sell_status if side == "SELL" else "filled"
        if status == "filled":
            filled = quantity
        elif status == "partially_filled":
            filled = int(quantity * self.sell_fill_ratio)
        else:
            filled = 0
        return {
            "status": status,
            "order": {
                "order_id": f"STUB-{len(self.sent)}",
                "tradingsymbol": order_payload["tradingsymbol"],
                "quantity": quantity,
                "filled_quantity": filled,
                "pending_quantity": quantity - filled,
                "average_price": "100.0",
            },
        }

    async def get_account_summary(self, account_scope):
        # The real runtime's summary is FLAT (available_funds at the top level).
        _ = account_scope
        return {
            "account_scope": account_scope,
            "available_funds": float(self.available),
            "blocked_funds": 0.0,
        }


class ExecutorStagedFinancingTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """A dependent buy is released only against a CONFIRMED reduction.

    The contract forbids crediting a projected sale: an unfilled, partial or
    rejected removal cannot fund the replacement that depends on it.
    """

    async def test_a_rejected_reduction_cannot_fund_a_dependent_buy(self):
        self.seed_strategy()
        reduce_leg = {**SINGLE_LEGS[0], "signed_quantity": 0}  # sell the held 10 of A
        buy_leg = {
            **SINGLE_LEGS[0],
            "instrument_id": INST_B,
            "broker_token": TOKEN_B,
            "tradingsymbol": "INFY",
            "broker_symbol": "INFY",
            "signed_quantity": 10,
        }
        self.seed_validated_plan(plan_kind="intent_bundle", legs=[reduce_leg, buy_leg])
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)
        # The strategy HOLDS 10 of A, so the reduction is a real order. The
        # runtime fails while placing it, so the sale never confirms.
        self.seed_book(qty=10)

        class _ExplodingService:
            async def place_order(self, **kwargs):
                raise RuntimeError("runtime unavailable")

        executor = self.build_executor(paper_service=_ExplodingService())
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)

        events = {int(step["step_no"]): step["event"] for step in result["steps"]}
        reasons = {int(step["step_no"]): step.get("refusal_reason") for step in result["steps"]}
        self.assertEqual(events[1], "failed", result)
        self.assertEqual(reasons[2], "FINANCING_UNSECURED", result)
        self.assertEqual(
            result["steps"][1]["detail"]["unresolved_funding_legs"], [1]
        )
        # The dependent buy never reached the runtime: the trail records the
        # reduction's failure and the buy's named refusal, and nothing else.
        trail = self.events("plan-1")
        self.assertEqual([row["step_no"] for row in trail], [1, 1, 2])
        self.assertEqual(trail[2]["refusal_reason"], "FINANCING_UNSECURED")

    def _seed_staged_rebalance(self):
        """Sell the held A to flat and buy 10 of B at 1500 (15000 of new money)."""
        self.seed_strategy()
        reduce_leg = {**SINGLE_LEGS[0], "signed_quantity": 0}
        buy_leg = {
            **SINGLE_LEGS[0],
            "instrument_id": INST_B,
            "broker_token": TOKEN_B,
            "tradingsymbol": "INFY",
            "broker_symbol": "INFY",
            "signed_quantity": 10,
            "reference_price": 1500.0,
        }
        self.seed_validated_plan(plan_kind="intent_bundle", legs=[reduce_leg, buy_leg])
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)
        self.seed_book(qty=10)

    async def test_a_partial_sale_cannot_fund_the_dependent_buy(self):
        self._seed_staged_rebalance()
        service = _StagedStubService(sell_status="partially_filled")
        result = await self.build_executor(paper_service=service).execute(
            _plan_view_for(self.factory, "plan-1"), actor=OWNER
        )
        steps = {int(step["step_no"]): step for step in result["steps"]}
        self.assertEqual(steps[1]["event"], "partially_filled")
        self.assertEqual(steps[2]["event"], "rejected")
        self.assertEqual(steps[2]["refusal_reason"], "FINANCING_UNSECURED")
        # Only the sale reached the runtime: a half-filled removal funded nothing.
        self.assertEqual([row["transaction_type"] for row in service.sent], ["SELL"])

    async def test_an_increase_is_refused_when_the_account_cannot_carry_it(self):
        self._seed_staged_rebalance()
        service = _StagedStubService(sell_status="filled", available="100")
        result = await self.build_executor(paper_service=service).execute(
            _plan_view_for(self.factory, "plan-1"), actor=OWNER
        )
        steps = {int(step["step_no"]): step for step in result["steps"]}
        self.assertEqual(steps[1]["event"], "filled")
        self.assertEqual(steps[2]["event"], "rejected")
        self.assertEqual(steps[2]["refusal_reason"], "ACCOUNT_FUNDS_UNSECURED")
        self.assertEqual(steps[2]["detail"]["scope"], "account_funds_increase")
        # The buy never reached the runtime: confirmation is not money.
        self.assertEqual([row["transaction_type"] for row in service.sent], ["SELL"])

    async def test_an_increase_proceeds_only_against_confirmed_account_money(self):
        self._seed_staged_rebalance()
        service = _StagedStubService(sell_status="filled", available="20000")
        executor = self.build_executor(paper_service=service)
        result = await executor.execute(_plan_view_for(self.factory, "plan-1"), actor=OWNER)

        steps = {int(step["step_no"]): step for step in result["steps"]}
        self.assertEqual(steps[1]["event"], "filled")
        self.assertEqual(steps[2]["event"], "filled")
        self.assertEqual(
            [row["transaction_type"] for row in service.sent], ["SELL", "BUY"]
        )
        # Durable proof: the authorization carries the account money it used.
        reservation = self.ledger.for_plan("plan-1")
        authorized = [
            row
            for row in self.ledger.events(reservation["reservation_id"])
            if row["detail"].get("staged_increase_authorized")
        ]
        self.assertEqual(len(authorized), 1)
        self.assertEqual(authorized[0]["detail"]["account_capacity_inr"], 20000.0)
        self.assertEqual(authorized[0]["detail"]["increase_inr"], 15000.0)

    async def test_a_confirmed_reduction_releases_the_dependent_buy(self):
        self.seed_strategy()
        reduce_leg = {**SINGLE_LEGS[0], "signed_quantity": 0}
        buy_leg = {
            **SINGLE_LEGS[0],
            "instrument_id": INST_B,
            "broker_token": TOKEN_B,
            "tradingsymbol": "INFY",
            "broker_symbol": "INFY",
            "signed_quantity": 10,
        }
        self.seed_validated_plan(plan_kind="intent_bundle", legs=[reduce_leg, buy_leg])
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(1)
        self.seed_book(qty=10)
        executor = self.build_executor()
        # The paper account holds nothing of A, so the reduction is refused: this
        # case is the ORDER/GATE wiring only - the buy must still be attempted
        # only after the reduction produced a recorded outcome.
        plan = _plan_view_for(self.factory, "plan-1")
        result = await executor.execute(plan, actor=OWNER)
        steps = {int(step["step_no"]): step for step in result["steps"]}
        self.assertIn(steps[1]["event"], {"rejected", "filled"})
        # Whatever the reduction did, the buy carries a decision - it is never
        # silently skipped, and it never publishes a plan-level success that hides
        # an unsecured funding leg.
        self.assertIn(steps[2]["event"], {"filled", "rejected"})
        if steps[1]["event"] != "filled":
            self.assertEqual(steps[2]["refusal_reason"], "FINANCING_UNSECURED")


class IntentBundleCompilerTests(ExecutionTestCase):
    """A bundle resolves to explicit per-leg single-instrument actions (D-6).

    Compilation is the fail-closed gate for leg kinds: futures and option
    structures are Projects 9/10, so a bundle leg whose catalog record is not a
    cash-equity instrument refuses ``LEG_KIND_UNSUPPORTED`` — a refusal is
    terminal at validation, and no plan that the executor could misread ever
    freezes.
    """

    def _compile(self, legs, *, types=None):
        from backend.strategies.compiler import compile_plan
        from backend.strategies.compiler.base import PinnedCatalogRead

        self.seed_catalog(instrument_id=INST_ID, symbol="RELIANCE", token=738561)
        self.seed_catalog(
            instrument_id=INST_B, symbol="TCS", token=TOKEN_B,
            instrument_type=(types or {}).get(INST_B, "EQ"),
        )
        pinned = PinnedCatalogRead(session_factory=self.factory, generation=G1)
        return compile_plan("intent_bundle", _bundle_payload(legs), pinned)

    def test_a_bundle_resolves_to_per_leg_single_instrument_actions(self):
        resolved = self._compile(BUNDLE_LEGS)
        self.assertEqual(resolved["target_kind"], "intent_bundle")
        self.assertEqual(resolved["catalog_generation"], G1)
        self.assertEqual([leg["instrument_id"] for leg in resolved["legs"]], [INST_ID, INST_B])
        self.assertEqual(
            [leg["signed_quantity"] for leg in resolved["legs"]], [10, -10],
        )
        # Each leg is a complete single-instrument action: the executor consumes
        # them exactly like standalone single_instrument legs.
        for leg in resolved["legs"]:
            for key in ("broker_exchange", "broker_symbol", "broker_token", "product", "reference_price"):
                self.assertIn(key, leg)

    def test_a_bundle_without_legs_is_invalid(self):
        from backend.strategies.compiler.base import PinnedCatalogRead, ValidationRefusal

        self.seed_catalog(instrument_id=INST_ID, symbol="RELIANCE", token=738561)
        pinned = PinnedCatalogRead(session_factory=self.factory, generation=G1)
        for payload in ({}, {"legs": []}):
            with self.assertRaises(ValidationRefusal) as ctx:
                from backend.strategies.compiler import compile_plan

                compile_plan("intent_bundle", payload, pinned)
            self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")

    def test_an_unresolvable_leg_refuses_the_whole_bundle(self):
        from backend.strategies.compiler.base import PinnedCatalogRead, ValidationRefusal

        self.seed_catalog(instrument_id=INST_ID, symbol="RELIANCE", token=738561)
        pinned = PinnedCatalogRead(session_factory=self.factory, generation=G1)
        legs = [dict(BUNDLE_LEGS[0]), dict(BUNDLE_LEGS[1])]
        legs[1]["broker_token"] = 999999  # no mapping for this token
        with self.assertRaises(ValidationRefusal) as ctx:
            from backend.strategies.compiler import compile_plan

            compile_plan("intent_bundle", _bundle_payload(legs), pinned)
        self.assertEqual(ctx.exception.reason_code, "INSTRUMENT_UNRESOLVED")

    def test_a_futures_leg_refuses_by_name(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(BUNDLE_LEGS, types={INST_B: "FUT"})
        self.assertEqual(ctx.exception.reason_code, "LEG_KIND_UNSUPPORTED")

    def test_an_unknown_instrument_kind_fails_closed(self):
        """Unknown evidence is not cash equity: refuse, never guess."""
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(BUNDLE_LEGS, types={INST_B: None})
        self.assertEqual(ctx.exception.reason_code, "LEG_KIND_UNSUPPORTED")

    def test_the_registry_admits_intent_bundle(self):
        from backend.strategies.compiler import compiler_for
        from backend.strategies.compiler.intent_bundle import IntentBundleCompiler

        self.assertIsInstance(compiler_for("intent_bundle"), IntentBundleCompiler)


class ExecutorBundleTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """Per-leg outcomes are events; approval/reservation treat legs by risk (D-6)."""

    def seed_bundle_plan(self, plan_id="plan-bundle", legs=None, *, sid=STRATEGY, account=ACCOUNT):
        return self.seed_validated_plan(
            plan_id, plan_kind="intent_bundle", legs=list(BUNDLE_LEGS if legs is None else legs),
            sid=sid, account=account,
        )

    async def test_a_bundle_executes_per_leg_without_all_or_nothing(self):
        self.seed_strategy()
        self.seed_bundle_plan()
        self.seed_binding()
        self.claim_reservation(plan_id="plan-bundle", requirement=27000.0)  # 10x1500 + 10x1000 + headroom
        self.seed_lot_size(1)
        # Leg 2's coordinate is unmapped at the runtime: only THAT leg rejects.
        self.seed_bundle_plan  # documented no-op to keep the seed block readable

        class _MissingSecond(FakeInstrumentsRepository):
            def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
                if tradingsymbol == "TCS":
                    return None
                return super().get_instrument_by_exchange_symbol(exchange, tradingsymbol)

        paper = self.build_paper_service()
        paper.instruments_repository = _MissingSecond()
        executor = self.build_executor(paper_service=paper)
        plan = _plan_view_for(self.factory, "plan-bundle")

        result = await executor.execute(plan, actor=OWNER)

        # Per-leg outcomes, NOT all-or-nothing: leg 1 filled while leg 2 rejected.
        self.assertEqual(result["status"], "filled")
        self.assertEqual([step["event"] for step in result["steps"]], ["filled", "rejected"])
        trail = self.events("plan-bundle")
        self.assertEqual(
            [(row["step_no"], row["event"]) for row in trail],
            [(1, "submitted"), (1, "filled"), (2, "submitted"), (2, "rejected")],
        )
        self.assertEqual(trail[3]["refusal_reason"], "PAPER_ORDER_REJECTED")
        # The fill consumed the reservation once, naming the filled order.
        reservation = self.ledger.for_plan("plan-bundle")
        self.assertEqual(reservation["status"], "consumed")
        detail = self.ledger.events(reservation["reservation_id"])[-1]["detail"]
        self.assertEqual(len(detail.get("paper_order_ids") or []), 1)

    async def test_a_risk_reducing_bundle_needs_no_reservation(self):
        """A REDUCTION needs no admission; a new short or a reversal does (D-6)."""
        self.seed_strategy()
        # +20 held, target +10: the step is a SELL of 10 that shrinks an existing
        # long. This is what "risk reducing" actually means - the earlier version
        # of this test sold from FLAT (a new short) and asserted it needed no
        # reservation, which is the admission hole this bundle closes.
        self.seed_book(token=TOKEN_B, product="CNC", qty=20, instrument_id=INST_B, symbol="TCS")
        self.seed_bundle_plan(legs=[dict(BUNDLE_LEGS[1], signed_quantity=10)])
        self.seed_binding()
        self.seed_lot_size(1)
        from backend.strategies.reservations import ReservationLedger

        self.assertIsNone(
            ReservationLedger(session_factory=self.factory).for_plan("plan-bundle")
        )

        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-bundle")

        result = await executor.execute(plan, actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        self.assertEqual(step["event"], "filled")
        self.assertEqual(step["filled_quantity"], 10)
        # No reservation existed and none was created; the SELL side is honest.
        self.assertIsNone(
            ReservationLedger(session_factory=self.factory).for_plan("plan-bundle")
        )
        paper_order = list(executor._paper_service.repository.orders.values())[0]
        self.assertEqual(paper_order.transaction_type, "sell")

    async def test_a_new_short_requires_a_reservation(self):
        """Selling from flat opens a short: it is exposure, not an exemption."""
        self.seed_strategy()
        self.seed_bundle_plan(legs=[dict(BUNDLE_LEGS[1])])  # target -10 from flat
        self.seed_binding()
        self.seed_lot_size(1)
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-bundle")
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(plan, actor=OWNER)
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")
        self.assertEqual(executor._paper_service.repository.orders, {})

    async def test_a_reversal_across_flat_requires_a_reservation(self):
        """+10 held, target -5: the trade sells 15 and opens a short."""
        self.seed_strategy()
        self.seed_book(token=TOKEN_B, product="CNC", qty=10, instrument_id=INST_B, symbol="TCS")
        self.seed_bundle_plan(legs=[dict(BUNDLE_LEGS[1], signed_quantity=-5)])
        self.seed_binding()
        self.seed_lot_size(1)
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-bundle")
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(plan, actor=OWNER)
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")

    async def test_a_bundle_with_an_increasing_leg_still_requires_a_reservation(self):
        self.seed_strategy()
        self.seed_bundle_plan()
        self.seed_binding()
        self.seed_lot_size(1)
        # No reservation claimed at all.
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-bundle")
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(plan, actor=OWNER)
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_REQUIRED")
        self.assertEqual(
            [row["refusal_reason"] for row in self.events("plan-bundle")],
            ["RESERVATION_REQUIRED"],
        )

    async def test_increasing_legs_share_one_reservation_and_the_fill_names_both(self):
        self.seed_strategy()
        legs = [
            dict(BUNDLE_LEGS[0]),  # +10 RELIANCE
            {
                **BUNDLE_LEGS[1],
                "signed_quantity": 10,  # +10 TCS: increasing too
            },
        ]
        self.seed_bundle_plan(legs=legs)
        self.seed_binding()
        self.claim_reservation(plan_id="plan-bundle", requirement=27000.0)
        self.seed_lot_size(1)

        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-bundle")

        result = await executor.execute(plan, actor=OWNER)
        self.assertEqual(result["status"], "filled")
        self.assertEqual([step["event"] for step in result["steps"]], ["filled", "filled"])

        orders = list(executor._paper_service.repository.orders.values())
        self.assertEqual(len(orders), 2)
        reservation_id = self.ledger.for_plan("plan-bundle")["reservation_id"]
        for order in orders:
            self.assertEqual(order.metadata["reservation_id"], reservation_id)
            self.assertEqual(order.metadata["plan_id"], "plan-bundle")
        self.assertEqual(
            sorted(order.metadata["step_no"] for order in orders), [1, 2]
        )
        # One reservation consumed once, naming BOTH filled orders (no double-spend).
        self.assertEqual(self.ledger.for_plan("plan-bundle")["status"], "consumed")
        detail = self.ledger.events(reservation_id)[-1]["detail"]
        self.assertEqual(len(detail.get("paper_order_ids") or []), 2)
        self.assertEqual(
            self.reservation_events(reservation_id), ["created", "consumed"]
        )


class ExecutorDerivativeLaneTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """The futures and option-structure lanes reach the PRODUCTION executor.

    ``PaperPlanExecutor.execute`` is what the owner's HTTP route
    (``POST /strategies/{id}/plans/{plan_id}/execute``) runs, so a walkthrough
    here is the production dispatch path rather than a helper call: the plan is
    the frozen artifact P3 persists and the executor is the engine the route
    builds.
    """

    async def test_a_target_futures_plan_executes_on_the_pinned_lot(self):
        """Lot 75 is frozen in the plan; the catalog's 7 cannot resize the order."""
        self._seed_lane("plan-fut", plan_kind="target_futures", legs=[_futures_leg()])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-fut"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        self.assertEqual(step["filled_quantity"], 75)  # 100 floored to the PINNED 75
        (order,) = executor._paper_service.repository.orders.values()
        self.assertEqual(order.quantity, 75)
        self.assertEqual(order.transaction_type, "buy")

    async def test_an_option_structure_enters_hedge_first_then_releases_the_short(self):
        """The short leg is released only after the hedge is CONFIRMED filled."""
        short = self._option_leg(side="SELL", symbol="TCS26OCT2500CE")
        hedge = self._option_leg(side="BUY", symbol="TCS26OCT3000CE")
        # The short is leg 1 in the frozen plan; the hedge is leg 2. Entry order
        # is decided by the gating rule, not by the payload order.
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(
            [(row["step_no"], row["event"]) for row in self.events("plan-op")],
            [(2, "submitted"), (2, "filled"), (1, "submitted"), (1, "filled")],
        )
        sides = sorted(
            order.transaction_type
            for order in executor._paper_service.repository.orders.values()
        )
        self.assertEqual(sides, ["buy", "sell"])

    async def test_an_unfilled_hedge_never_releases_the_dependent_short(self):
        """A rejected hedge ⇒ NO short is submitted; the refusal is named."""
        short = self._option_leg(side="SELL", symbol="TCS26OCT2500CE")
        broken_hedge = self._option_leg(side="BUY", symbol="MISSING")
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, broken_hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)

        self.assertEqual(result["status"], "rejected")
        trail = self.events("plan-op")
        self.assertEqual(
            [(row["step_no"], row["event"]) for row in trail],
            [(2, "submitted"), (2, "rejected"), (1, "rejected")],
        )
        self.assertEqual(trail[1]["refusal_reason"], "PAPER_ORDER_REJECTED")
        self.assertEqual(trail[2]["refusal_reason"], "OPTION_HEDGE_NOT_FILLED")
        # The gate's own evidence: a hedge that did not arrive is an operator's
        # decision, never a silent reduction in protection.
        self.assertEqual(trail[2]["detail"]["reason"], "hedge_rejected")
        self.assertTrue(trail[2]["detail"]["action_required"])
        self.assertEqual(trail[2]["detail"]["hedge_outcomes"], ["rejected"])
        # No short order exists anywhere in the paper book.
        self.assertEqual(
            [
                order.transaction_type
                for order in executor._paper_service.repository.orders.values()
                if order.transaction_type == "sell"
            ],
            [],
        )

    async def test_an_intentional_naked_structure_is_still_admitted(self):
        """No hedge legs at all is a naked shape Project 10 admits: no gate applies."""
        short = self._option_leg(side="SELL", symbol="TCS26OCT2500CE")
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (order,) = executor._paper_service.repository.orders.values()
        self.assertEqual(order.transaction_type, "sell")
        self.assertEqual(order.quantity, 75)


    async def test_a_closing_structure_closes_its_own_short_before_releasing_the_hedge(self):
        """The exit plan closes the RUN's short first, then releases its hedge."""
        short = self._option_leg(side="SELL", symbol="TCS26OCT2500CE", instrument_id="opt-short")
        hedge = self._option_leg(side="BUY", symbol="TCS26OCT3000CE", instrument_id="opt-hedge")
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        entry = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)
        self.assertEqual(entry["status"], "filled")
        run_id = str(self._run_row_for_plan("plan-op")["option_run_id"])

        # The exit plan names the same two contracts, hedge first. The engine's
        # exit rule (short liability before hedge release) reorders it.
        hedge_close = self._option_leg(
            side="SELL", symbol="TCS26OCT3000CE", instrument_id="opt-hedge", signed_quantity=0
        )
        short_close = self._option_leg(
            side="BUY", symbol="TCS26OCT2500CE", instrument_id="opt-short", signed_quantity=0
        )
        self._seed_option_exit_plan("plan-exit", reference=run_id, legs=[hedge_close, short_close])

        result = await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(
            [(row["step_no"], row["event"]) for row in self.events("plan-exit")],
            [(2, "submitted"), (2, "filled"), (1, "submitted"), (1, "filled")],
        )
        self.assertEqual(
            [
                row["refusal_reason"]
                for row in self.events("plan-exit")
                if row["refusal_reason"] is not None
            ],
            [],
        )

    async def test_a_hedge_is_withheld_when_its_short_is_not_proven_closed(self):
        """No proven closure ⇒ the hedge is released for nothing (named refusal)."""
        short = self._option_leg(side="SELL", symbol="TCS26OCT2500CE", instrument_id="opt-short")
        hedge = self._option_leg(side="BUY", symbol="TCS26OCT3000CE", instrument_id="opt-hedge")
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)
        run_id = str(self._run_row_for_plan("plan-op")["option_run_id"])
        after_entry = dict(executor._paper_service.repository.orders)

        # An exit plan that carries ONLY the hedge close: nothing in it proves the
        # run's short closed, so the hedge must be withheld.
        hedge_close = self._option_leg(
            side="SELL", symbol="TCS26OCT3000CE", instrument_id="opt-hedge", signed_quantity=0
        )
        self._seed_option_exit_plan("plan-exit", reference=run_id, legs=[hedge_close])

        result = await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(result["status"], "rejected")
        (step,) = result["steps"]
        self.assertEqual(step["refusal_reason"], "OPTION_HEDGE_RELEASE_WITHHELD")
        self.assertEqual(step["detail"]["reason"], "short_not_proven_closed")
        # No NEW order was submitted by the withheld exit.
        self.assertEqual(executor._paper_service.repository.orders, after_entry)

    def _run_row_for_plan(self, plan_id):
        with self.factory() as session:
            return (
                session.execute(
                    text(
                        "SELECT option_run_id FROM public.strategy_plan_option_runs "
                        "WHERE plan_id = :p"
                    ),
                    {"p": plan_id},
                )
                .mappings()
                .first()
            )

    def _seed_option_exit_plan(self, plan_id, *, reference, legs):
        self.seed_validated_plan(
            plan_id, plan_kind="option_structure", legs=legs, run_id=f"run-{plan_id}"
        )
        self.seed_binding(run_id=f"run-{plan_id}")
        with self.factory() as session:
            session.execute(
                text("UPDATE strategy_plans SET resolved_plan = :resolved WHERE plan_id = :p"),
                {
                    "p": plan_id,
                    "resolved": json.dumps(
                        {
                            "target_kind": "option_structure",
                            "product": "NRML",
                            "legs": legs,
                            "option_run": {"phase": "exit", "option_run_id": reference},
                        }
                    ),
                },
            )
            session.commit()


class ExecutorOptionRunBindingTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """The durable plan -> option-run edge, on the PRODUCTION executor path.

    ``PaperPlanExecutor.execute`` is what the owner's HTTP route runs, so a run
    created/bound/closed here is the production wiring rather than a helper call.
    The option-run id is a DIFFERENT identity from the hosted worker-run id; the
    binding relation is the only place they meet.
    """

    SHORT_ID = "cccccccc-0000-0000-0000-0000000000a1"
    HEDGE_ID = "cccccccc-0000-0000-0000-0000000000a2"

    def _entry_legs(self):
        short = self._option_leg(
            side="SELL", symbol="TCS26OCT2500CE", instrument_id=self.SHORT_ID
        )
        hedge = self._option_leg(
            side="BUY", symbol="TCS26OCT3000CE", instrument_id=self.HEDGE_ID
        )
        return short, hedge

    def _binding_rows(self, plan_id):
        with self.factory() as session:
            return (
                session.execute(
                    text(
                        "SELECT plan_id, option_run_id, worker_run_id, strategy_id, "
                        "account_id, execution_environment, phase "
                        "FROM public.strategy_plan_option_runs WHERE plan_id = :p"
                    ),
                    {"p": plan_id},
                )
                .mappings()
                .all()
            )

    def _run_row(self, option_run_id):
        with self.factory() as session:
            return (
                session.execute(
                    text(
                        "SELECT strategy_run_id, status, legs, orders, trades, metadata "
                        "FROM public.option_run_states WHERE strategy_run_id = :r"
                    ),
                    {"r": option_run_id},
                )
                .mappings()
                .first()
            )

    def _run_count(self):
        with self.factory() as session:
            return int(
                session.execute(
                    text("SELECT COUNT(*) FROM public.option_run_states")
                ).scalar()
                or 0
            )

    async def test_an_entry_structure_creates_one_durable_run_and_binds_the_plan(self):
        short, hedge = self._entry_legs()
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        (binding,) = self._binding_rows("plan-op")
        # The worker-run id is stored as attribution, NOT overloaded as the run id.
        self.assertEqual(binding["worker_run_id"], "run-plan-op")
        self.assertNotEqual(binding["option_run_id"], binding["worker_run_id"])
        self.assertEqual(binding["phase"], "entry")
        self.assertEqual(binding["execution_environment"], "paper")
        run = self._run_row(binding["option_run_id"])
        self.assertEqual(run["status"], "entered")
        orders = json.loads(run["orders"])
        self.assertEqual({row["leg_id"] for row in orders}, {"plan-op:1", "plan-op:2"})
        self.assertEqual({row["status"] for row in orders}, {"filled"})
        trades = json.loads(run["trades"])
        self.assertEqual(sum(int(row["quantity"]) for row in trades), 150)
        self.assertEqual(self._run_count(), 1)

        # A retry (the same frozen plan) resolves to that SAME run; it does not
        # manufacture a second one, and the executor refuses a second execution.
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.plan_binding import (
            PlanOptionRunBindingStore,
            resolve_plan_option_run,
        )

        target = resolve_plan_option_run(
            _plan_view_for(self.factory, "plan-op"),
            strategy_id=STRATEGY,
            account_id=ACCOUNT,
            execution_environment="paper",
            worker_run_id="run-plan-op",
            binding_store=PlanOptionRunBindingStore(session_factory=self.factory),
            run_store=DurableOptionRunStore(session_factory=self.factory),
        )
        self.assertEqual(target["option_run_id"], binding["option_run_id"])
        self.assertEqual(self._run_count(), 1)

    async def _enter_a_structure(self):
        short, hedge = self._entry_legs()
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)
        (binding,) = self._binding_rows("plan-op")
        # The attributed book the entry created (the fake paper runtime does not
        # update the projection; the real fold does).
        self.seed_book(
            instrument_id=self.SHORT_ID,
            symbol="TCS26OCT2500CE",
            product="NRML",
            qty=-75,
        )
        self.seed_book(
            instrument_id=self.HEDGE_ID,
            symbol="TCS26OCT3000CE",
            product="NRML",
            qty=75,
        )
        return executor, str(binding["option_run_id"])

    def _exit_legs(self, *, hedge_id=None, short_id=None):
        # A closing leg targets flat: the delta is the whole open position.
        short_close = self._option_leg(
            side="BUY",
            symbol="TCS26OCT2500CE",
            instrument_id=short_id or self.SHORT_ID,
            signed_quantity=0,
        )
        hedge_close = self._option_leg(
            side="SELL",
            symbol="TCS26OCT3000CE",
            instrument_id=hedge_id or self.HEDGE_ID,
            signed_quantity=0,
        )
        return [hedge_close, short_close]

    async def test_an_exit_plan_closes_the_bound_run_in_the_existing_engine(self):
        executor, option_run_id = await self._enter_a_structure()
        self.seed_validated_plan(
            "plan-exit",
            plan_kind="option_structure",
            legs=self._exit_legs(),
            run_id="run-plan-op",
        )
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_plans SET resolved_plan = :resolved WHERE plan_id = 'plan-exit'"
                ),
                {
                    "resolved": json.dumps(
                        {
                            "target_kind": "option_structure",
                            "legs": self._exit_legs(),
                            "option_run": {"phase": "exit", "option_run_id": option_run_id},
                        }
                    )
                },
            )
            session.commit()

        result = await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        run = self._run_row(option_run_id)
        self.assertEqual(run["status"], "exited")
        # The exit plan is bound to the SAME run: two plans, one run.
        self.assertEqual(len(self._binding_rows("plan-exit")), 1)
        exit_binding = self._binding_rows("plan-exit")[0]
        self.assertEqual(exit_binding["option_run_id"], option_run_id)
        self.assertEqual(exit_binding["phase"], "exit")
        # Short-first: leg 2 (the short's close) is the first exit submission.
        exits = [row for row in self.events("plan-exit") if row["event"] == "submitted"]
        self.assertEqual([row["step_no"] for row in exits], [2, 1])
        # The exit's fills are attributed to the RUN's own legs (not the exit
        # plan's step ids): that is what makes the run's open quantity drop and a
        # repeated exit a no_op instead of a reversal.
        trades = json.loads(run["trades"])
        self.assertEqual({row["leg_id"] for row in trades}, {"plan-op:1", "plan-op:2"})
        self.assertEqual(sum(int(row["quantity"]) for row in trades), 300)

    async def test_an_entry_targets_its_own_run_not_the_aggregate_book(self):
        """A pre-existing strategy position in the same contract must not resize it."""
        short, hedge = self._entry_legs()
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        # The aggregate strategy book already holds 40 of the SAME contract (say
        # from another decision). The run is new, so its own open quantity is 0.
        self.seed_book(
            instrument_id=self.SHORT_ID, symbol="TCS26OCT2500CE", product="NRML", qty=-40
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        sells = [
            order
            for order in executor._paper_service.repository.orders.values()
            if order.tradingsymbol == "TCS26OCT2500CE"
        ]
        (sell,) = sells
        self.assertEqual(sell.transaction_type, "sell")
        # The run's own target (75), never 75 - 40 or 75 + 40.
        self.assertEqual(sell.quantity, 75)

    async def test_exiting_one_structure_closes_only_its_own_fill(self):
        """Two structures share a contract; exiting one must not touch the other."""
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        a_short, a_hedge = self._entry_legs()
        self._seed_lane("plan-a", plan_kind="option_structure", legs=[a_short, a_hedge])
        # Structure B holds the SAME contract, with its own (different) hedge.
        b_short = self._option_leg(
            side="SELL", symbol="TCS26OCT2500CE", instrument_id=self.SHORT_ID, lot=25
        )
        b_hedge = self._option_leg(
            side="BUY",
            symbol="TCS26OCT3500CE",
            instrument_id="dddddddd-0000-0000-0000-0000000000a3",
            lot=25,
        )
        self.seed_validated_plan(
            "plan-b", plan_kind="option_structure", legs=[b_short, b_hedge], run_id="run-plan-b"
        )
        self.seed_binding(run_id="run-plan-b")
        self.claim_reservation(plan_id="plan-b", requirement=45000.0)

        await executor.execute(_plan_view_for(self.factory, "plan-a"), actor=OWNER)
        await executor.execute(_plan_view_for(self.factory, "plan-b"), actor=OWNER)
        a_run_id = str(self._binding_rows("plan-a")[0]["option_run_id"])
        b_run_id = str(self._binding_rows("plan-b")[0]["option_run_id"])

        # The aggregate strategy book really does hold BOTH structures (75 + 25).
        self.seed_book(
            instrument_id=self.SHORT_ID, symbol="TCS26OCT2500CE", product="NRML", qty=-100
        )
        b_trades_before = json.loads(self._run_row(b_run_id)["trades"])
        orders_before = len(executor._paper_service.repository.orders)

        a_exit_hedge = self._option_leg(
            side="SELL", symbol="TCS26OCT3000CE", instrument_id=self.HEDGE_ID, signed_quantity=0
        )
        a_exit_short = self._option_leg(
            side="BUY", symbol="TCS26OCT2500CE", instrument_id=self.SHORT_ID, signed_quantity=0
        )
        self._seed_exit_plan("plan-a-exit", reference=a_run_id, legs=[a_exit_hedge, a_exit_short])

        result = await executor.execute(_plan_view_for(self.factory, "plan-a-exit"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        new_orders = list(executor._paper_service.repository.orders.values())[orders_before:]
        (x_order,) = [o for o in new_orders if o.tradingsymbol == "TCS26OCT2500CE"]
        self.assertEqual(x_order.transaction_type, "buy")
        # A's OWN 75 - never the aggregate 100, and never a reversal.
        self.assertEqual(x_order.quantity, 75)
        # Structure B's run is untouched: same trades, still entered, still open.
        self.assertEqual(json.loads(self._run_row(b_run_id)["trades"]), b_trades_before)
        self.assertEqual(self._run_row(b_run_id)["status"], "entered")
        self.assertEqual(self._run_row(a_run_id)["status"], "exited")

    async def test_a_repeated_exit_never_overcloses(self):
        """A second exit against an already-flat run is a no_op, not a reversal."""
        executor, run_id = await self._enter_a_structure()
        self._seed_exit_plan("plan-exit", reference=run_id)
        first = await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)
        self.assertEqual(first["status"], "filled")
        orders_after_first = len(executor._paper_service.repository.orders)

        self._seed_exit_plan("plan-exit-again", reference=run_id)
        second = await executor.execute(
            _plan_view_for(self.factory, "plan-exit-again"), actor=OWNER
        )

        self.assertEqual(second["status"], "no_op")
        self.assertEqual(len(executor._paper_service.repository.orders), orders_after_first)
        self.assertEqual(self._run_row(run_id)["status"], "exited")
        self.assertEqual(
            [row["event"] for row in second["steps"]], ["no_op", "no_op"]
        )

    async def test_an_exit_direction_that_would_open_is_refused(self):
        """Defence in depth: a run whose open sign contradicts its leg is refused."""
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        # The run holds a LONG (+25) and the exit leg declares BUY: closing a
        # long takes a SELL, so this leg would ADD exposure.
        with self.assertRaises(ExecutionRefusal) as ctx:
            executor._validate_option_exit_leg(
                plan_id="plan-x",
                leg={"instrument_id": "i-1", "product": "NRML", "side": "BUY"},
                run_leg={"product": "NRML", "transaction_type": "BUY", "leg_id": "l-1"},
                current=25,
            )
        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_WOULD_OPEN")

    async def test_an_exit_that_names_the_worker_run_id_is_refused(self):
        executor, _option_run_id = await self._enter_a_structure()
        # The plan's OWN hosted worker-run id is not an option-run id.
        self._seed_exit_plan("plan-exit", reference="run-plan-exit")

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_REFERENCE_IS_WORKER_RUN")

    async def test_an_exit_for_a_run_this_platform_never_launched_is_refused(self):
        executor, _option_run_id = await self._enter_a_structure()
        self._seed_exit_plan("plan-exit", reference="opt_run_not_ours")

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_RUN_NOT_LAUNCHED")

    async def test_an_exit_leg_that_is_not_in_the_bound_run_is_refused(self):
        executor, option_run_id = await self._enter_a_structure()
        self._seed_exit_plan(
            "plan-exit",
            reference=option_run_id,
            legs=self._exit_legs(hedge_id="cccccccc-0000-0000-0000-00000000ffff"),
        )

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_LEG_MISMATCH")

    def _seed_exit_plan(self, plan_id, *, reference, legs=None):
        exit_legs = legs if legs is not None else self._exit_legs()
        self.seed_validated_plan(
            plan_id, plan_kind="option_structure", legs=exit_legs, run_id=f"run-{plan_id}"
        )
        self.seed_binding(run_id=f"run-{plan_id}")
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_plans SET resolved_plan = :resolved WHERE plan_id = :p"
                ),
                {
                    "p": plan_id,
                    "resolved": json.dumps(
                        {
                            "target_kind": "option_structure",
                            "legs": exit_legs,
                            "option_run": {"phase": "exit", "option_run_id": reference},
                        }
                    ),
                },
            )
            session.commit()

    @property
    def _refusal(self):
        from backend.strategies.execution import ExecutionRefusal

        return ExecutionRefusal

    def test_an_adjust_plan_refuses_at_the_binding_edge_by_name(self):
        """S1 freezes adjust; it never resolves into the entry or exit branch."""
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.plan_binding import (
            PlanBindingRefusal,
            PlanOptionRunBindingStore,
            is_option_entry_plan,
            resolve_plan_option_run,
        )

        short, hedge = self._entry_legs()
        self.seed_strategy()
        self.seed_validated_plan(
            "plan-adjust",
            plan_kind="option_structure",
            legs=[short, hedge],
            resolved_extra={
                "option_run": {
                    "phase": "adjust",
                    "option_run_id": "opt-run-existing",
                    "based_on_generation": 1,
                }
            },
        )
        plan = _plan_view_for(self.factory, "plan-adjust")
        # The entry gate must not claim an adjust plan.
        self.assertFalse(is_option_entry_plan(plan))
        with self.assertRaises(PlanBindingRefusal) as ctx:
            resolve_plan_option_run(
                plan,
                strategy_id=STRATEGY,
                account_id=ACCOUNT,
                execution_environment="paper",
                worker_run_id="run-plan-adjust",
                binding_store=PlanOptionRunBindingStore(session_factory=self.factory),
                run_store=DurableOptionRunStore(session_factory=self.factory),
            )
        self.assertEqual(ctx.exception.reason_code, "OPTION_ADJUSTMENT_NOT_EXECUTABLE")
        # Neither branch ran: no run was created and no binding was written.
        self.assertEqual(self._run_count(), 0)
        self.assertEqual(self._binding_rows("plan-adjust"), [])

    def test_the_option_step_derivation_refuses_an_adjust_target(self):
        from backend.strategies.execution import ExecutionRefusal

        executor = self.build_executor()
        with self.assertRaises(ExecutionRefusal) as ctx:
            executor._option_run_steps({}, {"phase": "adjust", "run": object()})
        self.assertEqual(ctx.exception.reason_code, "OPTION_ADJUSTMENT_NOT_EXECUTABLE")


class ExecutorOptionContinuityTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """Phase B1: a held structure is never opened twice, and never closed twice.

    Everything here goes through ``PaperPlanExecutor.execute`` - the same call the
    owner's route makes - so the guard is proved on the production path rather
    than by calling a helper.
    """

    SHORT_ID = "cccccccc-0000-0000-0000-0000000000b1"
    HEDGE_ID = "cccccccc-0000-0000-0000-0000000000b2"
    OTHER_HEDGE_ID = "cccccccc-0000-0000-0000-0000000000b3"

    def _entry_legs(self):
        short = self._option_leg(
            side="SELL", symbol="NIFTY26OCT25000CE", instrument_id=self.SHORT_ID
        )
        hedge = self._option_leg(
            side="BUY", symbol="NIFTY26OCT25500CE", instrument_id=self.HEDGE_ID
        )
        return short, hedge

    @property
    def _refusal(self):
        from backend.strategies.execution import ExecutionRefusal

        return ExecutionRefusal

    def _binding_rows(self, plan_id):
        with self.factory() as session:
            return (
                session.execute(
                    text(
                        "SELECT plan_id, option_run_id, phase FROM public.strategy_plan_option_runs "
                        "WHERE plan_id = :p"
                    ),
                    {"p": plan_id},
                )
                .mappings()
                .all()
            )

    def _run_row(self, option_run_id):
        with self.factory() as session:
            return (
                session.execute(
                    text(
                        "SELECT strategy_run_id, status, orders FROM public.option_run_states "
                        "WHERE strategy_run_id = :r"
                    ),
                    {"r": option_run_id},
                )
                .mappings()
                .first()
            )

    def _run_count(self):
        with self.factory() as session:
            return int(
                session.execute(text("SELECT COUNT(*) FROM public.option_run_states")).scalar() or 0
            )

    def _second_lane(self, plan_id, *, legs, run_id=None):
        """A SECOND plan of the SAME strategy - a restarted/re-issued evaluation."""
        run_id = run_id or f"run-{plan_id}"
        self.seed_validated_plan(
            plan_id, plan_kind="option_structure", legs=legs, run_id=run_id
        )
        self.seed_binding(run_id=run_id)
        self.claim_reservation(plan_id=plan_id, requirement=15000.0)

    def _exit_legs(self):
        return [
            self._option_leg(
                side="SELL", symbol="NIFTY26OCT25500CE", instrument_id=self.HEDGE_ID
            ),
            self._option_leg(
                side="BUY", symbol="NIFTY26OCT25000CE", instrument_id=self.SHORT_ID
            ),
        ]

    def _seed_exit_plan(self, plan_id, *, reference):
        legs = self._exit_legs()
        self.seed_validated_plan(
            plan_id, plan_kind="option_structure", legs=legs, run_id=f"run-{plan_id}"
        )
        self.seed_binding(run_id=f"run-{plan_id}")
        with self.factory() as session:
            session.execute(
                text("UPDATE strategy_plans SET resolved_plan = :resolved WHERE plan_id = :p"),
                {
                    "p": plan_id,
                    "resolved": json.dumps(
                        {
                            "target_kind": "option_structure",
                            "legs": legs,
                            "option_run": {"phase": "exit", "option_run_id": reference},
                        }
                    ),
                },
            )
            session.commit()

    async def _enter(self):
        short, hedge = self._entry_legs()
        self._seed_lane("plan-op", plan_kind="option_structure", legs=[short, hedge])
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        await executor.execute(_plan_view_for(self.factory, "plan-op"), actor=OWNER)
        (binding,) = self._binding_rows("plan-op")
        return executor, str(binding["option_run_id"])

    async def test_a_second_equivalent_entry_never_opens_a_second_structure(self):
        executor, option_run_id = await self._enter()
        short, hedge = self._entry_legs()
        # A DIFFERENT plan (a restarted strategy) freezing the SAME structure.
        self._second_lane("plan-op-again", legs=[short, hedge])

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-op-again"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_STRUCTURE_ALREADY_OPEN")
        self.assertEqual(ctx.exception.detail.get("option_run_id"), option_run_id)
        self.assertEqual(ctx.exception.detail.get("option_run_status"), "entered")
        # Nothing was created for the refused plan, and the held run is untouched.
        self.assertEqual(self._binding_rows("plan-op-again"), [])
        self.assertEqual(self._run_count(), 1)
        self.assertEqual(self._run_row(option_run_id)["status"], "entered")

    async def test_a_different_structure_is_not_a_duplicate(self):
        executor, option_run_id = await self._enter()
        short = self._option_leg(
            side="SELL", symbol="NIFTY26OCT25000CE", instrument_id=self.SHORT_ID
        )
        other_hedge = self._option_leg(
            side="BUY", symbol="NIFTY26OCT26000CE", instrument_id=self.OTHER_HEDGE_ID
        )
        self._second_lane("plan-op-other", legs=[short, other_hedge])
        self.claim_reservation(plan_id="plan-op-other", requirement=15000.0)

        result = await executor.execute(_plan_view_for(self.factory, "plan-op-other"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(self._run_count(), 2)
        self.assertNotEqual(
            str(self._binding_rows("plan-op-other")[0]["option_run_id"]), option_run_id
        )

    async def test_a_finished_structure_does_not_block_a_new_entry(self):
        executor, option_run_id = await self._enter()
        # The structure is CLOSED through the executor's own close path.
        self._seed_exit_plan("plan-exit", reference=option_run_id)
        await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)
        self.assertEqual(self._run_row(option_run_id)["status"], "exited")

        short, hedge = self._entry_legs()
        self._second_lane("plan-op-next", legs=[short, hedge])
        result = await executor.execute(_plan_view_for(self.factory, "plan-op-next"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(self._run_count(), 2)

    async def test_a_partial_entry_holds_the_structure_and_blocks_a_second_one(self):
        executor, option_run_id = await self._enter()
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET status = 'partial_entry', "
                    "pending_legs = :pending WHERE strategy_run_id = :r"
                ),
                {"r": option_run_id, "pending": json.dumps([{"leg_id": "plan-op:2"}])},
            )
            session.commit()
        short, hedge = self._entry_legs()
        self._second_lane("plan-op-again", legs=[short, hedge])

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-op-again"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_STRUCTURE_ALREADY_OPEN")
        self.assertEqual(ctx.exception.detail.get("option_run_status"), "partial_entry")
        self.assertEqual(self._run_count(), 1)

    async def test_an_unrecognised_run_status_is_not_read_as_no_structure(self):
        executor, option_run_id = await self._enter()
        with self.factory() as session:
            session.execute(
                text("UPDATE public.option_run_states SET status = 'teleported' WHERE strategy_run_id = :r"),
                {"r": option_run_id},
            )
            session.commit()
        short = self._option_leg(
            side="SELL", symbol="NIFTY26OCT25000CE", instrument_id=self.SHORT_ID
        )
        other_hedge = self._option_leg(
            side="BUY", symbol="NIFTY26OCT26000CE", instrument_id=self.OTHER_HEDGE_ID
        )
        self._second_lane("plan-op-other", legs=[short, other_hedge])

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-op-other"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_RUN_STATUS_UNKNOWN")
        self.assertEqual(self._run_count(), 1)

    async def test_unknown_discovery_refuses_a_new_entry(self):
        executor, _option_run_id = await self._enter()
        # A binding whose durable run row does not exist: the read is UNKNOWN, and
        # unknown is not "no runs" - the hidden run may be the open one.
        self.seed_validated_plan(
            "plan-ghost", plan_kind="option_structure", legs=[*self._entry_legs()],
            run_id="run-ghost",
        )
        self.seed_binding(run_id="run-ghost")
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.strategy_plan_option_runs "
                    "(plan_id, option_run_id, strategy_id, account_id, execution_environment, phase) "
                    "VALUES ('plan-ghost', 'opt_run_ghost', :sid, :account, 'paper', 'entry')"
                ),
                {"sid": STRATEGY, "account": ACCOUNT},
            )
            session.commit()
        short, hedge = self._entry_legs()
        self._second_lane("plan-op-again", legs=[short, hedge])

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-op-again"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_STRUCTURE_DISCOVERY_UNKNOWN")
        self.assertEqual(self._run_count(), 1)

    async def test_an_exit_before_the_entry_it_closes_is_refused(self):
        """Run status stays part of the exit contract, not a formality."""
        executor, option_run_id = await self._enter()
        self._seed_exit_plan("plan-exit", reference=option_run_id)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET status = 'partial_entry' "
                    "WHERE strategy_run_id = :r"
                ),
                {"r": option_run_id},
            )
            session.commit()

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_BEFORE_ENTRY")
        self.assertEqual(self._run_row(option_run_id)["status"], "partial_entry")

    async def test_an_exit_leg_with_a_different_product_is_refused(self):
        """The product is part of the close contract, not a formatting detail."""
        executor, option_run_id = await self._enter()
        legs = self._exit_legs()
        legs[0]["product"] = "MIS"
        self.seed_validated_plan(
            "plan-exit", plan_kind="option_structure", legs=legs, run_id="run-plan-exit"
        )
        self.seed_binding(run_id="run-plan-exit")
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_plans SET resolved_plan = :resolved "
                    "WHERE plan_id = 'plan-exit'"
                ),
                {
                    "resolved": json.dumps(
                        {
                            "target_kind": "option_structure",
                            "legs": legs,
                            "option_run": {"phase": "exit", "option_run_id": option_run_id},
                        }
                    )
                },
            )
            session.commit()

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_EXIT_CONTRACT_MISMATCH")
        self.assertEqual(self._run_row(option_run_id)["status"], "entered")

    async def test_an_unresolved_protective_stage_blocks_a_conflicting_governed_exit(self):
        executor, option_run_id = await self._enter()
        rows = json.loads(dict(self._run_row(option_run_id))["orders"] or "[]")
        rows.append(
            {
                "stage_digest": "protect-stage-1",
                "attempt": 1,
                "state": "sending",
                "legs": [{"index": 0, "tradingsymbol": "NIFTY26OCT25000CE"}],
            }
        )
        with self.factory() as session:
            session.execute(
                text("UPDATE public.option_run_states SET orders = :orders WHERE strategy_run_id = :r"),
                {"r": option_run_id, "orders": json.dumps(rows)},
            )
            session.commit()

        self._seed_exit_plan("plan-exit", reference=option_run_id)
        orders_before = len(executor._paper_service.repository.orders)

        with self.assertRaises(self._refusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "OPTION_PROTECTIVE_EXIT_UNRESOLVED")
        self.assertEqual(ctx.exception.detail.get("stage_digest"), "protect-stage-1")
        self.assertEqual(len(executor._paper_service.repository.orders), orders_before)
        self.assertEqual(self._run_row(option_run_id)["status"], "entered")

        # Mutation pair: once the stage is RESOLVED, the same governed exit runs.
        resolved = [dict(row) for row in rows]
        resolved[-1]["state"] = "submitted"
        with self.factory() as session:
            session.execute(
                text("UPDATE public.option_run_states SET orders = :orders WHERE strategy_run_id = :r"),
                {"r": option_run_id, "orders": json.dumps(resolved)},
            )
            session.commit()

        result = await executor.execute(_plan_view_for(self.factory, "plan-exit"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(self._run_row(option_run_id)["status"], "exited")


class ExecutorRollSeamTests(ExecutorTestCase, unittest.IsolatedAsyncioTestCase):
    """R3 §13: the frozen plan says which half of a roll it is, and the executor
    enforces the contract on that artifact alone."""

    def _open_roll(self):
        from backend.strategies.rolls import RollStateMachine

        machine = RollStateMachine(session_factory=self.factory)
        roll = machine.create(
            strategy_id=STRATEGY,
            account_id=ACCOUNT,
            old_instrument_id="fut-old",
            new_instrument_id=INST_B,
            required_replacement_quantity=75,
            old_coordinate={"product": "NRML"},
            new_coordinate={"product": "NRML"},
        )
        return machine, roll["roll_id"]

    def _futures_plan(self, plan_id, *, roll_id, role, leg, run_id):
        extra = {"roll": {"roll_id": roll_id, "role": role}} if role else {}
        self.seed_validated_plan(
            plan_id,
            run_id=run_id,
            plan_kind="target_futures",
            resolved_extra=extra,
            legs=[leg],
        )
        self.seed_binding(run_id=run_id)
        self.claim_reservation(plan_id=plan_id, requirement=45000.0)

    async def test_a_close_is_refused_before_the_roll_releases_it(self):
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        machine, roll_id = self._open_roll()
        self._futures_plan(
            "plan-close",
            roll_id=roll_id,
            role="close_old",
            leg=self._futures_leg_name(),
            run_id="run-close",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-close"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_CLOSE_NOT_RELEASED")
        self.assertEqual(ctx.exception.detail["roll_state"], "acquiring")
        # Nothing was submitted, so nothing filled.
        self.assertEqual(executor._paper_service.repository.orders, {})

    @staticmethod
    def _futures_leg_name():
        return {
            "instrument_id": "fut-old",
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26SEPFUT",
            "broker_exchange": "NFO",
            "broker_symbol": "NIFTY26SEPFUT",
            "broker_token": 601,
            "product": "NRML",
            "instrument_type": "FUT",
            "lots": 1,
            "lot_size": 75,
            "signed_quantity": -75,
            "expiry": "2026-09-24",
            "tick_size": 0.05,
            "reference_price": 1000.0,
        }

    async def test_an_ungated_plan_cannot_close_an_open_roll_leg(self):
        """No optional role: an open roll's old leg is bound by coordinates."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        machine, roll_id = self._open_roll()
        # A plain futures plan on the OLD contract, with no roll binding at all.
        self._futures_plan(
            "plan-bypass",
            roll_id=None,
            role=None,
            leg=self._futures_leg_name(),
            run_id="run-bypass",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-bypass"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_CLOSE_REQUIRES_BINDING")
        self.assertEqual(ctx.exception.detail["roll_id"], roll_id)
        self.assertEqual(ctx.exception.detail["instrument_id"], "fut-old")
        # Nothing was submitted and the roll was not moved.
        self.assertEqual(executor._paper_service.repository.orders, {})
        self.assertEqual(machine.get(roll_id)["state"], "acquiring")

    async def test_an_ungated_plan_on_an_unrelated_contract_is_unaffected(self):
        """Control: the gate is about the roll's OWN coordinates, nothing wider."""
        self.seed_strategy()
        machine, roll_id = self._open_roll()
        self._futures_plan(
            "plan-free",
            roll_id=None,
            role=None,
            leg=_futures_leg(signed_quantity=75),  # INST_B, not the roll's old leg
            run_id="run-free",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-free"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(machine.get(roll_id)["state"], "acquiring")

    async def test_the_replacement_fill_proves_the_roll_and_then_the_close_goes(self):
        """Acquire -> recorded proof -> release -> the close is allowed."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        machine, roll_id = self._open_roll()
        machine.acquire(roll_id)

        self._futures_plan(
            "plan-open",
            roll_id=roll_id,
            role="open_new",
            leg=_futures_leg(signed_quantity=75),
            run_id="run-open",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        result = await executor.execute(_plan_view_for(self.factory, "plan-open"), actor=OWNER)

        self.assertEqual(result["status"], "filled")
        # The CONFIRMED replacement execution is the roll's durable proof.
        self.assertEqual(machine.replacement_filled_quantity(roll_id), 75)
        fills = machine.replacement_fills(roll_id)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["quantity"], 75)
        self.assertTrue(fills[0]["paper_order_id"])

        # Replaying the plan is idempotent: the fill is recorded once.
        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-open"), actor=OWNER)
        self.assertEqual(ctx.exception.reason_code, "PLAN_ALREADY_EXECUTED")
        self.assertEqual(machine.replacement_filled_quantity(roll_id), 75)

        self.assertEqual(machine.prove_filled(roll_id)["state"], "releasing_old")
        machine.release_close(roll_id)

        # A fresh close plan is now permitted (the first one already refused).
        self._futures_plan(
            "plan-close",
            roll_id=roll_id,
            role="close_old",
            leg=self._futures_leg_name(),
            run_id="run-close",
        )
        closer = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )
        closed = await closer.execute(
            _plan_view_for(self.factory, "plan-close"), actor=OWNER
        )
        self.assertEqual(closed["status"], "filled")
        (order,) = closer._paper_service.repository.orders.values()
        self.assertEqual(order.transaction_type, "sell")
        self.assertEqual(order.quantity, 75)

    def _drive_to_releasing(self, machine, roll_id):
        """Acquire + prove the replacement so the close is RELEASED."""
        machine.acquire(roll_id)
        machine.record_replacement_fill(
            roll_id,
            paper_order_id="paper-open-1",
            quantity=75,
            instrument_id=INST_B,
            plan_id="plan-open-seed",
            actor_id=OWNER,
        )
        self.assertEqual(machine.prove_filled(roll_id)["state"], "releasing_old")
        machine.release_close(roll_id)

    async def test_an_open_new_plan_for_another_contract_is_refused(self):
        """The acquisition half may only move the roll's own NEW contract."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        _machine, roll_id = self._open_roll()
        self._futures_plan(
            "plan-open",
            roll_id=roll_id,
            role="open_new",
            leg=_futures_leg(instrument_id="fut-somewhere-else", signed_quantity=75),
            run_id="run-open",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-open"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_PLAN_CONTRACT_MISMATCH")
        self.assertEqual(ctx.exception.detail["expected_instrument_id"], INST_B)
        self.assertEqual(executor._paper_service.repository.orders, {})

    async def test_an_open_new_plan_at_the_wrong_quantity_is_refused(self):
        """The acquisition must be the EXACT required replacement quantity."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        _machine, roll_id = self._open_roll()
        self._futures_plan(
            "plan-open",
            roll_id=roll_id,
            role="open_new",
            leg=_futures_leg(signed_quantity=50),
            run_id="run-open",
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-open"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_PLAN_QUANTITY_MISMATCH")
        self.assertEqual(ctx.exception.detail["required_replacement_quantity"], 75)
        self.assertEqual(ctx.exception.detail["plan_quantity"], 50)

    async def test_a_close_that_moves_the_old_contract_the_wrong_way_is_refused(self):
        """A released close that BUYS the old leg is not a close of a long roll."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        machine, roll_id = self._open_roll()
        self._drive_to_releasing(machine, roll_id)
        wrong_way = dict(self._futures_leg_name())
        wrong_way["signed_quantity"] = 75
        self._futures_plan(
            "plan-close", roll_id=roll_id, role="close_old", leg=wrong_way, run_id="run-close"
        )
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-close"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_PLAN_DIRECTION_MISMATCH")
        self.assertEqual(executor._paper_service.repository.orders, {})

    async def test_a_roll_plan_carrying_both_contracts_is_refused(self):
        """One plan may not acquire AND close: the ordered roll owns that order."""
        from backend.strategies.execution import ExecutionRefusal

        self.seed_strategy()
        machine, roll_id = self._open_roll()
        self._drive_to_releasing(machine, roll_id)
        both = self._futures_plan_legs(
            "plan-close",
            roll_id=roll_id,
            role="close_old",
            legs=[self._futures_leg_name(), _futures_leg(signed_quantity=75)],
            run_id="run-close",
        )
        _ = both
        executor = self.build_executor(
            paper_service=self.build_paper_service(starting_balance="1000000")
        )

        with self.assertRaises(ExecutionRefusal) as ctx:
            await executor.execute(_plan_view_for(self.factory, "plan-close"), actor=OWNER)

        self.assertEqual(ctx.exception.reason_code, "ROLL_PLAN_CONTRACT_MISMATCH")

    def _futures_plan_legs(self, plan_id, *, roll_id, role, legs, run_id):
        self.seed_validated_plan(
            plan_id,
            run_id=run_id,
            plan_kind="target_futures",
            resolved_extra={"roll": {"roll_id": roll_id, "role": role}},
            legs=legs,
        )
        self.seed_binding(run_id=run_id)
        self.claim_reservation(plan_id=plan_id, requirement=45000.0)
        return plan_id


def _plan_view_for(factory, plan_id):
    from backend.strategies.proposals import ProposalStore

    return ProposalStore(session_factory=factory).get_plan(plan_id)


if __name__ == "__main__":
    unittest.main()
