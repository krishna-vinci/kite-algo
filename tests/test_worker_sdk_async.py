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
