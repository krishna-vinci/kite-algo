import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.algo_runtime.models import (  # noqa: E402
    AlgoInstance,
    CandleSeriesSpec,
    DependencySpec,
    ExecutionMode,
    IndicatorSpec,
    MarketDataMode,
    OrderScope,
    OrderIntent,
    TriggerEvent,
    TriggerType,
)


class AlgoRuntimeModelTests(unittest.TestCase):
    def test_candle_series_normalizes_values(self):
        spec = CandleSeriesSpec(token="256265", timeframe=" 5Minute ", lookback=25)

        self.assertEqual(spec.token, 256265)
        self.assertEqual(spec.timeframe, "5minute")
        self.assertEqual(spec.lookback, 25)

    def test_dependency_spec_normalizes_market_tokens_and_triggers(self):
        dependency_spec = DependencySpec(
            market_tokens={"256265": "FULL"},
            triggers={"tick", TriggerType.CANDLE_CLOSE},
        )

        self.assertEqual(dependency_spec.market_tokens, {256265: MarketDataMode.FULL})
        self.assertEqual(dependency_spec.triggers, {TriggerType.TICK, TriggerType.CANDLE_CLOSE})

    def test_dependency_spec_merge_prefers_stronger_market_mode_and_dedupes(self):
        left = DependencySpec(
            market_tokens={256265: "ltp"},
            candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=20)],
            indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 20})],
            triggers={TriggerType.TICK},
        )
        right = DependencySpec(
            market_tokens={256265: "full"},
            candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=20)],
            indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 20})],
            triggers={TriggerType.CANDLE_CLOSE},
        )

        merged = left.merged_with(right)

        self.assertEqual(merged.market_tokens[256265], MarketDataMode.FULL)
        self.assertEqual(len(merged.candle_series), 1)
        self.assertEqual(len(merged.indicators), 1)
        self.assertEqual(merged.triggers, {TriggerType.TICK, TriggerType.CANDLE_CLOSE})

    def test_dependency_spec_rejects_invalid_order_scope(self):
        with self.assertRaises(ValueError):
            DependencySpec(order_scope="wrong")

    def test_dependency_spec_rejects_invalid_market_mode(self):
        with self.assertRaises(ValueError):
            DependencySpec(market_tokens={256265: "bad-mode"})

    def test_dependency_spec_defaults_to_none_order_scope(self):
        dependency_spec = DependencySpec()

        self.assertEqual(dependency_spec.order_scope, OrderScope.NONE)

    def test_algo_instance_requires_non_empty_identity_fields(self):
        instance = AlgoInstance(instance_id="algo-1", algo_type="index_stoploss")

        self.assertEqual(instance.instance_id, "algo-1")
        self.assertEqual(instance.algo_type, "index_stoploss")
        self.assertEqual(instance.execution_mode, ExecutionMode.LIVE)

    def test_algo_instance_accepts_execution_mode(self):
        instance = AlgoInstance(instance_id="algo-1", algo_type="index_stoploss", execution_mode="paper")

        self.assertEqual(instance.execution_mode, ExecutionMode.PAPER)

    def test_trigger_event_accepts_alias_type_field(self):
        trigger = TriggerEvent(type="tick", token=256265)

        self.assertEqual(trigger.trigger_type, TriggerType.TICK)
        self.assertEqual(trigger.token, 256265)

    def test_order_intent_preserves_payload_and_dedupe_key(self):
        action = OrderIntent(intent_type="exit_positions", payload={"strategy_id": "abc"}, dedupe_key="intent-1")

        self.assertEqual(action.intent_type, "exit_positions")
        self.assertEqual(action.payload["strategy_id"], "abc")
        self.assertEqual(action.dedupe_key, "intent-1")
