from __future__ import annotations

import asyncio
import json

import pytest
import requests

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from kite_algo_worker import AlgoWorkerConfig, AsyncKiteAlgoWorkerClient, KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.options.async_client import AsyncOptionWorkerClient  # noqa: E402


def _sync_client(calls: list[dict]):
    client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))

    def request(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = json.dumps({"items": [], "next_cursor": None}).encode("utf-8")
        response._content_consumed = True
        return response

    client.session.request = request  # type: ignore[method-assign]
    return client


class _AsyncResponse:
    status_code = 200
    content = b'{"items": [], "next_cursor": null}'
    text = '{"items": [], "next_cursor": null}'

    @staticmethod
    def json():
        return {"items": [], "next_cursor": None}


class _AsyncTransport:
    def __init__(self, calls):
        self.calls = calls

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _AsyncResponse()

    async def aclose(self):
        return None


def _async_client(calls: list[dict]) -> AsyncKiteAlgoWorkerClient:
    config = AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")
    client = object.__new__(AsyncKiteAlgoWorkerClient)
    object.__setattr__(client, "config", config)
    object.__setattr__(client, "client", _AsyncTransport(calls))
    object.__setattr__(client, "options", AsyncOptionWorkerClient(client))
    return client


def test_sync_list_runs_uses_collection_route_and_omits_none_cursor():
    calls: list[dict] = []
    client = _sync_client(calls)

    assert client.list_runs() == {"items": [], "next_cursor": None}
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs"
    assert calls[0]["kwargs"]["params"] == {"limit": 25}

    client.list_runs(limit=7, cursor="opaque-cursor")
    assert calls[1]["kwargs"]["params"] == {"limit": 7, "cursor": "opaque-cursor"}


def test_async_list_runs_matches_sync_query_serialization():
    calls: list[dict] = []
    client = _async_client(calls)

    async def run():
        try:
            assert await client.list_runs() == {"items": [], "next_cursor": None}
            assert await client.list_runs(limit=7, cursor="opaque-cursor") == {"items": [], "next_cursor": None}
        finally:
            await client.close()

    asyncio.run(run())
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs"
    assert calls[0]["kwargs"]["params"] == {"limit": 25}
    assert calls[1]["kwargs"]["params"] == {"limit": 7, "cursor": "opaque-cursor"}


@pytest.mark.parametrize("limit", [0, 101, True])
def test_list_runs_rejects_limits_outside_backend_contract(limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")).list_runs(limit=limit)

    client = _async_client([])

    async def run():
        with pytest.raises(ValueError, match="between 1 and 100"):
            await client.list_runs(limit=limit)
        await client.close()

    asyncio.run(run())
