# FastMCP Integration Guide for Your Broker API (Kite) — Expose FastAPI endpoints as Model Context Protocol tools

This guide shows how to:
- Wrap your existing FastAPI endpoints in [broker_api/broker_api.py](broker_api/broker_api.py) into MCP tools using [FastMCP.from_openapi()](mcp_broker_server.py:1).
- Optionally keep or replace the manual wrappers in [mcp_server.py](mcp_server.py).
- Run and access your MCP server from popular AI clients (Claude Desktop, Cursor, Open WebUI, or via a Python [Client.call_tool()](mcp_client_test.py:1)).

You asked to disregard the current MCP setup; this guide provides a clean path that automatically exposes your existing endpoints with minimal code.


## What you have now (key endpoints)

Your FastAPI app (mounted at `/broker`) already provides rich broker operations in [broker_api/broker_api.py](broker_api/broker_api.py), including:

- Authentication and session:
  - POST `/broker/login_kite`
  - POST `/broker/logout_kite`
  - GET `/broker/profile_kite`
  - GET `/broker/holdings_kite`

- Instruments import and search:
  - POST `/broker/import_instruments/nse | nfo | commodity | all`
  - GET `/broker/instruments/nse | nfo | commodity | search/{symbol}`

- Historical data:
  - POST `/broker/clear_historical_data`
  - POST `/broker/fetch_historical_data`
  - POST `/broker/update_historical_data`
  - GET `/broker/historical_data_progress`

These endpoints already encapsulate your business logic, access tokens, cookies, database updates, and background jobs. We can turn them into MCP tools with near-zero duplication using [FastMCP.from_openapi()](mcp_broker_server.py:1).


## Recommended approach: Auto-generate MCP tools from your FastAPI OpenAPI

FastMCP can convert an OpenAPI spec into live MCP tools. We’ll point it at the OpenAPI JSON from your running backend (e.g., `http://localhost:8000/openapi.json`). Crucially, we’ll reuse a single [httpx.AsyncClient](mcp_broker_server.py:1) so cookies (your `login_kite` session) persist across tool calls.

### 1) Prerequisites

- Python 3.10+
- Install dependencies:
  - `fastmcp` (MCP server framework)
  - `httpx` (async HTTP client for the OpenAPI bridge)
  - Your project’s normal dependencies (see [requirements.txt](requirements.txt))

Example:
```bash
uv pip install fastmcp httpx
# or
pip install fastmcp httpx
```

Ensure your FastAPI app is running (typically at `http://localhost:8000`). Your app entrypoint is [main.py](main.py) which includes your [broker_api/broker_api.py](broker_api/broker_api.py) router at `/broker`.


### 2) Create a dedicated MCP server that wraps your running FastAPI

Create a new file [mcp_broker_server.py](mcp_broker_server.py) with the following content:

```python
# mcp_broker_server.py
import os
import asyncio
import httpx
from fastmcp import FastMCP

# Base URL of your running FastAPI app (serving broker API + /openapi.json)
BASE_URL = os.getenv("BROKER_API_URL", "http://localhost:8000")

# A single AsyncClient instance so cookies (like 'kite_session_id') persist across tool calls
_http_client: httpx.AsyncClient | None = None

async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(base_url=BASE_URL, timeout=60.0, follow_redirects=True)
    return _http_client

async def _build_server() -> FastMCP:
    client = await _get_client()

    # Pull OpenAPI spec from your running FastAPI backend
    # NOTE: Ensure the backend is running before you start this MCP server.
    resp = await client.get("/openapi.json")
    resp.raise_for_status()
    spec = resp.json()

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,                     # Persist cookies across tool calls (e.g., after login)
        # name="Kite Broker API MCP"       # Optional: set a custom name visible to clients
    )

    # Optional: health route for HTTP transport
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("OK")

    return mcp

# Export 'mcp' for use by fastmcp CLI and fastmcp.json entrypoint
# We build it lazily to avoid event loop issues in CLIs and stdio transports
mcp = FastMCP("Kite Broker API MCP")
# When running with the CLI, 'mcp' will be replaced with the real server instance in __main__

if __name__ == "__main__":
    async def main():
        server = await _build_server()
        # Run as HTTP by default; change to stdio for local desktop clients like Claude.
        server.run(transport="http", host="127.0.0.1", port=8001)

    asyncio.run(main())
```

