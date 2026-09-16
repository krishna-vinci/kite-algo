"""Contract harness: run the same catalog-parity assertions against the Go adapter.

Set KITE_MCP_PARITY_BIN to a built Go binary (mcp/go/cmd/kite-algo-mcp) and
these tests spawn it on a free port and assert tools/list parity with the
Python adapter's exported golden catalog. Without the variable, they skip, so
normal CI runs of tests/mcp are unaffected.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastmcp import Client

GOLDEN = Path(__file__).resolve().parents[1] / "mcp" / "fixtures" / "go_parity_tool_names.json"
BIN = Path(__file__).resolve().parents[2] / "mcp" / "go"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def go_server_url():
    binary = os.environ.get("KITE_MCP_PARITY_BIN")
    if not binary:
        pytest.skip("KITE_MCP_PARITY_BIN not set; building the Go adapter first")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"go adapter exited early: {proc.returncode}")
            time.sleep(0.1)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _golden_names() -> list[str]:
    return json.loads(GOLDEN.read_text())


def _list_tools(url: str) -> tuple[list[str], dict[str, dict]]:
    async def _collect():
        names, by_name = [], {}
        async with Client(url) as client:
            tools = await client.list_tools()
            for tool in tools:
                names.append(tool.name)
                by_name[tool.name] = tool.model_dump(mode="json") if hasattr(tool, "model_dump") else {"name": tool.name}
        return names, by_name

    import asyncio

    return asyncio.run(_collect())


def test_go_adapter_tool_names_match_python_catalog(go_server_url):
    names, _ = _list_tools(go_server_url)
    assert names == _golden_names(), "tools/list order or names diverge from the reviewed catalog"


def test_go_adapter_every_tool_has_schema(go_server_url):
    _, by_name = _list_tools(go_server_url)
    for name in _golden_names():
        tool = by_name.get(name)
        assert tool, f"{name} missing"
        schema = tool.get("inputSchema") or (tool.get("input_schema") if isinstance(tool, dict) else None)
        assert schema, f"{name} has no inputSchema"
        if isinstance(schema, dict) and "type" in schema:
            assert schema["type"] == "object", f"{name} schema root must be object"
