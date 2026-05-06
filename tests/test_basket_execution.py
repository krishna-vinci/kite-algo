from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.basket_execution import BasketExecutionStore, recompute_basket_status


class _FakeResult:
    def __init__(self, *, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _FakeStoreDB:
    def __init__(self):
        self.cursor = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "INSERT INTO public.worker_execution_events" in sql:
            self.cursor += 1
            return _FakeResult(one=SimpleNamespace(cursor=self.cursor))
        return _FakeResult()


class BasketExecutionTests(unittest.TestCase):
    def test_recompute_basket_status_completed_when_all_legs_filled(self):
        state = recompute_basket_status(
            [
                {"status": "filled", "requested_quantity": 10, "last_seen_filled_quantity": 10},
                {"status": "filled", "requested_quantity": 5, "last_seen_filled_quantity": 5},
            ]
        )
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["total_filled_quantity"], 15)

    def test_recompute_basket_status_partial_when_terminal_underfill_exists(self):
        state = recompute_basket_status(
            [
                {"status": "filled", "requested_quantity": 10, "last_seen_filled_quantity": 10},
                {"status": "partial_terminal", "requested_quantity": 10, "last_seen_filled_quantity": 4},
            ]
        )
        self.assertEqual(state["status"], "partial")

    def test_append_worker_execution_event_returns_monotonic_cursor(self):
        store = BasketExecutionStore()
        db = _FakeStoreDB()
        first = store.append_worker_execution_event(
            db,  # type: ignore[arg-type]
            strategy_run_id="run-1",
            account_id="kite:acct",
            basket_execution_id="basket-1",
            event_type="basket.status_changed",
            payload={"status": "active"},
        )
        second = store.append_worker_execution_event(
            db,  # type: ignore[arg-type]
            strategy_run_id="run-1",
            account_id="kite:acct",
            basket_execution_id="basket-1",
            event_type="basket.status_changed",
            payload={"status": "completed"},
        )
        self.assertGreater(first, 0)
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
