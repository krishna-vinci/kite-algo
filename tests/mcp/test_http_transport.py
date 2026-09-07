from __future__ import annotations

import json
import asyncio
import socket

import httpx
import pytest
import uvicorn

from kite_algo_mcp.config import ConfigurationError, MCPConfig, load_config
from kite_algo_mcp.http_transport import create_http_app


CLIENT_TOKEN = "test-client-token-with-at-least-32-characters"


class Worker:
    async def health(self):
        return {"status": "ok", "allowed_actions": ["market:read", "runs:read"]}

    async def search_tickers(self, query, exchange=None, limit=20):
        return {"items": [{"symbol": query.upper()}]}


def config(**overrides):
    fields = dict(api_url="http://127.0.0.1:18777", worker_token="worker-only-secret",
                  transport="http", http_token=CLIENT_TOKEN,
                  allowed_hosts=("192.168.1.100",))
    return MCPConfig(**(fields | overrides))


def test_http_config_requires_separate_strong_token_and_hosts():
    for overrides in ({"http_token": ""}, {"http_token": "short"},
                      {"worker_token": CLIENT_TOKEN}, {"allowed_hosts": ()},
                      {"allowed_hosts": ("*",)}, {"port": 0}, {"port": 65536},
                      {"transport": "sse"}):
        with pytest.raises(ConfigurationError):
            config(**overrides)
    assert "worker-only-secret" not in repr(config())
    assert CLIENT_TOKEN not in repr(config())


def test_docker_backend_http_requires_explicit_opt_in():
    with pytest.raises(ConfigurationError):
        config(api_url="http://finance-app:8777")
    assert config(api_url="http://finance-app:8777", allow_insecure_backend_http=True).api_url == "http://finance-app:8777"


def test_secret_files_and_env_transport(tmp_path):
    token_file = tmp_path / "client-token"
    token_file.write_text(CLIENT_TOKEN + "\n")
    env = {"KITE_MCP_API_URL": "http://finance-app:8777", "KITE_MCP_WORKER_TOKEN": "worker-secret",
           "KITE_MCP_TRANSPORT": "http", "KITE_MCP_HTTP_TOKEN_FILE": str(token_file),
           "KITE_MCP_ALLOWED_HOSTS": "192.168.1.100,kite.local",
           "KITE_MCP_ALLOW_INSECURE_BACKEND_HTTP": "true"}
    result = load_config(env)
    assert result.http_token == CLIENT_TOKEN
    assert result.host == "0.0.0.0"
    assert result.port == 8788
    assert result.allowed_hosts == ("192.168.1.100", "kite.local")
    with pytest.raises(ConfigurationError):
        load_config(env | {"KITE_MCP_HTTP_TOKEN": CLIENT_TOKEN})


@pytest.mark.asyncio
async def test_http_auth_protocol_and_origin_enforcement():
    app = create_http_app(config(), client=Worker())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.100:18788") as client:
            headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
            body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "lan-test", "version": "1"}}}
            assert (await client.post("/mcp", headers=headers, json=body)).status_code == 401
            assert (await client.post("/mcp", headers=headers | {"Authorization": "Bearer wrong"}, json=body)).status_code == 401
            headers["Authorization"] = "Bearer " + CLIENT_TOKEN
            response = await client.post("/mcp", headers=headers, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["result"]["serverInfo"]["name"] == "kite-algo-mcp"
            listing = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in listing.json()["result"]["tools"]}
            assert "search_instruments" in names
            assert "place_order" not in names
            call = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3,
                "method": "tools/call", "params": {"name": "search_instruments", "arguments": {"request": {"query": "abc"}}}})
            assert call.status_code == 200
            assert "ABC" in json.dumps(call.json())
            assert (await client.post("/mcp", headers=headers | {"Origin": "http://evil.example"}, json=body)).status_code == 403
            assert (await client.post("/mcp", headers=headers | {"Host": "evil.example"}, json=body)).status_code == 421
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert CLIENT_TOKEN not in response.text + health.text + listing.text + call.text


def test_http_entry_point_selects_transport(monkeypatch):
    import kite_algo_mcp.__main__ as entry
    calls = []
    monkeypatch.setattr(entry, "load_config", lambda: config())
    monkeypatch.setattr(entry, "run_http", lambda cfg: calls.append(cfg.transport))
    assert entry.main() == 0
    assert calls == ["http"]


@pytest.mark.asyncio
async def test_two_network_clients_use_streamable_http():
    from fastmcp import Client

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_http_app(config(), client=Worker()),
        log_level="critical", access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async def wait_started():
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_started(), timeout=10)

        async def device(query):
            async with Client(f"http://127.0.0.1:{port}/mcp", auth=CLIENT_TOKEN) as client:
                names = {tool.name for tool in await client.list_tools()}
                assert "search_instruments" in names
                result = await client.call_tool("search_instruments", {"request": {"query": query}})
                assert query.upper() in str(result)

        await asyncio.wait_for(asyncio.gather(device("abc"), device("xyz")), timeout=20)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            sock.close()
