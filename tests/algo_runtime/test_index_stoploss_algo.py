import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from strategies.modular import ModularIndexStoplossAlgo, register_builtin_algos  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402


def build_snapshot(*, price: float, positions: dict | None = None) -> Snapshot:
    return Snapshot(
        algo_instance_id="algo-1",
        algo_type="index_stoploss",
        trigger=TriggerEvent(type="tick", token=256265),
        market={
            "ltp": {"256265": price},
            "ticks": {"256265": {"instrument_token": 256265, "last_price": price}},
        },
        positions={"filtered": positions or {}},
    )


class ModularIndexStoplossAlgoTests(unittest.IsolatedAsyncioTestCase):
    async def test_upper_stoploss_trigger_emits_notify_order_intent_and_state_patch(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "upper_stoploss": 24000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(
            build_snapshot(
                price=24210.55,
                positions={
                    "NFO:NIFTY24APR24200CE:MIS": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APR24200CE",
                        "product": "MIS",
                        "quantity": -50,
                    }
                },
            ),
            state={},
        )

        self.assertEqual(actions[0].action_type, "notify")
        self.assertEqual(actions[1].action_type, "order_intent")
        self.assertEqual(actions[1].intent_type, "place_basket")
        self.assertEqual(actions[1].payload["basket"]["orders"][0]["transaction_type"], "BUY")
        self.assertEqual(actions[2].action_type, "state_patch")
        self.assertTrue(actions[2].patch["triggered"])

    async def test_filtered_empty_does_not_fall_back_to_all_positions(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "upper_stoploss": 24000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(
            Snapshot(
                algo_instance_id="algo-1",
                algo_type="index_stoploss",
                trigger=TriggerEvent(type="tick", token=256265),
                market={"ltp": {"256265": 24210.55}},
                positions={
                    "filtered": {},
                    "all": {
                        "NFO:SHOULDNOTEXIT:MIS": {
                            "exchange": "NFO",
                            "tradingsymbol": "SHOULDNOTEXIT",
                            "product": "MIS",
                            "quantity": 50,
                        }
                    },
                },
            ),
            state={},
        )

        self.assertFalse(any(getattr(action, "action_type", None) == "order_intent" for action in actions))

    async def test_invalid_position_is_skipped_from_exit_basket(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "upper_stoploss": 24000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(
            build_snapshot(
                price=24210.55,
                positions={
                    "invalid": {"exchange": "NFO", "product": "MIS", "quantity": 50},
                    "valid": {"exchange": "NFO", "tradingsymbol": "NIFTY24APR24200CE", "product": "MIS", "quantity": 50},
                },
            ),
            state={},
        )

        self.assertEqual(actions[1].payload["basket"]["orders"][0]["tradingsymbol"], "NIFTY24APR24200CE")
        self.assertTrue(any(getattr(action, "metadata", {}).get("skipped_positions") == 1 for action in actions if getattr(action, "action_type", None) == "notify"))

    async def test_no_trigger_only_updates_last_seen_price(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "upper_stoploss": 25000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(build_snapshot(price=24210.55), state={})

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "state_patch")
        self.assertEqual(actions[0].patch["last_seen_price"], 24210.55)

    async def test_already_triggered_state_returns_noop(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "lower_stoploss": 24000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(build_snapshot(price=23900.0), state={"triggered": True})

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "noop")

    async def test_lower_stoploss_trigger_path_emits_order_intent(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "lower_stoploss": 24000,
                },
            },
        )()
        algo = ModularIndexStoplossAlgo(instance)

        actions = await algo.evaluate(
            build_snapshot(
                price=23900.0,
                positions={
                    "NFO:NIFTY24APR24200PE:MIS": {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APR24200PE",
                        "product": "MIS",
                        "quantity": 50,
                    }
                },
            ),
            state={},
        )

        self.assertEqual(actions[1].payload["trigger_reason"], "index_lower_stoploss_triggered")

    def test_invalid_config_fails_fast(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "index_token": 256265,
                    "upper_stoploss": 24000,
                    "order_tag": "bad tag!",
                },
            },
        )()

        with self.assertRaises(ValueError):
            ModularIndexStoplossAlgo(instance)

    def test_builtin_registry_registration(self):
        registry = AlgoRegistry()
        register_builtin_algos(registry)

        self.assertTrue(registry.has("index_stoploss"))
