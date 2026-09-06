"""Configuration and startup validation for the local MCP adapter.

Configuration is intentionally boring and side-effect free.  In particular,
loading this module never contacts the worker or logs a bearer token.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when the adapter cannot start safely."""


_PROFILES = frozenset({"read", "paper", "live"})


@dataclass(frozen=True)
class MCPConfig:
    api_url: str
    worker_token: str
    profile: str = "read"
    allow_data_refresh: bool = False
    timeout_seconds: float = 30.0
    max_concurrency: int = 4

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
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise ConfigurationError("HTTP is allowed only for an explicit loopback API URL")
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
        object.__setattr__(self, "api_url", url)
        object.__setattr__(self, "worker_token", token)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "allow_data_refresh", bool(self.allow_data_refresh))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_concurrency", concurrency)

    @property
    def token(self) -> str:
        """Compatibility alias used when constructing the SDK config."""

        return self.worker_token


def _is_loopback(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"}


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError("KITE_MCP_ALLOW_DATA_REFRESH must be a boolean")


def load_config(environ: dict[str, str] | None = None) -> MCPConfig:
    """Load and validate environment configuration without network I/O."""

    env = os.environ if environ is None else environ
    try:
        timeout = float(env.get("KITE_MCP_TIMEOUT_SECONDS", "30"))
        concurrency = int(env.get("KITE_MCP_MAX_CONCURRENCY", "4"))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("KITE_MCP_TIMEOUT_SECONDS and MAX_CONCURRENCY must be numeric") from exc
    return MCPConfig(
        api_url=env.get("KITE_MCP_API_URL", ""),
        worker_token=env.get("KITE_MCP_WORKER_TOKEN", ""),
        profile=env.get("KITE_MCP_PROFILE", "read"),
        allow_data_refresh=_env_bool(env.get("KITE_MCP_ALLOW_DATA_REFRESH")),
        timeout_seconds=timeout,
        max_concurrency=concurrency,
    )


__all__ = ["ConfigurationError", "MCPConfig", "load_config"]
