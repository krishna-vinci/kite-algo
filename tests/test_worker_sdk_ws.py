import asyncio
import sys
import types
from pathlib import Path

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.kite_orders", None)


def _install_websockets_stub(routes):
    module = types.ModuleType("websockets")
    module.calls = []

    class FakeWebSocket:
        def __init__(self, messages):
            self.messages = list(messages)
            self.closed = False

        async def recv(self):
            if not self.messages:
                raise RuntimeError("no more messages")
            return self.messages.pop(0)

        async def close(self):
            self.closed = True

    class _Connect:
        def __init__(self, websocket):
            self.websocket = websocket

        async def __aenter__(self):
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb):
            await self.websocket.close()

    def connect(url):
        module.calls.append(url)
        for needle, messages in routes.items():
            if needle in url:
                return _Connect(FakeWebSocket(messages))
        raise AssertionError(f"unexpected websocket url: {url}")

    module.connect = connect
    sys.modules["websockets"] = module
    return module

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import WorkerWebSocketClient  # noqa: E402


def test_websocket_stream_alias_reads_tick_snapshot():
    websockets = _install_websockets_stub(
        {"/worker/ws/market/ticks": ['{"event": "snapshot", "data": {"ticks": [{"instrument_token": 408065}]}}']}
    )

    async def main():
        client = WorkerWebSocketClient(base_url="ws://localhost:8765", token="kwa_test")
        async with client.stream(symbols=["NSE:NIFTY 50"], mode="quote") as stream:
            return await stream.recv()

    event = asyncio.run(main())

    assert event["event"] == "snapshot"
    assert event["data"]["ticks"][0]["instrument_token"] == 408065
    assert "token=kwa_test" in websockets.calls[0]
    assert "symbols=NSE%3ANIFTY+50" in websockets.calls[0]


def test_websocket_candle_and_run_pnl_streams_read_json_events():
    _install_websockets_stub(
        {
            "/worker/ws/market/candles": ['{"event": "snapshot", "data": {"current": {"interval": "5minute", "close": 123.4}}}'],
            "/worker/ws/runs/run-1/pnl": ['{"event": "snapshot", "data": {"totals": {"net_pnl": 12.5}}}'],
        }
    )

    async def main():
        client = WorkerWebSocketClient(base_url="ws://localhost:8765", token="kwa_test")
        async with client.stream_candles(symbol="NSE:INFY") as candle_stream:
            candle = await candle_stream.recv()
        async with client.stream_run_pnl("run-1") as pnl_stream:
            pnl = await pnl_stream.recv()
        return candle, pnl

    candle, pnl = asyncio.run(main())

    assert candle["data"]["current"]["close"] == 123.4
    assert pnl["data"]["totals"]["net_pnl"] == 12.5
