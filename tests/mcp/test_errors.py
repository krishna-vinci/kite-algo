from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


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
