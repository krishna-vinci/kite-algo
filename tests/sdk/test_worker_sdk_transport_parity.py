from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx as real_httpx
import requests

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, AsyncKiteAlgoWorkerClient, KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.options.async_client import AsyncOptionWorkerClient  # noqa: E402


_BASE_URL = "http://localhost:8000"
_TOKEN = "kwa_test"
_SELECTED_HEADERS = (
    "authorization",
    "accept",
    "content-type",
    "x-worker-session-nonce",
)


def _canonical_request(request: Any) -> dict[str, Any]:
    if isinstance(request, real_httpx.Request):
        method = request.method
        url = str(request.url)
        headers = request.headers
        body = request.content
    else:
        method = request.method
        url = request.url
        headers = request.headers
        body = request.body

    if isinstance(body, str):
        body = body.encode("utf-8")
    decoded_body = json.loads(body.decode("utf-8")) if body else None
    return {
        "method": method,
        "path": urlsplit(url).path,
        "query": tuple(parse_qsl(urlsplit(url).query, keep_blank_values=True)),
        "body": decoded_body,
        "headers": {
            name: headers.get(name)
            for name in _SELECTED_HEADERS
            if headers.get(name) is not None
        },
    }


def _response_content(path: str) -> tuple[bytes, str]:
    if path.endswith("/fundamentals/export.csv"):
        return b"symbol,value\nINFY,1\n", "text/csv"
    if path.endswith("/execution-events/stream"):
        return (
            b'event: execution\ndata: {"cursor": 1, "event_type": "order.updated"}\n\n'
            b"event: end\ndata: {}\n\n",
            "text/event-stream",
        )
    return b'{"ok": true}', "application/json"


def _run_sync_requests() -> list[dict[str, Any]]:
    captured: list[Any] = []
    client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url=_BASE_URL, token=_TOKEN))

    def send(prepared: requests.PreparedRequest, **_: Any) -> requests.Response:
        captured.append(prepared)
        body, content_type = _response_content(urlsplit(prepared.url).path)
        response = requests.Response()
        response.status_code = 200
        response.url = prepared.url
        response.request = prepared
        response.headers["Content-Type"] = content_type
        response._content = body
        response._content_consumed = True
        response.encoding = "utf-8"
        return response

    client.session.send = send  # type: ignore[method-assign]
    client.list_execution_events("run-1")
    client.list_execution_events(
        "run-1",
        after_cursor=0,
        limit=1,
        basket_execution_id="basket-1",
        event_type="order.updated",
    )
    client.place_order(
        "run-1",
        {"symbol": "NSE:INFY", "quantity": 1},
        "run-1:entry:1",
        metadata={"strategy": "demo"},
        safety_token="safe-1",
        session_nonce="nonce-1",
    )
    assert client.export_fundamentals_csv(symbols=["INFY"]) == "symbol,value\nINFY,1\n"
    client.get_historical_candles("NSE:INFY", ingest=False, passthrough=False)
    list(
        client.stream_execution_events(
            "run-1",
            after_cursor=0,
            limit=0,
            basket_execution_id=None,
            event_type=None,
        )
    )
    return [_canonical_request(request) for request in captured]


def _new_async_client(handler: Any) -> AsyncKiteAlgoWorkerClient:
    """Build a valid SDK instance without depending on a test's httpx module state."""

    config = AlgoWorkerConfig(base_url=_BASE_URL, token=_TOKEN)
    client = object.__new__(AsyncKiteAlgoWorkerClient)
    object.__setattr__(client, "config", config)
    object.__setattr__(
        client,
        "client",
        real_httpx.AsyncClient(
            transport=real_httpx.MockTransport(handler),
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=config.timeout,
        ),
    )
    object.__setattr__(client, "options", AsyncOptionWorkerClient(client))
    return client


async def _run_async_requests(captured: list[Any]) -> None:
    def handler(request: real_httpx.Request) -> real_httpx.Response:
        captured.append(request)
        body, content_type = _response_content(request.url.path)
        return real_httpx.Response(
            200,
            content=body,
            headers={"Content-Type": content_type},
            request=request,
        )

    client = _new_async_client(handler)
    try:
        await client.list_execution_events("run-1")
        await client.list_execution_events(
            "run-1",
            after_cursor=0,
            limit=1,
            basket_execution_id="basket-1",
            event_type="order.updated",
        )
        await client.place_order(
            "run-1",
            {"symbol": "NSE:INFY", "quantity": 1},
            "run-1:entry:1",
            metadata={"strategy": "demo"},
            safety_token="safe-1",
            session_nonce="nonce-1",
        )
        assert await client.export_fundamentals_csv(symbols=["INFY"]) == "symbol,value\nINFY,1\n"
        await client.get_historical_candles("NSE:INFY", ingest=False, passthrough=False)
        events = []
        async for event in client.stream_execution_events(
            "run-1",
            after_cursor=0,
            limit=0,
            basket_execution_id=None,
            event_type=None,
        ):
            events.append(event)
        assert events == [{"cursor": 1, "event_type": "order.updated"}]
    finally:
        await client.close()


def test_real_transports_serialize_equivalent_reads_mutations_and_streams():
    sync_requests = _run_sync_requests()
    async_requests: list[Any] = []
    asyncio.run(_run_async_requests(async_requests))

    assert [_canonical_request(request) for request in async_requests] == sync_requests
    assert sync_requests[0]["query"] == (("after_cursor", "0"), ("limit", "200"))
    assert sync_requests[0]["query"] == tuple(
        pair for pair in sync_requests[0]["query"] if pair[0] not in {"basket_execution_id", "event_type"}
    )
    assert sync_requests[1]["query"] == (
        ("after_cursor", "0"),
        ("limit", "1"),
        ("basket_execution_id", "basket-1"),
        ("event_type", "order.updated"),
    )
    assert sync_requests[2]["headers"]["x-worker-session-nonce"] == "nonce-1"
    assert sync_requests[2]["body"]["safety_token"] == "safe-1"
    assert sync_requests[3]["query"] == (
        ("symbols", "INFY"),
        ("dataset", "fundamentals_features"),
        ("schema_version", "1"),
    )
    assert sync_requests[4]["query"] == (
        ("timeframe", "day"),
        ("ingest", "False"),
        ("passthrough", "False"),
        ("symbol", "NSE:INFY"),
    )
    assert sync_requests[5]["query"] == (("after_cursor", "0"), ("limit", "0"))
