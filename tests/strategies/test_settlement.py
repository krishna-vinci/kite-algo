"""The durable execution-quiescence barrier and four-axis settlement evidence.

Why this suite exists (R3 §16, plan D-1..D-5)
---------------------------------------------

Quiescence is NEVER inferred: not from a quiet window, not from two identical
reads, not from account flatness. It is a durable proof recorded on a barrier
whose version is bumped by every work transition in the same transaction — so
any later work event invalidates every prior proof by plain version arithmetic.

SQLite runs with the established ``public.`` ATTACH fixture: the new settlement
tables come from the shared ``Base`` metadata, while the pre-existing platform
tables the in-flight enumeration reads are ``public.``-qualified. The advisory
lock is a dialect-guarded no-op here; the real serialization race is proved in
the disposable-PostgreSQL suite (``tests/integration/test_settlement_barrier_postgres.py``).
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base
import backend.strategies.models  # noqa: F401  registers the hosted tables on Base
import backend.strategies.attribution_models  # noqa: F401  registers attribution + settlement tables

ACCOUNT = "kite:A"
STRATEGY = "stg-A"
ENV = "live"

TERMINAL_ORDER_STATUSES = ("COMPLETE", "CANCELLED", "REJECTED", "LAPSED")


class SettlementTestCase(unittest.TestCase):
    """Shared fixture: canonical strategy + the platform tables enumeration reads."""

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
                CREATE TABLE public.worker_live_execution_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, broker_order_id TEXT, trade_id TEXT,
                    client_order_ref TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.order_state_projection (
                    account_id TEXT NOT NULL, order_id TEXT NOT NULL,
                    latest_status TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, order_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.order_trade_fills (
                    account_id TEXT NOT NULL, order_id TEXT NOT NULL, trade_id TEXT NOT NULL,
                    instrument_token BIGINT, exchange TEXT, tradingsymbol TEXT, product TEXT,
                    transaction_type TEXT, quantity INTEGER, fill_timestamp TEXT,
                    payload_json TEXT, PRIMARY KEY (account_id, trade_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.live_order_intents (
                    intent_id TEXT PRIMARY KEY, client_order_ref TEXT NOT NULL,
                    account_id TEXT NOT NULL, strategy_run_id TEXT NOT NULL, broker_order_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.account_positions (
                    account_id TEXT NOT NULL, instrument_token BIGINT NOT NULL,
                    product TEXT NOT NULL, exchange TEXT, tradingsymbol TEXT,
                    net_quantity INT NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (account_id, instrument_token, product)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.paper_positions (
                    account_scope TEXT NOT NULL, instrument_token BIGINT NOT NULL,
                    product TEXT NOT NULL DEFAULT 'MIS', exchange TEXT, tradingsymbol TEXT,
                    net_quantity INT NOT NULL DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (account_scope, instrument_token, product)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.algo_worker_runs (
                    strategy_run_id TEXT PRIMARY KEY, token_id TEXT, template_id TEXT,
                    account_scope TEXT, execution_mode TEXT, status TEXT NOT NULL DEFAULT 'open'
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.live_plan_submissions (
                    submission_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    step_ref TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    execution_environment TEXT NOT NULL,
                    state TEXT NOT NULL,
                    broker_order_ids TEXT NOT NULL DEFAULT '[]',
                    delta_snapshot TEXT NOT NULL DEFAULT '{}',
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (plan_id, step_no)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.strategy_jobs (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, owner_id TEXT,
                    account_scope TEXT, execution_mode TEXT, status TEXT NOT NULL DEFAULT 'queued'
                )
                """
            )
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)

        from backend.strategies.attribution_models import Strategy

        with self.factory() as session:
            session.add(
                Strategy(
                    id=STRATEGY, owner_id="app:o", name="A", account_scope=ACCOUNT, status="active"
                )
            )
            session.commit()

        self.barrier = self._barrier()

    def tearDown(self):
        self.engine.dispose()

    def _barrier(self):
        from backend.strategies.settlement import ExecutionBarrier

        return ExecutionBarrier(session_factory=self.factory)

    # ---------------------------------------------------------------- seeding

    def _bind(self, run_id, *, strategy=STRATEGY, env=ENV, account=ACCOUNT):
        from backend.strategies.attribution_models import StrategyRunBinding

        with self.factory() as session:
            session.add(
                StrategyRunBinding(
                    strategy_run_id=run_id,
                    strategy_id=strategy,
                    owner_id="app:o",
                    account_id=account,
                    execution_environment=env,
                    bound_by="test",
                    binding_source="hosted_job",
                )
            )
            session.commit()

    def _order_link(self, order_id, *, run_id="run-1", trade_id=None, account=ACCOUNT):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.worker_live_execution_links "
                    "(strategy_run_id, account_id, broker_order_id, trade_id) "
                    "VALUES (:run_id, :account, :order_id, :trade_id)"
                ),
                {"run_id": run_id, "account": account, "order_id": order_id, "trade_id": trade_id},
            )
            session.commit()

    def _order_state(self, order_id, status, *, account=ACCOUNT):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.order_state_projection (account_id, order_id, latest_status) "
                    "VALUES (:account, :order_id, :status)"
                ),
                {"account": account, "order_id": order_id, "status": status},
            )
            session.commit()

    def _fill(self, trade_id, *, order_id="OID-1", side="BUY", qty=100, account=ACCOUNT):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.order_trade_fills "
                    "(account_id, order_id, trade_id, instrument_token, exchange, tradingsymbol, "
                    " product, transaction_type, quantity) "
                    "VALUES (:account, :order_id, :trade_id, 738561, 'NSE', 'RELIANCE', 'CNC', "
                    " :side, :qty)"
                ),
                {"account": account, "order_id": order_id, "trade_id": trade_id, "side": side, "qty": qty},
            )
            session.commit()

    def _intent(self, intent_id, *, run_id="run-1", order_id=None, status="placed", account=ACCOUNT):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.live_order_intents "
                    "(intent_id, client_order_ref, account_id, strategy_run_id, broker_order_id, status) "
                    "VALUES (:intent_id, :ref, :account, :run_id, :order_id, :status)"
                ),
                {
                    "intent_id": intent_id,
                    "ref": f"REF-{intent_id}",
                    "account": account,
                    "run_id": run_id,
                    "order_id": order_id,
                    "status": status,
                },
            )
            session.commit()

    def _book_row(self, token=738561, product="CNC", qty=100, *, strategy=STRATEGY, env=ENV, account=ACCOUNT):
        from backend.strategies.attribution_models import StrategyPositionProjection

        with self.factory() as session:
            session.add(
                StrategyPositionProjection(
                    account_id=account,
                    strategy_id=strategy,
                    execution_environment=env,
                    identity_kind="canonical",
                    identity_key=f"inst-{token}",
                    canonical_instrument_id=f"inst-{token}",
                    instrument_token=token,
                    exchange="NSE",
                    tradingsymbol="RELIANCE",
                    product=product,
                    net_quantity=qty,
                    projection_version=1,
                )
            )
            session.commit()

    def _reconciliation(self, token, divergence_class, *, account=ACCOUNT, product="CNC"):
        from backend.strategies.attribution_models import StrategyReconciliationState

        with self.factory() as session:
            session.add(
                StrategyReconciliationState(
                    account_id=account,
                    instrument_token=token,
                    exchange="NSE",
                    tradingsymbol="RELIANCE",
                    product=product,
                    divergence_class=divergence_class,
                    broker_quantity=100,
                    attributed_quantity=100 if divergence_class == "aligned" else 0,
                    manual_quantity=0,
                    residual_quantity=0 if divergence_class == "aligned" else 100,
                )
            )
            session.commit()

    def _ingest_state(self, status, *, account=ACCOUNT):
        from backend.strategies.attribution_models import AccountIngestState

        with self.factory() as session:
            session.add(AccountIngestState(account_id=account, status=status, ingest_generation=1))
            session.commit()

    def _run(self, run_id, status="open", *, account=ACCOUNT, mode=ENV):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs "
                    "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
                    "VALUES (:run_id, 'tok', 'tmpl', :account, :mode, :status)"
                ),
                {"run_id": run_id, "account": account, "mode": mode, "status": status},
            )
            session.commit()

    def _job(self, job_id, status, *, strategy=STRATEGY, account=ACCOUNT, mode=ENV):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.strategy_jobs "
                    "(id, strategy_id, owner_id, account_scope, execution_mode, status) "
                    "VALUES (:job_id, :strategy, 'app:o', :account, :mode, :status)"
                ),
                {"job_id": job_id, "strategy": strategy, "account": account, "mode": mode, "status": status},
            )
            session.commit()

    def _event_rows(self):
        with self.factory() as session:
            return session.execute(
                text(
                    "SELECT event, version FROM strategy_execution_barrier_events "
                    "WHERE account_id = :a AND strategy_id = :s AND execution_environment = :e "
                    "ORDER BY version ASC, id ASC"
                ),
                {"a": ACCOUNT, "s": STRATEGY, "e": ENV},
            ).fetchall()

    def _barrier_row(self):
        with self.factory() as session:
            row = session.execute(
                text(
                    "SELECT barrier_version, quiet_since_version, last_proof_at "
                    "FROM strategy_execution_barriers "
                    "WHERE account_id = :a AND strategy_id = :s AND execution_environment = :e"
                ),
                {"a": ACCOUNT, "s": STRATEGY, "e": ENV},
            ).fetchone()
        return row


