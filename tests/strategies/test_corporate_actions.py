"""Corporate-action detection: detect, freeze, escalate — never rebase (G13).

The invariant under test is negative as much as positive: a split-like broker
change must **not** be absorbed. Absorbing it silently rewrites the strategy's
quantity and cost basis with nobody in the loop, and the owner finds out when the
P&L is wrong. So the tests pin that detection happens, that the coordinate freezes
through the existing divergence machinery, that escalation is single, and that the
freeze lifts only when a human names the adjustment that resolved it.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
COORD = {"instrument_token": 738561, "exchange": "NSE", "tradingsymbol": "RELIANCE",
         "product": "CNC"}


class CorporateActionTestCase(unittest.TestCase):
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
            # The broker's own book: a pre-existing platform table, so it lives in
            # the public schema and is read with qualified SQL.
            cursor.execute(
                """
                CREATE TABLE public.account_positions (
                    account_id TEXT NOT NULL, instrument_token BIGINT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT '', tradingsymbol TEXT NOT NULL DEFAULT '',
                    product TEXT NOT NULL, net_quantity BIGINT NOT NULL,
                    PRIMARY KEY (account_id, instrument_token, product)
                )
                """
            )
            dbapi_connection.commit()

        # The reconciliation path reads the whole account-truth family, so the
        # fixture creates the shared metadata rather than enumerating tables.
        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.notified: list = []
        self.detector = self._detector()

    def tearDown(self):
        self.engine.dispose()

    def _detector(self, *, notifier=None):
        from backend.strategies.corporate_actions import CorporateActionDetector

        def default_notifier(account_id, event):
            self.notified.append((account_id, event["action_kind"]))
            return True

        return CorporateActionDetector(
            session_factory=self.factory, notifier=notifier or default_notifier
        )

    def detect(self, **overrides):
        values = {
            "account_id": "kite:A",
            "coordinate": COORD,
            "broker_quantity": 200,
            "attributed_quantity": 100,
            "manual_quantity": 0,
        }
        values.update(overrides)
        return self.detector.detect(**values)


class DetectionTests(CorporateActionTestCase):
    def test_a_split_like_change_is_detected_not_absorbed(self):
        result = self.detect()
        self.assertIsNotNone(result)
        self.assertEqual(result["action_kind"], "suspected_split")
        self.assertEqual(result["reason"], "SUSPECTED_CORPORATE_ACTION")
        self.assertEqual(result["evidence"]["ratio"], 2.0)
        self.assertEqual(result["evidence"]["gap"], 100)

    def test_classification_by_ratio(self):
        from backend.strategies.corporate_actions import classify_ratio

        self.assertEqual(classify_ratio(200, 100), "suspected_split")
        self.assertEqual(classify_ratio(1000, 100), "suspected_split")
        self.assertEqual(classify_ratio(150, 100), "suspected_bonus")
        self.assertEqual(classify_ratio(50, 100), "suspected_merger")
        # A ratio that is not a clean one is not evidence of a corporate action.
        self.assertEqual(classify_ratio(137, 100), None)
        self.assertEqual(classify_ratio(100, 0), None)

    def test_an_unclassifiable_ratio_still_records_but_as_unclassified(self):
        result = self.detect(broker_quantity=137, attributed_quantity=100)
        self.assertEqual(result["action_kind"], "unclassified")
        # It still freezes and escalates: an unexplained quantity change is not
        # made safe by being hard to name.
        self.assertTrue(result["frozen"])

    def test_a_matching_quantity_is_not_a_corporate_action(self):
        self.assertIsNone(self.detect(broker_quantity=100, attributed_quantity=100))

    def test_offsetting_trades_make_it_an_ingest_gap_not_a_corporate_action(self):
        """Unattributed fills explain the gap, so it is ingestion catching up."""
        self.assertIsNone(
            self.detect(
                broker_quantity=200, attributed_quantity=100, offsetting_trade_quantity=100
            )
        )
        self.assertEqual(self.detector.events(), [])

    def test_manual_exposure_counts_toward_the_expectation(self):
        # 100 attributed + 100 manual = 200 expected: no divergence, no detection.
        self.assertIsNone(
            self.detect(broker_quantity=200, attributed_quantity=100, manual_quantity=100)
        )


class FreezeTests(CorporateActionTestCase):
    def test_detection_freezes_the_coordinate_through_phase_2_machinery(self):
        from backend.strategies.account_truth import AccountTruthStore

        result = self.detect()
        self.assertTrue(result["frozen"])
        store = AccountTruthStore(session_factory=self.factory)
        # The class that freezes new exposure while leaving reducing exits open.
        self.assertEqual(
            store.is_frozen_coordinate(
                account_id="kite:A",
                coordinate=(738561, "NSE", "RELIANCE", "CNC"),
            ),
            "unexplained",
        )
        state = store.reconciliation_state(account_id="kite:A")[(738561, "NSE", "RELIANCE", "CNC")]
        self.assertEqual(state["broker_quantity"], 200)
        self.assertEqual(state["residual_quantity"], 100)

    def test_the_event_log_records_detection_and_the_freeze(self):
        result = self.detect()
        events = [row["event"] for row in self.detector.log_for(result["id"])]
        self.assertEqual(events, ["detected", "freeze_confirmed", "escalated"])


class EscalationTests(CorporateActionTestCase):
    def test_escalation_reaches_the_account_owner_once(self):
        result = self.detect()
        self.assertTrue(result["escalated"])
        self.assertEqual(self.notified, [("kite:A", "suspected_split")])
        event = self.detector.events()[0]
        self.assertEqual(event["status"], "escalated")

    def test_a_failed_escalation_leaves_the_detection_standing(self):
        """Detection and the freeze never depend on the notification succeeding."""
        from backend.strategies.account_truth import AccountTruthStore

        detector = self._detector(notifier=lambda *_: False)
        result = detector.detect(
            account_id="kite:A", coordinate=COORD, broker_quantity=200,
            attributed_quantity=100, manual_quantity=0,
        )
        self.assertFalse(result["escalated"])
        self.assertTrue(result["frozen"])
        # Still recorded, still detected: only the status is not advanced.
        self.assertEqual(detector.events()[0]["status"], "detected")
        self.assertEqual(
            AccountTruthStore(session_factory=self.factory).is_frozen_coordinate(
                account_id="kite:A", coordinate=(738561, "NSE", "RELIANCE", "CNC")
            ),
            "unexplained",
        )


class ResolutionTests(CorporateActionTestCase):
    def test_resolution_requires_an_explicit_owner_adjustment(self):
        result = self.detect()
        # There is no automatic path — resolving means naming the adjustment that
        # recorded what a human decided had happened.
        resolved = self.detector.resolve(
            result["id"], adjustment_id="adj-1", actor_id="app:owner", now=NOW
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_adjustment_id"], "adj-1")
        events = [row["event"] for row in self.detector.log_for(result["id"])]
        self.assertEqual(events[-1], "resolved")
        self.assertEqual(self.detector.events()[0]["status"], "resolved")

    def test_resolving_an_unknown_event_is_refused(self):
        with self.assertRaises(ValueError):
            self.detector.resolve("nope", adjustment_id="adj-1", actor_id="app:owner")

    def test_detection_does_not_rebase_the_book(self):
        """No automatic rebasing: a detection changes no position quantity."""
        from backend.strategies.attribution_models import StrategyPositionProjection
        from sqlalchemy import select

        self.detect()
        with self.factory() as session:
            rows = session.execute(select(StrategyPositionProjection)).scalars().all()
        # Nothing was rewritten: the book is untouched and the freeze is what
        # stands between the divergence and further exposure.
        self.assertEqual(rows, [])


class ReconcileHookTests(CorporateActionTestCase):
    """Detection runs from reconciliation, because that is where evidence lands."""

    def test_a_persistent_split_like_divergence_is_detected_automatically(self):
        import asyncio

        from backend.strategies.account_truth import AccountTruthStore, ReconciliationService
        from backend.strategies.attribution_models import (
            StrategyPositionProjection,
            StrategyReconciliationState,
        )
        from sqlalchemy import select

        # The broker says 200, the strategy's own book says 100, and there are no
        # fills to explain the gap: exactly the shape a split leaves behind.
        with self.factory() as session:
            # The projection's composite FK requires the canonical strategy.
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, exchange, tradingsymbol, product, net_quantity) "
                    "VALUES ('kite:A', 738561, 'NSE', 'RELIANCE', 'CNC', 200)"
                )
            )
            session.add(
                StrategyPositionProjection(
                    account_id="kite:A", strategy_id="stg-A", execution_environment="live",
                    identity_kind="canonical", identity_key="inst-REL",
                    canonical_instrument_id="inst-REL", product="CNC", instrument_token=738561,
                    exchange="NSE", tradingsymbol="RELIANCE", net_quantity=100,
                    projection_version=1,
                )
            )
            session.add(
                StrategyReconciliationState(
                    account_id="kite:A", instrument_token=738561, exchange="NSE",
                    tradingsymbol="RELIANCE", product="CNC", divergence_class="pending_ingest",
                    broker_quantity=200, attributed_quantity=100, manual_quantity=0,
                    residual_quantity=100, refresh_attempts=3,
                )
            )
            session.commit()

        service = ReconciliationService(
            AccountTruthStore(session_factory=self.factory),
            run_async=lambda func, **kwargs: _inline(func, **kwargs),
            corporate_actions=self.detector,
            max_attempts=1,
        )
        asyncio.run(service.reconcile_account("kite:A"))

        events = self.detector.events()
        self.assertEqual([row["action_kind"] for row in events], ["suspected_split"])
        self.assertEqual(self.notified, [("kite:A", "suspected_split")])
        with self.factory() as session:
            rows = session.execute(select(StrategyReconciliationState)).scalars().all()
        # And the coordinate is frozen by the same run.
        self.assertEqual(rows[0].divergence_class, "unexplained")


async def _inline(func, /, **kwargs):
    return func(**kwargs)


if __name__ == "__main__":
    unittest.main()
