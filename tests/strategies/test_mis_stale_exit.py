"""MIS stale-worker exit: attach the policy, exit only what is owned, record it (D-3).

A stale MIS worker leaves an *intraday* position behind, and the schedule fires at
15:20 while the worker may have died at 09:45. The tests below pin the three things
that keep the attachment honest: it acts only when the policy asks and the worker is
genuinely stale, it exits only MIS legs and only up to what the strategy owns, and
it submits through the durable claim path rather than issuing an order directly.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)


class StaleExitTestCase(unittest.TestCase):
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
                    "VALUES ('stg-B', 'app:o', 'B', 'kite:A', 'active')"
                )
            )
            session.commit()
        self.claims: list = []

    def tearDown(self):
        self.engine.dispose()

    def policy(self, *, enabled=True, stale_seconds=300):
        return {
            "enabled": True,
            "operations": {"exit_on_worker_stale": enabled, "worker_stale_sec": stale_seconds},
        }

    def run(self, **overrides):
        values = {
            "strategy_run_id": "run-B",
            "account_scope": "kite:A",
            "execution_mode": "paper",
            "last_heartbeat_at": (NOW - timedelta(minutes=30)).isoformat(),
            "runtime_state": {"backend_protection": self.policy()},
        }
        values.update(overrides)
        return values

    def legs(self, *specs):
        """``specs`` are ``(symbol, product, quantity)`` triples."""
        return [
            {
                "strategy_id": "stg-B",
                "tradingsymbol": symbol,
                "product": product,
                "exchange": "NSE",
                "attributed_quantity": quantity,
                "net_quantity": quantity,
            }
            for symbol, product, quantity in specs
        ]

    def policy_runner(self, *, claim_id="claim-1"):
        from backend.strategies.mis_stale_exit import MisStaleExitPolicy

        async def submitter(run, leg, quantity):
            self.claims.append((run.get("strategy_run_id"), leg.get("tradingsymbol"), quantity))
            return claim_id

        return MisStaleExitPolicy(
            session_factory=self.factory, claim_submitter=submitter, clock=lambda: NOW
        )


class PolicyGateTests(StaleExitTestCase):
    def test_a_fresh_worker_is_left_alone(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(last_heartbeat_at=NOW.isoformat()),
                positions=self.legs(("RELIANCE", "MIS", 40)),
                now=NOW,
            )
        )
        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.reason, "worker_not_stale")
        self.assertEqual(self.claims, [])

    def test_a_run_without_the_policy_is_untouched(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(runtime_state={"backend_protection": self.policy(enabled=False)}),
                positions=self.legs(("RELIANCE", "MIS", 40)),
                now=NOW,
            )
        )
        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.reason, "policy_disabled")

    def test_a_worker_that_never_heartbeated_is_not_liquidated(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(last_heartbeat_at=None),
                positions=self.legs(("RELIANCE", "MIS", 40)),
                now=NOW,
            )
        )
        # Unknown heartbeat is not evidence of staleness, and guessing would
        # liquidate somebody's position on a missing field.
        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.reason, "worker_not_stale")


class ExitScopingTests(StaleExitTestCase):
    def test_a_stale_mis_run_is_exited_with_evidence_and_a_claim(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(), positions=self.legs(("RELIANCE", "MIS", 40)), now=NOW
            )
        )
        self.assertTrue(outcome.acted)
        self.assertEqual(outcome.reason, "stale_worker_exit")
        # The exit closes the long book, so it sells, and it goes through the claim
        # path rather than being issued directly.
        self.assertEqual(self.claims, [("run-B", "RELIANCE", -40)])
        self.assertEqual(outcome.evidence[0]["outcome"], "stale_worker_exit")
        self.assertEqual(outcome.evidence[0]["exit_claim_id"], "claim-1")

    def test_the_exit_is_never_oversized(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(), positions=self.legs(("RELIANCE", "MIS", 40)), now=NOW
            )
        )
        quantity = outcome.evidence[0]["detail"]["exit_quantity"]
        self.assertEqual(abs(quantity), 40)
        self.assertLessEqual(abs(quantity), 40)

    def test_only_mis_legs_are_exited(self):
        """A stale worker's CNC book is not at risk and is not touched."""
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(),
                positions=self.legs(("RELIANCE", "MIS", 40), ("INFY", "CNC", 100)),
                now=NOW,
            )
        )
        self.assertEqual([row["tradingsymbol"] for row in outcome.exited], ["RELIANCE"])
        self.assertEqual(self.claims, [("run-B", "RELIANCE", -40)])

    def test_a_short_mis_book_is_bought_back(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(), positions=self.legs(("RELIANCE", "MIS", -40)), now=NOW
            )
        )
        # Exiting a short buys it back; the magnitude is still bounded by the book.
        self.assertEqual(self.claims, [("run-B", "RELIANCE", 40)])

    def test_no_mis_exposure_means_nothing_to_do(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(), positions=self.legs(("INFY", "CNC", 100)), now=NOW
            )
        )
        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.reason, "no_mis_exposure")
        self.assertEqual(self.claims, [])

    def test_a_flat_mis_leg_is_skipped(self):
        policy = self.policy_runner()
        outcome = asyncio.run(
            policy.apply(
                self.run(), positions=self.legs(("RELIANCE", "MIS", 0)), now=NOW
            )
        )
        self.assertFalse(outcome.acted)
        self.assertEqual(self.claims, [])

    def test_evidence_lands_in_the_store_per_run(self):
        policy = self.policy_runner()
        asyncio.run(policy.apply(self.run(), positions=self.legs(("RELIANCE", "MIS", 40)), now=NOW))
        rows = policy.evidence.for_run(strategy_run_id="run-B")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "stale_worker_exit")
        self.assertEqual(rows[0]["product"], "MIS")
        self.assertEqual(rows[0]["detail"]["attributed_quantity"], 40)
        # A stale exit is a completed exit, not an unresolved one.
        self.assertEqual(policy.evidence.unresolved_for_run(strategy_run_id="run-B"), [])


if __name__ == "__main__":
    unittest.main()
