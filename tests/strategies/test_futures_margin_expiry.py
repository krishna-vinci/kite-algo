"""Futures margin and expiry: the peak prechecked, the deadline escalated (D-3, D-4).

Two things are refused rather than discovered. A roll's margin PEAK is the old
contract held while the replacement is acquired — not either leg alone — and a
shortfall there does not fail cleanly at the broker, it fails as a half-executed
roll. And an unrolled expiring contract approaching its last session escalates to
the owner rather than being closed by improvisation, because the platform does not
get to decide what a position was for.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)
OLD = "a0000000-0000-0000-0000-00000000000a"
NEW = "b0000000-0000-0000-0000-00000000000b"


def fut_leg(*, price=25000.0, quantity=75, side="BUY", instrument_id=NEW, expiry="2026-10-29"):
    return {
        "instrument_id": instrument_id,
        "instrument_type": "FUT",
        "product": "NRML",
        "quantity": quantity,
        "signed_quantity": quantity if side == "BUY" else -quantity,
        "side": side,
        "reference_price": price,
        "expiry": expiry,
    }


class PeakMarginTests(unittest.TestCase):
    def test_a_single_leg_peak_is_its_own_margin(self):
        from backend.strategies.futures_margin import peak_margin_evidence

        peak = peak_margin_evidence(new_legs=[fut_leg()])
        self.assertGreater(peak["peak_margin_inr"], 0)
        self.assertEqual(peak["old_legs_margin_inr"], 0.0)
        self.assertFalse(peak["concurrent"])

    def test_a_roll_peak_is_both_legs_concurrently(self):
        """The peak is the pair, not the larger of them — that is the whole point."""
        from backend.strategies.futures_margin import peak_margin_evidence

        new_only = peak_margin_evidence(new_legs=[fut_leg()])["peak_margin_inr"]
        both = peak_margin_evidence(
            new_legs=[fut_leg()], old_legs=[fut_leg(instrument_id=OLD, side="SELL")]
        )
        self.assertTrue(both["concurrent"])
        self.assertAlmostEqual(both["peak_margin_inr"], new_only * 2, places=2)

    def test_a_peak_that_fits_is_not_refused(self):
        from backend.strategies.futures_margin import (
            futures_peak_refusal,
            peak_margin_evidence,
        )

        peak = peak_margin_evidence(new_legs=[fut_leg()])
        self.assertIsNone(futures_peak_refusal(peak=peak, available_inr=10_000_000.0))

    def test_a_peak_that_does_not_fit_refuses_with_its_arithmetic(self):
        from backend.strategies.futures_margin import (
            futures_peak_refusal,
            peak_margin_evidence,
        )

        peak = peak_margin_evidence(
            new_legs=[fut_leg()], old_legs=[fut_leg(instrument_id=OLD, side="SELL")]
        )
        refusal = futures_peak_refusal(peak=peak, available_inr=1.0)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["rejection_reason"], "MARGIN_INSUFFICIENT")
        # A margin refusal without the numbers is indistinguishable from a bug.
        self.assertGreater(refusal["required_peak_margin_inr"], refusal["available_inr"])
        self.assertTrue(refusal["concurrent"])
        self.assertGreater(refusal["old_legs_margin_inr"], 0.0)

    def test_an_absent_capacity_does_not_refuse(self):
        """The policy check already refuses a live plan without one."""
        from backend.strategies.futures_margin import (
            futures_peak_refusal,
            peak_margin_evidence,
        )

        peak = peak_margin_evidence(new_legs=[fut_leg()])
        self.assertIsNone(futures_peak_refusal(peak=peak, available_inr=None))

    def test_the_paper_margin_engine_provides_the_evidence(self):
        from backend.paper_runtime.margin_engine import PaperMarginEngine
        from backend.strategies.futures_margin import futures_leg_margin

        expected = PaperMarginEngine().required_margin(
            side="BUY", product="NRML", quantity=75,
            reference_price=Decimal("25000"), instrument_type="FUT",
        )
        self.assertEqual(futures_leg_margin(fut_leg()), Decimal(expected))


class AdmissionPeakTests(unittest.TestCase):
    """The peak is an admission axis, and a preview still holds nothing."""

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
                "CREATE TABLE public.instrument_catalog_generations "
                "(id TEXT PRIMARY KEY, status TEXT, published_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE public.instrument_catalog_records "
                "(instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT, "
                " lifecycle_status TEXT NOT NULL DEFAULT 'active', current_generation_id TEXT, "
                " instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL, "
                " underlying TEXT)"
            )
            cursor.execute(
                "CREATE TABLE public.instrument_broker_mappings "
                "(mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT, "
                " broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER, "
                " valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER)"
            )
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        # The policy's composite FK needs the canonical strategy to exist.
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def _service(self):
        from backend.strategies.admission import AdmissionService

        return AdmissionService(session_factory=self.factory)

    def plan(self, *, old_legs=None, allocation=None):
        resolved = {"legs": [fut_leg()]}
        if old_legs:
            resolved["old_legs"] = old_legs
        return {
            "plan_id": "plan-1", "strategy_id": "stg-A", "account_id": "kite:A",
            "plan_hash": "h" * 64,
            "resolved_plan": resolved,
        }

    def test_a_futures_plan_over_its_capacity_refuses_the_peak(self):
        from backend.strategies.admission import AdmissionService
        from backend.strategies.futures_margin import peak_margin_evidence

        service = self._service()
        service.upsert_policy(
            strategy_id="stg-A", account_id="kite:A", updated_by="app:o",
            allocation_inr=100_000_000.0,
        )
        # A MARGIN capacity below the concurrent peak, which is the axis under test.
        single = peak_margin_evidence(new_legs=[fut_leg()])["peak_margin_inr"]
        verdict = service.evaluate(
            self.plan(old_legs=[fut_leg(instrument_id=OLD, side="SELL")]),
            execution_environment="paper",
            peak_capacity_inr=single * 1.5,
        )
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal_reason, "MARGIN_INSUFFICIENT")
        self.assertTrue(verdict.detail["concurrent"])
        _ = AdmissionService

    def test_a_futures_plan_within_its_capacity_admits(self):
        service = self._service()
        service.upsert_policy(
            strategy_id="stg-A", account_id="kite:A", updated_by="app:o",
            allocation_inr=100_000_000.0,
        )
        verdict = service.evaluate(self.plan(), execution_environment="paper")
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertIn("peak_margin", verdict.detail)


class ExpiryEscalationTests(unittest.TestCase):
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
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()
        self.notified: list = []
        from backend.strategies.rolls import RollStateMachine

        self.machine = RollStateMachine(
            session_factory=self.factory,
            notifier=lambda account_id, roll: self.notified.append(account_id) or True,
        )
        self.policy = self._policy

    def _policy(self):
        from backend.strategies.expiry_policy import expiry_warning_days

        return expiry_warning_days()

    def _roll(self, expiry):
        return self.machine.create(
            strategy_id="stg-A", account_id="kite:A",
            old_instrument_id=OLD, new_instrument_id=NEW,
            required_replacement_quantity=75,
            old_coordinate={"product": "NRML", "expiry": expiry},
            new_coordinate={"product": "NRML", "expiry": expiry, "side": "BUY"},
        )

    def test_the_warning_window_defaults_to_five_days(self):
        import os

        from backend.strategies.expiry_policy import expiry_warning_days

        os.environ.pop("FUTURES_EXPIRY_WARNING_DAYS", None)
        self.assertEqual(expiry_warning_days(), 5)
        os.environ["FUTURES_EXPIRY_WARNING_DAYS"] = "9"
        try:
            self.assertEqual(expiry_warning_days(), 9)
        finally:
            os.environ.pop("FUTURES_EXPIRY_WARNING_DAYS", None)

    def test_an_unrolled_contract_inside_the_window_escalates_once(self):
        from backend.strategies.expiry_policy import check_expiry_cutoff

        roll = self._roll((NOW + timedelta(days=3)).date().isoformat())
        self.machine.acquire(roll["roll_id"])
        result = check_expiry_cutoff(
            self.machine, roll["roll_id"], now=NOW, notify=True, notifier=None
        )
        self.assertTrue(result["escalated"])
        self.assertEqual(result["warning_days"], 5)
        self.assertEqual(result["days_to_expiry"], 3)
        # action_required, and the notification is the owner's, once.
        self.assertEqual(self.machine.get(roll["roll_id"])["state"], "action_required")
        self.assertEqual(self.notified, ["kite:A"])

    def test_a_contract_outside_the_window_is_not_escalated(self):
        from backend.strategies.expiry_policy import check_expiry_cutoff

        roll = self._roll((NOW + timedelta(days=40)).date().isoformat())
        result = check_expiry_cutoff(
            self.machine, roll["roll_id"], now=NOW, notify=True, notifier=None
        )
        self.assertFalse(result["escalated"])
        self.assertEqual(result["days_to_expiry"], 40)
        self.assertEqual(self.notified, [])

    def test_no_improvised_close_is_attempted(self):
        """The escalation reports; it does not close anything."""
        from backend.strategies.expiry_policy import check_expiry_cutoff

        roll = self._roll((NOW + timedelta(days=2)).date().isoformat())
        check_expiry_cutoff(self.machine, roll["roll_id"], now=NOW, notify=True, notifier=None)
        events = [row["event"] for row in self.machine.events(roll["roll_id"])]
        # Stalled and escalated, never close_released: the platform does not decide
        # what the position was for.
        self.assertIn("stalled", events)
        self.assertIn("escalated", events)
        self.assertNotIn("close_released", events)

    def test_an_expired_contract_also_escalates(self):
        from backend.strategies.expiry_policy import check_expiry_cutoff

        roll = self._roll((NOW - timedelta(days=1)).date().isoformat())
        result = check_expiry_cutoff(
            self.machine, roll["roll_id"], now=NOW, notify=True, notifier=None
        )
        self.assertTrue(result["escalated"])
        self.assertLessEqual(result["days_to_expiry"], 0)


if __name__ == "__main__":
    unittest.main()
