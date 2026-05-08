import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.indicators import BuiltInIndicatorReader, compute_ema_series  # noqa: E402
from algo_runtime.models import IndicatorSpec  # noqa: E402


class IndicatorReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ema_indicator_uses_history_plus_latest_closed_without_duplicates(self):
        reader = BuiltInIndicatorReader()

        payload = await reader.get_indicator(
            IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 3}),
            candles={
                "history": [
                    {"ts": "2026-04-07T09:15:00+00:00", "close": 100},
                    {"ts": "2026-04-07T09:20:00+00:00", "close": 101},
                    {"ts": "2026-04-07T09:25:00+00:00", "close": 102},
                ],
                "latest_closed": {"ts": "2026-04-07T09:25:00+00:00", "close": 102},
            },
        )

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["history_len"], 3)
        self.assertAlmostEqual(payload["value"], compute_ema_series([100.0, 101.0, 102.0], 3)[-1])
        self.assertAlmostEqual(payload["previous"], compute_ema_series([100.0, 101.0, 102.0], 3)[-2])

    async def test_ema_indicator_reports_not_ready_until_length_met(self):
        reader = BuiltInIndicatorReader()

        payload = await reader.get_indicator(
            IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 5}),
            candles={"history": [{"ts": "2026-04-07T09:15:00+00:00", "close": 100}]},
        )

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["history_len"], 1)

    async def test_ema_indicator_orders_mixed_timestamp_formats_chronologically(self):
        reader = BuiltInIndicatorReader()

        payload = await reader.get_indicator(
            IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 3}),
            candles={
                "history": [
                    {"ts": "2026-04-07T09:20:00Z", "close": 101},
                    {"ts": "2026-04-07T09:15:00+00:00", "close": 100},
                ],
                "latest_closed": {"ts": "2026-04-07T09:25:00+00:00", "close": 102},
            },
        )

        self.assertAlmostEqual(payload["value"], compute_ema_series([100.0, 101.0, 102.0], 3)[-1])


class IndicatorMathTests(unittest.TestCase):
    def test_compute_ema_series_rejects_invalid_length(self):
        with self.assertRaises(ValueError):
            compute_ema_series([1.0, 2.0], 0)
