"""Reservation ledger: atomic capacity claims and the lifecycle (G10).

The lifecycle rules are R3 §8 verbatim, and two of them are the reason this
module exists at all: an unstarted reservation expires with its authority, and
capital backing an actual open position is **never** released because its
evaluation expired. The tests below pin the whole state machine plus the
refusals that stop an owner reclaiming capacity from active execution.

SQLite runs with the established ``public.`` ATTACH fixture; the advisory lock
that makes the claim atomic on PostgreSQL is a dialect-guarded no-op here, and
the real race is proved in the PostgreSQL suite.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

G1 = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class ReservationTestCase(unittest.TestCase):
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

        from backend.strategies.attribution_models import (
            AccountReconciliationVersion,
            Strategy,
            StrategyAdmissionPolicy,
            StrategyApproval,
            StrategyPlan,
            StrategyProposal,
            StrategyReservation,
            StrategyReservationEvent,
        )

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyAdmissionPolicy.__table__,
                StrategyReservation.__table__,
                StrategyReservationEvent.__table__,
                StrategyApproval.__table__,
                AccountReconciliationVersion.__table__,
            ],
        )
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()
        self.ledger = self._ledger()

    def tearDown(self):
        self.engine.dispose()

    def _ledger(self):
        from backend.strategies.reservations import ReservationLedger

        return ReservationLedger(session_factory=self.factory)

    def seed_plan(self, plan_id):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, 'stg-A', 'kite:A', :pid, 'run_now', 'run-1', "
                    " 'single_instrument', '{}', 'sha', 'validated')"
                ),
                {"pid": f"prop-{plan_id}"},
            )
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:pid, :prop, 'stg-A', 'kite:A', 'single_instrument', 'h', '{}', '{}', :gen)"
                ),
                {"pid": plan_id, "prop": f"prop-{plan_id}", "gen": G1},
            )
            session.commit()

    def claim(self, plan_id="plan-1", *, requirement=1000.0, allocation=10000.0, valid_for=3600,
              actor="app:o", now=NOW):
        from backend.strategies.reservations import ClaimRequest

        self.seed_plan(plan_id)
        return self.ledger.claim(
            ClaimRequest(
                plan_id=plan_id,
                strategy_id="stg-A",
                account_id="kite:A",
                evaluation_id=f"eval-{plan_id}",
                execution_environment="live",
                requirement_inr=requirement,
                valid_until=now + timedelta(seconds=valid_for),
                allocation_inr=allocation,
                actor_id=actor,
            ),
            now=now,
        )

    def events(self, reservation_id):
        return [row["event"] for row in self.ledger.events(reservation_id)]


class CapacityClaimTests(ReservationTestCase):
    def test_claim_records_capacity_and_an_event(self):
        reservation = self.claim(requirement=2500.0)
        self.assertEqual(reservation["status"], "active")
        self.assertEqual(reservation["reserved_notional_inr"], 2500.0)
        self.assertEqual(self.events(reservation["reservation_id"]), ["created"])
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 2500.0)

    def test_loser_is_refused_with_capacity_exceeded(self):
        from backend.strategies.reservations import CapacityExceeded

        self.claim(plan_id="plan-a", requirement=8000.0, allocation=10000.0)
        with self.assertRaises(CapacityExceeded) as ctx:
            self.claim(plan_id="plan-b", requirement=4000.0, allocation=10000.0)
        self.assertEqual(ctx.exception.reason_code, "CAPACITY_EXCEEDED")
        # The loser left nothing behind: no reservation, no event.
        self.assertIsNone(self.ledger.for_plan("plan-b"))
        self.assertEqual(
            self.ledger.held_notional(account_id="kite:A"), 8000.0
        )

    def test_capacity_is_exactly_the_allocation_boundary(self):
        self.claim(plan_id="plan-a", requirement=6000.0, allocation=10000.0)
        # Exactly filling the allocation is admitted; one rupee more is not.
        self.assertTrue(self.claim(plan_id="plan-b", requirement=4000.0, allocation=10000.0))
        from backend.strategies.reservations import CapacityExceeded

        with self.assertRaises(CapacityExceeded):
            self.claim(plan_id="plan-c", requirement=1.0, allocation=10000.0)

    def test_released_capacity_becomes_available_again(self):
        first = self.claim(plan_id="plan-a", requirement=9000.0, allocation=10000.0)
        from backend.strategies.reservations import CapacityExceeded

        with self.assertRaises(CapacityExceeded):
            self.claim(plan_id="plan-b", requirement=5000.0, allocation=10000.0)
        self.ledger.release(first["reservation_id"], reason="terminal_unfilled", actor_id="app:o")
        self.assertTrue(self.claim(plan_id="plan-b", requirement=5000.0, allocation=10000.0))

    def test_claim_is_idempotent_per_plan(self):
        first = self.claim(plan_id="plan-a", requirement=1000.0)
        second = self.ledger.claim(
            __import__("backend.strategies.reservations", fromlist=["ClaimRequest"]).ClaimRequest(
                plan_id="plan-a", strategy_id="stg-A", account_id="kite:A",
                evaluation_id="eval-again", execution_environment="live",
                requirement_inr=5000.0, valid_until=NOW + timedelta(hours=1),
                allocation_inr=10000.0, actor_id="app:o",
            ),
            now=NOW,
        )
        # One plan claims capacity once, ever — the retry returns the original.
        self.assertEqual(second["reservation_id"], first["reservation_id"])
        self.assertEqual(second["reserved_notional_inr"], 1000.0)
        self.assertEqual(self.events(first["reservation_id"]), ["created"])


class LifecycleTests(ReservationTestCase):
    def test_unstarted_expires_with_validity_and_never_before(self):
        from backend.strategies.reservations import ReservationStateError

        reservation = self.claim(valid_for=3600)
        with self.assertRaises(ReservationStateError):
            self.ledger.expire(reservation["reservation_id"], now=NOW + timedelta(seconds=60))
        expired = self.ledger.expire(reservation["reservation_id"], now=NOW + timedelta(hours=2))
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(self.events(reservation["reservation_id"]), ["created", "expired"])
        # Expiry frees the capacity, because nothing was ever executed.
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 0.0)

    def test_renewal_extends_validity_and_records_the_event(self):
        reservation = self.claim(valid_for=600)
        renewed = self.ledger.renew(
            reservation["reservation_id"], actor_id="worker:1", extend_seconds=900, now=NOW
        )
        # Renewal extends the EXISTING window rather than restarting it, so it can
        # never shorten validity: 600s remaining, then 900s more from that point.
        self.assertEqual(
            renewed["valid_until"], (NOW + timedelta(seconds=600 + 900)).isoformat()
        )
        self.assertEqual(self.events(reservation["reservation_id"]), ["created", "renewed"])

    def test_renewal_after_expiry_is_refused(self):
        from backend.strategies.reservations import ReservationStateError

        reservation = self.claim(valid_for=60)
        self.ledger.expire(reservation["reservation_id"], now=NOW + timedelta(hours=1))
        with self.assertRaises(ReservationStateError):
            self.ledger.renew(reservation["reservation_id"], now=NOW + timedelta(hours=1))

    def test_consume_is_terminal_and_never_expires(self):
        """Capital backing an open position survives its evaluation's expiry."""
        from backend.strategies.reservations import ReservationStateError

        reservation = self.claim(valid_for=60)
        consumed = self.ledger.consume(reservation["reservation_id"], actor_id="worker:1", now=NOW)
        self.assertEqual(consumed["status"], "consumed")

        # Long after the evaluation's authority ended, the capacity is still held.
        with self.assertRaises(ReservationStateError):
            self.ledger.expire(reservation["reservation_id"], now=NOW + timedelta(days=30))
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 1000.0)
        self.assertEqual(self.events(reservation["reservation_id"]), ["created", "consumed"])

    def test_terminal_unfilled_release_frees_capacity(self):
        reservation = self.claim()
        released = self.ledger.release(
            reservation["reservation_id"], reason="plan_step_unfilled", actor_id="app:o"
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["release_reason"], "plan_step_unfilled")
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 0.0)
        self.assertEqual(self.events(reservation["reservation_id"]), ["created", "released"])

    def test_no_api_can_force_release_capacity_backing_active_execution(self):
        from backend.strategies.reservations import ReleaseForbidden

        reservation = self.claim()
        # Verified progress happened: the plan is no longer unstarted.
        self.ledger.advance(reservation["reservation_id"], actor_id="worker:1", now=NOW)
        with self.assertRaises(ReleaseForbidden) as ctx:
            self.ledger.release(reservation["reservation_id"], actor_id="app:owner")
        self.assertEqual(ctx.exception.reason_code, "RELEASE_FORBIDDEN")
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 1000.0)

        # And a consumed reservation is equally unreleasable, by anyone.
        other = self.claim(plan_id="plan-2")
        self.ledger.consume(other["reservation_id"], now=NOW)
        with self.assertRaises(ReleaseForbidden):
            self.ledger.release(other["reservation_id"], actor_id="app:owner")

    def test_action_required_disposition_is_refused_as_unproven(self):
        from backend.strategies.reservations import DispositionUnproven

        reservation = self.claim()
        flagged = self.ledger.require_action(reservation["reservation_id"], actor_id="worker:1", now=NOW)
        self.assertEqual(flagged["status"], "action_required")
        # Capacity stays held while flagged — an unresolved plan does not silently
        # release it.
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 1000.0)

        with self.assertRaises(DispositionUnproven) as ctx:
            self.ledger.confirm_disposition(reservation["reservation_id"], actor_id="app:owner")
        self.assertEqual(ctx.exception.reason_code, "DISPOSITION_UNPROVEN")
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 1000.0)

    def test_unstarted_cancellation_releases_atomically(self):
        reservation = self.claim()
        released = self.ledger.release(
            reservation["reservation_id"], reason="cancelled_unstarted", actor_id="app:o"
        )
        self.assertEqual(released["status"], "released")
        # The status change and its event landed together.
        self.assertEqual(self.events(reservation["reservation_id"]), ["created", "released"])
        self.assertEqual(self.ledger.held_notional(account_id="kite:A"), 0.0)

    def test_event_log_covers_every_transition(self):
        reservation = self.claim()
        rid = reservation["reservation_id"]
        self.ledger.renew(rid, actor_id="worker:1", now=NOW)
        self.ledger.advance(rid, actor_id="worker:1", now=NOW)
        self.ledger.consume(rid, actor_id="worker:1", now=NOW)
        self.assertEqual(
            self.events(rid), ["created", "renewed", "advanced", "consumed"]
        )


class LedgerReadTests(ReservationTestCase):
    def test_unknown_reservation_is_not_found(self):
        from backend.strategies.reservations import ReservationNotFound

        self.assertIsNone(self.ledger.get("nope"))
        with self.assertRaises(ReservationNotFound):
            self.ledger.renew("nope", now=NOW)

    def test_listing_is_strategy_scoped(self):
        self.claim(plan_id="plan-a")
        self.claim(plan_id="plan-b")
        rows = self.ledger.list_for_strategy(strategy_id="stg-A")
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.ledger.list_for_strategy(strategy_id="stg-OTHER"), [])


if __name__ == "__main__":
    unittest.main()
