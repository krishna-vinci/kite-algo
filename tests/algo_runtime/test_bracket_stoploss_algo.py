import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402
from strategies.modular import ModularBracketStoplossAlgo, register_builtin_algos  # noqa: E402


def build_snapshot(*, price: float, positions: dict | None = None, orders: list | None = None) -> Snapshot:
    return Snapshot(
        algo_instance_id="algo-1",
        algo_type="bracket_stoploss",
        trigger=TriggerEvent(type="tick", token=12345),
        market={
            "ltp": {"12345": price},
            "ticks": {"12345": {"instrument_token": 12345, "last_price": price}},
        },
        positions={"filtered": positions or {}},
        orders={"relevant": orders or []},
    )


class ModularBracketStoplossAlgoTests(unittest.IsolatedAsyncioTestCase):
    def _instance(self):
        return type(
            "Instance",
            (),
            {
                "instance_id": "algo-1",
                "config": {
                    "session_id": "test-session-id",
                    "trigger_token": 12345,
                    "direction": "long",
                    "entry_price": 100.0,
                    "stop_distance": 5.0,
                    "target_distance": 8.0,
                    "trailing_distance": 2.0,
                    "trailing_activation_distance": 4.0,
                },
            },
        )()

    async def test_target_hit_emits_order_intent_and_state_patch(self):
        algo = ModularBracketStoplossAlgo(self._instance())

        actions = await algo.evaluate(
            build_snapshot(
                price=108.0,
                positions={
                    "NFO:LONG:MIS": {
                        "exchange": "NFO",
                        "tradingsymbol": "LONG",
                        "product": "MIS",
                        "quantity": 50,
                    }
                },
            ),
            state={},
        )

        self.assertEqual(actions[1].action_type, "order_intent")
        self.assertEqual(actions[1].payload["trigger_reason"], "bracket_target_hit")
        self.assertEqual(actions[-1].patch["trigger_reason"], "bracket_target_hit")

    async def test_trailing_stop_updates_and_then_triggers(self):
        algo = ModularBracketStoplossAlgo(self._instance())
        positions = {
            "NFO:LONG:MIS": {
                "exchange": "NFO",
                "tradingsymbol": "LONG",
                "product": "MIS",
                "quantity": 50,
            }
        }

        first_actions = await algo.evaluate(build_snapshot(price=105.0, positions=positions), state={})
        second_actions = await algo.evaluate(
            build_snapshot(price=102.5, positions=positions),
            state=first_actions[0].patch,
        )

        self.assertEqual(first_actions[0].patch["active_stop_price"], 103.0)
        self.assertEqual(second_actions[1].payload["trigger_reason"], "bracket_stoploss_hit")

    async def test_open_exit_order_blocks_duplicate_exit_intent(self):
        algo = ModularBracketStoplossAlgo(self._instance())

        actions = await algo.evaluate(
            build_snapshot(
                price=95.0,
                positions={
                    "NFO:LONG:MIS": {
                        "exchange": "NFO",
                        "tradingsymbol": "LONG",
                        "product": "MIS",
                        "quantity": 50,
                    }
                },
                orders=[{"tradingsymbol": "LONG", "transaction_type": "SELL", "latest_status": "OPEN"}],
            ),
            state={},
        )

        self.assertFalse(any(getattr(action, "action_type", None) == "order_intent" for action in actions))
        self.assertEqual(actions[-1].patch["blocked_symbols"], ["LONG"])

    def test_builtin_registry_registration(self):
        registry = AlgoRegistry()
        register_builtin_algos(registry)

        self.assertTrue(registry.has("bracket_stoploss"))
