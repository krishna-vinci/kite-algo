from __future__ import annotations

import json
from typing import Any, Mapping


OPTION_SNAPSHOT_SCHEMA_VERSION = 1
OPTION_SNAPSHOT_TTL_SECONDS = 120


def normalize_option_snapshot_underlying(underlying: str) -> str:
    return str(underlying or "").strip().upper()


def option_snapshot_v1_key(underlying: str) -> str:
    normalized = normalize_option_snapshot_underlying(underlying)
    return f"options:chain:v1:{normalized}"


def option_snapshot_v1_updates_channel(underlying: str) -> str:
    normalized = normalize_option_snapshot_underlying(underlying)
    return f"options:chain:v1:updates:{normalized}"


def build_option_snapshot_v1_payload(snapshot: Mapping[str, Any], underlying: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": OPTION_SNAPSHOT_SCHEMA_VERSION,
        "underlying": normalize_option_snapshot_underlying(underlying),
        "updated_at": snapshot.get("updated_at"),
        "cadence_sec": snapshot.get("cadence_sec"),
        "expiries": snapshot.get("expiries", []),
        "per_expiry": snapshot.get("per_expiry", {}),
        "desired_token_count": snapshot.get("desired_token_count"),
    }
    if "resource_error" in snapshot:
        payload["resource_error"] = snapshot.get("resource_error")
    return payload


def serialize_option_snapshot_v1(snapshot: Mapping[str, Any], underlying: str) -> str:
    payload = build_option_snapshot_v1_payload(snapshot, underlying)
    return json.dumps(payload, default=str)


async def read_option_snapshot_from_redis(redis_client: Any, underlying: str) -> dict[str, Any] | None:
    raw = await redis_client.get(option_snapshot_v1_key(underlying))
    if raw is None:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != OPTION_SNAPSHOT_SCHEMA_VERSION:
        return None

    return payload
