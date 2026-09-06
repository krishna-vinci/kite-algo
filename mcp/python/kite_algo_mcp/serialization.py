"""Safe conversion of worker values into bounded MCP JSON."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
import json
import math
from enum import Enum
from typing import Any, Mapping

from .contracts import MAX_RESULT_BYTES, ToolResult


class SerializationLimitError(ValueError):
    """Raised when a valid backend response is too large for one MCP result."""


_REDACT_KEYS = {
    "authorization", "worker_token", "session_nonce", "nonce", "token", "bearer", "access_token", "refresh_token",
    "api_key", "secret", "password", "client_secret",
}


def _is_redacted_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _REDACT_KEYS or normalized.endswith("_token") and normalized != "instrument_token"


def _convert(value: Any, warnings: list[str], *, key: str | None = None) -> Any:
    if key is not None and _is_redacted_key(key):
        warning = f"redacted sensitive field: {key}"
        if warning not in warnings:
            warnings.append(warning)
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        warning = "non-finite numerical value serialized as null"
        if warning not in warnings:
            warnings.append(warning)
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _convert(value.value, warnings)
    if hasattr(value, "model_dump"):
        return _convert(value.model_dump(mode="python"), warnings, key=key)
    if is_dataclass(value):
        return _convert(asdict(value), warnings, key=key)
    if isinstance(value, Mapping):
        return {
            str(item_key): _convert(item_value, warnings, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_convert(item, warnings) for item in value]
    # Numpy scalars and similar numeric wrappers expose item().  Keep this
    # narrow; arbitrary objects must not leak reprs containing credentials.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _convert(item(), warnings, key=key)
        except Exception:
            pass
    return str(value)


def to_jsonable(value: Any) -> tuple[Any, list[str]]:
    warnings: list[str] = []
    return _convert(value, warnings), warnings


def serialize_json(value: Any, *, max_bytes: int = MAX_RESULT_BYTES) -> tuple[Any, list[str]]:
    converted, warnings = to_jsonable(value)
    encoded = json.dumps(converted, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise SerializationLimitError(
            f"serialized result is {len(encoded)} bytes; narrow the request below {max_bytes} bytes"
        )
    return converted, warnings


def ok_result(data: Any, *, next_cursor: str | None = None, max_bytes: int = MAX_RESULT_BYTES) -> ToolResult:
    converted, warnings = serialize_json(data, max_bytes=max_bytes)
    return ToolResult(status="ok", data=converted, warnings=warnings, next_cursor=next_cursor)


def error_result(code: str, message: str, *, retryable: bool = False, outcome_unknown: bool = False,
                 reconcile_with: str | None = None,
                 identifiers: Mapping[str, str | int] | None = None) -> ToolResult:
    return ToolResult(
        status="error",
        error={
            "code": code,
            "message": message,
            "retryable": retryable,
            "outcome_unknown": outcome_unknown,
            "reconcile_with": reconcile_with,
            "identifiers": dict(identifiers or {}),
        },
    )


__all__ = ["SerializationLimitError", "to_jsonable", "serialize_json", "ok_result", "error_result"]
