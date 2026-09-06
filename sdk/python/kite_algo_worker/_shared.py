"""Transport-neutral request validation and payload builders.

Both worker clients intentionally keep their transports separate.  This module
contains only the deterministic pieces that must remain byte-for-byte aligned
between ``requests`` and ``httpx`` callers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

JsonDict = dict[str, Any]


def omit_none_params(params: Any) -> Any:
    """Remove omitted query values while preserving sync query semantics.

    ``requests`` skips ``None`` query values while preparing a request, but
    ``httpx`` serializes them as empty values.  Keep this normalization in the
    transport-neutral module so async JSON, text, and streaming calls share the
    same wire contract as the synchronous client.  ``requests`` also spells
    boolean query values as ``True``/``False`` while ``httpx`` lowercases them,
    so normalize that representation without dropping meaningful false-y
    values.  Lists/tuples of query pairs are supported as well as mappings
    because both are accepted by the HTTP clients.
    """

    def _normalize_value(value: Any) -> Any:
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, list):
            return [_normalize_value(item) for item in value if item is not None]
        if isinstance(value, tuple):
            return tuple(_normalize_value(item) for item in value if item is not None)
        return value

    if params is None:
        return None
    if isinstance(params, Mapping):
        return {
            key: _normalize_value(value)
            for key, value in params.items()
            if value is not None
        }
    if isinstance(params, list):
        return [
            (key, _normalize_value(value))
            for key, value in params
            if value is not None
        ]
    if isinstance(params, tuple):
        return tuple(
            (key, _normalize_value(value))
            for key, value in params
            if value is not None
        )
    return params


def require_identity_param(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def run_list_params(*, limit: int = 25, cursor: Optional[str] = None) -> JsonDict:
    """Build the common token-scoped run-list query for both transports."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    params: JsonDict = {"limit": limit}
    if cursor is not None:
        cursor_text = str(cursor).strip()
        if not cursor_text:
            raise ValueError("cursor must not be empty when provided")
        params["cursor"] = cursor_text
    return params


def require_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("idempotency_key is required for order intents")
    if not 8 <= len(key) <= 160:
        raise ValueError("idempotency_key must be between 8 and 160 characters")
    return key


def session_headers(session_nonce: Optional[str]) -> Optional[dict[str, str]]:
    return {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None


def split_instruments(instruments: Iterable[str | int]) -> tuple[list[str], list[int]]:
    symbols: list[str] = []
    tokens: list[int] = []
    for item in instruments:
        value = str(item).strip()
        if isinstance(item, int) or value.isdigit():
            tokens.append(int(value))
        elif value:
            symbols.append(value)
    return symbols, tokens


def build_heartbeat_payload(
    *, worker_id: Optional[str], status: str, metrics: Optional[Mapping[str, Any]]
) -> JsonDict:
    payload: JsonDict = {"status": status, "metrics": dict(metrics or {})}
    if worker_id is not None:
        payload["worker_id"] = worker_id
    return payload


def build_create_run_payload(
    *,
    template_id: str,
    account_scope: str,
    strategy_run_id: Optional[str],
    execution_mode: str,
    summary_fields: Optional[Iterable[Mapping[str, Any]]],
    risk_schema: Optional[Iterable[Mapping[str, Any]]],
    allowed_actions: Optional[Iterable[str]],
    runtime_state: Optional[Mapping[str, Any]],
    metadata: Optional[Mapping[str, Any]],
    backend_protection: Any = None,
) -> JsonDict:
    state = dict(runtime_state or {})
    if backend_protection is not None:
        state["backend_protection"] = backend_protection.to_dict()
    payload: JsonDict = {
        "template_id": template_id,
        "account_scope": account_scope,
        "execution_mode": execution_mode,
        "summary_fields": [dict(item) for item in (summary_fields or [])],
        "risk_schema": [dict(item) for item in (risk_schema or [])],
        "allowed_actions": list(allowed_actions or ["edit_risk", "exit_strategy"]),
        "runtime_state": state,
        "metadata": dict(metadata or {}),
    }
    if strategy_run_id is not None:
        payload["strategy_run_id"] = strategy_run_id
    return payload


def build_intent_payload(
    *,
    intent_type: str,
    body_key: str,
    body: Any,
    idempotency_key: str,
    metadata: Optional[Mapping[str, Any]],
    safety_token: Optional[str],
    extras: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    payload: JsonDict = {
        "intent_type": intent_type,
        "payload": {body_key: body},
        "idempotency_key": require_idempotency_key(idempotency_key),
        "metadata": dict(metadata or {}),
    }
    payload["payload"].update(dict(extras or {}))
    if safety_token is not None:
        payload["safety_token"] = str(safety_token)
    return payload


def build_historical_date_params(
    *, from_date: Any = None, to_date: Any = None, lookback_days: Optional[int] = None
) -> JsonDict:
    if lookback_days is not None and lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if from_date is not None and lookback_days is not None:
        raise ValueError("from_date and lookback_days are mutually exclusive")

    params: JsonDict = {}
    if to_date is not None:
        params["to"] = to_date.isoformat() if isinstance(to_date, datetime) else to_date
    elif lookback_days is not None:
        params["to"] = datetime.now(timezone.utc).isoformat()

    if from_date is not None:
        params["from"] = from_date.isoformat() if isinstance(from_date, datetime) else from_date
    elif lookback_days is not None:
        to_dt = datetime.fromisoformat(str(params["to"]).replace("Z", "+00:00"))
        if to_dt.tzinfo is None:
            raise ValueError("to_date must include timezone information when lookback_days is used")
        params["from"] = (to_dt - timedelta(days=int(lookback_days))).isoformat()
    return params


def fundamentals_scope_params(symbols: Optional[Iterable[str]], index: Optional[str]) -> JsonDict:
    if bool(symbols) == bool(index):
        raise ValueError("provide exactly one of 'symbols' or 'index'")
    if symbols:
        cleaned = [str(item).strip().upper() for item in symbols if str(item).strip()]
        if not cleaned:
            raise ValueError("symbols must not be empty when provided")
        return {"symbols": cleaned}
    return {"index": require_identity_param(index, field_name="index")}


def normalize_calendar_date_params(
    from_date: Any, to_date: Any, *, exchange: Any, segment: Any
) -> JsonDict:
    from_text = require_identity_param(from_date, field_name="from_date")
    to_text = require_identity_param(to_date, field_name="to_date")
    try:
        start, end = date.fromisoformat(from_text), date.fromisoformat(to_text)
    except ValueError as exc:
        raise ValueError("from_date and to_date must be ISO dates (YYYY-MM-DD)") from exc
    if start > end:
        raise ValueError("from_date must not be after to_date")
    return {
        "from": from_text,
        "to": to_text,
        "exchange": require_identity_param(exchange, field_name="exchange").upper(),
        "segment": require_identity_param(segment, field_name="segment").upper(),
    }


__all__ = [
    "JsonDict",
    "build_create_run_payload",
    "build_heartbeat_payload",
    "build_historical_date_params",
    "build_intent_payload",
    "fundamentals_scope_params",
    "normalize_calendar_date_params",
    "omit_none_params",
    "require_idempotency_key",
    "require_identity_param",
    "run_list_params",
    "session_headers",
    "split_instruments",
]
