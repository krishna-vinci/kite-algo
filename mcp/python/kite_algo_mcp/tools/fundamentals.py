from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..contracts import FundamentalsRefreshRequest, FundamentalsScopeRequest, FundamentalsStatementRequest
from ..server import MCPRuntime
from .common import args_model, register_tool


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def get_fundamentals_features(request: FundamentalsScopeRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "get_fundamentals_features", values,
            lambda _lease: runtime.client.get_fundamentals_features(symbols=request.symbols, index=request.index),
        )

    async def get_fundamentals_statements(request: FundamentalsStatementRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "get_fundamentals_statements", values,
            lambda _lease: runtime.client.get_fundamentals_statements(request.symbol, dataset=request.dataset, statement_scope=request.statement_scope),
        )

    async def get_fundamentals_status(request: FundamentalsScopeRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "get_fundamentals_status", values,
            lambda _lease: runtime.client.get_fundamentals_status(symbols=request.symbols, index=request.index),
        )

    async def refresh_fundamentals(request: FundamentalsRefreshRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "refresh_fundamentals", values,
            lambda _lease: runtime.client.refresh_fundamentals(symbols=request.symbols, index=request.index, mode=request.mode),
        )

    for name, function in {
        "get_fundamentals_features": get_fundamentals_features,
        "get_fundamentals_statements": get_fundamentals_statements,
        "get_fundamentals_status": get_fundamentals_status,
        "refresh_fundamentals": refresh_fundamentals,
    }.items():
        register_tool(server, runtime, name, function)
