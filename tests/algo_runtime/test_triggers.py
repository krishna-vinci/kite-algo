import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import (  # noqa: E402
    CandleSeriesSpec,
    DependencySpec,
    IndicatorSpec,
    OrderScope,
    TriggerEvent,
    TriggerType,
)
from algo_runtime.triggers import trigger_matches  # noqa: E402


class TriggerMatchTests(unittest.TestCase):
    def test_tick_trigger_matches_only_declared_market_token(self):
        dependency_spec = DependencySpec(market_tokens={256265: "full"}, triggers={TriggerType.TICK})

        self.assertTrue(trigger_matches(dependency_spec, TriggerEvent(type="tick", token=256265)))
        self.assertFalse(trigger_matches(dependency_spec, TriggerEvent(type="tick", token=12345)))

    def test_candle_close_matches_declared_series_or_indicator(self):
        dependency_spec = DependencySpec(
            candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=30)],
            indicators=[IndicatorSpec(kind="ema", token=12345, timeframe="15minute", params={"length": 9})],
            triggers={TriggerType.CANDLE_CLOSE},
        )

        self.assertTrue(trigger_matches(dependency_spec, TriggerEvent(type="candle_close", token=256265, timeframe="5minute")))
        self.assertTrue(trigger_matches(dependency_spec, TriggerEvent(type="candle_close", token=12345, timeframe="15minute")))
        self.assertFalse(trigger_matches(dependency_spec, TriggerEvent(type="candle_close", token=256265, timeframe="15minute")))

    def test_order_update_requires_non_none_order_scope(self):
        dependency_spec = DependencySpec(
            account_scope="kite:AB1234",
            order_scope=OrderScope.ACCOUNT_RELEVANT,
            triggers={TriggerType.ORDER_UPDATE},
        )

        self.assertTrue(trigger_matches(dependency_spec, TriggerEvent(type="order_update", account_id="kite:AB1234")))
        self.assertFalse(trigger_matches(dependency_spec, TriggerEvent(type="order_update", account_id="kite:OTHER")))
