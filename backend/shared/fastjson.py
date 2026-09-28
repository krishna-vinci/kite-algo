"""Fast JSON helpers for hot-path payloads, with a stdlib fallback."""
from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Callable

try:
    import orjson
except ImportError:  # pragma: no cover - exercised through reload in tests.
    orjson = None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, date):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def loads(payload: str | bytes) -> Any:
    if orjson is not None:
        try:
            return orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            document = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
            raise json.JSONDecodeError(str(exc), document, 0) from exc
    return json.loads(payload)


def dumps_str(value: Any, *, default: Callable[[Any], Any] = str) -> str:
    safe_value = _json_safe(value)
    if orjson is not None:
        return orjson.dumps(
            safe_value,
            default=default,
            option=orjson.OPT_NON_STR_KEYS,
        ).decode("utf-8")
    return json.dumps(safe_value, default=default, allow_nan=False)
