"""Operator-facing health: what the API can VERIFY, and what it cannot.

Two sources, never blended into a single confident-looking number:

1. **Database-derived facts.** The service persists per-tick receipt
   bookkeeping into the evaluation checkpoint on every accepted LTP tick
   (``last_tick_received_at``, ``last_tick_ts``) and persists a continuity
   invalidation with its reason (``continuity_invalidated_at``,
   ``continuity_invalidation_reason``). Those are durable, so the API process —
   which is NOT the evaluation process — can compute real tick age and real
   stale reasons from the database alone. This is what makes §3.1's "stale
   health observable with no tick arriving" true across a process boundary and
   across a restart: the age is derived from a stored timestamp, so silence
   ages on its own without any tick, counter or in-memory state.

2. **Worker-runtime facts.** Quarantine, per-subscription failure counters,
   suppression counters and required-task liveness live in the evaluation
   worker's memory. The worker publishes them to its health file, which is the
   channel its own container healthcheck already reads. When that file is
   reachable the API merges them; when it is not, the response says
   ``available: false`` with the reason instead of reporting zeros — an
   unreachable worker and a healthy worker must never look the same.

The distinction matters because "quarantined: 0" and "I cannot see quarantine
state" lead an operator to opposite conclusions, and only one of them is true.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "ALERTS_WORKER_HEALTH_FILE_ENV",
    "DEFAULT_WORKER_HEALTH_FILE",
    "MAX_HEALTH_FILE_BYTES",
    "derive_subscription_freshness",
    "parse_timestamp",
    "read_worker_health",
    "runtime_health_view",
]

ALERTS_WORKER_HEALTH_FILE_ENV = "ALERTS_WORKER_HEALTH_FILE"
DEFAULT_WORKER_HEALTH_FILE = "/app/alerts-health.json"

#: The file is written by our own worker, but a bound costs nothing and stops a
#: wrong path (a log file, a database file) from being read into memory.
MAX_HEALTH_FILE_BYTES = 4 * 1024 * 1024

#: Runtime-only sections copied through from the worker's health file. Named
#: explicitly rather than passing the whole snapshot through, so the operator
#: API cannot start leaking a new internal field by accident.
_RUNTIME_SECTIONS = (
    "quarantined",
    "subscription_failures",
    "tasks",
    "rejected_ticks",
    "stale_tick_instruments",
    "never_ticked_instruments",
    "future_ticks",
    "stale_ticks",
    "last_health_at",
    "ltp_freshness_enabled",
    "ltp_max_gap_s",
    "startup_error",
)


def parse_timestamp(raw: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp; None when absent or unparseable.

    Naive timestamps are read as UTC because that is what the writers emit; a
    naive value interpreted as local time would silently shift the age by the
    server offset.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def read_worker_health(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the evaluation worker's published health file.

    Returns a dict with ``available`` plus, when available, the runtime-only
    sections. Any failure — no path configured, missing file, unreadable,
    oversized, not JSON, not an object — resolves to ``available: False`` with a
    machine-readable reason, because guessing here would be indistinguishable
    from good news.
    """
    resolved = path or os.environ.get(ALERTS_WORKER_HEALTH_FILE_ENV) or DEFAULT_WORKER_HEALTH_FILE
    if not resolved:
        return {"available": False, "reason": "no_health_file_configured"}

    try:
        if not os.path.exists(resolved):
            return {
                "available": False,
                "reason": "health_file_absent",
                "path": resolved,
                "hint": (
                    "the evaluation worker publishes this file; if it runs in "
                    "another container the path must be mounted for the API to "
                    "read it"
                ),
            }
        size = os.path.getsize(resolved)
        if size > MAX_HEALTH_FILE_BYTES:
            return {
                "available": False,
                "reason": "health_file_too_large",
                "path": resolved,
                "size_bytes": size,
            }
        with open(resolved, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        return {
            "available": False,
            "reason": "health_file_unreadable",
            "path": resolved,
            "detail": str(exc),
        }
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "available": False,
            "reason": "health_file_invalid",
            "path": resolved,
            "detail": str(exc),
        }

    if not isinstance(payload, dict):
        return {
            "available": False,
            "reason": "health_file_not_an_object",
            "path": resolved,
        }

    view: Dict[str, Any] = {"available": True, "path": resolved}
    for key in _RUNTIME_SECTIONS:
        if key in payload:
            view[key] = payload[key]
    return view


def runtime_health_view(path: Optional[str] = None) -> Dict[str, Any]:
    """``read_worker_health`` plus a plain-language note on what it means."""
    view = read_worker_health(path)
    if view.get("available"):
        view["note"] = (
            "quarantine, per-subscription failure counts and task liveness are "
            "reported by the evaluation worker process; they are in-memory "
            "there and reset on restart"
        )
    else:
        view["note"] = (
            "the API could not read the evaluation worker's health file, so "
            "quarantine, failure counts and task liveness are UNKNOWN — this is "
            "not a report that they are zero or healthy"
        )
    return view


def derive_subscription_freshness(
    *,
    state: Optional[Dict[str, Any]],
    clock: Optional[str],
    now: Optional[datetime] = None,
    stale_after_s: float = 300.0,
) -> Dict[str, Any]:
    """Freshness for one subscription, from durable checkpoint state alone.

    Returns ``last_tick_received_at``, ``tick_age_s``, a named ``stale_reason``
    and the last continuity invalidation — all derived from timestamps the
    service already persists, so no worker state is required.

    ``stale_reason`` is named rather than boolean because the operator has to
    act differently in each case: ``no_accepted_tick`` means nothing has ever
    been evaluated for this subscription (a wiring or activation problem),
    ``tick_age_exceeded`` means it evaluated before and the feed has since gone
    quiet (a data problem), and ``continuity_invalidated`` means a silence was
    already detected and the crossing state deliberately reset (the alert needs
    a fresh crossing before it can fire again).
    """
    moment = now or datetime.now(timezone.utc)
    payload = state if isinstance(state, dict) else {}
    received = parse_timestamp(payload.get("last_tick_received_at"))
    event_ts = parse_timestamp(payload.get("last_tick_ts"))
    invalidated = parse_timestamp(payload.get("continuity_invalidated_at"))

    result: Dict[str, Any] = {
        "last_tick_received_at": received.isoformat() if received else None,
        "last_tick_ts": event_ts.isoformat() if event_ts else None,
        "tick_age_s": None,
        "stale": None,
        "stale_reason": None,
        "continuity_invalidated_at": invalidated.isoformat() if invalidated else None,
        "continuity_invalidation_reason": payload.get("continuity_invalidation_reason"),
        "received_at_is_receipt_not_event_time": bool(received is not None),
    }

    if clock != "ltp":
        # Only the LTP path writes per-tick receipt bookkeeping. A candle
        # subscription's freshness is the candle path's concern (`_is_stale_bar`
        # / checkpoint `updated_at`), so this deliberately reports no tick age
        # rather than reading candle semantics into a tick field.
        result["stale_reason"] = "not_an_ltp_subscription"
        return result

    if received is None:
        result["stale_reason"] = "no_accepted_tick"
        return result

    age = max(0.0, (moment - received).total_seconds())
    result["tick_age_s"] = round(age, 3)
    if age > stale_after_s:
        result["stale"] = True
        result["stale_reason"] = "tick_age_exceeded"
    elif payload.get("continuity_invalidation_reason"):
        result["stale"] = False
        result["stale_reason"] = "continuity_invalidated"
    else:
        result["stale"] = False
    return result
