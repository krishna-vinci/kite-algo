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
