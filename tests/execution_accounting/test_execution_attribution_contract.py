import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.algo_runtime.execution_attribution import build_paper_execution_attribution  # noqa: E402


class ExecutionAttributionContractTests(unittest.TestCase):
    def test_paper_attribution_overrides_conflicting_metadata(self):
        result = build_paper_execution_attribution(
            strategy_run_id="run-1",
            strategy_family="indicator_strategy",
            strategy_name="Mean Reversion",
            account_ref="kite:paper-a",
            entry_surface="algo_worker",
            source="algo_worker",
            idempotency_key="run-1:entry:1",
            metadata={"strategy_run_id": "evil", "strategy_name": "wrong"},
            extras={"algo_instance_id": "algo-1"},
        )
        self.assertEqual(result["strategy_run_id"], "run-1")
        self.assertEqual(result["strategy_name"], "Mean Reversion")
        self.assertEqual(result["metadata"]["strategy_run_id"], "run-1")
        self.assertEqual(result["metadata"]["strategy_name"], "Mean Reversion")
        self.assertEqual(result["algo_instance_id"], "algo-1")

    def test_paper_attribution_requires_primary_identity(self):
        with self.assertRaises(ValueError) as ctx:
            build_paper_execution_attribution(
                strategy_run_id="",
                strategy_family="indicator_strategy",
                strategy_name="MR",
                account_ref="kite:paper-a",
                entry_surface="algo_worker",
                idempotency_key="abc12345",
                metadata={},
            )
        self.assertIn("strategy_run_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
