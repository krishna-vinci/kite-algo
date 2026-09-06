# kite-algo-mcp

`kite-algo-mcp` is the local stdio Model Context Protocol adapter for the
public `kite-algo-worker` SDK. It keeps the MCP dependency tree in this
directory's isolated environment and leaves the backend environment alone.

The server is deliberately explicit: profiles are `read`, `paper`, and
`live`; the backend token remains the final authorization boundary; and data
refresh is a separate opt-in. It does not expose generic HTTP, SQL, Python,
filesystem, Telegram, screening, scheduling, rebalancing, or optimizer tools.

## Local setup

```sh
python3 -m venv mcp/python/.venv
mcp/python/.venv/bin/python -m pip install -e ./sdk/python -e ./mcp/python
export KITE_MCP_API_URL=http://127.0.0.1:18777
export KITE_MCP_WORKER_TOKEN=replace-with-a-worker-token
export KITE_MCP_PROFILE=read
export KITE_MCP_ALLOW_DATA_REFRESH=false
mcp/python/.venv/bin/kite-algo-mcp
```

Use HTTPS for non-loopback URLs. `paper` and `live` require explicit run and
account fields; profile selection or a host approval does not bypass worker
authorization. A write timeout is reported as an unknown outcome and must be
reconciled with the corresponding read tool rather than retried.

The adapter uses Asia/Kolkata-compatible ISO timestamps returned by the worker,
preserves missing values, and bounds symbols, candles, event pages, basket
legs, and serialized responses. MCP hosts should additionally require an
explicit approval for write/destructive tools. No credentials are included in
tool output, resources, logs, or command-line arguments.