Key choices:
- We use [FastMCP.from_openapi()](mcp_broker_server.py:1) so every FastAPI route becomes an MCP tool automatically (parameters and descriptions come from your OpenAPI).
- We use one shared [httpx.AsyncClient](mcp_broker_server.py:1) to keep cookies (i.e. session set by `/broker/login_kite`) between calls. That means a single MCP client session can:
  1. Call `login_kite`
  2. Then call `profile_kite`, `holdings_kite`, etc.
- This avoids duplicating your logic and respects your auth and background-task design.

Notes:
- If you only want `/broker/*` endpoints, consider serving a narrowed OpenAPI spec or filtering the spec before passing into `from_openapi`. Otherwise, all your published FastAPI routes become tools.
- Keep your FastAPI backend (from [main.py](main.py)) running while you run this MCP server.


### 3) Configure fastmcp.json (optional but recommended)

Create a [fastmcp.json](fastmcp.json) for reproducible runs:

```json
{
  "$schema": "https://gofastmcp.com/public/schemas/fastmcp.json/v1.json",
  "source": {
    "path": "mcp_broker_server.py",
    "entrypoint": "mcp"
  },
  "environment": {
    "dependencies": ["fastmcp", "httpx"]
  },
  "deployment": {
    "transport": "http",
    "host": "127.0.0.1",
    "port": 8001,
    "env": {
      "BROKER_API_URL": "http://localhost:8000"
    }
  }
}
```

Run it with:
```bash
# If fastmcp.json is in the current directory:
fastmcp run

# Or explicitly:
fastmcp run fastmcp.json
```

Environment variable `BROKER_API_URL` lets you point at staging/production or a Docker service name without code changes.


### 4) Running options

- HTTP transport (remote access and easy debugging):
  - As in [mcp_broker_server.py](mcp_broker_server.py) `__main__` or via `fastmcp.json`.
  - MCP endpoint will be at `http://127.0.0.1:8001/mcp` with a health route at `http://127.0.0.1:8001/health`.

- STDIO transport (best for local clients like Claude Desktop or Cursor):
  - Change the run command to `server.run(transport="stdio")` when running from code; or
  - Override the transport via CLI: `fastmcp run fastmcp.json --transport stdio`.


### 5) Tool names and parameters

FastMCP generates tool names based on your OpenAPI `operationId` (if present) or from the method/path. With your current router, examples you will see after `login_kite` include:

- `profile_kite` → GET `/broker/profile_kite`
- `holdings_kite` → GET `/broker/holdings_kite`
- `import_instruments/nse` → POST `/broker/import_instruments/nse`
- `import_instruments/nfo` → POST `/broker/import_instruments/nfo`
- `import_instruments/commodity` → POST `/broker/import_instruments/commodity`
- `import_instruments/all` → POST `/broker/import_instruments/all`
- `fetch_historical_data` → POST `/broker/fetch_historical_data`
- `update_historical_data` → POST `/broker/update_historical_data`
- `historical_data_progress` → GET `/broker/historical_data_progress`

Tip: Use the inspector (`fastmcp dev fastmcp.json`) or a client to run [Client.list_tools()](mcp_client_test.py:1) and confirm exact names.


### 6) Minimal Python client example (to smoke-test tools)

You can call the MCP server from Python using the FastMCP [Client](mcp_client_test.py:1). For HTTP transport:

