from __future__ import annotations

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class OrderReadFake:
    async def health(self):
        return {"allowed_actions": ["runs:read", "market:read"]}

    async def list_orders(self, run_id):
        return {"orders": [{"order_id": "ord-1", "status": "COMPLETE"}]}

    async def list_trades(self, run_id):
        return {"trades": [{"order_id": "ord-1", "quantity": 1}]}

    async def get_order_snapshot(self, run_id, order_id):
        return {"order_id": order_id, "status": "COMPLETE"}

    async def get_order_history(self, run_id, order_id):
        return {"events": []}


@pytest.mark.asyncio
async def test_order_and_trade_reads_preserve_execution_state() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=OrderReadFake())
    async with Client(server) as client:
        orders = await client.call_tool("list_orders", {"request": {"strategy_run_id": "run-1"}})
        assert "COMPLETE" in orders.content[0].text
        trades = await client.call_tool("list_trades", {"request": {"strategy_run_id": "run-1"}})
        assert "quantity" in trades.content[0].text
