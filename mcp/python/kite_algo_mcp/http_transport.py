"""Authenticated LAN Streamable HTTP transport, separate from worker policy."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import uvicorn
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.responses import JSONResponse

from .config import ConfigurationError, MCPConfig


class SharedTokenVerifier(TokenVerifier):
    """A single owner's devices share an MCP credential, never a worker token.

    This verifies pre-provisioned bearer credentials; it is not an OAuth issuer.
    Restart the server to rotate the secret and invalidate all old credentials.
    """

    def __init__(self, token: str):
        super().__init__(required_scopes=["mcp"])
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).digest(), self._digest):
            return None
        return AccessToken(token=token, client_id="kite-lan-owner", scopes=["mcp"])


def create_http_app(config: MCPConfig, *, client: Any | None = None):
    if config.transport != "http":
        raise ConfigurationError("HTTP app requires KITE_MCP_TRANSPORT=http")
    from .server import create_server

    server = create_server(config, client=client, auth=SharedTokenVerifier(config.http_token))

    @server.custom_route("/healthz", methods=["GET"])
    async def health(_request):
        # Liveness only. No worker call, identity, mode, balances or credentials.
        return JSONResponse({"status": "ok"})

    return server.http_app(
        path="/mcp", transport="http", stateless_http=True, json_response=True,
        host_origin_protection=True, allowed_hosts=list(config.allowed_hosts),
        allowed_origins=list(config.allowed_origins),
    )


def run_http(config: MCPConfig) -> None:
    uvicorn.run(create_http_app(config), host=config.host, port=config.port,
                access_log=False, proxy_headers=False, log_level="warning")
