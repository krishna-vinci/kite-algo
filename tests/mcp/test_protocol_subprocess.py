from __future__ import annotations

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_official_mcp_client_can_initialize_stdio_server() -> None:
    env = dict(os.environ)
    env.update(
        KITE_MCP_API_URL="http://127.0.0.1:18777",
        KITE_MCP_API_TOKEN="test-token",
        KITE_MCP_WORKER_TOKEN="worker-token",
        KITE_MCP_PROFILE="read",
    )
    params = StdioServerParameters(command=sys.executable, args=["-m", "kite_algo_mcp"], env=env)

    async def probe() -> None:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                result = await session.list_tools()
                resources = await session.list_resources()
                usage = await session.read_resource("kite://usage")
                assert initialized.protocol_version == "2025-11-25"
                assert len(result.tools) == 53
                assert {tool.name for tool in result.tools}.isdisjoint({"place_order", "place_gtt"})
                assert {str(resource.uri) for resource in resources.resources} == {"kite://capabilities", "kite://usage"}
                assert "unknown_write" in usage.contents[0].text

    await asyncio.wait_for(probe(), timeout=15)
