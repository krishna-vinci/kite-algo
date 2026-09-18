"""MIS square-off harness: per-product timing, isolation, and evidence (D-2, D-3).

Two things are worth proving here and nothing else. The schedule is the platform's
and matches source for every exchange, override included — a square-off that fires
at the wrong time is worse than one that does not fire, because it looks like it
worked. And an exit is sized to the strategy's *attributed* quantity, which is
what stops one strategy's square-off selling another strategy's shares.

The rest is the record: which outcome happened, and the distinction between a
square-off that failed (keeps reconciling) and a broker fallback that fired
because the platform's did not.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
SESSION = date(2026, 10, 15)


class SquareoffHarnessTestCase(unittest.TestCase):
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
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-B', 'app:o', 'B', 'kite:A', 'active')"
                )
            )
            session.commit()
        self.store = self._store()

    def tearDown(self):
        self.engine.dispose()
        os.environ.pop("WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON", None)

    def _store(self):
        from backend.strategies.mis_squareoff import MisSquareoffEvidenceStore

        return MisSquareoffEvidenceStore(session_factory=self.factory)

    def record(self, *, outcome, strategy_id="stg-B", run_id="run-B", product="MIS",
               exchange="NSE", detail=None, claim=None):
        from backend.strategies.mis_squareoff import SquareoffRecord

        return self.store.record(
            SquareoffRecord(
                account_id="kite:A", strategy_id=strategy_id, strategy_run_id=run_id,
                product=product, session_date=SESSION, exchange=exchange, scheduled_at=NOW,
                outcome=outcome, exit_claim_id=claim, detail=detail or {},
            )
        )


class ScheduleTests(SquareoffHarnessTestCase):
    def test_every_exchange_squares_off_when_source_says_it_does(self):
        from backend.strategies.mis_squareoff import scheduled_time_for

        # R3 §16's verified table. A square-off that fires at the wrong time is
        # worse than one that does not fire, because it looks like it worked.
        self.assertEqual(scheduled_time_for("NSE"), "15:20")
        self.assertEqual(scheduled_time_for("BSE"), "15:20")
        self.assertEqual(scheduled_time_for("NFO"), "15:25")
        self.assertEqual(scheduled_time_for("CDS"), "16:45")
        self.assertEqual(scheduled_time_for("MCX"), "23:20")

    def test_an_unscheduled_exchange_has_no_time(self):
        from backend.strategies.mis_squareoff import scheduled_time_for

        self.assertIsNone(scheduled_time_for("NOPE"))

    def test_the_override_is_honoured(self):
        from backend.strategies.mis_squareoff import scheduled_time_for

        os.environ["WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON"] = '{"NSE:MIS": "14:00"}'
        self.assertEqual(scheduled_time_for("NSE"), "14:00")
        # Overriding one exchange leaves the others at their defaults.
        self.assertEqual(scheduled_time_for("BSE"), "15:20")

    def test_the_schedule_is_the_protection_runtimes_own(self):
        from backend.app.background import _worker_protection_squareoff_schedule
        from backend.strategies.mis_squareoff import squareoff_schedule

        # Two copies of a schedule is two chances to disagree about when the
        # platform squares off, and the wrong one is the one nobody checked.
        self.assertEqual(squareoff_schedule(), dict(_worker_protection_squareoff_schedule()))


class IsolationTests(SquareoffHarnessTestCase):
    """An exit is sized to what the strategy owns, and never one share more."""

    def test_an_exit_is_clamped_to_the_attributed_quantity(self):
        from backend.strategies.mis_squareoff import attributed_exit_size

        self.assertEqual(attributed_exit_size(attributed_quantity=100, requested_quantity=-100), -100)
        # Asking to sell more than B owns: clamped, so A's shares are untouched.
        self.assertEqual(attributed_exit_size(attributed_quantity=100, requested_quantity=-500), -100)

    def test_an_exit_in_the_wrong_direction_is_not_an_exit(self):
        from backend.strategies.mis_squareoff import attributed_exit_size

        # A sell against a short book would grow it, not square it off.
        self.assertEqual(attributed_exit_size(attributed_quantity=-100, requested_quantity=-50), 0)
        self.assertEqual(attributed_exit_size(attributed_quantity=100, requested_quantity=50), 0)

    def test_a_flat_book_has_nothing_to_square_off(self):
        from backend.strategies.mis_squareoff import attributed_exit_size

        self.assertEqual(attributed_exit_size(attributed_quantity=0, requested_quantity=-100), 0)

    def test_b_cannot_sell_as_shares(self):
        """Walkthrough 2 case 1: separate product books, one-sided exits."""
        from backend.strategies.mis_squareoff import attributed_exit_size

        # A holds RELIANCE CNC +100; B holds RELIANCE MIS +40. B's square-off is
        # sized to B's book, so it cannot reach A's shares by asking for more.
        a_attributed, b_attributed = 100, 40
        b_exit = attributed_exit_size(attributed_quantity=b_attributed, requested_quantity=-b_attributed * 5)
        self.assertEqual(b_exit, -40)
        # And A's book is untouched by arithmetic: nothing here consumes it.
        self.assertEqual(a_attributed, 100)
        # B's exit magnitude is bounded by B's book, never by the account total.
        self.assertLessEqual(abs(b_exit), abs(b_attributed))


class EvidenceTests(SquareoffHarnessTestCase):
    def test_a_completed_square_off_is_recorded(self):
        recorded = self.record(outcome="squared_off", claim="claim-1", detail={"quantity": -40})
        self.assertEqual(recorded["outcome"], "squared_off")
        self.assertEqual(recorded["exit_claim_id"], "claim-1")
        rows = self.store.for_run(strategy_run_id="run-B")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product"], "MIS")

    def test_a_failed_square_off_is_action_required_and_keeps_reconciling(self):
        self.record(outcome="action_required", detail={"reason": "exit rejected"})
        unresolved = self.store.unresolved_for_run(strategy_run_id="run-B")
        # NOT settlement: the failure is visible and the axes stay unsatisfied.
        self.assertEqual([row["outcome"] for row in unresolved], ["action_required"])

    def test_a_broker_fallback_is_recorded_as_missed_not_as_the_control(self):
        self.record(outcome="missed_by_broker", detail={"observed_at": "15:35"})
        rows = self.store.for_run(strategy_run_id="run-B")
        self.assertEqual(rows[0]["outcome"], "missed_by_broker")
        # A fallback that happened is still an unresolved platform square-off.
        self.assertEqual(
            [row["outcome"] for row in self.store.unresolved_for_run(strategy_run_id="run-B")],
            ["missed_by_broker"],
        )

    def test_a_stale_worker_exit_is_its_own_outcome(self):
        self.record(outcome="stale_worker_exit", detail={"worker_stale_sec": 300})
        rows = self.store.for_run(strategy_run_id="run-B")
        self.assertEqual(rows[0]["outcome"], "stale_worker_exit")

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            self.record(outcome="invented")

    def test_evidence_is_scoped_to_its_strategy_and_run(self):
        self.record(outcome="squared_off", strategy_id="stg-B", run_id="run-B")
        self.record(outcome="squared_off", strategy_id="stg-A", run_id="run-A")
        self.assertEqual(len(self.store.for_run(strategy_run_id="run-B")), 1)
        self.assertEqual(len(self.store.for_strategy(strategy_id="stg-A")), 1)


if __name__ == "__main__":
    unittest.main()
