from typing import Any
from fastmcp import FastMCP
from contextvars import ContextVar

mcp = FastMCP(name="Kite MCP")

# Optional fallback: a global KiteConnect (e.g., set during startup via headless login)
mcp_kite_instance: Any = None

# Per-request KiteConnect instance (safe for concurrent requests)
_request_kite: ContextVar[Any] = ContextVar("_request_kite", default=None)

def set_request_kite(kite: Any):
    """Set the KiteConnect instance for the current HTTP request context; returns a context token."""
    return _request_kite.set(kite)

def reset_request_kite(token) -> None:
    """Reset the request-scoped KiteConnect context to its previous value."""
    try:
        _request_kite.reset(token)
    except Exception:
        # Ignore if already reset or invalid token
        pass

def _get_kite() -> Any | None:
    """Get the authenticated KiteConnect instance, if available."""
    kite = _request_kite.get()
    if kite is None:
        kite = mcp_kite_instance
    return kite

@mcp.tool
def mcp_get_profile() -> Any:
    """Fetches the user's broker profile."""
    kite = _get_kite()
    if kite is None:
        return {"error": "Authentication failed. Please log in.", "code": 401}
    try:
        return kite.profile()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool
def mcp_get_holdings() -> Any:
    """Fetches the user's current stock holdings."""
    kite = _get_kite()
    if kite is None:
        return {"error": "Authentication failed. Please log in.", "code": 401}
    try:
        return kite.holdings()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool
def mcp_get_margins() -> Any:
    """Fetches the user's account margins."""
    kite = _get_kite()
    if kite is None:
        return {"error": "Authentication failed. Please log in.", "code": 401}
    try:
        return kite.margins()
    except Exception as e:
        return {"error": str(e)}