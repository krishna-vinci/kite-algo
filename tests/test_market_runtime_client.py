import unittest
from datetime import datetime, timezone
from importlib.util import find_spec


if find_spec("redis") is not None:
    from broker_api.market_runtime_client import MarketDataRuntime
else:
    MarketDataRuntime = None


@unittest.skipIf(MarketDataRuntime is None, "redis package not installed in test environment")
class MarketDataRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MarketDataRuntime()

    def test_get_websocket_status_maps_runtime_status(self):
        self.runtime.runtime_status = {"status": "healthy"}
        self.assertEqual(self.runtime.get_websocket_status(), "CONNECTED")

        self.runtime.runtime_status = {"status": "waiting_for_token"}
        self.assertEqual(self.runtime.get_websocket_status(), "WAITING_FOR_TOKEN")

    def test_normalize_tick_payload_parses_iso_timestamps(self):
        tick = self.runtime._normalize_tick_payload(
            {
                "instrument_token": "256265",
                "last_price": "24850.35",
                "exchange_timestamp": "2026-04-05T04:15:10+00:00",
                "last_trade_time": "2026-04-05T04:15:09+00:00",
            }
        )
        self.assertIsNotNone(tick)
        assert tick is not None
        self.assertEqual(tick["instrument_token"], 256265)
        self.assertEqual(tick["last_price"], 24850.35)
        self.assertIsInstance(tick["exchange_timestamp"], datetime)
        self.assertEqual(tick["exchange_timestamp"].tzinfo, timezone.utc)

    def test_normalize_order_update_payload_uses_runtime_shape(self):
        payload = self.runtime._normalize_order_update_payload(
            {
                "order_id": "123",
                "status": "COMPLETE",
                "exchange": "NFO",
                "tradingsymbol": "NIFTY26APR22500CE",
                "instrument_token": 101,
                "quantity": 50,
                "filled_quantity": 50,
                "average_price": 125.5,
                "order_timestamp": "2026-04-05T04:15:10+00:00",
            }
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["order_id"], "123")
        self.assertEqual(payload["instrument_token"], 101)
        self.assertEqual(payload["status"], "COMPLETE")
        self.assertEqual(payload["order_timestamp"], "2026-04-05T04:15:10+00:00")


if __name__ == "__main__":
    unittest.main()
