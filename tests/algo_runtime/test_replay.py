import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from strategies.modular.bracket_stoploss import ModularBracketStoplossAlgo  # noqa: E402
from strategies.modular.ema_monitor import ModularEmaMonitorAlgo  # noqa: E402


class ReplayVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bracket_stoploss_replay_sequence_triggers_once_after_trailing_move(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-bracket",
                "config": {
                    "session_id": "test-session-id",
                    "trigger_token": 12345,
                    "direction": "long",
                    "entry_price": 100.0,
                    "stop_distance": 5.0,
                    "target_distance": 20.0,
                    "trailing_distance": 2.0,
                    "trailing_activation_distance": 4.0,
                },
            },
        )()
        algo = ModularBracketStoplossAlgo(instance)
        state = {}
        positions = {
            "NFO:LONG:MIS": {"exchange": "NFO", "tradingsymbol": "LONG", "product": "MIS", "quantity": 50}
        }

        terminal_actions = None
        for price in [100.0, 104.5, 106.0, 103.5]:
            terminal_actions = await algo.evaluate(
                Snapshot(
                    algo_instance_id="algo-bracket",
                    algo_type="bracket_stoploss",
                    trigger=TriggerEvent(type="tick", token=12345),
                    market={"ltp": {"12345": price}},
                    positions={"filtered": positions},
                    orders={"relevant": []},
                ),
                state=state,
            )
            patch_actions = [action for action in terminal_actions if getattr(action, "action_type", None) == "state_patch"]
            if patch_actions:
                state.update(patch_actions[-1].patch)

        self.assertIsNotNone(terminal_actions)
        self.assertTrue(any(getattr(action, "action_type", None) == "order_intent" for action in terminal_actions))
        self.assertEqual(state["trigger_reason"], "bracket_stoploss_hit")

    async def test_ema_monitor_replay_sequence_only_notifies_on_regime_change(self):
        instance = type(
            "Instance",
            (),
            {
                "instance_id": "algo-ema",
                "config": {"token": 256265, "timeframe": "5minute", "fast_length": 9, "slow_length": 21},
            },
        )()
        algo = ModularEmaMonitorAlgo(instance)
        state = {}
        notify_count = 0

        for fast, slow in [(99.0, 100.0), (98.5, 100.0), (100.5, 100.0), (101.0, 100.0)]:
            actions = await algo.evaluate(
                Snapshot(
                    algo_instance_id="algo-ema",
                    algo_type="ema_monitor",
                    trigger=TriggerEvent(type="candle_close", token=256265, timeframe="5minute"),
                    candles={"256265:5minute:21:0": {"latest_closed": {"close": 100.0}}},
                    indicators={
                        "ema:256265:5minute:length=9": {"value": fast, "ready": True},
                        "ema:256265:5minute:length=21": {"value": slow, "ready": True},
                    },
                ),
                state=state,
            )
            notify_count += sum(1 for action in actions if getattr(action, "action_type", None) == "notify")
            patch_actions = [action for action in actions if getattr(action, "action_type", None) == "state_patch"]
            if patch_actions:
                state.update(patch_actions[-1].patch)

        self.assertEqual(notify_count, 1)
        self.assertEqual(state["regime"], "bullish")
