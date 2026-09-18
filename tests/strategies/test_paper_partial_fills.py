"""Paper partial fills: tranches, progress and the reservation consequence (G12).

Before this, every paper fill was instant and full, which flatters a rebalance:
real orders fill across trades and leave remainders open. The rules under test are
about what a remainder *means* — it is in-flight, not settled, so nothing may
treat an open order as a finished one, and the reservation must react to verified
progress rather than to hope.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class PartialFillTestCase(unittest.TestCase):
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
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.store = self._store()

    def tearDown(self):
        self.engine.dispose()

    def _store(self):
        from backend.paper_runtime.partial_fills import PaperFillProgressStore

        return PaperFillProgressStore(session_factory=self.factory)


class TrancheTests(PartialFillTestCase):
    def test_the_ratio_decides_how_much_fills_per_attempt(self):
        from backend.paper_runtime.partial_fills import next_tranche

        self.assertEqual(next_tranche(100, 0.5), 50)
        self.assertEqual(next_tranche(100, 0.25), 25)
        self.assertEqual(next_tranche(100, 1.0), 100)

    def test_a_tranche_is_never_rounded_away(self):
        from backend.paper_runtime.partial_fills import next_tranche

        # A small remainder with a small ratio must still fill something: an
        # order that never progresses while claiming to make progress is worse
        # than one that fills late.
        self.assertEqual(next_tranche(1, 0.5), 1)
        self.assertEqual(next_tranche(3, 0.1), 1)
        self.assertEqual(next_tranche(0, 0.5), 0)

    def test_partial_fills_are_opt_in(self):
        """The default is instant-full, so an upgrade changes no existing behaviour.

        Partial fills change what an execution MEANS — an order that used to be
        finished is now in flight — so a deployment chooses them rather than
        inheriting them.
        """
        from backend.paper_runtime.partial_fills import next_tranche, partial_fill_ratio

        self.assertEqual(partial_fill_ratio(), 1.0)
        self.assertEqual(next_tranche(100, partial_fill_ratio()), 100)

    def test_the_ratio_is_configurable(self):
        from backend.paper_runtime.partial_fills import next_tranche, partial_fill_ratio

        os.environ["PAPER_PARTIAL_FILL_RATIO"] = "0.25"
        try:
            self.assertEqual(partial_fill_ratio(), 0.25)
            self.assertEqual(next_tranche(100, partial_fill_ratio()), 25)
        finally:
            os.environ.pop("PAPER_PARTIAL_FILL_RATIO", None)

    def test_a_nonsense_ratio_falls_back_to_instant_full(self):
        from backend.paper_runtime.partial_fills import partial_fill_ratio

        for value in ("0", "-1", "not-a-number"):
            os.environ["PAPER_PARTIAL_FILL_RATIO"] = value
            try:
                self.assertEqual(partial_fill_ratio(), 1.0, value)
            finally:
                os.environ.pop("PAPER_PARTIAL_FILL_RATIO", None)


class ProgressStoreTests(PartialFillTestCase):
    def test_progress_starts_open_and_advances_by_tranches(self):
        from backend.paper_runtime.partial_fills import next_tranche

        progress = self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.assertEqual((progress.filled_quantity, progress.remaining_quantity, progress.status),
                         (0, 100, "open"))

        first = next_tranche(progress.remaining_quantity, 0.5)
        progress = self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1",
            filled_quantity=first, quantity=100,
        )
        self.assertEqual((progress.filled_quantity, progress.remaining_quantity, progress.status),
                         (50, 50, "partially_filled"))
        self.assertTrue(progress.is_open)
        self.assertFalse(progress.is_complete)

        # Each attempt fills half of what is LEFT, so it converges rather than
        # completing on the second try — and it must always terminate.
        attempts = 0
        while not progress.is_complete and attempts < 20:
            tranche = next_tranche(progress.remaining_quantity, 0.5)
            progress = self.store.record_fill(
                account_scope="kite:paper", paper_order_id="PAPER-1",
                filled_quantity=tranche, quantity=100,
            )
            attempts += 1
        self.assertTrue(progress.is_complete, progress)
        self.assertEqual((progress.filled_quantity, progress.remaining_quantity, progress.status),
                         (100, 0, "filled"))
        # Monotonic: every attempt filled something, so it cannot stall.
        self.assertLess(attempts, 20)

    def test_starting_twice_returns_the_existing_progress(self):
        first = self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1", filled_quantity=50, quantity=100
        )
        again = self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        # A second attempt must not reset progress it already made.
        self.assertEqual(again.filled_quantity, 50)
        self.assertEqual(first.paper_order_id, again.paper_order_id)

    def test_an_order_with_no_progress_row_is_simply_unknown(self):
        # Which is what makes the table additive: existing instant-full orders
        # behave exactly as before because nothing ever created a row for them.
        self.assertIsNone(
            self.store.progress_for(account_scope="kite:paper", paper_order_id="PAPER-NONE")
        )

    def test_an_open_remainder_is_in_flight(self):
        self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1", filled_quantity=50, quantity=100
        )
        self.store.start(account_scope="kite:paper", paper_order_id="PAPER-2", quantity=10)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-2", filled_quantity=10, quantity=10
        )

        self.assertTrue(self.store.has_open_remainder(account_scope="kite:paper"))
        open_rows = self.store.open_remainder_for(account_scope="kite:paper")
        # Only the unfinished order counts: a filled one is not in flight.
        self.assertEqual([row.paper_order_id for row in open_rows], ["PAPER-1"])

    def test_a_cancelled_order_is_not_in_flight(self):
        self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1", filled_quantity=50, quantity=100
        )
        self.store.cancel(account_scope="kite:paper", paper_order_id="PAPER-1")
        self.assertFalse(self.store.has_open_remainder(account_scope="kite:paper"))

    def test_remainders_can_be_asked_about_by_order_id_alone(self):
        self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1", filled_quantity=40, quantity=100
        )
        rows = self.store.open_remainder_for(paper_order_ids=["PAPER-1", "PAPER-OTHER"])
        self.assertEqual([row.paper_order_id for row in rows], ["PAPER-1"])


class ReservationConsequenceTests(PartialFillTestCase):
    """The Phase 4 renewal condition, now that progress can be partial."""

    def _executor(self):
        from backend.strategies.execution import PaperPlanExecutor
        from backend.strategies.reservations import ReservationLedger

        ledger = ReservationLedger(session_factory=self.factory)
        executor = PaperPlanExecutor(
            session_factory=self.factory, ledger=ledger, fill_progress_store=self.store
        )
        return executor, ledger

    def _reservation(self, ledger):
        """A real active reservation, so the transitions are real ones."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES ('prop-1', 'stg-A', 'kite:A', 'eval-1', 'run_now', 'run-1', "
                    " 'target_weights', '{}', 'sha', 'validated')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES ('plan-1', 'prop-1', 'stg-A', 'kite:A', 'target_weights', 'h', '{}', "
                    " '{}', '11111111-1111-1111-1111-111111111111', 'rev-1', 'mh-1')"
                )
            )
            session.commit()
        return ledger.claim(
            __import__("backend.strategies.reservations", fromlist=["ClaimRequest"]).ClaimRequest(
                plan_id="plan-1", strategy_id="stg-A", account_id="kite:A", evaluation_id="eval-1",
                execution_environment="paper", requirement_inr=1000.0,
                valid_until=NOW + timedelta(hours=1), allocation_inr=10000.0, actor_id="app:o",
            ),
            now=NOW,
        )

    def test_verified_progress_renews_instead_of_consuming(self):
        executor, ledger = self._executor()
        reservation = self._reservation(ledger)
        self.store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        self.store.record_fill(
            account_scope="kite:paper", paper_order_id="PAPER-1", filled_quantity=50, quantity=100
        )

        executor._settle_reservation(
            reservation, "worker:1", filled_ids=["PAPER-1"], partial_ids=["PAPER-1"],
            failed=False, submitted_any=True,
        )

        current = ledger.get(reservation["reservation_id"])
        # A tranche filled and more is outstanding: the capacity is extended, not
        # consumed (the plan is unfinished) and not released (it is still working).
        self.assertEqual(current["status"], "renewed")
        events = [row["event"] for row in ledger.events(reservation["reservation_id"])]
        self.assertEqual(events, ["created", "renewed"])

    def test_an_unresolved_remainder_holds_capacity_and_flags_the_owner(self):
        executor, ledger = self._executor()
        reservation = self._reservation(ledger)
        executor._settle_reservation(
            reservation, "worker:1", filled_ids=[], partial_ids=["PAPER-1"],
            failed=False, submitted_any=True,
        )
        current = ledger.get(reservation["reservation_id"])
        # Capacity is NOT released on an open remainder: it is flagged instead.
        self.assertEqual(current["status"], "action_required")
        self.assertEqual(ledger.held_notional(account_id="kite:A"), 1000.0)

    def test_a_fully_filled_bundle_still_consumes(self):
        executor, ledger = self._executor()
        reservation = self._reservation(ledger)
        # No open remainder: the paper order finished, so the capacity becomes
        # attributed exposure.
        executor._settle_reservation(
            reservation, "worker:1", filled_ids=["PAPER-1"], partial_ids=[],
            failed=False, submitted_any=True,
        )
        self.assertEqual(ledger.get(reservation["reservation_id"])["status"], "consumed")

    def test_a_terminal_unfilled_bundle_still_releases(self):
        executor, ledger = self._executor()
        reservation = self._reservation(ledger)
        executor._settle_reservation(
            reservation, "worker:1", filled_ids=[], partial_ids=[],
            failed=False, submitted_any=True,
        )
        self.assertEqual(ledger.get(reservation["reservation_id"])["status"], "released")

    def test_unreadable_progress_is_not_proof_of_completion(self):
        executor, _ = self._executor()

        class _Broken:
            def open_remainder_for(self, **kwargs):
                raise RuntimeError("boom")

        executor.fill_progress = _Broken()
        # Failing to read progress must not be read as "nothing is outstanding".
        self.assertEqual(executor._open_remainders(["PAPER-1"]), ["PAPER-1"])
        self.assertEqual(executor._open_remainders([]), [])


