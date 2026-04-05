import unittest
from datetime import timezone

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from broker_api.candle_aggregator import CandleAggregator


class _FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def publish(self, channel, payload):
        return 1


class CandleAggregatorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_tick_processes_iso_exchange_timestamp(self):
        aggregator = CandleAggregator("test-key")
        aggregator.redis = _FakeRedis()
        aggregator.intervals = ["minute"]
        aggregator.source = "market_runtime"

        await aggregator._process_ticks(
            [
                {
                    "instrument_token": 256265,
                    "last_price": 22450.5,
                    "exchange_timestamp": "2026-04-05T09:45:10+05:30",
                    "volume_traded": 100,
                    "oi": 50,
                }
            ]
        )

        state = aggregator.candle_states[(256265, "minute")]
        self.assertEqual(state.open, 22450.5)
        self.assertEqual(state.close, 22450.5)
        self.assertEqual(state.oi, 50)
        self.assertEqual(state.bucket_start_ts.tzinfo, timezone.utc)

    def test_normalize_tick_timestamp_handles_iso_string(self):
        aggregator = CandleAggregator("test-key")
        ts = aggregator._normalize_tick_timestamp("2026-04-05T09:45:10+05:30")
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
