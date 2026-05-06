import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402
from strategies.modular import ModularCombinedPremiumStoplossAlgo, register_builtin_algos  # noqa: E402


def build_snapshot(*, net_premium: float | None, positions: dict | None = None) -> Snapshot:
    options = {}
    if net_premium is not None:
        options = {"NIFTY:nearest:snapshot:5:": {"positions_net_premium": net_premium}}
    return Snapshot(
        algo_instance_id="options-premium-1",
        algo_type="combined_premium_stoploss",
        trigger=TriggerEvent(type="tick", token=111),
        options=options,
        positions={"filtered": positions or {}},
        orders={"relevant": []},
    )


class ModularCombinedPremiumStoplossAlgoTests(unittest.IsolatedAsyncioTestCase):
    def _instance(self):
        return type(
            "Instance",
            (),
            {
                "instance_id": "options-premium-1",
                "config": {
                    "session_id": "test-session-id",
                    "underlying": "NIFTY",
                    "expiry_mode": "nearest",
                    "entry_type": "short",
                    "profit_target": 20.0,
                    "dry_run": True,
                },
            },
        )()

    async def test_combined_premium_stoploss_triggers_exit_when_short_profit_target_hit(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        actions = await algo.evaluate(
            build_snapshot(
                net_premium=80.0,
                positions={
                    "p1": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                    }
                },
            ),
            {"initial_net_premium": 100.0},
        )

        self.assertEqual(actions[0].action_type, "notify")
        self.assertEqual(actions[1].action_type, "order_intent")
        self.assertEqual(actions[1].payload["basket"]["dry_run"], True)
        self.assertEqual(actions[1].payload["basket"]["orders"][0]["transaction_type"], "BUY")
        self.assertEqual(actions[1].dedupe_key, "options-premium-1:combined_premium_profit_target")
        self.assertEqual(actions[-1].action_type, "state_patch")
        self.assertTrue(actions[-1].patch["triggered"])

    async def test_no_trigger_only_updates_state(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        actions = await algo.evaluate(build_snapshot(net_premium=95.0), {"initial_net_premium": 100.0})

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "state_patch")
        self.assertEqual(actions[0].patch["net_pnl"], 5.0)

    async def test_missing_net_premium_returns_noop(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        actions = await algo.evaluate(build_snapshot(net_premium=None), {})

        self.assertEqual(actions[0].action_type, "noop")
        self.assertEqual(actions[0].reason, "missing_positions_net_premium")

    async def test_only_matching_underlying_and_expiry_mode_payload_is_used(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        snapshot = Snapshot(
            algo_instance_id="options-premium-1",
            algo_type="combined_premium_stoploss",
            trigger=TriggerEvent(type="tick", token=111),
            options={
                "BANKNIFTY:nearest:snapshot:5:": {"positions_net_premium": 10.0},
                "NIFTY:nearest:snapshot:5:": {"positions_net_premium": 80.0},
            },
            positions={
                "filtered": {
                    "p1": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                    }
                }
            },
            orders={"relevant": []},
        )

        actions = await algo.evaluate(snapshot, {"initial_net_premium": 100.0})

        self.assertEqual(actions[1].action_type, "order_intent")
        self.assertEqual(actions[1].dedupe_key, "options-premium-1:combined_premium_profit_target")

    async def test_invalid_first_payload_does_not_hide_valid_matching_premium(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        snapshot = Snapshot(
            algo_instance_id="options-premium-1",
            algo_type="combined_premium_stoploss",
            trigger=TriggerEvent(type="tick", token=111),
            options={
                "NIFTY:nearest:snapshot:5:": {"positions_net_premium": "bad"},
                "NIFTY:nearest:snapshot:10:": {"positions_net_premium": 80.0},
            },
            positions={
                "filtered": {
                    "p1": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                    }
                }
            },
            orders={"relevant": []},
        )

        actions = await algo.evaluate(snapshot, {"initial_net_premium": 100.0})

        self.assertEqual(actions[1].action_type, "order_intent")

    async def test_positions_last_price_fallback_supports_live_runtime_without_snapshot_premium(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        snapshot = Snapshot(
            algo_instance_id="options-premium-1",
            algo_type="combined_premium_stoploss",
            trigger=TriggerEvent(type="tick", token=111),
            options={"NIFTY:nearest:snapshot:5:": {"spot_ltp": 23123.65}},
            positions={
                "filtered": {
                    "ce": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                        "last_price": 45.0,
                    },
                    "pe": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRPE",
                        "product": "MIS",
                        "quantity": -50,
                        "last_price": 35.0,
                    },
                }
            },
            orders={"relevant": []},
        )

        actions = await algo.evaluate(snapshot, {"initial_net_premium": 100.0})

        self.assertEqual(actions[1].action_type, "order_intent")

    async def test_positions_fallback_ignores_non_matching_underlying_symbols(self):
        algo = ModularCombinedPremiumStoplossAlgo(self._instance())

        snapshot = Snapshot(
            algo_instance_id="options-premium-1",
            algo_type="combined_premium_stoploss",
            trigger=TriggerEvent(type="tick", token=111),
            options={"NIFTY:nearest:snapshot:5:": {"spot_ltp": 23123.65}},
            positions={
                "filtered": {
                    "ce": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                        "last_price": 30.0,
                    },
                    "pe": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APRPE",
                        "product": "MIS",
                        "quantity": -50,
                        "last_price": 25.0,
                    },
                    "other": {
                        "exchange": "NFO",
                        "tradingsymbol": "BANKNIFTY24APRCE",
                        "product": "MIS",
                        "quantity": -50,
                        "last_price": 1.0,
                    },
                }
            },
            orders={"relevant": []},
        )

        actions = await algo.evaluate(snapshot, {"initial_net_premium": 80.0})

        self.assertEqual(actions[1].action_type, "order_intent")

    def test_builtin_registry_registration(self):
        registry = AlgoRegistry()
        register_builtin_algos(registry)

        self.assertTrue(registry.has("combined_premium_stoploss"))
