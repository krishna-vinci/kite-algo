"""Smoke-test only a disposable MCP image; never connect to a real worker.

Targets the Go adapter image (mcp/go/Dockerfile). Run:
    python3 scripts/check_mcp_http_docker.py --image kite-algo-mcp:latest
Creates/stops its own ephemeral containers. Credentials are random test values,
passed by environment name (not command arguments). No live trading calls.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request


def docker(*args: str, env=None) -> str:
    return subprocess.check_output(["docker", *args], text=True, env=env, timeout=45).strip()


def check(image: str, profile: str) -> dict:
    client_token = secrets.token_urlsafe(32)
    env = os.environ | {
        "KITE_MCP_HTTP_TOKEN": client_token,
        "KITE_MCP_WORKER_TOKEN": secrets.token_urlsafe(32),
        # Intentionally unreachable within this disposable container.
        "KITE_MCP_API_URL": "http://127.0.0.1:9",
        "KITE_MCP_ALLOWED_HOSTS": "127.0.0.1",
        "KITE_MCP_PROFILE": profile,
        "KITE_MCP_ALLOW_DATA_REFRESH": "false",
    }
    env_args = [part for key in env if key.startswith("KITE_MCP_") for part in ("-e", key)
                if key in {"KITE_MCP_HTTP_TOKEN", "KITE_MCP_WORKER_TOKEN", "KITE_MCP_API_URL",
                           "KITE_MCP_ALLOWED_HOSTS", "KITE_MCP_PROFILE", "KITE_MCP_ALLOW_DATA_REFRESH"}]
    container = docker("run", "--rm", "-d", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", "-p", "127.0.0.1::8788", *env_args, image, env=env)
    try:
        port = docker("port", container, "8788/tcp").rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
                    # Plain liveness body; the Go adapter serves "ok", not JSON.
                    assert response.read().decode().strip() == "ok"
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable MCP container did not become healthy") from None
                time.sleep(0.1)

        def request(method: str, params=None, token=None):
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-11-25"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params is not None:
                body["params"] = params
            req = urllib.request.Request(url + "/mcp", data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.load(response)

        for token in (None, "wrong"):
            try:
                request("tools/list", token=token)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("HTTP accepted missing or invalid authentication")
        init = request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "docker-smoke", "version": "1"}}, token=client_token)
        assert init["result"]["serverInfo"]["name"] == "kite-algo-mcp"
        listing = request("tools/list", token=client_token)
        names = {tool["name"] for tool in listing["result"]["tools"]}
        assert names, "adapter listed no tools"
        assert all(tool.get("inputSchema") for tool in listing["result"]["tools"]), "a tool has no inputSchema"

        # The Go adapter registers the whole catalog and gates per call rather
        # than per listing, so a profile's reach is whichever tools are not
        # refused with `tool_disabled`. A reachable tool fails later against
        # the intentionally unreachable backend instead.
        def refused(tool: str) -> bool:
            result = request("tools/call", {"name": tool, "arguments": {}}, token=client_token)
            body = json.loads(result["result"]["content"][0]["text"])
            return body.get("error", {}).get("code") == "tool_disabled"

        reach = {
            "place_order": profile in {"paper", "live"},
            "create_gtt": profile == "live",
            "refresh_fundamentals": False,
        }
        for tool, should_be_reachable in reach.items():
            assert refused(tool) is not should_be_reachable, f"{tool} gating is wrong for the {profile} profile"

        assert docker("exec", container, "id", "-u") == "10001"
        assert client_token not in json.dumps(init) + json.dumps(listing)
        return {"profile": profile, "tools": len(names), "authentication": "passed",
                "protocol": "passed", "non_root": True, "broker_calls": 0}
    finally:
        # Exact ID returned by this test's docker run; never target app containers.
        docker("stop", "--time", "3", container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="kite-algo-mcp:latest")
    args = parser.parse_args()
    for profile in ("read", "paper", "live"):
        print(json.dumps(check(args.image, profile)))
