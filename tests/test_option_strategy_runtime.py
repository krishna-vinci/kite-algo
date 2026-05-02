import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from strategies.option_strategy.runtime import build_runtime_option_instance  # noqa: E402


class OptionStrategyRuntimeBuilderTests(unittest.TestCase):
    def test_runtime_instance_requires_selected_legs(self):
        with self.assertRaises(ValueError):
            build_runtime_option_instance(
                strategy_id="run-1",
                execution_mode="paper",
                account_scope="default",
                selected_legs=[],
                strategy_preview={
                    "user_intent": "manual",
                    "inferred_structure": "manual",
                    "inferred_family": "premium-managed-structure",
                    "direction_bias": "structure",
                    "classification_confidence": 1.0,
                    "classification_reason": "test",
                    "description": "test",
                    "primary_metric": "basket_mtm_rupees",
                    "warnings": [],
                    "precedence": ["emergency_guard", "hard_stop", "profit_target", "trailing_stop"],
                    "inputs": {},
                    "rules": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
