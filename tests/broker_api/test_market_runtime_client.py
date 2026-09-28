import asyncio
import unittest
from datetime import datetime, timezone
from importlib.util import find_spec
from unittest.mock import AsyncMock, patch


if find_spec("redis") is not None:
    from backend.broker_api.orders.market_runtime_client import MarketDataRuntime
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

    def test_tick_listeners_receive_ticks_and_a_failing_listener_is_isolated(self):
        seen = []

        def boom(_token, _tick):
            raise RuntimeError("listener bug")

        self.runtime.add_tick_listener(boom)
        remove = self.runtime.add_tick_listener(
            lambda token, tick: seen.append((token, tick.get("last_price")))
        )
        asyncio.run(
            self.runtime._handle_tick_message(
                {"instrument_token": 256265, "last_price": 25000.5}
            )
        )
        self.assertEqual(seen, [(256265, 25000.5)])
        self.assertEqual(self.runtime.latest_ticks[256265]["last_price"], 25000.5)
        remove()
        asyncio.run(
            self.runtime._handle_tick_message(
                {"instrument_token": 256265, "last_price": 25001.0}
            )
        )
        self.assertEqual(len(seen), 1)

    def test_tick_subscriptions_deliver_in_order_and_close_removes_listener(self):
        previous_count = self.runtime.tick_listener_count
        subscription = self.runtime.subscribe_ticks(maxsize=4)
        asyncio.run(
            self.runtime._handle_tick_message(
                {"instrument_token": 256265, "last_price": 25000.5}
            )
        )
        asyncio.run(
            self.runtime._handle_tick_message(
                {"instrument_token": 256265, "last_price": 25001.0}
            )
        )
        self.assertEqual(subscription.get_nowait()["last_price"], 25000.5)
        self.assertEqual(subscription.get_nowait()["last_price"], 25001.0)
        subscription.close()
        self.assertEqual(self.runtime.tick_listener_count, previous_count)
        asyncio.run(
            self.runtime._handle_tick_message(
                {"instrument_token": 256265, "last_price": 25002.0}
            )
        )
        self.assertIsNone(subscription.get_nowait())

    def test_tick_subscription_drops_oldest_and_counts(self):
        subscription = self.runtime.subscribe_ticks(maxsize=2)
        for price in (25000.5, 25001.0, 25002.0):
            asyncio.run(
                self.runtime._handle_tick_message(
                    {"instrument_token": 256265, "last_price": price}
                )
            )
        self.assertEqual(subscription.dropped, 1)
        self.assertEqual(subscription.get_nowait()["last_price"], 25001.0)
        self.assertEqual(subscription.get_nowait()["last_price"], 25002.0)
        self.assertEqual(self.runtime.dropped, 1)
        self.assertEqual(self.runtime.tick_status_meta()["tick_listener_count"], 1)
        subscription.close()
        self.assertEqual(self.runtime.dropped, 1)

    def test_order_update_listeners_are_called(self):
        seen = []
        self.runtime.add_order_update_listener(
            lambda update: seen.append(update.get("order_id"))
        )
        with (
            patch(
                "backend.broker_api.orders.market_runtime_client.order_event_runtime.ingest_ws_event",
                new=AsyncMock(return_value={"duplicate": False, "canonical_event_id": "event-1"}),
            ),
            patch(
                "backend.broker_api.orders.market_runtime_client.publish_event",
                new=AsyncMock(),
            ),
        ):
            asyncio.run(
                self.runtime._handle_order_update_message(
                    {"order_id": "order-1", "status": "COMPLETE"}
                )
            )

        self.assertEqual(seen, ["order-1"])


if __name__ == "__main__":
    unittest.main()
