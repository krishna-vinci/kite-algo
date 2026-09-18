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
        "signed_quantity": 10,
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
                    instrument_type TEXT,
                    lot_size INTEGER,
                    current_generation_id TEXT,
                    expiry TEXT, tick_size REAL, underlying TEXT
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
            StrategyExecutionBarrier,
            StrategyExecutionBarrierEvent,
            StrategyPlan,
            StrategyPlanExecutionEvent,
            StrategyPositionProjection,
            StrategyProposal,
            StrategyReservation,
            StrategyReservationEvent,
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
                # The executor reads fill progress to decide whether a step is
                # resolved, so the table must exist even when nothing writes to it.
                PaperOrderFillProgress.__table__,
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

    async def test_a_target_weights_plan_is_refused_by_name(self):
        """No target_weights execution in this phase: the kind itself refuses."""
        self.seed_validated_plan("plan-tw", plan_kind="target_weights", legs=[])
        self.seed_binding(run_id="run-tw")
        exc = await self._refused(plan_id="plan-tw")
        self.assertEqual(exc.reason_code, "PLAN_KIND_UNSUPPORTED")
        self.assertEqual(
            [row["refusal_reason"] for row in self.events("plan-tw")],
            ["PLAN_KIND_UNSUPPORTED"],
        )

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

    async def test_lot_size_floors_the_step_to_the_pinned_catalog_lot(self):
        self.seed_strategy()
        self.seed_validated_plan()  # target +10
        self.seed_binding()
        self.claim_reservation(requirement=15000.0)
        self.seed_lot_size(4)  # pinned catalog says lots of 4: 10 -> 8
        executor = self.build_executor()
        plan = _plan_view_for(self.factory, "plan-1")

        result = await executor.execute(plan, actor=OWNER)
        self.assertEqual(result["status"], "filled")
        (step,) = result["steps"]
        self.assertEqual(step["filled_quantity"], 8)
        paper = executor._paper_service
        (order,) = paper.repository.orders.values()
        self.assertEqual(order.quantity, 8)

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
        "reference_price": 1000.0,
    },
]


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
        """Capacity is reserved only when exposure increases (D-6); sells are exempt."""
        self.seed_strategy()
        self.seed_bundle_plan(legs=[dict(BUNDLE_LEGS[1])])  # a single SELL leg
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


def _plan_view_for(factory, plan_id):
    from backend.strategies.proposals import ProposalStore

    return ProposalStore(session_factory=factory).get_plan(plan_id)


if __name__ == "__main__":
    unittest.main()