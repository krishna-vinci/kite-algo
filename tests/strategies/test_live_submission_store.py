"""The durable live-submission claim is portable (SQLite) and once-only.

PostgreSQL is where the claim's uniqueness is enforced in production (proved in
``tests/integration/test_live_adapter_preparation_postgres.py``); this keeps the
store's SQL working on the established SQLite fixture too, so the adapter cannot
silently depend on one dialect's JSON/`NOW()` handling.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class LiveSubmissionStoreTests(unittest.TestCase):
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
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
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
            dbapi_connection.commit()

        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def _store(self):
        from backend.strategies.live_adapter import LiveSubmissionStore

        return LiveSubmissionStore(session_factory=self.factory)

    def _claim(self, store, *, plan_id="plan-1", state="pending", delta=None):
        row, created = store.claim(
            plan_id=plan_id,
            step_no=1,
            step_ref=f"live-plan:{plan_id}:step:1",
            strategy_id="stg-A",
            account_id="kite:A",
            execution_environment="live",
            delta_snapshot={"delta": 10} if delta is None else delta,
            state=state,
        )
        return row, created

    def test_the_first_claim_wins_and_a_second_one_reads_it(self):
        store = self._store()
        first, created = self._claim(store)
        self.assertTrue(created)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["delta_snapshot"], {"delta": 10})

        second, created_again = self._claim(store, state="rejected", delta={"delta": 99})
        self.assertFalse(created_again)
        # The winner's row is returned verbatim: a second caller cannot rewrite
        # the claim, its state or its authorised delta.
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertEqual(second["state"], "pending")
        self.assertEqual(second["delta_snapshot"], {"delta": 10})

    def test_an_outcome_is_recorded_on_the_existing_claim(self):
        store = self._store()
        self._claim(store)

        updated = store.record_outcome(
            plan_id="plan-1",
            step_no=1,
            state="uncertain",
            detail={"note": "no order reference"},
        )

        self.assertEqual(updated["state"], "uncertain")
        self.assertEqual(updated["detail"], {"note": "no order reference"})
        self.assertEqual(store.get(plan_id="plan-1", step_no=1)["state"], "uncertain")


if __name__ == "__main__":
    unittest.main()