class ExecutorOutcomeTests(PartialFillTestCase):
    """A partially filled step is progress that is NOT resolution."""

    def setUp(self):
        super().setUp()
        # The execution trail FKs to a real plan, so the plan must exist.
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:paper', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES ('prop-1', 'stg-A', 'kite:paper', 'eval-1', 'run_now', 'run-1', "
                    " 'target_weights', '{}', 'sha', 'validated')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES ('plan-1', 'prop-1', 'stg-A', 'kite:paper', 'target_weights', 'h', "
                    " '{}', '{}', '11111111-1111-1111-1111-111111111111', 'rev-1', 'mh-1')"
                )
            )
            session.commit()

    def test_a_partially_filled_step_advances_without_resolving_work(self):
        from backend.strategies.execution import PaperPlanExecutor

        executor = PaperPlanExecutor(session_factory=self.factory, fill_progress_store=self.store)
        # The barrier must not hear a resolution for an order that is still open.
        recorded: list = []
        executor.barrier.record_work_event = lambda **kwargs: recorded.append(kwargs)

        class _Service:
            async def place_order(self, *, account_scope, order_payload, attribution):
                return {
                    "mode": "paper",
                    "status": "partially_filled",
                    "order": {
                        "order_id": "PAPER-1",
                        "tradingsymbol": "RELIANCE",
                        "filled_quantity": 50,
                        "pending_quantity": 50,
                    },
                }

        executor._paper_service = _Service()
        plan = {"plan_id": "plan-1", "strategy_id": "stg-A", "account_id": "kite:paper",
                "strategy_run_id": "run-1"}
        binding = {"strategy_id": "stg-A", "account_id": "kite:paper",
                   "strategy_run_id": "run-1"}
        submission = asyncio.run(
            executor._submit_step(
                plan,
                {"reservation_id": None},
                "worker:1",
                step_no=1,
                leg={"tradingsymbol": "RELIANCE", "product": "CNC", "instrument_id": "inst-1"},
                quantity=100,
                side="BUY",
                binding=binding,
                at=NOW,
            )
        )
        self.assertEqual(submission["outcome"]["event"], "partially_filled")
        # The work is created but NOT resolved: an open remainder is still in
        # flight, and resolving it would assert a flatness the account lacks.
        events = [row.get("event") for row in recorded]
        self.assertIn("work_created", events)
        self.assertNotIn("work_resolved", events)

    def test_a_filled_step_still_resolves_work(self):
        from backend.strategies.execution import PaperPlanExecutor

        executor = PaperPlanExecutor(session_factory=self.factory, fill_progress_store=self.store)
        recorded: list = []
        executor.barrier.record_work_event = lambda **kwargs: recorded.append(kwargs)

        class _Service:
            async def place_order(self, *, account_scope, order_payload, attribution):
                return {
                    "mode": "paper",
                    "status": "filled",
                    "order": {"order_id": "PAPER-1", "tradingsymbol": "RELIANCE",
                              "filled_quantity": 100, "average_price": "10"},
                }

        executor._paper_service = _Service()
        plan = {"plan_id": "plan-1", "strategy_id": "stg-A", "account_id": "kite:paper",
                "strategy_run_id": "run-1"}
        submission = asyncio.run(
            executor._submit_step(
                plan,
                {"reservation_id": None},
                "worker:1",
                step_no=1,
                leg={"tradingsymbol": "RELIANCE", "product": "CNC", "instrument_id": "inst-1"},
                quantity=100,
                side="BUY",
                binding={"strategy_id": "stg-A", "account_id": "kite:paper",
                         "strategy_run_id": "run-1"},
                at=NOW,
            )
        )
        self.assertEqual(submission["outcome"]["event"], "filled")
        # A completed fill IS resolution, so the barrier hears about it.
        self.assertIn("work_resolved", [row.get("event") for row in recorded])


if __name__ == "__main__":
    unittest.main()
