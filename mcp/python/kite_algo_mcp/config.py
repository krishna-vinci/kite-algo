"""Configuration and startup validation for the local MCP adapter.

Configuration is intentionally boring and side-effect free.  In particular,
loading this module never contacts the worker or logs a bearer token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when the adapter cannot start safely."""


_PROFILES = frozenset({"read", "paper", "live"})


@dataclass(frozen=True)
class MCPConfig:
    api_url: str
    worker_token: str = field(repr=False)
    profile: str = "read"
    allow_data_refresh: bool = False
    timeout_seconds: float = 30.0
    max_concurrency: int = 4
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8788
    http_token: str = field(default="", repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    allow_insecure_backend_http: bool = False

    def __post_init__(self) -> None:
        url = str(self.api_url or "").strip().rstrip("/")
        token = str(self.worker_token or "").strip()
        profile = str(self.profile or "read").strip().lower()
        if not url:
            raise ConfigurationError("KITE_MCP_API_URL is required")
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ConfigurationError("KITE_MCP_API_URL must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError("KITE_MCP_API_URL must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise ConfigurationError("KITE_MCP_API_URL must not contain a query or fragment")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname) and not self.allow_insecure_backend_http:
            raise ConfigurationError("Non-loopback backend HTTP requires KITE_MCP_ALLOW_INSECURE_BACKEND_HTTP=true")
        if not token:
            raise ConfigurationError("KITE_MCP_WORKER_TOKEN is required")
        if profile not in _PROFILES:
            raise ConfigurationError("KITE_MCP_PROFILE must be one of read, paper, or live")
        timeout = float(self.timeout_seconds)
        if timeout <= 0 or timeout > 30:
            raise ConfigurationError("KITE_MCP_TIMEOUT_SECONDS must be between 0 and 30")
        concurrency = int(self.max_concurrency)
        if concurrency < 1 or concurrency > 4:
            raise ConfigurationError("KITE_MCP_MAX_CONCURRENCY must be between 1 and 4")
        transport = str(self.transport).strip().lower()
        if transport not in {"stdio", "http"}:
            raise ConfigurationError("KITE_MCP_TRANSPORT must be stdio or http (Streamable HTTP)")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ConfigurationError("KITE_MCP_PORT must be between 1 and 65535")
        http_token = self.http_token.strip()
        if transport == "http":
            if len(http_token) < 32 or not http_token.isascii() or any(c.isspace() for c in http_token):
                raise ConfigurationError("HTTP requires KITE_MCP_HTTP_TOKEN with at least 32 non-whitespace ASCII characters")
            if http_token == token:
                raise ConfigurationError("HTTP client token must differ from the backend worker token")
            if not self.allowed_hosts or any(not host or any(c in host for c in "*/?#@ ") for host in self.allowed_hosts):
                raise ConfigurationError("KITE_MCP_ALLOWED_HOSTS must list explicit LAN IPs or hostnames without wildcards or URLs")
            for origin in self.allowed_origins:
                value = urlsplit(origin)
                if value.scheme not in {"http", "https"} or not value.hostname or value.username or value.password or value.path or value.query or value.fragment or "*" in origin:
                    raise ConfigurationError("KITE_MCP_ALLOWED_ORIGINS must contain exact http(s) origins")
        object.__setattr__(self, "api_url", url)
        object.__setattr__(self, "worker_token", token)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "allow_data_refresh", bool(self.allow_data_refresh))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_concurrency", concurrency)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "http_token", http_token)

    @property
    def token(self) -> str:
        """Compatibility alias used when constructing the SDK config."""

        return self.worker_token


def _is_loopback(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"}


def _env_bool(value: str | None, *, default: bool = False, name: str = "KITE_MCP_ALLOW_DATA_REFRESH") -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _secret(env: dict[str, str], name: str) -> str:
    value, filename = env.get(name, "").strip(), env.get(name + "_FILE", "").strip()
    if value and filename:
        raise ConfigurationError(f"Set only {name} or {name}_FILE, not both")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ConfigurationError(f"Cannot read {name}_FILE") from None
    return value


def load_config(environ: dict[str, str] | None = None) -> MCPConfig:
    """Load and validate environment configuration without network I/O."""

    env = os.environ if environ is None else environ
    try:
        timeout = float(env.get("KITE_MCP_TIMEOUT_SECONDS", "30"))
        concurrency = int(env.get("KITE_MCP_MAX_CONCURRENCY", "4"))
        port = int(env.get("KITE_MCP_PORT", "8788"))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("KITE_MCP_TIMEOUT_SECONDS, MAX_CONCURRENCY and PORT must be numeric") from exc
    return MCPConfig(
        api_url=env.get("KITE_MCP_API_URL", ""),
        worker_token=_secret(env, "KITE_MCP_WORKER_TOKEN"),
        profile=env.get("KITE_MCP_PROFILE", "read"),
        allow_data_refresh=_env_bool(env.get("KITE_MCP_ALLOW_DATA_REFRESH")),
        timeout_seconds=timeout,
        max_concurrency=concurrency,
        transport=env.get("KITE_MCP_TRANSPORT", "stdio"),
        host=env.get("KITE_MCP_HOST", "0.0.0.0"),
        port=port,
        http_token=_secret(env, "KITE_MCP_HTTP_TOKEN"),
        allowed_hosts=tuple(v.strip() for v in env.get("KITE_MCP_ALLOWED_HOSTS", "").split(",") if v.strip()),
        allowed_origins=tuple(v.strip() for v in env.get("KITE_MCP_ALLOWED_ORIGINS", "").split(",") if v.strip()),
        allow_insecure_backend_http=_env_bool(env.get("KITE_MCP_ALLOW_INSECURE_BACKEND_HTTP"), name="KITE_MCP_ALLOW_INSECURE_BACKEND_HTTP"),
    )


__all__ = ["ConfigurationError", "MCPConfig", "load_config"]