class WorkEventTests(SettlementTestCase):
    def test_work_event_bumps_barrier_version_and_records_the_event(self):
        """A work transition bumps the version and inserts its event in ONE transaction."""
        self.assertEqual(self._barrier_row(), None)
        version = self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV,
            event="work_created", ref="order:OID-1",
        )
        self.assertEqual(version, 1)
        version = self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV,
            event="work_resolved", ref="order:OID-1",
        )
        self.assertEqual(version, 2)
        row = self._barrier_row()
        self.assertEqual(int(row[0]), 2)
        self.assertEqual([(event, ver) for event, ver in self._event_rows()],
                         [("work_created", 1), ("work_resolved", 2)])

    def test_events_carry_the_new_bumped_version(self):
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.assertEqual(
            [(event, int(ver)) for event, ver in self._event_rows()],
            [("work_created", 1), ("work_created", 2)],
        )


class ProofTests(SettlementTestCase):
    def test_proof_on_untouched_barrier_is_recorded_at_version_zero(self):
        """A book with no recorded work can prove quiescence at version 0."""
        self._bind("run-1")
        result = self.barrier.record_proof(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertTrue(result.recorded)
        self.assertEqual(result.barrier_version, 0)
        row = self._barrier_row()
        self.assertEqual(int(row[0]), 0)  # proofs do NOT bump the version
        self.assertEqual(int(row[1]), 0)  # quiet_since_version == barrier_version
        self.assertIsNotNone(row[2])
        self.assertEqual([(event, int(ver)) for event, ver in self._event_rows()],
                         [("proof_recorded", 0)])

    def test_proof_requires_empty_inflight(self):
        """In-flight work fails the proof and records NOTHING."""
        self._bind("run-1")
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self._order_link("OID-1")  # no order_state_projection row: non-terminal
        result = self.barrier.record_proof(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertFalse(result.recorded)
        self.assertEqual(result.reason, "inflight_work_present")
        self.assertTrue(result.inflight)
        row = self._barrier_row()
        self.assertEqual(int(row[0]), 1)
        self.assertIsNone(row[1])  # no quiet_since stamp: nothing was proved
        self.assertEqual([event for event, _ in self._event_rows()], ["work_created"])

    def test_proof_records_quiescence_when_no_work_is_in_flight(self):
        self._bind("run-1")
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_resolved"
        )
        self._order_link("OID-1")
        self._order_state("OID-1", "COMPLETE")
        result = self.barrier.record_proof(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertTrue(result.recorded)
        self.assertEqual(result.inflight, [])
        state = self.barrier.state(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertTrue(state["proof_valid"])
        self.assertEqual(state["barrier_version"], state["quiet_since_version"])

    def test_a_proof_is_scoped_to_one_strategy_and_environment(self):
        """A proof covers exactly one book: another strategy or mode is unproven."""
        self._bind("run-1")
        self.assertTrue(
            self.barrier.record_proof(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
            ).recorded
        )
        own = self.barrier.state(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertTrue(own["proof_valid"])
        # Another strategy on the same account is NOT covered by that proof.
        other_strategy = self.barrier.state(
            account_id=ACCOUNT, strategy_id="stg-OTHER", execution_environment=ENV
        )
        self.assertFalse(other_strategy["proof_valid"])
        self.assertFalse(other_strategy["exists"])
        # Nor is another execution environment for the same strategy (the
        # fixture proves the ``live`` book, so ``paper`` must stay unproven).
        other_env = self.barrier.state(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
        )
        self.assertFalse(other_env["proof_valid"])

    def test_unreadable_evidence_refuses_the_proof_and_records_nothing(self):
        """An unreadable work source is NOT an empty one: refuse, record nothing."""
        from backend.strategies import settlement as settlement_module

        def _unreadable(**_kwargs):
            raise settlement_module.SettlementEvidenceUnavailable("order_trade_fills")

        original = settlement_module.enumerate_inflight_work
        settlement_module.enumerate_inflight_work = _unreadable
        try:
            result = self.barrier.record_proof(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
            )
        finally:
            settlement_module.enumerate_inflight_work = original

        self.assertFalse(result.recorded)
        self.assertEqual(result.reason, "evidence_unavailable")
        self.assertEqual(result.unavailable, ["order_trade_fills"])
        # No proof row and no quiet stamp: the failure wrote nothing.
        self.assertIsNone(self._barrier_row())
        self.assertEqual(self._event_rows(), [])

    def test_any_later_work_event_invalidates_every_prior_proof(self):
        """The quiet window proves nothing: one work event kills the proof."""
        self._bind("run-1")
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_resolved"
        )
        result = self.barrier.record_proof(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertTrue(result.recorded)
        self.assertTrue(
            self.barrier.state(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)[
                "proof_valid"
            ]
        )
        # A quiet window and two identical reads later: still valid…
        self.assertTrue(
            self.barrier.state(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)[
                "proof_valid"
            ]
        )
        # …until ANY work event (here a late fill: work_created) bumps the version.
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV,
            event="work_created", ref="fill:T-9",
        )
        state = self.barrier.state(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertFalse(state["proof_valid"])
        self.assertNotEqual(state["barrier_version"], state["quiet_since_version"])

    def test_work_resolved_after_proof_does_not_restore_validity(self):
        self._bind("run-1")
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_resolved"
        )
        # Resolution is work too: the proof must be re-recorded, never assumed.
        state = self.barrier.state(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertFalse(state["proof_valid"])

    def test_unknown_evidence_fails_the_proof_never_an_empty_set(self):
        """A missing platform table is UNAVAILABLE evidence, not an empty book."""
        # A store pointed at a database with the barrier table but none of the
        # platform tables enumeration must read: fail closed, record nothing.
        from backend.strategies.attribution_models import StrategyExecutionBarrier

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine, tables=[StrategyExecutionBarrier.__table__])
        try:
            from backend.strategies.settlement import ExecutionBarrier

            broken = ExecutionBarrier(session_factory=sessionmaker(bind=engine))
            result = broken.record_proof(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
            )
            self.assertFalse(result.recorded)
            self.assertEqual(result.reason, "evidence_unavailable")
            self.assertTrue(result.unavailable)
            self.assertFalse(
                broken.state(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)[
                    "proof_valid"
                ]
            )
        finally:
            engine.dispose()

    def test_proof_transaction_takes_the_book_advisory_lock(self):
        """The proof runs under the book's advisory lock (SQLite: the call is pinned)."""
        calls = []

        original = type(self.barrier)._lock_barrier

        def _spy(session, account_id, strategy_id, execution_environment):
            calls.append((account_id, strategy_id, execution_environment))
            return original(session, account_id, strategy_id, execution_environment)

        type(self.barrier)._lock_barrier = staticmethod(_spy)
        try:
            self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        finally:
            type(self.barrier)._lock_barrier = staticmethod(original)
        self.assertEqual(calls, [(ACCOUNT, STRATEGY, ENV)])

    def test_work_events_take_the_same_book_lock(self):
        """Work bumps serialize on the book lock too: no bump inside a proof window."""
        calls = []

        original = type(self.barrier)._lock_barrier

        def _spy(session, account_id, strategy_id, execution_environment):
            calls.append((account_id, strategy_id, execution_environment))
            return original(session, account_id, strategy_id, execution_environment)

        type(self.barrier)._lock_barrier = staticmethod(_spy)
        try:
            self.barrier.record_work_event(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
            )
        finally:
            type(self.barrier)._lock_barrier = staticmethod(original)
        self.assertEqual(calls, [(ACCOUNT, STRATEGY, ENV)])


class InflightEnumerationTests(SettlementTestCase):
    def _enumerate(self):
        from backend.strategies.settlement import enumerate_inflight_work

        with self.factory() as session:
            items = enumerate_inflight_work(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, db=session
            )
        return {(item.kind, item.ref) for item in items}

    def test_empty_book_and_no_bindings_enumerates_empty(self):
        self.assertEqual(self._enumerate(), set())

    def test_non_terminal_attributed_order_is_in_flight(self):
        self._bind("run-1")
        self._order_link("OID-1")
        self.assertIn(("order_non_terminal", "run-1:OID-1"), self._enumerate())

    def test_terminal_order_is_not_in_flight(self):
        self._bind("run-1")
        for status in TERMINAL_ORDER_STATUSES:
            order_id = f"OID-{status}"
            self._order_link(order_id)
            self._order_state(order_id, status)
        self.assertEqual(self._enumerate(), set())

    def test_missing_order_state_projection_is_non_terminal(self):
        """No projection row for a placed order is UNKNOWN state, i.e. in flight."""
        self._bind("run-1")
        self._order_link("OID-MISSING")
        self.assertIn(("order_non_terminal", "run-1:OID-MISSING"), self._enumerate())

    def test_placed_intent_without_terminal_outcome_is_in_flight(self):
        self._bind("run-1")
        self._intent("it-1", order_id="OID-I")
        self.assertIn(("intent_unresolved", "it-1"), self._enumerate())

    def test_failed_intent_and_terminal_outcome_are_not_in_flight(self):
        self._bind("run-1")
        self._intent("it-failed", order_id="OID-F", status="failed")
        self._intent("it-done", order_id="OID-D")
        self._order_state("OID-D", "COMPLETE")
        items = self._enumerate()
        self.assertNotIn(("intent_unresolved", "it-failed"), items)
        self.assertNotIn(("intent_unresolved", "it-done"), items)

    def test_unresolved_trade_link_net_is_in_flight(self):
        """Trade-linked fills netting non-zero are unresolved execution (G1 logic)."""
        self._bind("run-1")
        self._order_link("OID-1", trade_id="T-1")
        self._fill("T-1", side="BUY", qty=100)
        self._fill("T-2", order_id="OID-2", side="SELL", qty=40)
        self._order_link("OID-2", trade_id="T-2")
        self.assertIn(("execution_link_unresolved", "run-1"), self._enumerate())

    def test_balanced_trade_links_are_not_in_flight(self):
        self._bind("run-1")
        self._order_link("OID-1", trade_id="T-1")
        self._fill("T-1", side="BUY", qty=100)
        self._fill("T-2", order_id="OID-1", side="SELL", qty=100)
        self._order_link("OID-2", trade_id="T-2")
        self.assertNotIn(("execution_link_unresolved", "run-1"), self._enumerate())

    def test_non_aligned_reconciliation_coordinate_of_the_book_is_in_flight(self):
        self._bind("run-1")
        self._book_row()
        self._reconciliation(738561, "pending_ingest")
        self.assertIn(
            ("reconciliation_non_aligned", f"738561:NSE:RELIANCE:CNC"), self._enumerate()
        )

    def test_unexplained_reconciliation_coordinate_is_in_flight(self):
        self._bind("run-1")
        self._book_row()
        self._reconciliation(738561, "unexplained")
        self.assertIn(("reconciliation_non_aligned", "738561:NSE:RELIANCE:CNC"), self._enumerate())

    def test_aligned_reconciliation_coordinate_is_not_in_flight(self):
        self._bind("run-1")
        self._book_row()
        self._reconciliation(738561, "aligned")
        self.assertEqual(self._enumerate(), set())

    def test_refreshing_ingest_state_is_in_flight(self):
        self._ingest_state("refreshing")
        self.assertIn(("ingest_refreshing", ACCOUNT), self._enumerate())

    def test_idle_ingest_state_is_not_in_flight(self):
        self._ingest_state("idle")
        self.assertEqual(self._enumerate(), set())

    def test_enumeration_is_scoped_to_this_strategy_and_environment(self):
        """Another strategy's in-flight work never blocks THIS book's proof."""
        from backend.strategies.attribution_models import Strategy

        with self.factory() as session:
            session.add(Strategy(id="stg-B", owner_id="app:o", name="B", account_scope=ACCOUNT, status="active"))
            session.commit()
        self._bind("run-mine")
        self._bind("run-other", strategy="stg-B")
        self._order_link("OID-OTHER", run_id="run-other")
        self._intent("it-other", run_id="run-other", order_id="OID-I2")
        self._book_row(strategy="stg-B")
        self._reconciliation(738561, "pending_ingest")
        self._ingest_state("refreshing", account="kite:OTHER")
        # ...and a paper binding for this same strategy must not leak either.
        self._bind("run-paper", env="paper")
        self._order_link("OID-PAPER", run_id="run-paper")

        items = self._enumerate()
        self.assertEqual(items, set())

    def test_enumeration_items_are_sorted_and_deterministic(self):
        self._bind("run-1")
        self._order_link("OID-B")
        self._order_link("OID-A")
        self._intent("it-2")
        self._intent("it-1")
        from backend.strategies.settlement import enumerate_inflight_work

        with self.factory() as session:
            first = [
                (item.kind, item.ref)
                for item in enumerate_inflight_work(
                    account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, db=session
                )
            ]
            second = [
                (item.kind, item.ref)
                for item in enumerate_inflight_work(
                    account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, db=session
                )
            ]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Four-axis settlement assessment (D-3, D-5)
# ---------------------------------------------------------------------------

import uuid as _uuid
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


class AssessmentTestCase(SettlementTestCase):
    """Fixture extensions for the four axes: plans, reservations, approvals."""

    def setUp(self):
        super().setUp()
        from backend.strategies.settlement import ExecutionBarrier

        self.service = self._service()

    def _service(self):
        from backend.strategies.settlement import SettlementService

        return SettlementService(session_factory=self.factory)

    def _proposal_and_plan(self, plan_id="plan-1", *, proposal_id=None, strategy=STRATEGY, account=ACCOUNT):
        proposal_id = proposal_id or f"prop-{plan_id}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, :strategy, :account, :eid, 'run_now', 'run-1', "
                    " 'single_instrument', '{}', 'sha', 'validated')"
                ),
                {"pid": proposal_id, "strategy": strategy, "account": account, "eid": f"eval-{plan_id}"},
            )
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:plan_id, :pid, :strategy, :account, 'single_instrument', 'h', "
                    " '{}', '{}', '11111111-1111-1111-1111-111111111111')"
                ),
                {"plan_id": plan_id, "pid": proposal_id, "strategy": strategy, "account": account},
            )
            session.commit()

    def _reservation(self, plan_id, status="active", *, reservation_id="res-1", strategy=STRATEGY, account=ACCOUNT):
        self._proposal_and_plan(plan_id)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_reservations "
                    "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                    " execution_environment, status, reserved_notional_inr, valid_until) "
                    "VALUES (:rid, :plan_id, :strategy, :account, :eid, 'live', :status, 1000.0, :until)"
                ),
                {
                    "rid": reservation_id,
                    "plan_id": plan_id,
                    "strategy": strategy,
                    "account": account,
                    "eid": f"eval-{plan_id}",
                    "status": status,
                    "until": _iso(NOW + timedelta(hours=1)),
                },
            )
            session.commit()

    def _approval(self, status="active", *, approval_id="appr-1", plan_id="plan-1", strategy=STRATEGY, account=ACCOUNT):
        self._reservation(plan_id, "consumed", reservation_id=f"res-for-{approval_id}", strategy=strategy, account=account)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_approvals "
                    "(approval_id, plan_id, strategy_id, account_id, reservation_id, plan_hash, "
                    " exposure_snapshot_version, reconciliation_version, catalog_generation, "
                    " actor_id, status, valid_from, valid_until) "
                    "VALUES (:aid, :plan_id, :strategy, :account, :rid, 'h', 1, 0, "                " '11111111-1111-1111-1111-111111111111', 'app:o', :status, :from, :until)"
                ),
                {
                    "aid": approval_id,
                    "rid": f"res-for-{approval_id}",
                    "plan_id": plan_id,
                    "strategy": strategy,
                    "account": account,
                    "status": status,
                    "from": _iso(NOW - timedelta(minutes=5)),
                    "until": _iso(NOW + timedelta(minutes=55)),
                },
            )
            session.commit()

    def _archived_strategy(self):
        with self.factory() as session:
            session.execute(
                text("UPDATE strategies SET status = 'archived' WHERE id = :sid"), {"sid": STRATEGY}
            )
            session.commit()

    def _axis(self, service_result):
        return service_result["axes"]


