import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from strategies.option_strategy.store import OptionStrategyStore  # noqa: E402


class _FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0

    def execute(self, statement, params):
        text_sql = str(statement)
        self.calls.append((text_sql, params))
        if "RETURNING id" in text_sql:
            return _FakeResult("run-123")
        return _FakeResult()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


class OptionStrategyStoreTests(unittest.TestCase):
    def test_create_and_update_execution_result(self):
        session = _FakeSession()
        store = OptionStrategyStore(session_factory=lambda: session)

        run_id = store.create_run(
            underlying="NIFTY",
            expiry="2026-04-30",
            user_intent="short_straddle",
            inferred_structure="short_straddle",
            inferred_family="neutral-short-premium",
            execution_mode="paper",
            selected_legs=[{"tradingsymbol": "NIFTY24APR22500CE"}],
            canonical_strategy={"primary_metric": "combined_premium_points"},
            order_plan={"orders": [{"tradingsymbol": "NIFTY24APR22500CE"}]},
        )
        store.update_execution_result("run-123", status="success", execution_result={"status": "success"})

        self.assertEqual(run_id, "run-123")
        self.assertEqual(session.committed, 2)
        self.assertEqual(session.rolled_back, 0)
        self.assertEqual(session.closed, 2)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("option_strategy_runs", session.calls[0][0])
        self.assertIn("algo_instance_id", session.calls[0][0])
        self.assertIsNone(session.calls[0][1]["algo_instance_id"])
        self.assertEqual(session.calls[1][1]["status"], "success")


if __name__ == "__main__":
    unittest.main()
