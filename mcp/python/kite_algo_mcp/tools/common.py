from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from fastmcp import FastMCP

from ..catalog import TOOL_CATALOG, ToolSpec
from ..server import MCPRuntime, _annotations


def register_tool(
    server: FastMCP,
    runtime: MCPRuntime,
    name: str,
    function: Callable[..., Awaitable[Any]],
) -> bool:
    spec = TOOL_CATALOG[name]
    if not runtime.policy.visible(name):
        return False
    server.tool(
        name=name,
        description=spec.description,
        tags={spec.group, spec.effect},
        annotations=_annotations(spec),
    )(function)
    return True


def args_model(model: Any) -> Mapping[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)