class QuiescenceAxisTests(AssessmentTestCase):
    def test_valid_proof_satisfies_the_quiescence_axis(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        result = self.service.assess(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        axis = self._axis(result)["quiescence"]
        self.assertEqual(axis["state"], "satisfied")
        self.assertTrue(axis["satisfied"])

    def test_no_proof_is_unknown_never_failed(self):
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        result = self.service.assess(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        axis = self._axis(result)["quiescence"]
        self.assertEqual(axis["state"], "unknown")
        self.assertFalse(axis["satisfied"])

    def test_work_after_the_proof_makes_quiescence_unknown(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        result = self.service.assess(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertEqual(self._axis(result)["quiescence"]["state"], "unknown")

    def test_proof_older_than_the_required_transition_is_unknown(self):
        """Run settlement's floor: the proof must postdate the run's terminal move."""
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        result = self.service.assess(
            account_id=ACCOUNT,
            strategy_id=STRATEGY,
            execution_environment=ENV,
            proof_not_before=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        axis = self._axis(result)["quiescence"]
        self.assertEqual(axis["state"], "unknown")
        self.assertEqual(axis["detail"].get("reason"), "proof_predates_required_transition")


class FlatnessAxisTests(AssessmentTestCase):
    def test_open_book_with_fresh_broker_truth_is_failed(self):
        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "failed")
        self.assertEqual(axis["detail"]["open_legs"], 1)

    def test_stale_broker_snapshot_is_unknown_even_with_an_open_book(self):
        """Refresh-before-decide: a stale snapshot cannot decide anything."""
        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc) - timedelta(seconds=600))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "unknown")
        self.assertEqual(axis["detail"].get("reason"), "stale_broker_snapshot")

    def test_zero_book_is_flat_scoped_to_this_strategy(self):
        """Shared line (G2 rule): another strategy holding the instrument — the
        broker aggregate is non-zero at that coordinate — never unsettles THIS
        zero book. Account flatness (or its absence) never substitutes."""
        from backend.strategies.attribution_models import Strategy

        with self.factory() as session:
            session.add(Strategy(id="stg-B", owner_id="app:o", name="B", account_scope=ACCOUNT, status="active"))
            session.commit()
        self._book_row(strategy="stg-B", qty=500)  # another strategy's line
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 500, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "satisfied")
        self.assertEqual(axis["detail"]["open_legs"], 0)

    def test_open_book_is_failed_even_when_the_account_coordinate_is_flat(self):
        """Account flatness never substitutes: the strategy's OWN book governs."""
        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 0, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "failed")

    def test_paper_flatness_uses_paper_positions(self):
        result = self.service.assess(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
        )
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "satisfied")

    def test_paper_open_position_is_failed(self):
        self._book_row(env="paper")
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.paper_positions "
                    "(account_scope, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        result = self.service.assess(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
        )
        self.assertEqual(self._axis(result)["attribution_scoped_flatness"]["state"], "failed")

    def test_broker_snapshot_max_age_is_configurable(self):
        """SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS: default 60, overridable."""
        import os
        from unittest.mock import patch

        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc) - timedelta(seconds=10))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["attribution_scoped_flatness"]["state"], "failed")
        with patch.dict(os.environ, {"SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS": "5"}):
            result = self.service.assess(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
            )
        axis = self._axis(result)["attribution_scoped_flatness"]
        self.assertEqual(axis["state"], "unknown")
        self.assertEqual(axis["detail"].get("reason"), "stale_broker_snapshot")


