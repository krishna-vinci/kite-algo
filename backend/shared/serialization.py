from __future__ import annotations

import hashlib
import json as _json
from datetime import datetime
from typing import Any, Dict


def _query_int_param(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        default_value = getattr(value, "default", default)
        try:
            return int(default_value)
        except Exception:
            return int(default)


def _utcnow() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _json_dumps(value: Any) -> str:
    return _json.dumps(value, default=_json_default)


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        return _json.loads(value)
    return value


def _row_mapping(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if isinstance(row, dict):
        return dict(row)
    return {
        key: getattr(row, key)
        for key in dir(row)
        if not key.startswith("_") and not callable(getattr(row, key))
    }


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default
