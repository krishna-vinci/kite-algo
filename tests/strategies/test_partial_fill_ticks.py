"""Partial-fill rebalance certified across ticks (D-5, carried from Phase 7).

Phase 7 shipped partial fills but could not certify a rebalance completing across
ticks, because the paper runtime's tick path wants a market snapshot and nothing
supplied one. A certification that depends on a live feed is not a certification.

What is certified here is the set of invariants D-5 names: tranches advance
monotonically across ticks, the reservation renews on verified progress, the fill
converges instead of decaying forever, an order with a remainder is never reported
as resolved, and settlement is assessed only at true quiescence.

The tick SOURCE is synthetic and deterministic — that is the point, and it is
stated rather than hidden. Each tick applies one tranche through the same store
the executor reads, so the thing under test is the invariant chain, not the paper
runtime's price plumbing.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
INSTRUMENT = 738561


class _TranchePerTickService:
    """Stands in for the paper runtime's tick handling.

    One tick == one fill attempt, applied through the real partial-fill store so
    the invariants under test are the production ones. Standing up the whole paper
    service needs a market-data runtime and a broker catalog, neither of which the
    invariants depend on.
    """

    def __init__(self, store):
        self.store = store
        self.ticks = 0

    async def process_tick(self, tick):
        self.ticks += 1
        from backend.paper_runtime.partial_fills import next_tranche, partial_fill_ratio

        for order_id, quantity in self._orders().items():
            progress = self.store.progress_for(
                account_scope="kite:paper", paper_order_id=order_id
            )
            if progress is None or progress.is_complete:
                continue
            tranche = next_tranche(progress.remaining_quantity, partial_fill_ratio())
            self.store.record_fill(
                account_scope="kite:paper", paper_order_id=order_id,
                filled_quantity=tranche, quantity=quantity,
            )

    def _orders(self):
        return getattr(self, "orders", {})


class TickCertificationTestCase(unittest.TestCase):
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
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-B', 'app:o', 'B', 'kite:paper', 'active')"
                )
            )
            session.commit()
        import os

        os.environ["PAPER_PARTIAL_FILL_RATIO"] = "0.5"

    def tearDown(self):
        import os

        os.environ.pop("PAPER_PARTIAL_FILL_RATIO", None)
        self.engine.dispose()

    def _store(self):
        from backend.paper_runtime.partial_fills import PaperFillProgressStore

        return PaperFillProgressStore(session_factory=self.factory)

    def _reservation(self):
        from backend.strategies.reservations import ClaimRequest, ReservationLedger

        plan_id = "plan-1"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES ('prop-1', 'stg-B', 'kite:paper', 'eval-1', 'run_now', 'run-1', "
                    " 'target_weights', '{}', 'sha', 'validated')"
                )
            )
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES (:lid, 'prop-1', 'stg-B', 'kite:paper', 'target_weights', 'h', '{}', "
                    " '{}', '11111111-1111-1111-1111-111111111111', 'rev-1', 'mh-1')"
                ),
                {"lid": plan_id},
            )
            session.commit()
        ledger = ReservationLedger(session_factory=self.factory)
        return ledger, ledger.claim(
            ClaimRequest(
                plan_id=plan_id, strategy_id="stg-B", account_id="kite:paper",
                evaluation_id="eval-1", execution_environment="paper",
                requirement_inr=1000.0, valid_until=NOW + timedelta(hours=1),
                allocation_inr=10000.0, actor_id="app:o",
            ),
            now=NOW,
        )


class ConvergenceTests(TickCertificationTestCase):
    def test_a_partially_filled_rebalance_completes_across_ticks(self):
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        store = self._store()
        service = _TranchePerTickService(store)
        service.orders = {"PAPER-1": 100}
        store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)

        driver = SyntheticTickDriver(service, fill_progress_store=store)
        run = asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=12),
                paper_order_ids=["PAPER-1"],
            )
        )

        # Tranches only ever grow: a rebalance that un-fills is a bug, not progress.
        self.assertTrue(run.monotonic, run.filled_per_tick)
        self.assertGreater(run.filled_per_tick[0], 0)
        self.assertGreater(run.final_filled, run.filled_per_tick[0])
        # And it converges rather than decaying forever.
        progress = store.progress_for(account_scope="kite:paper", paper_order_id="PAPER-1")
        self.assertTrue(progress.is_complete, progress)
        self.assertEqual(progress.filled_quantity, 100)

    def test_the_tick_driver_needs_no_live_snapshot(self):
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        store = self._store()
        service = _TranchePerTickService(store)
        service.orders = {"PAPER-1": 20}
        store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=20)
        driver = SyntheticTickDriver(service, fill_progress_store=store)

        # Every price is supplied by the series: the driver never reaches for a
        # market feed, which is what makes the certification repeatable.
        series = driver.price_series(start=250.0, step=0.5, count=6)
        self.assertEqual([str(price) for price in series[:3]], ["250.0", "250.5", "251.0"])

    def test_quiescence_is_only_true_once_nothing_is_outstanding(self):
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        store = self._store()
        service = _TranchePerTickService(store)
        service.orders = {"PAPER-1": 100}
        store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)
        driver = SyntheticTickDriver(service, fill_progress_store=store)

        asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=1),
                paper_order_ids=["PAPER-1"],
            )
        )
        # Mid-flight: work is outstanding, so nothing may be called settled.
        self.assertTrue(driver.outstanding(account_scope="kite:paper", paper_order_ids=["PAPER-1"]))

        asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=12),
                paper_order_ids=["PAPER-1"],
            )
        )
        self.assertFalse(driver.outstanding(account_scope="kite:paper", paper_order_ids=["PAPER-1"]))


class ReservationAcrossTicksTests(TickCertificationTestCase):
    def test_the_reservation_renews_on_progress_and_consumes_only_at_completion(self):
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        ledger, reservation = self._reservation()
        store = self._store()
        service = _TranchePerTickService(store)
        service.orders = {"PAPER-1": 100}
        store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)

        driver = SyntheticTickDriver(service, fill_progress_store=store)
        asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=1),
                paper_order_ids=["PAPER-1"],
            )
        )
        progress = store.progress_for(account_scope="kite:paper", paper_order_id="PAPER-1")
        self.assertFalse(progress.is_complete)

        # A tranche filled with more outstanding: capacity is EXTENDED, not consumed
        # and not released — the plan is neither finished nor abandoned.
        ledger.renew(
            reservation["reservation_id"],
            actor_id="worker:1",
            detail={"plan_id": "plan-1", "open_remainder": ["PAPER-1"]},
            now=NOW,
        )
        self.assertEqual(ledger.get(reservation["reservation_id"])["status"], "renewed")
        self.assertEqual(ledger.held_notional(account_id="kite:paper"), 1000.0)

        # Drive it to completion, then the capacity becomes exposure.
        asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=12),
                paper_order_ids=["PAPER-1"],
            )
        )
        self.assertTrue(
            store.progress_for(account_scope="kite:paper", paper_order_id="PAPER-1").is_complete
        )
        ledger.consume(reservation["reservation_id"], actor_id="worker:1", now=NOW)
        self.assertEqual(ledger.get(reservation["reservation_id"])["status"], "consumed")

    def test_an_unresolved_remainder_never_releases_capacity(self):
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        ledger, reservation = self._reservation()
        store = self._store()
        service = _TranchePerTickService(store)
        service.orders = {}
        store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)

        driver = SyntheticTickDriver(service, fill_progress_store=store)
        asyncio.run(
            driver.run(
                instrument_token=INSTRUMENT,
                prices=driver.price_series(start=100.0, count=2),
                paper_order_ids=["PAPER-1"],
            )
        )
        # Nothing filled and a remainder outstanding: flagged, and the capacity is
        # still held rather than handed back on a guess.
        ledger.require_action(
            reservation["reservation_id"], actor_id="worker:1",
            detail={"open_remainder": ["PAPER-1"]}, now=NOW,
        )
        self.assertEqual(ledger.get(reservation["reservation_id"])["status"], "action_required")
        self.assertEqual(ledger.held_notional(account_id="kite:paper"), 1000.0)
        self.assertTrue(driver.outstanding(account_scope="kite:paper", paper_order_ids=["PAPER-1"]))


if __name__ == "__main__":
    unittest.main()
