import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.algo_runtime.dependencies import DependencyAggregator  # noqa: E402
from backend.algo_runtime.models import CandleSeriesSpec, DependencySpec, IndicatorSpec, TriggerType  # noqa: E402


class DependencyAggregatorTests(unittest.TestCase):
    def test_summarize_merges_dependency_views(self):
        aggregator = DependencyAggregator()
        left = DependencySpec(
            market_tokens={256265: "full"},
            candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=50)],
            triggers={TriggerType.TICK},
            account_scope="kite:AB1234",
        )
        right = DependencySpec(
            indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 20})],
            triggers={TriggerType.CANDLE_CLOSE},
            account_scope="kite:AB1234",
        )

        summary = aggregator.summarize([left, right])

        self.assertIn(256265, summary["market_tokens"])
        self.assertEqual(summary["account_scopes"], {"kite:AB1234"})
        self.assertIn("256265:5minute:50:0", summary["candle_series"])
        self.assertTrue(any(key.startswith("ema:256265:5minute") for key in summary["indicator_keys"]))
        self.assertEqual(summary["triggers"], {"tick", "candle_close"})

    def test_summarize_handles_generator_inputs(self):
        aggregator = DependencyAggregator()

        summary = aggregator.summarize(
            DependencySpec(account_scope=scope, triggers={TriggerType.TICK})
            for scope in ["kite:AB1234", "kite:XY9999"]
        )

        self.assertEqual(summary["account_scopes"], {"kite:AB1234", "kite:XY9999"})