```python
# mcp_client_test_example.py
import asyncio
from fastmcp import Client

async def main():
    client = Client("http://127.0.0.1:8001")
    async with client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        # 1) Login so that session cookie is set in the backend
        #    (Your /broker/login_kite sets 'kite_session_id' cookie)
        try:
            login_result = await client.call_tool("login_kite", {})
            print("Login:", login_result)
        except Exception as e:
            print("Login error:", e)

        # 2) Now profile and holdings should work in the same session
        try:
            profile = await client.call_tool("profile_kite", {})
            print("Profile:", profile)
            holdings = await client.call_tool("holdings_kite", {})
            print("Holdings:", holdings)
        except Exception as e:
            print("Data error:", e)

if __name__ == "__main__":
    asyncio.run(main())
```

Run:
```bash
python mcp_client_test_example.py
```


## Using your MCP server with AI services

Below are the most common client setups.

### Claude Desktop (local stdio MCP)

Claude Desktop expects STDIO transports:
- Ensure your broker FastAPI backend is running at the URL set in `BROKER_API_URL`.
- Start your MCP server with stdio:
  ```bash
  fastmcp run fastmcp.json --transport stdio
  ```
- In Claude Desktop’s MCP configuration (JSON), add an entry like:
  ```json
  {
    "mcpServers": {
      "kite-broker-mcp": {
        "command": "fastmcp",
        "args": ["run", "fastmcp.json", "--transport", "stdio"],
        "env": {
          "BROKER_API_URL": "http://localhost:8000"
        }
      }
    }
  }
  ```
- Restart Claude Desktop. It should detect the `kite-broker-mcp` tools.
- Prompt hint: “List available tools” or ask to “Log in to Kite and fetch holdings.”

Because we used a persistent [httpx.AsyncClient](mcp_broker_server.py:1), tool calls within the same MCP session share cookies set by `/broker/login_kite`.

### Cursor AI (local stdio MCP)

Similar to Claude Desktop:
- Configure a “Custom MCP Server” pointing to:
  - Command: `fastmcp`
  - Args: `run`, `fastmcp.json`, `--transport`, `stdio`
  - Env: `BROKER_API_URL=http://localhost:8000`

### Open WebUI

Open WebUI supports MCP servers. Refer to your [MCP Support  Open WebUI.md](MCP%20Support%20%20Open%20WebUI.md) and add a custom MCP server pointing to the same stdio command above. Then test the `login_kite` → `profile_kite` flow.

### Generic HTTP LLM clients

If your LLM client supports MCP over HTTP:
- Use: `http://127.0.0.1:8001/mcp` (from `fastmcp.json` defaults).
- Be sure your client can maintain a single session context so that cookie-based flows work as intended (most clients do).



## Alternative approach: Manual tool wrappers (what you had in mcp_server.py)

You also have a hand-written MCP server in [mcp_server.py](mcp_server.py) that registers tools like:
- [get_profile()](mcp_server.py:1)
- [get_holdings()](mcp_server.py:1)
- [get_margins()](mcp_server.py:1)
- [place_order()](mcp_server.py:1), [modify_order()](mcp_server.py:1), [cancel_order()](mcp_server.py:1)
- [get_quote()](mcp_server.py:1)

This pattern accepts an `access_token` parameter and calls the Kite SDK directly. It works but duplicates logic and bypasses the cookie/session flows you already implemented in your FastAPI app. If you prefer manual wrappers for certain high-value operations, you can combine both strategies:
- Keep the auto-generated tools for most endpoints.
- Maintain a curated set of manual tools for specific workflows, advanced schemas, or richer return types ([ToolResult](fastmcp-document.md:719)).



## Security, auth, and environment notes

- Your current broker auth sets a `kite_session_id` cookie in the DB and response; by using a shared [httpx.AsyncClient](mcp_broker_server.py:1) all subsequent tool calls re-use it automatically.
- Environment variables:
  - `BROKER_API_URL` for pointing the MCP server at dev/staging/prod or Docker hostnames.
  - Your backend already uses `DATABASE_URL` and other envs in [broker_api/broker_api.py](broker_api/broker_api.py).
