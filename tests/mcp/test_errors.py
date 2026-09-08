from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [
    (400, "invalid_request", False), (422, "invalid_request", False),
    (401, "backend_unauthorized", False), (403, "backend_unauthorized", False),
    (404, "not_found", False), (409, "conflict", False),
    (429, "rate_limited", True),
])
async def test_http_errors_are_typed_and_sanitized(status, code, retryable) -> None:
    class BackendFailure(Exception):
        status_code = status

    class Worker:
        async def health(self):
            return {"allowed_actions": ["market:read"]}

        async def search_tickers(self, *args):
            raise BackendFailure("private-worker-token private-upstream-body")

    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=Worker())
    async with Client(server) as client:
        result = await client.call_tool("search_instruments", {"request": {"query": "INFY"}}, raise_on_error=False)
        assert result.is_error
        text = result.content[0].text
        assert "private-worker-token" not in text
        assert "private-upstream-body" not in text
        error = json.loads(text)["error"]
        assert error["code"] == code
        assert error["retryable"] is retryable


class TimeoutWorker:
    async def health(self):
        return {"allowed_actions": ["intents:submit", "market:read", "runs:read"]}

    async def claim_session(self, run_id):
        return {"session_nonce": "hidden"}

    async def release_session(self, run_id, *, session_nonce):
        pass

    async def run_heartbeat(self, run_id, *, session_nonce, status):
        pass

    async def safety_check(self, run_id):
        return {"allowed": True}

    async def place_order(self, *args, **kwargs):
        raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_mutation_timeout_is_unknown_and_never_retried() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=TimeoutWorker())
    async with Client(server) as client:
        with pytest.raises(Exception, match="write_outcome_unknown"):
            try:
                await client.call_tool(
                    "place_order",
                    {"request": {"strategy_run_id": "run", "idempotency_key": "idem-1234", "order": {"symbol": "ABC", "transaction_type": "BUY", "quantity": 1}}},
                )
            except Exception as exc:
                assert "get_order" in str(exc)
                assert "idem-1234" in str(exc)
                raise
