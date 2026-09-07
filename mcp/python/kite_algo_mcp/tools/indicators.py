from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..contracts import IndicatorRequest
from ..server import MCPRuntime
from .common import args_model, register_tool


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def calculate_indicator(request: IndicatorRequest) -> Any:
        values = args_model(request)

        async def operation(_lease: Any) -> Any:
            # Numerical compute lives on the worker (POST /worker/indicators);
            # the adapter stays pandas-free and only forwards bounded candles.
            return await runtime.client.calculate_indicator(values)

        return await runtime.invoke("calculate_indicator", values, operation)

    register_tool(server, runtime, "calculate_indicator", calculate_indicator)
