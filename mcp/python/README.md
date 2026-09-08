# kite-algo-mcp

`kite-algo-mcp` exposes the public `kite-algo-worker` SDK through authenticated
LAN Streamable HTTP or local stdio. Its Docker image/isolated environment keeps
MCP dependencies separate from the backend environment.

The locked MCP runtime requires Python 3.11+ (including its NumPy/Pandas pins).
The worker SDK keeps its independent Python 3.9+ support.

The server is deliberately explicit: profiles are `read`, `paper`, and
`live`; the backend token remains the final authorization boundary; and data
refresh is a separate opt-in. It does not expose generic HTTP, SQL, Python,
filesystem, Telegram, screening, scheduling, rebalancing, or optimizer tools.

## Docker: optional LAN Streamable HTTP

The `mcp` service exists in both `compose.yml` and `compose.dev.yml`. It is off
by default. Set these values in your local `.env` (never commit real tokens):

```dotenv
COMPOSE_PROFILES=mcp
KITE_MCP_PROFILE=read
KITE_MCP_ALLOW_DATA_REFRESH=false
KITE_MCP_HOST_PORT=18788
KITE_MCP_BIND_ADDRESS=0.0.0.0
KITE_MCP_ALLOWED_HOSTS=192.168.1.100,kite.local
KITE_MCP_WORKER_TOKEN=<existing-authorized-worker-token>
KITE_MCP_HTTP_TOKEN=<separate-random-client-secret>
```

Replace `192.168.1.100,kite.local` with the **server's** LAN IP/hostname, not
the device IPs. Every device connects to the same server address. This is
HTTP Host/Origin validation, not a firewall rule. No firewall settings are
changed. Generate the separate client secret locally with `openssl rand -hex 32`;
the server requires at least 32 non-whitespace ASCII characters and refuses to
reuse the worker token as the client secret.

```sh
# Build/start only MCP against an already-running backend; no backend restart.
docker compose --profile mcp up -d --build --no-deps mcp
# Or use the enabled profile on a normal full-stack startup:
docker compose up -d
docker compose ps mcp
```

For the development stack, use `docker compose -f compose.dev.yml` with the same
commands. The dev MCP image is built from source too; rebuild it after changes.

Clients use Streamable HTTP at `http://192.168.1.100:18788/mcp` with header
`Authorization: Bearer <KITE_MCP_HTTP_TOKEN>`. Do not give clients the backend
worker token. Clients must support supplying bearer authentication; this server
does not provide OAuth login or dynamic client registration. No personal Codex
configuration is installed automatically.

The packaged Docker image is the supported runtime path; clients need only the
MCP URL and client bearer secret. The host does not need a Python installation,
editable checkout, virtual environment, or `scripts/mcp_codex_headers.py` to
connect. For an isolated image regression run:

```sh
docker build --pull -f mcp/Dockerfile -t kite-algo-mcp:verify .
docker build -f mcp/test.Dockerfile \
  --build-arg MCP_RUNTIME_IMAGE=kite-algo-mcp:verify \
  -t kite-algo-mcp-tests:verify .
docker run --rm --network none kite-algo-mcp-tests:verify
docker run --rm --network none kite-algo-mcp:verify python -m pip check
```

MCP enablement and execution permission are independent:

| Setting | Meaning |
|---|---|
| `COMPOSE_PROFILES=mcp` | Include the optional MCP container on startup |
| `KITE_MCP_PROFILE=read` | Read/research tools; default |
| `KITE_MCP_PROFILE=paper` | Paper execution tools, subject to worker permissions |
| `KITE_MCP_PROFILE=live` | Live-capable tools, still subject to backend authorization and explicit requests |
| `KITE_MCP_ALLOW_DATA_REFRESH=true` | Separately opt into supported ingestion operations |

Changing `.env` requires recreating MCP: `docker compose --profile mcp up -d
--no-deps --force-recreate mcp` (run as one line). Enabling `live` never submits
orders on startup. All clients of this instance share the configured worker
identity and permissions; run separate instances/credentials if isolation is
needed. Use host confirmation for trading tools. HTTP bearer auth does not
encrypt traffic: use HTTPS termination when confidentiality on the network is
required. Docker publishes on all host interfaces by default, not exclusively
private/LAN interfaces; use `KITE_MCP_BIND_ADDRESS` to select a specific host IP.

To disable, remove `mcp` from `COMPOSE_PROFILES` **and** stop the running service:

```sh
docker compose stop mcp
```

Removing a profile alone does not stop existing containers. Explicitly naming
the service also starts it even without the profile enabled. Missing credentials
or allowed hosts cause startup to fail closed. `/healthz` is a public liveness
probe only; authenticated `get_capabilities` verifies backend connectivity.

## Direct HTTP configuration

Outside Compose set `KITE_MCP_TRANSPORT=http`, `KITE_MCP_HOST=0.0.0.0`,
`KITE_MCP_PORT=8788`, the host allowlist and both credentials. Set
`KITE_MCP_API_URL` to your backend URL. Non-loopback plaintext backend URLs
require explicit `KITE_MCP_ALLOW_INSECURE_BACKEND_HTTP=true`; Compose sets this
only for its internal `http://finance-app:8777` connection. Browser clients can
add exact origins through comma-separated `KITE_MCP_ALLOWED_ORIGINS`; native
MCP clients do not need an Origin header. This is not an unrestricted CORS proxy.

`KITE_MCP_WORKER_TOKEN_FILE` and `KITE_MCP_HTTP_TOKEN_FILE` alternatively read
mounted UTF-8 secret files. Set the corresponding direct variable empty and
mount readable files through a local Compose override; do not configure both
forms simultaneously. Restart/recreate MCP after rotating a credential.

## Optional development-only local stdio setup

```sh
python3 -m venv mcp/python/.venv
mcp/python/.venv/bin/python -m pip install -e ./sdk/python -e ./mcp/python
export KITE_MCP_API_URL=http://127.0.0.1:18777
export KITE_MCP_WORKER_TOKEN=replace-with-a-worker-token
export KITE_MCP_PROFILE=read
export KITE_MCP_TRANSPORT=stdio
export KITE_MCP_ALLOW_DATA_REFRESH=false
mcp/python/.venv/bin/kite-algo-mcp
```

Use HTTPS for non-loopback backend URLs unless opting into plaintext explicitly.
`paper` and `live` require explicit run and
account fields; profile selection or a host approval does not bypass worker
authorization. A write timeout is reported as an unknown outcome and must be
reconciled with the corresponding read tool rather than retried.

The adapter uses Asia/Kolkata-compatible ISO timestamps returned by the worker,
preserves missing values, and bounds symbols, candles, event pages, basket
legs, and serialized responses. MCP hosts should additionally require an
explicit approval for write/destructive tools. No credentials are included in
tool output, resources, logs, or command-line arguments.
