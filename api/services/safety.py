from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

_ALLOWED_OPTION_STATUSES = {
    "created",
    "entry_previewed",
    "entering",
    "entered",
    "partial_entry",
}


def build_safety_fingerprint(
    *,
    run_status: str,
    generic_status: str,
    generic_exit_submitted: bool,
    option_run_status: str | None,
) -> str:
    raw = "|".join(
        [
            str(run_status or ""),
            str(generic_status or ""),
            "1" if generic_exit_submitted else "0",
            str(option_run_status or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def option_run_status_blocks_trading(status: str | None) -> bool:
    if status is None:
        return False
    return str(status) not in _ALLOWED_OPTION_STATUSES


def build_signed_safety_token(
    *,
    strategy_run_id: str,
    fingerprint: str,
    secret: str,
    now: datetime,
    ttl_seconds: int = 10,
) -> str:
    expires_at = (now + timedelta(seconds=ttl_seconds)).astimezone(timezone.utc).isoformat()
    payload = {
        "strategy_run_id": str(strategy_run_id),
        "fingerprint": str(fingerprint),
        "expires_at": expires_at,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    envelope = {"payload": payload, "sig": signature}
    token_raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(token_raw).decode("utf-8")


def verify_signed_safety_token(token: str, strategy_run_id: str, secret: str, *, now: datetime) -> dict[str, Any] | None:
    try:
        encoded = token.encode("utf-8")
        padding = b"=" * ((4 - (len(encoded) % 4)) % 4)
        decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        envelope = json.loads(decoded)
        payload = dict(envelope["payload"])
        supplied_sig = str(envelope["sig"])
        if payload.get("strategy_run_id") != strategy_run_id:
            return None
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected_sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_sig, expected_sig):
            return None
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        if expires_at <= now.astimezone(timezone.utc):
            return None
        return payload
    except Exception:
        return None
