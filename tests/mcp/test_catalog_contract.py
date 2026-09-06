from __future__ import annotations

import asyncio
import inspect
import json

from kite_algo_mcp.catalog import TOOL_CATALOG
from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


def test_coverage_and_registered_catalog_are_one_to_one() -> None:
    records = json.loads(open("mcp/python/kite_algo_mcp/coverage.json", encoding="utf-8").read())
    mapped = {tool for record in records for tool in record["tools"]}
    assert mapped == set(TOOL_CATALOG)
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="live", allow_data_refresh=True), client=object())
    names = {item.name for item in asyncio.run(server.list_tools())}
    assert names == set(TOOL_CATALOG)


def test_forbidden_products_and_unbounded_payload_signatures_are_absent() -> None:
    forbidden = ("telegram", "screener", "screen", "scheduler", "rebalance", "optimizer", "generic_http", "execute_python")
    assert not any(any(word in name.lower() for word in forbidden) for name in TOOL_CATALOG)
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="live", allow_data_refresh=True), client=object())
    for function_tool in asyncio.run(server.list_tools()):
        parameters = inspect.signature(function_tool.fn).parameters
        assert not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
