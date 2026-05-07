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
            return _FakeResult(
                one=SimpleNamespace(
                    cursor=self.cursor,
                    strategy_run_id=params.get("strategy_run_id"),
                    account_id=params.get("account_id"),
                    basket_execution_id=params.get("basket_execution_id"),
                    event_kind=params.get("event_kind"),
                    event_source=params.get("event_source"),
                    event_type=params.get("event_type"),
                    related_resource_type=params.get("related_resource_type"),
                    related_resource_id=params.get("related_resource_id"),
                    summary=params.get("summary"),
                    payload_json=params.get("payload_json"),
                    created_at=None,
                )
            )
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
        self.assertGreater(first["cursor"], 0)
        self.assertGreater(second["cursor"], first["cursor"])
        self.assertEqual(first["event_kind"], "execution")
        self.assertEqual(first["event_source"], "basket_runtime")

    def test_non_basket_order_event_uses_broker_order_related_ref(self):
        store = BasketExecutionStore()
        db = _FakeStoreDB()
        row = store.append_worker_execution_event(
            db,  # type: ignore[arg-type]
            strategy_run_id="run-2",
            account_id="kite:acct",
            basket_execution_id=None,
            event_type="order.updated",
            related_resource_type="broker_order",
            related_resource_id="OID-2",
            payload={"order_id": "OID-2", "status": "COMPLETE"},
        )
        self.assertEqual(row["related_resource_type"], "broker_order")
        self.assertEqual(row["related_resource_id"], "OID-2")


if __name__ == "__main__":
    unittest.main()
