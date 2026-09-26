"""The platform status read model behind ``GET /api/platform/status``.

One screen, one honest answer per component. Every state here is derived from a
specific piece of evidence, and a component whose evidence is missing reports
``unknown`` (or ``down`` where the platform itself says it is not healthy) rather
than being guessed from a neighbour:

* **broker** - presence of the ``system`` ``kite_sessions`` row. This is a
  RECORDED session, not a re-verified token; the detail string says so instead of
  implying a broker round-trip happened.
* **market_data** - the runtime's own published status
  (``market:status``, written by ``market-runtime``) and how old its last tick
  is. A live market with quiet ticks is reported ``stale``; no published status
  at all is ``down``.
* **strategy_runner** - the newest mutation on a LEASED job row. Heartbeats
  renew ``lease_until`` and touch ``updated_at``, so that timestamp is the last
  evidence of a live supervisor. No leased job means ``unknown``: the supervisor
  itself keeps no database session and cannot be observed when idle.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select

from backend.platform.settings import platform_session_factory

logger = logging.getLogger(__name__)

#: The Redis key ``market-runtime`` writes its own ``RuntimeStatus`` to
#: (``market-runtime/internal/service/redis.go``).
MARKET_STATUS_KEY = "market:status"

#: How old the runtime's last published tick may be before the feed is ``stale``.
#: The marketwatch stream heartbeat is 15 s, so a runtime that is publishing has
#: ticked well inside this window.
MARKET_TICK_STALE_SECONDS = 15.0

#: Job statuses that mean a supervisor lease is (or should be) live. The same set
#: ``repository.renew_lease`` requires, so "leased" means one thing platform-wide.
LIVE_JOB_STATUSES = ("starting", "running", "fencing")

#: The runtime's own healthy word (``market-runtime/internal/service/service.go``).
RUNTIME_HEALTHY = "healthy"

MarketStatusReader = Callable[[], Any]


def _utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any, now: datetime) -> Optional[float]:
    moment = _utc(value)
    if moment is None:
        return None
    age = (now - moment).total_seconds()
    # A clock skew between the platform and the runtime must not report a
    # NEGATIVE age; the event is simply "just now".
    return max(0.0, round(age, 3))


def mask_account_scope(scope: str) -> str:
    """``kite:XJJ12345`` -> ``kite:XJJ***``. Never a full account identity."""
    value = str(scope or "")
    scheme, sep, identifier = value.partition(":")
    if not sep:
        return value if len(value) <= 3 else f"{value[:3]}***"
    return f"{scheme}:{identifier[:3]}***"


def _system_kite_session(
    session_factory: Optional[Callable[[], Any]],
) -> Dict[str, Any]:
    """The ``system`` broker session, or a reason it could not be read."""
    from backend.broker_api.session.kite_session import KiteSession

    try:
        with platform_session_factory(session_factory)() as session:
            row = session.execute(
                select(KiteSession).where(KiteSession.session_id == "system")
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - an unreadable store is "unknown"
        logger.warning("broker session store could not be read", exc_info=True)
        return {
            "state": "unknown",
            "detail": "broker session store unavailable",
            "account": None,
        }
    if row is None or not str(getattr(row, "access_token", "") or ""):
        return {
            "state": "expired",
            "detail": "no system kite session on record; a fresh broker login is required",
            "account": None,
        }
    from backend.broker_api.session.kite_session import make_account_id

    account = make_account_id(getattr(row, "broker_user_id", None))
    return {
        "state": "connected",
        "detail": (
            "system kite session recorded; token validity is not re-verified here"
        ),
        "account": account,
    }


def broker_status_view(
    session_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    session = _system_kite_session(session_factory)
    return {"state": session["state"], "detail": session["detail"]}


def broker_account_view(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    account_scopes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """The platform account: the recorded session's account, or the single
    allowlisted scope when exactly one is configured.

    ``allowed`` is the server allowlist's verdict (``HOSTED_STRATEGY_ACCOUNT_SCOPES``
    is default-deny), so the UI can show "this account is not authorised for
    hosted strategies" without inventing a scope.
    """
    if account_scopes is None:
        from backend.api.services.hosted_strategy_authz import authorized_account_scopes

        scopes: List[str] = list(authorized_account_scopes())
    else:
        scopes = [str(scope) for scope in account_scopes]

    recorded = _system_kite_session(session_factory)["account"]
    if recorded:
        candidate = str(recorded)
    elif len(scopes) == 1:
        candidate = str(scopes[0])
    else:
        candidate = ""
    return {
        "scope": mask_account_scope(candidate),
        "allowed": bool(candidate) and candidate in scopes,
    }


async def redis_market_status() -> Optional[Mapping[str, Any]]:
    """The runtime's published ``RuntimeStatus``, or ``None`` when there is none."""
    from backend.broker_api.core.redis_events import get_redis

    raw = await get_redis().get(MARKET_STATUS_KEY)
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("%s held a non-JSON runtime status", MARKET_STATUS_KEY)
        return None
    return payload if isinstance(payload, Mapping) else None


