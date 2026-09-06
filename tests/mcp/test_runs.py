from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class RunFake:
    def __init__(self):
        self.submissions = 0
        self.claims = 0

    async def health(self):
        return {"allowed_actions": ["runs:read", "runs:create", "runs:exit", "runs:log", "risk:update", "intents:submit", "market:read"]}

    async def claim_session(self, run_id):
        self.claims += 1
        return {"session_nonce": "hidden"}

    async def release_session(self, run_id, *, session_nonce):
        pass

    async def run_heartbeat(self, run_id, *, session_nonce, status):
        pass

    async def safety_check(self, run_id):
        return {"allowed": True}

    async def create_run(self, **kwargs):
        return {"strategy_run_id": kwargs.get("strategy_run_id") or "run-1", "execution_mode": kwargs["execution_mode"]}

    async def get_run(self, run_id):
        return {"strategy_run_id": run_id, "status": "running"}

    async def preview_order(self, run_id, order):
        return {"preview": True, "order": order}

    async def preview_basket(self, run_id, orders, **kwargs):
        return {"preview": True, "orders": orders}

    async def place_order(self, run_id, order, idempotency_key, **kwargs):
        self.submissions += 1
        return {"order_id": "ord-1", "status": "accepted", "idempotency_key": idempotency_key}

    async def place_basket(self, *args, **kwargs):
        self.submissions += 1
        return {"status": "partial", "results": [{"index": 0, "status": "success"}]}

    async def patch_risk(self, *args, **kwargs):
        return {"updated": True}

    async def update_backend_protection(self, *args, **kwargs):
        return {"updated": True}

    async def exit_run(self, *args, **kwargs):
        return {"status": "exited"}

    async def log_decision_event(self, *args, **kwargs):
        return {"logged": True}

    async def get_run_pnl(self, run_id):
        return {"strategy_run_id": run_id, "pnl": 0}

    async def list_timeline(self, *args, **kwargs):
        return {"items": []}

    async def list_execution_events(self, *args, **kwargs):
        return {"items": []}

    async def get_run_protection_state(self, run_id):
        return {"strategy_run_id": run_id, "enabled": True}


@pytest.mark.asyncio
async def test_preview_is_non_mutating_and_explicit_paper_submit_uses_one_lease() -> None:
    fake = RunFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=fake)
    async with Client(server) as client:
        preview = await client.call_tool("preview_order", {"request": {"strategy_run_id": "run-1", "idempotency_key": "preview-1234", "order": {"symbol": "ABC", "transaction_type": "BUY", "quantity": 1}}})
        assert json.loads(preview.content[0].text)["data"]["preview"] is True
        assert fake.submissions == 0
        placed = await client.call_tool("place_order", {"request": {"strategy_run_id": "run-1", "idempotency_key": "submit-1234", "order": {"symbol": "ABC", "transaction_type": "BUY", "quantity": 1}}})
        assert json.loads(placed.content[0].text)["data"]["order_id"] == "ord-1"
        assert fake.submissions == 1
        assert fake.claims == 1


@pytest.mark.asyncio
async def test_exit_and_risk_tools_are_run_scoped() -> None:
    fake = RunFake()
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=fake)
    async with Client(server) as client:
        await client.call_tool("update_run_risk", {"request": {"strategy_run_id": "run-1", "max_daily_loss": 100, "reason": "test"}})
        exited = await client.call_tool("exit_run", {"request": {"strategy_run_id": "run-1", "idempotency_key": "exit-1234"}})
        assert json.loads(exited.content[0].text)["data"]["status"] == "exited"
