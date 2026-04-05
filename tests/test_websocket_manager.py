import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.websocket_manager import WebSocketManager


class FakeKiteTicker:
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self.connected = True
        self.subscribe_calls = []
        self.unsubscribe_calls = []
        self.set_mode_calls = []

    def connect(self, threaded=True):
        self.connected = True

    def stop(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def subscribe(self, tokens):
        self.subscribe_calls.append(list(tokens))

    def unsubscribe(self, tokens):
        self.unsubscribe_calls.append(list(tokens))

    def set_mode(self, mode, tokens):
        self.set_mode_calls.append((mode, list(tokens)))


class WebSocketManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("broker_api.websocket_manager.KiteTicker", FakeKiteTicker)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.manager = WebSocketManager("api-key", "token", asyncio.get_running_loop())
        self.manager.main_event_loop.call_soon_threadsafe = lambda callback, *args: callback(*args)
        self.manager._broadcast_status_async = MagicMock()
        self.manager.send_latest_ticks_to_all_clients = AsyncMock()

    async def test_external_union_keeps_client_subscription_and_downgrades_mode(self):
        client = MagicMock()
        client.subscriptions = {101: "quote"}
        self.manager.clients = {object(): client}
        self.manager.token_refcount = {101: 1}
        self.manager.token_mode_agg = {101: "quote"}

        await self.manager.set_desired_tokens_union({101, 202})

        self.assertEqual(self.manager._desired_tokens_union, {101, 202})
        self.assertEqual(self.manager.token_mode_agg[101], "full")
        self.assertEqual(self.manager.token_mode_agg[202], "full")
        self.assertEqual(self.manager.kws.subscribe_calls, [[202]])
        self.assertIn(("full", [202]), self.manager.kws.set_mode_calls)
        self.assertIn(("full", [101]), self.manager.kws.set_mode_calls)

        self.manager._last_converge_ts = 0.0
        await self.manager.set_desired_tokens_union({202})

        self.assertEqual(self.manager._desired_tokens_union, {202})
        self.assertEqual(self.manager.kws.unsubscribe_calls, [])
        self.assertEqual(self.manager.token_mode_agg[101], "quote")
        self.assertIn(("quote", [101]), self.manager.kws.set_mode_calls)

    async def test_on_connect_resubscribes_clients_and_external_tokens(self):
        client = MagicMock()
        client.subscriptions = {101: "quote"}
        self.manager.clients = {object(): client}
        self.manager.token_refcount = {101: 1}
        self.manager.token_mode_agg = {101: "quote", 202: "full"}
        self.manager._desired_tokens_union = {202}

        self.manager.on_connect(None, None)
        await asyncio.sleep(0)

        self.assertEqual(self.manager.websocket_status, "CONNECTED")
        self.assertEqual(self.manager.kws.subscribe_calls[-1], [101, 202])
        self.assertIn(("quote", [101]), self.manager.kws.set_mode_calls)
        self.assertIn(("full", [202]), self.manager.kws.set_mode_calls)
        self.manager._broadcast_status_async.assert_called_once_with("CONNECTED")
        self.manager.send_latest_ticks_to_all_clients.assert_awaited_once()