async def market_data_view(
    *,
    reader: Optional[MarketStatusReader] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    read = reader or redis_market_status
    try:
        status = read()
        if inspect.isawaitable(status):
            status = await status
    except Exception:  # noqa: BLE001 - an unreachable feed is "down", never a 500
        logger.warning("market runtime status could not be read", exc_info=True)
        return {"state": "down", "last_tick_age_s": None}
    if not isinstance(status, Mapping):
        return {"state": "down", "last_tick_age_s": None}

    age = _age_seconds(status.get("last_tick_at"), moment)
    runtime_state = str(status.get("status") or "").strip().lower()
    if runtime_state and runtime_state != RUNTIME_HEALTHY:
        # The runtime publishes its own unhealthy word (degraded, exhausted,
        # waiting_for_token). Report the component down rather than translating
        # one of its words into an "ok" of our own.
        return {"state": "down", "last_tick_age_s": age}
    if age is None:
        # Up, but no tick has ever been published: not fresh evidence.
        return {"state": "stale", "last_tick_age_s": None}
    return {
        "state": "ok" if age <= MARKET_TICK_STALE_SECONDS else "stale",
        "last_tick_age_s": age,
    }


def strategy_runner_view(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    from backend.strategies.models import StrategyJob

    moment = now or datetime.now(timezone.utc)
    try:
        with platform_session_factory(session_factory)() as session:
            rows = session.execute(
                select(StrategyJob.updated_at, StrategyJob.lease_until).where(
                    StrategyJob.status.in_(LIVE_JOB_STATUSES),
                    StrategyJob.lease_until.is_not(None),
                )
            ).all()
    except Exception:  # noqa: BLE001 - an unreadable runner view is "unknown"
        logger.warning("hosted runner status could not be read", exc_info=True)
        return {"state": "unknown", "last_seen_age_s": None}

    if not rows:
        # Idle and healthy, or never started: with no lease on record the
        # platform has no evidence either way, so it says unknown.
        return {"state": "unknown", "last_seen_age_s": None}

    latest = max((moment_ for moment_, _ in rows if moment_ is not None), default=None)
    age = _age_seconds(latest, moment)
    lease_live = any(
        (lease := _utc(lease_until)) is not None and lease > moment
        for _, lease_until in rows
    )
    return {
        "state": "ok" if lease_live else "stale",
        "last_seen_age_s": None if age is None else int(age),
    }


async def platform_status_view(
    *,
    session_factory: Optional[Callable[[], Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    market_reader: Optional[MarketStatusReader] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble the whole status document. ``mode`` is derived, never stored."""
    from backend.strategies.live_service import enabled_live_lanes
    from backend.strategies.live_settings import hosted_live_enabled

    moment = now or datetime.now(timezone.utc)
    source = os.environ if environ is None else environ
    live_enabled = hosted_live_enabled(source)
    lanes_open = enabled_live_lanes(environ)
    return {
        "mode": "live" if live_enabled and lanes_open else "paper",
        "broker": broker_status_view(session_factory),
        "market_data": await market_data_view(reader=market_reader, now=moment),
        "strategy_runner": strategy_runner_view(session_factory, now=moment),
        "live": {"enabled": live_enabled, "lanes_open": list(lanes_open)},
    }


__all__ = [
    "MARKET_STATUS_KEY",
    "MARKET_TICK_STALE_SECONDS",
    "broker_account_view",
    "broker_status_view",
    "market_data_view",
    "mask_account_scope",
    "platform_status_view",
    "redis_market_status",
    "strategy_runner_view",
]