- CORS is not relevant for stdio/HTTP MCP itself, but your FastAPI app’s CORS config in [main.py](main.py) remains for browser-based frontends.



## Troubleshooting

- “OpenAPI fetch failed”: ensure your FastAPI backend is running and `BASE_URL/openapi.json` is reachable.
- “Login works but profile fails”: confirm the MCP client keeps a single session context; with `from_openapi()` and a shared [httpx.AsyncClient](mcp_broker_server.py:1), cookies should persist after `login_kite`.
- “Tool not found”: list available tools via an MCP inspector or [Client.list_tools()](mcp_client_test.py:1) to verify names (they derive from OpenAPI).
- Long-running tasks: endpoints like `update_historical_data` queue background work; use `historical_data_progress` to poll progress.



## Reference mapping (common tasks)

After `login_kite`, call:

- Import instruments:
  - `import_instruments/nse`
  - `import_instruments/nfo`
  - `import_instruments/commodity`
  - `import_instruments/all`

- Query instruments:
  - `instruments/nse`
  - `instruments/nfo`
  - `instruments/commodity`
  - `instruments/search/{symbol}`

- Historical data:
  - `fetch_historical_data`
  - `update_historical_data`
  - `historical_data_progress`

- Account data:
  - `profile_kite`
  - `holdings_kite`



## Best practices and next steps

- Prefer [FastMCP.from_openapi()](mcp_broker_server.py:1) to stay DRY and leverage your existing FastAPI behavior.
- If you want a purely in-process server (no network fetch of OpenAPI), you can also export your FastAPI app or router from a shared module and use [FastMCP.from_fastapi()](mcp_broker_server.py:1), but avoid circular imports with [main.py](main.py).
- For production:
  - Run MCP via `fastmcp run fastmcp.json` and manage with systemd, Docker, or your orchestrator.
  - Consider FastMCP Cloud if you want a hosted URL (see [fastmcp-hosting.md](fastmcp-hosting.md)).

With this setup, your LLMs get first-class tools representing your real API, including auth, background jobs, and DB-backed operations, without re-implementing logic in the MCP layer.
## Multi‑device access (bind to 0.0.0.0 and connect from LAN)

The MCP server now binds on 0.0.0.0 so it’s reachable from other devices on your local network.

- Server binding:
  - [`mcp.run(..., host="0.0.0.0", port=8001)`](mcp_broker_server.py:1)
  - [`"host": "0.0.0.0"`](fastmcp.json:1)
- Connect from another device using your host’s LAN IP (NOT 0.0.0.0). Example:
  - MCP landing page: `http://192.168.x.y:8001/`
  - Health: `http://192.168.x.y:8001/health`
  - Init: `http://192.168.x.y:8001/init`
  - MCP endpoint (for MCP clients): `http://192.168.x.y:8001/mcp` (requires Accept: text/event-stream)
- Do not use 0.0.0.0 in URLs; it is only valid as a bind address. Use the host machine’s IP (e.g., 192.168.x.y).
- Ensure your OS firewall allows inbound TCP 8001.
- The backend (FastAPI) remains at `http://localhost:8777` from the MCP server’s perspective. That’s correct because MCP and Docker are on the same host. If you run MCP on a different machine than Docker, set:
  - `BROKER_API_URL=http://<docker-host-ip>:8777`
  - Or `BROKER_API_URLS=http://<docker-host-ip>:8777,http://localhost:8777`
- From other machines a browser will show `/` `/health` `/init`, but `/mcp` must be consumed by an MCP client (Claude, Cursor, Open WebUI, or FastMCP Client API). For a quick client test, point your script’s URL to `http://192.168.x.y:8001` instead of `http://127.0.0.1:8001`.