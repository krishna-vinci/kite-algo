import asyncio
import sys
import types
from pathlib import Path

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.kite_orders", None)


def _install_httpx_stub(responses):
    module = types.ModuleType("httpx")
    module.calls = []
    module.closed = 0

    class AsyncClient:
        def __init__(self, headers=None, timeout=None):
            self.headers = headers or {}
            self.timeout = timeout

        async def request(self, method, url, **kwargs):
            module.calls.append({"method": method, "url": url, "kwargs": kwargs, "headers": dict(self.headers), "timeout": self.timeout})
            return responses.pop(0)

        async def aclose(self):
            module.closed += 1

    module.AsyncClient = AsyncClient
    sys.modules["httpx"] = module
    return module


SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig  # noqa: E402
from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient  # noqa: E402


def test_async_client_get_run_preview_and_close():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1"}', text='{"strategy_run_id": "run-1"}', json=lambda: {"strategy_run_id": "run-1"}),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}}',
                text='{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}}',
                json=lambda: {"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}},
            ),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=2)) as client:
            run = await client.get_run("run-1")
            preview = await client.preview_order("run-1", {"exchange": "NSE", "tradingsymbol": "INFY"})
            return run, preview

    run, preview = asyncio.run(main())

    assert run == {"strategy_run_id": "run-1"}
    assert preview["mode"] == "live"
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1"
    assert httpx.calls[1]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/preview/order"
    assert httpx.closed == 1


def test_async_client_core_parity_methods_use_worker_endpoints():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(status_code=200, content=b'{"status": "ok"}', text='{"status": "ok"}', json=lambda: {"status": "ok"}),
            types.SimpleNamespace(status_code=200, content=b'{"account_scope": "kite:paper-a"}', text='{"account_scope": "kite:paper-a"}', json=lambda: {"account_scope": "kite:paper-a"}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "orders": []}', text='{"strategy_run_id": "run-1", "orders": []}', json=lambda: {"strategy_run_id": "run-1", "orders": []}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "trades": []}', text='{"strategy_run_id": "run-1", "trades": []}', json=lambda: {"strategy_run_id": "run-1", "trades": []}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}', text='{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}', json=lambda: {"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as client:
            health = await client.health()
            funds = await client.get_funds(mode="paper", account_scope="kite:paper-a")
            orders = await client.list_orders("run-1")
            trades = await client.list_trades("run-1")
            preview = await client.preview_basket(
                "run-1",
                [
                    {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "product": "CNC",
                        "order_type": "MARKET",
                        "quantity": 1,
                    }
                ],
            )
            return health, funds, orders, trades, preview

    health, funds, orders, trades, preview = asyncio.run(main())

    assert health == {"status": "ok"}
    assert funds["account_scope"] == "kite:paper-a"
    assert orders["strategy_run_id"] == "run-1"
    assert trades["strategy_run_id"] == "run-1"
    assert preview["preview"]["intent_type"] == "place_basket"
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/health"
    assert httpx.calls[1]["kwargs"]["params"] == {"mode": "paper", "account_scope": "kite:paper-a"}
    assert httpx.calls[2]["url"] == "http://localhost:8000/api/algo-workers/worker/orders"
    assert httpx.calls[2]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert httpx.calls[3]["url"] == "http://localhost:8000/api/algo-workers/worker/trades"
    assert httpx.calls[3]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert httpx.calls[4]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/preview/basket"
    assert httpx.calls[4]["kwargs"]["json"] == {
        "orders": [
            {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "product": "CNC",
                "order_type": "MARKET",
                "quantity": 1,
            }
        ],
        "metadata": {},
        "all_or_none": False,
    }
    assert httpx.closed == 1