class DomainTerminalAxisTests(AssessmentTestCase):
    def test_open_run_is_failed(self):
        self._bind("run-1")
        self._run("run-1", status="open")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["terminal_domain_state"]
        self.assertEqual(axis["state"], "failed")
        self.assertIn("run-1", axis["detail"]["non_terminal_runs"])

    def test_closed_run_is_terminal(self):
        self._bind("run-1")
        self._run("run-1", status="closed")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "satisfied")

    def test_running_job_is_failed(self):
        self._job("job-1", "running")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["terminal_domain_state"]
        self.assertEqual(axis["state"], "failed")
        self.assertIn("job-1", axis["detail"]["non_terminal_jobs"])

    def test_stopped_job_is_terminal(self):
        self._job("job-1", "stopped")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "satisfied")

    def test_job_of_another_environment_does_not_block(self):
        self._job("job-paper", "running", mode="paper")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "satisfied")

    def test_plan_without_reservation_is_non_terminal(self):
        self._proposal_and_plan("plan-1")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["terminal_domain_state"]
        self.assertEqual(axis["state"], "failed")
        self.assertIn("plan-1", axis["detail"]["non_terminal_plans"])

    def test_plan_with_active_reservation_is_non_terminal(self):
        self._reservation("plan-1", status="active")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "failed")

    def test_plan_with_consumed_reservation_is_terminal(self):
        self._reservation("plan-1", status="consumed")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "satisfied")

    def test_plan_with_expired_reservation_is_terminal(self):
        self._reservation("plan-1", status="expired")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["terminal_domain_state"]["state"], "satisfied")

