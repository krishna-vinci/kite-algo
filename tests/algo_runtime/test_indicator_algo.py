import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import Snapshot, TriggerEvent  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402
from strategies.modular import ModularEmaMonitorAlgo, register_builtin_algos  # noqa: E402


def build_snapshot(*, fast: float | None, slow: float | None, close: float = 100.0) -> Snapshot:
    indicators = {}
    if fast is not None:
        indicators["ema:256265:5minute:length=9"] = {"value": fast, "ready": True}
    if slow is not None:
        indicators["ema:256265:5minute:length=21"] = {"value": slow, "ready": True}
    return Snapshot(
        algo_instance_id="algo-ema",
        algo_type="ema_monitor",
        trigger=TriggerEvent(type="candle_close", token=256265, timeframe="5minute"),
        candles={"256265:5minute:21:0": {"latest_closed": {"close": close}}},
        indicators=indicators,
    )


class ModularEmaMonitorAlgoTests(unittest.IsolatedAsyncioTestCase):
    def _instance(self):
        return type(
            "Instance",
            (),
            {
                "instance_id": "algo-ema",
                "config": {
                    "token": 256265,
                    "timeframe": "5minute",
                    "fast_length": 9,
                    "slow_length": 21,
                },
            },
        )()

    async def test_first_ready_evaluation_only_patches_state(self):
        algo = ModularEmaMonitorAlgo(self._instance())

        actions = await algo.evaluate(build_snapshot(fast=101.0, slow=100.0), state={})

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "state_patch")
        self.assertEqual(actions[0].patch["regime"], "bullish")

    async def test_bullish_cross_emits_notify_and_state_patch(self):
        algo = ModularEmaMonitorAlgo(self._instance())

        actions = await algo.evaluate(
            build_snapshot(fast=101.0, slow=100.0, close=102.0),
            state={"regime": "bearish"},
        )

        self.assertEqual(actions[0].action_type, "notify")
        self.assertEqual(actions[0].metadata["signal"], "bullish_ema_cross")
        self.assertEqual(actions[1].patch["regime"], "bullish")

    async def test_latest_close_is_resolved_from_largest_matching_candle_series(self):
        algo = ModularEmaMonitorAlgo(self._instance())

        actions = await algo.evaluate(
            Snapshot(
                algo_instance_id="algo-ema",
                algo_type="ema_monitor",
                trigger=TriggerEvent(type="candle_close", token=256265, timeframe="5minute"),
                candles={
                    "256265:5minute:9:0": {"latest_closed": {"close": 99.0}},
                    "256265:5minute:50:0": {"latest_closed": {"close": 102.0}},
                },
                indicators={
                    "ema:256265:5minute:length=9": {"value": 101.0, "ready": True},
                    "ema:256265:5minute:length=21": {"value": 100.0, "ready": True},
                },
            ),
            state={"regime": "bearish"},
        )

        self.assertEqual(actions[0].metadata["close_price"], 102.0)

    async def test_missing_indicator_values_returns_noop(self):
        algo = ModularEmaMonitorAlgo(self._instance())

        actions = await algo.evaluate(build_snapshot(fast=None, slow=100.0), state={})

        self.assertEqual(actions[0].action_type, "noop")

    def test_builtin_registry_registration(self):
        registry = AlgoRegistry()
        register_builtin_algos(registry)

        self.assertTrue(registry.has("ema_monitor"))
