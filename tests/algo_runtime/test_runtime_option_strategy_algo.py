import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402
from strategies.modular import RuntimeManagedOptionStrategyAlgo, register_builtin_algos  # noqa: E402


def build_snapshot(*, index_price=None, total_pnl=None, positions=None, orders=None):
    market = {}
    if index_price is not None:
        market = {
            "ltp": {"256265": index_price},
            "ticks": {"256265": {"instrument_token": 256265, "last_price": index_price}},
        }
    return Snapshot(
        algo_instance_id="option-strategy:1",
        algo_type="runtime_option_strategy",
        trigger=TriggerEvent(type="tick", token=256265),
        market=market,
        positions={"filtered": positions or {}, "totals": {"total_pnl": total_pnl} if total_pnl is not None else {}},
        orders={"relevant": orders or []},
    )


class RuntimeManagedOptionStrategyAlgoTests(unittest.IsolatedAsyncioTestCase):
    def _instance(self):
        return type(
            "Instance",
            (),
            {
                "instance_id": "option-strategy:1",
                "config": {
                    "account_scope": "default",
                    "spot_token": 256265,
                    "selected_legs": [
                        {
                            "instrument_token": 101,
                            "tradingsymbol": "NIFTY24APR22500CE",
                            "strike": 22500,
                            "option_type": "CE",
                            "transaction_type": "SELL",
                            "ltp": 120.0,
                            "lot_size": 50,
                            "lots": 1,
                            "quantity": 50,
                        },
                        {
                            "instrument_token": 102,
                            "tradingsymbol": "NIFTY24APR22500PE",
                            "strike": 22500,
                            "option_type": "PE",
                            "transaction_type": "SELL",
                            "ltp": 110.0,
                            "lot_size": 50,
                            "lots": 1,
                            "quantity": 50,
                        },
                    ],
                    "rules": [
                        {"key": "emergency-low", "metric": "index_price", "role": "emergency_guard", "label": "index lower emergency", "operator": "lte", "threshold": 22350.0, "required": True, "source": "backend_required"},
                        {"key": "premium-target", "metric": "combined_premium_points", "role": "profit_target", "label": "combined premium target", "operator": "lte", "threshold": 150.0, "required": True, "source": "backend_default"},
                        {"key": "mtm-stop", "metric": "basket_mtm_rupees", "role": "hard_stop", "label": "basket MTM stoploss", "operator": "lte", "threshold": -5000.0, "required": False, "source": "backend_default"},
                    ],
                    "precedence": ["emergency_guard", "hard_stop", "profit_target", "trailing_stop"],
                },
            },
        )()

    async def test_combined_premium_profit_target_emits_exit_basket_and_stops_instance(self):
        algo = RuntimeManagedOptionStrategyAlgo(self._instance())
        actions = await algo.evaluate(
            build_snapshot(
                index_price=22500.0,
                positions={
                    "ce": {"instrument_token": 101, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500CE", "product": "MIS", "quantity": -50, "last_price": 80.0},
                    "pe": {"instrument_token": 102, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500PE", "product": "MIS", "quantity": -50, "last_price": 70.0},
                },
            ),
            {},
        )

        self.assertEqual(actions[1].action_type, "order_intent")
        self.assertEqual(actions[1].payload["trigger_reason"], "premium-target")
        self.assertEqual(actions[-1].patch["_instance_status"], "stopped")

    async def test_mtm_stop_has_priority_over_profit_target(self):
        algo = RuntimeManagedOptionStrategyAlgo(self._instance())
        actions = await algo.evaluate(
            build_snapshot(
                index_price=22500.0,
                total_pnl=-6000.0,
                positions={
                    "ce": {"instrument_token": 101, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500CE", "product": "MIS", "quantity": -50, "last_price": 80.0},
                    "pe": {"instrument_token": 102, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500PE", "product": "MIS", "quantity": -50, "last_price": 70.0},
                },
            ),
            {},
        )

        self.assertEqual(actions[-1].patch["trigger_rule"], "mtm-stop")

    async def test_existing_open_exit_order_blocks_duplicate_exit_intent(self):
        algo = RuntimeManagedOptionStrategyAlgo(self._instance())
        actions = await algo.evaluate(
            build_snapshot(
                index_price=22300.0,
                positions={
                    "ce": {"instrument_token": 101, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500CE", "product": "MIS", "quantity": -50, "last_price": 120.0},
                },
                orders=[{"tradingsymbol": "NIFTY24APR22500CE", "transaction_type": "BUY", "latest_status": "OPEN"}],
            ),
            {},
        )

        self.assertFalse(any(getattr(action, "action_type", None) == "order_intent" for action in actions))
        self.assertEqual(actions[-1].patch["blocked_symbols"], ["NIFTY24APR22500CE"])
        self.assertNotIn("_instance_status", actions[-1].patch)

    async def test_partial_blocked_exit_keeps_monitoring_active(self):
        algo = RuntimeManagedOptionStrategyAlgo(self._instance())
        actions = await algo.evaluate(
            build_snapshot(
                index_price=22300.0,
                positions={
                    "ce": {"instrument_token": 101, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500CE", "product": "MIS", "quantity": -50, "last_price": 120.0},
                    "pe": {"instrument_token": 102, "exchange": "NFO", "tradingsymbol": "NIFTY24APR22500PE", "product": "MIS", "quantity": -50, "last_price": 110.0},
                },
                orders=[{"tradingsymbol": "NIFTY24APR22500CE", "transaction_type": "BUY", "latest_status": "OPEN"}],
            ),
            {},
        )

        self.assertTrue(any(getattr(action, "action_type", None) == "order_intent" for action in actions))
        self.assertEqual(actions[-1].patch["blocked_symbols"], ["NIFTY24APR22500CE"])
        self.assertNotIn("_instance_status", actions[-1].patch)

    async def test_flat_positions_complete_and_stop_instance(self):
        algo = RuntimeManagedOptionStrategyAlgo(self._instance())
        actions = await algo.evaluate(build_snapshot(index_price=22500.0, positions={}), {})

        self.assertEqual(actions[0].patch["_instance_status"], "stopped")
        self.assertTrue(actions[0].patch["completed"])

    def test_builtin_registry_registration(self):
        registry = AlgoRegistry()
        register_builtin_algos(registry)
        self.assertTrue(registry.has("runtime_option_strategy"))


if __name__ == "__main__":
    unittest.main()