class NoAuthorityAxisTests(AssessmentTestCase):
    def test_active_approval_is_failed(self):
        self._approval(status="active")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        axis = self._axis(result)["no_live_evaluation_authority"]
        self.assertEqual(axis["state"], "failed")
        self.assertIn("appr-1", axis["detail"]["active_approvals"])

    def test_revoked_approval_is_not_authority(self):
        self._approval(status="revoked")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["no_live_evaluation_authority"]["state"], "satisfied")

    def test_active_strategy_with_no_runs_keeps_no_authority_satisfied(self):
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["no_live_evaluation_authority"]["state"], "satisfied")

    def test_open_run_on_active_strategy_is_failed(self):
        self._bind("run-1")
        self._run("run-1", status="open")
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["no_live_evaluation_authority"]["state"], "failed")

    def test_archived_strategy_has_no_authority_even_with_open_run(self):
        self._bind("run-1")
        self._run("run-1", status="open")
        self._archived_strategy()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._axis(result)["no_live_evaluation_authority"]["state"], "satisfied")


class RollupAndPersistenceTests(AssessmentTestCase):
    def test_all_axes_satisfied_roll_up_to_settled(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(result["overall"], "settled")

    def test_any_failed_axis_makes_the_assessment_unsettled(self):
        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(result["overall"], "unsettled")

    def test_any_unknown_axis_makes_the_assessment_unknown(self):
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(result["overall"], "unknown")

    def test_failed_takes_precedence_over_unknown(self):
        self._book_row()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, net_quantity, updated_at) "
                    "VALUES (:account, 738561, 'CNC', 100, :at)"
                ),
                {"account": ACCOUNT, "at": _iso(datetime.now(timezone.utc))},
            )
            session.commit()
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        states = {name: axis["state"] for name, axis in result["axes"].items()}
        self.assertIn("failed", states.values())
        self.assertIn("unknown", states.values())
        self.assertEqual(result["overall"], "unsettled")

    def test_assessments_are_append_only_snapshots_with_digests(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        first = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        second = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertNotEqual(first["assessment_id"], second["assessment_id"])
        self.assertEqual(first["overall"], "settled")
        self.assertEqual(second["overall"], "settled")
        for axis in second["axes"].values():
            self.assertTrue(axis["evidence_digest"])
        self.assertTrue(second["evidence_digest"])
        self.assertEqual(second["barrier_version"], 0)
        latest = self.service.latest_assessment(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        self.assertEqual(latest["assessment_id"], second["assessment_id"])

    def test_settled_assessment_is_detectably_stale_after_a_barrier_bump(self):
        """Late fill (work_created) invalidates: the snapshot says which version."""
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        settled = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(settled["overall"], "settled")
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV,
            event="work_created", ref="fill:late-1",
        )
        latest = self.service.latest_assessment(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )
        current = self.barrier.state(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV
        )["barrier_version"]
        self.assertTrue(latest["stale"])
        self.assertEqual(latest["barrier_version"], 0)
        self.assertEqual(current, 1)

    def test_domain_adapter_registry_hook_participates_in_the_rollup(self):
        from backend.strategies import settlement as settlement_module

        def failing_adapter(*, account_id, strategy_id, execution_environment, db):
            return [
                {
                    "name": "domain:option_runs",
                    "state": "failed",
                    "detail": {"reason": "phase-scope: none required now"},
                }
            ]

        settlement_module.register_domain_adapter(failing_adapter)
        try:
            result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
            self.assertIn("domain:option_runs", result["axes"])
            self.assertEqual(result["overall"], "unsettled")
        finally:
            settlement_module.settlement_domain_adapters.clear()

    def test_adapter_failure_is_unknown_never_satisfied(self):
        from backend.strategies import settlement as settlement_module

        def broken_adapter(**kwargs):
            raise RuntimeError("adapter exploded")

        settlement_module.register_domain_adapter(broken_adapter)
        try:
            result = self.service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
            axis = result["axes"]["domain:broken_adapter"]
            self.assertEqual(axis["state"], "unknown")
            self.assertEqual(result["overall"], "unknown")
        finally:
            settlement_module.settlement_domain_adapters.clear()


# ---------------------------------------------------------------------------
# Reconciliation consumes the barrier (D-4)
# ---------------------------------------------------------------------------


class BarrierQuiescenceStateTests(AssessmentTestCase):
    def _state(self, *, strategy_id=STRATEGY, env=ENV, account=ACCOUNT, barrier=None):
        from backend.strategies.reconciliation import barrier_quiescence_state

        return barrier_quiescence_state(
            account_id=account,
            strategy_id=strategy_id,
            execution_environment=env,
            barrier=barrier or self.barrier,
        )

    def test_valid_proof_verifies(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.assertEqual(self._state(), "verified")

    def test_no_proof_stays_unverified(self):
        self.assertEqual(self._state(), "unverified")

    def test_work_after_the_proof_stays_unverified(self):
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV)
        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment=ENV, event="work_created"
        )
        self.assertEqual(self._state(), "unverified")

    def test_unattributed_job_can_never_be_verified(self):
        self.assertEqual(self._state(strategy_id=None), "unverified")

    def test_unreadable_barrier_is_unverified_never_verified(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            broken = self._barrier()
            object.__setattr__(broken, "session_factory", sessionmaker(bind=engine))
            self.assertEqual(self._state(barrier=broken), "unverified")
        finally:
            engine.dispose()


class ReconciliationIntegrationTests(AssessmentTestCase):
    """Trading-capable reconciliation unblocks ONLY on a valid barrier proof."""

    def _collect(self, collector, job):
        import asyncio

        return asyncio.run(collector.collect(job))

    def _job(self):
        from types import SimpleNamespace

        from backend.strategies import service as strategy_service

        return SimpleNamespace(
            id="hsj_1",
            strategy_id=STRATEGY,
            attempt=1,
            status="recovery_required",
            desired_state="started",
            execution_mode="paper",
            account_scope=ACCOUNT,
            run_id="run-1",
            token_id="worker_1",
            handoff_at=datetime.now(timezone.utc),
            reconciled_at=None,
            process_cleanup_state="confirmed",
            process_cleanup_at=datetime.now(timezone.utc),
            process_cleanup_actor="sup-1",
            capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=True),
        )

    def _collector(self):
        from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector

        class _Worker:
            async def get_run(self, run_id):
                return {"status": "closed", "runtime_state": {}}

            async def get_token_status(self, token_id):
                return "revoked"

        class _Paper:
            async def get_strategy_run_settlement_readonly(self, account_scope, run_id):
                return {
                    "account_scope": ACCOUNT,
                    "strategy_run_id": "run-1",
                    "run_state": {
                        "strategy_run_id": "run-1",
                        "is_stale": False,
                        "last_event_at": "2026-09-17T10:00:00+00:00",
                        "positions": [],
                    },
                    "order_count": 2,
                    "pending_order_count": 0,
                    "coverage_complete": True,
                }

        return ReconciliationEvidenceCollector(
            worker_repo=_Worker(),
            paper_runtime=_Paper(),
            settlement_barrier=self.barrier,
        )

    def test_collector_sets_verified_only_on_a_valid_proof(self):
        from backend.strategies.reconciliation import (
            BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED,
            CASE_TRADING_SETTLED_FLAT,
            assess,
        )

        job = self._job()
        collector = self._collector()

        # No proof yet: the trading-capable attempt stays blocked.
        evidence = self._collect(collector, job)
        self.assertEqual(evidence.quiescence_state, "unverified")
        result = assess(evidence)
        self.assertFalse(result.allowed)
        self.assertIn(BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED, result.blocking_reasons)

        # A valid proof at assessment time verifies — and unblocks the attempt.
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper")
        evidence = self._collect(collector, job)
        self.assertEqual(evidence.quiescence_state, "verified")
        result = assess(evidence)
        self.assertTrue(result.allowed)
        self.assertEqual(result.case, CASE_TRADING_SETTLED_FLAT)

    def test_work_after_collection_invalidates_the_next_assessment(self):
        """The proof covers the book at assessment time; later work re-blocks."""
        from backend.strategies.reconciliation import (
            BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED,
            assess,
        )

        job = self._job()
        collector = self._collector()
        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper")
        evidence = self._collect(collector, job)
        self.assertEqual(evidence.quiescence_state, "verified")

        self.barrier.record_work_event(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper", event="work_created"
        )
        re_evidence = self._collect(collector, job)
        self.assertEqual(re_evidence.quiescence_state, "unverified")
        result = assess(re_evidence)
        self.assertFalse(result.allowed)
        self.assertIn(BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED, result.blocking_reasons)

    def test_digest_stability_is_preserved(self):
        """D-4 keeps the digest discipline: recomputation is stable, states differ."""
        from backend.strategies.reconciliation import evidence_digest

        job = self._job()
        collector = self._collector()
        unverified = self._collect(collector, job)
        self.assertEqual(evidence_digest(unverified), evidence_digest(unverified))

        self.barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper")
        verified = self._collect(collector, job)
        # quiescence_state is one of the digest's axes: the state move re-digests.
        self.assertNotEqual(evidence_digest(unverified), evidence_digest(verified))
