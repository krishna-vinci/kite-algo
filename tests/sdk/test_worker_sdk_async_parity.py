from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    AsyncKiteAlgoWorkerClient,
    BackendProtection,
    KiteAlgoWorkerError,
)
from kite_algo_worker.options import AsyncOptionWorkerClient  # noqa: E402


class Response:
    def __init__(self, payload=None, *, text: str | None = None, status_code: int = 200, lines=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else (str(payload) if payload is not None else "")
        self.content = self.text.encode("utf-8") if self.text else b"{}"
        self._lines = list(lines or [])
        self.read = False

    def json(self):
        return self._payload

    async def aread(self):
        self.read = True
        return self.content

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class StreamContext:
    def __init__(self, response: Response):
        self.response = response
        self.closed = False

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True


def _install_httpx_stub(responses=None, streams=None):
    module = types.ModuleType("httpx")
    module.calls = []
    module.stream_contexts = []
    response_queue = list(responses or [])
    stream_queue = list(streams or [])

    class AsyncClient:
        def __init__(self, headers=None, timeout=None):
            self.headers = headers or {}
            self.timeout = timeout

        async def request(self, method, url, **kwargs):
            module.calls.append({"method": method, "url": url, "kwargs": kwargs})
            return response_queue.pop(0)

        def stream(self, method, url, **kwargs):
            module.calls.append({"method": method, "url": url, "kwargs": kwargs, "stream": True})
            context = StreamContext(stream_queue.pop(0))
            module.stream_contexts.append(context)
            return context

        async def aclose(self):
            module.closed = getattr(module, "closed", 0) + 1

    module.AsyncClient = AsyncClient
    sys.modules["httpx"] = module
    return module


def _ok_response(payload=None):
    return Response(payload if payload is not None else {"ok": True}, text="json")


def test_async_lifecycle_and_mutations_match_sync_contract():
    safety = {
        "strategy_run_id": "run-1",
        "can_trade": True,
        "run_status": "open",
    }
    httpx = _install_httpx_stub(
        [
            _ok_response(),
            _ok_response(),
            _ok_response({"worker_session_nonce": "nonce-1"}),
            _ok_response(),
            Response(safety, text="json"),
            _ok_response(),
            _ok_response(),
            _ok_response(),
            _ok_response(),
            _ok_response(),
            _ok_response(),
            _ok_response(),
            _ok_response(),
        ]
    )

    async def main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        await sdk.heartbeat(status="healthy")
        await sdk.create_run(template_id="demo", account_scope="kite:paper-a")
        await sdk.claim_session("run-1")
        await sdk.run_heartbeat("run-1", session_nonce="nonce-1")
        await sdk.safety_check("run-1")
        await sdk.cancel_order("run-1", "ord-1")
        await sdk.modify_order("run-1", "ord-1", {"quantity": 2})
        await sdk.place_order("run-1", {"quantity": 1}, "run-1:entry:1", session_nonce="nonce-1")
        await sdk.place_basket("run-1", [{"quantity": 1}], "run-1:basket:1", session_nonce="nonce-1")
        await sdk.patch_risk("run-1", {"max_loss": 1000}, session_nonce="nonce-1")
        await sdk.update_backend_protection(
            "run-1", BackendProtection(enabled=False), session_nonce="nonce-1"
        )
        await sdk.exit_run("run-1", idempotency_key="run-1:exit:1", session_nonce="nonce-1")
        await sdk.release_session("run-1", session_nonce="nonce-1")
        await sdk.close()

    asyncio.run(main())

    assert httpx.calls[1]["url"].endswith("/worker/runs")
    assert httpx.calls[3]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert httpx.calls[7]["kwargs"]["json"]["intent_type"] == "place_order"
    assert httpx.calls[12]["method"] == "DELETE"


def test_async_execution_observability_and_text_transport():
    httpx = _install_httpx_stub(
        [
            _ok_response({"strategy_run_id": "run-1", "order_id": "ord-1", "history": []}),
            _ok_response({"strategy_run_id": "run-1", "baskets": []}),
            _ok_response({"basket_execution_id": "bex-1", "strategy_run_id": "run-1", "status": "active"}),
            _ok_response({"strategy_run_id": "run-1", "brackets": []}),
            _ok_response({"bracket_intent_id": "brk-1", "strategy_run_id": "run-1", "status": "armed"}),
            _ok_response({"strategy_run_id": "run-1", "after_cursor": 0, "last_cursor": 0, "events": []}),
            Response({"unused": True}, text="symbol,metric\nINFY,revenue\n"),
        ]
    )

    async def main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        await sdk.get_order_history("run-1", "ord-1")
        await sdk.list_baskets("run-1")
        await sdk.get_basket("run-1", "bex-1")
        await sdk.list_brackets("run-1")
        await sdk.get_bracket("run-1", "brk-1")
        await sdk.list_execution_events("run-1")
        csv_text = await sdk.export_fundamentals_csv(symbols=["INFY"])
        await sdk.close()
        return csv_text

    assert asyncio.run(main()) == "symbol,metric\nINFY,revenue\n"
    assert httpx.calls[-1]["kwargs"]["params"]["symbols"] == ["INFY"]


def test_async_sse_closes_stream_context_and_yields_events():
    stream = Response(
        lines=[
            "event: execution",
            'data: {"cursor": 4, "event_type": "order.updated"}',
            "",
            "event: end",
            'data: {"done": true}',
        ]
    )
    httpx = _install_httpx_stub(streams=[stream])

    async def main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        events = []
        async for event in sdk.stream_execution_events("run-1", after_cursor=3):
            events.append(event)
        await sdk.close()
        return events

    assert asyncio.run(main()) == [{"cursor": 4, "event_type": "order.updated"}]
    assert httpx.stream_contexts[0].closed is True


def test_async_error_parsing_reads_unbuffered_response_body_first():
    response = Response({"detail": "not allowed"}, text="json", status_code=403)
    _install_httpx_stub([response])

    async def main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        with pytest.raises(KiteAlgoWorkerError, match="not allowed"):
            await sdk.get_run("run-1")
        await sdk.close()

    asyncio.run(main())
    assert response.read is True

    stream_response = Response({"detail": "stream not allowed"}, text="json", status_code=403)
    httpx = _install_httpx_stub(streams=[stream_response])

    async def stream_main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        with pytest.raises(KiteAlgoWorkerError, match="stream not allowed"):
            async for _event in sdk.stream_execution_events("run-1"):
                pass
        await sdk.close()

    asyncio.run(stream_main())
    assert stream_response.read is True
    assert httpx.stream_contexts[0].closed is True


def test_async_options_namespace_covers_worker_options_routes():
    responses = [_ok_response() for _ in range(18)]
    responses[1] = _ok_response({"underlying": "NIFTY", "expiries": ["2026-09-10"]})
    httpx = _install_httpx_stub(responses)

    async def main():
        sdk = AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
        await sdk.options.ensure_session("NIFTY")
        await sdk.options.list_expiries("NIFTY")
        await sdk.options.get_chain("NIFTY")
        await sdk.options.get_mini_chain("NIFTY")
        await sdk.options.get_greeks("NIFTY")
        await sdk.options.resolve_contracts("NIFTY", {"legs": []})
        await sdk.options.get_pcr("NIFTY")
        await sdk.options.get_max_pain("NIFTY")
        await sdk.options.preview_strategy({"strategy_name": "iron_condor", "legs": []})
        await sdk.options.create_run(strategy_name="iron_condor", product="MIS", legs=[])
        await sdk.options.preview_run_entry("run-1")
        await sdk.options.enter("run-1", safety_token="safe-1", session_nonce="nonce-1")
        await sdk.options.preview_exit("run-1")
        await sdk.options.exit("run-1", safety_token="safe-1", session_nonce="nonce-1")
        await sdk.options.get_run_state("run-1")
        await sdk.options.update_protection("run-1", {"rules": []}, session_nonce="nonce-1")
        await sdk.options.get_protection_state("run-1")
        await sdk.options.replay_protection("run-1", [{"combined_premium": 100.0}])
        await sdk.close()

    asyncio.run(main())

    assert len(httpx.calls) == 18
    assert all("/api/algo-workers/worker/options/" in item["url"] for item in httpx.calls)
    assert httpx.calls[11]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}


def test_async_options_preview_entry_preserves_sync_compatibility_path():
    calls = []

    class FakeAsyncWorker:
        async def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"unexpected": True}

        async def preview_basket(self, strategy_run_id, orders, *, metadata=None, all_or_none=False):
            calls.append(
                {
                    "strategy_run_id": strategy_run_id,
                    "orders": list(orders),
                    "metadata": dict(metadata or {}),
                    "all_or_none": all_or_none,
                }
            )
            return {"preview": {"cost_contract": {"margin_required": 1000}}}

    async def main():
        return await AsyncOptionWorkerClient(FakeAsyncWorker()).preview_entry(
            "run-compat",
            [{"tradingsymbol": "NIFTY26MAY25000CE", "quantity": 75}],
            metadata={"flow": "compat"},
            all_or_none=True,
        )

    assert asyncio.run(main()) == {"preview": {"cost_contract": {"margin_required": 1000}}}
    assert calls == [
        {
            "strategy_run_id": "run-compat",
            "orders": [{"tradingsymbol": "NIFTY26MAY25000CE", "quantity": 75}],
            "metadata": {"flow": "compat"},
            "all_or_none": True,
        }
    ]
