"""The persisted live-lane setting row and the writes that change it.

``platform_live_settings`` is the operator's per-lane answer to "which lanes may
take NEW exposure", persisted so a change takes effect without a redeploy. It
gates nothing else: reductions, exits, the MIS square-off, repair and flatten
keep working while a lane is closed, exactly as they did when
``HOSTED_LIVE_LANES`` was the only source.

Two properties this module owes the live path:

* **Default deny, never a widened fail-open.** An absent row, an absent lane key,
  a non-boolean value and an unreadable database all mean "that lane is not
  open". A database that cannot be read falls back to the deployment env
  allowlist (the documented default), never to "every lane".
* **Every change is audited.** The new map and its audit row (actor, reason,
  previous map) are written in ONE transaction, so a settings change can never
  be legible without its author.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.platform.models import (
    LIVE_SETTINGS_SINGLETON_ID,
    PlatformLiveSetting,
    PlatformLiveSettingAudit,
)

logger = logging.getLogger(__name__)

#: The public lane names an operator chooses between. Exactly the lanes the live
#: executor maps plan kinds for (``live_service.KNOWN_LIVE_LANES``), so this map
#: cannot drift into naming a lane nothing can execute.
LIVE_LANE_KEYS = ("cnc", "mis", "futures", "options")

#: Set by tests to pin the session factory without touching the real database.
_session_factory_override: Optional[Callable[[], Any]] = None

#: Databases already reported unreadable, so a pre-migration deployment warns
#: once per process instead of on every release pass.
_WARNED_UNREADABLE: set = set()


def platform_session_factory(
    session_factory: Optional[Callable[[], Any]] = None,
) -> Callable[[], Any]:
    """The session factory to use: an explicit one, a test override, else the app's."""
    if session_factory is not None:
        return session_factory
    if _session_factory_override is not None:
        return _session_factory_override()
    from backend.app.database import SessionLocal

    return SessionLocal


def _as_flag(value: Any) -> bool:
    """Only a real boolean (or 0/1) opens a lane.

    A string such as ``"false"`` is truthy in Python, so coercing everything
    would turn a hand-edited row into an open lane. Anything that is not a
    boolean/0-1 - including a missing key - is NOT an opening answer.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return False


def normalise_lanes(raw: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """The full lane map, with every unknown/missing lane closed."""
    source = raw or {}
    return {lane: _as_flag(source.get(lane)) for lane in LIVE_LANE_KEYS}


@dataclass(frozen=True)
class PersistedLaneSettings:
    """The stored lane map plus who last wrote it."""

    lanes: Dict[str, bool]
    updated_at: Optional[datetime]
    updated_by: Optional[str]


def read_live_settings(
    session_factory: Optional[Callable[[], Any]] = None,
) -> Optional[PersistedLaneSettings]:
    """The persisted settings row, or ``None`` when there is none.

    An unreadable database (no table yet, no connection) is NOT an exception
    raised at the caller: it reports ``None`` so the caller keeps the deployment
    env allowlist it already had, which can only be narrower than "open".
    """
    factory = platform_session_factory(session_factory)
    try:
        with factory() as session:
            row = session.execute(
                select(PlatformLiveSetting).where(
                    PlatformLiveSetting.settings_id == LIVE_SETTINGS_SINGLETON_ID
                )
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - an unreadable row is "no row"
        marker = type(exc).__name__
        if marker not in _WARNED_UNREADABLE:
            _WARNED_UNREADABLE.add(marker)
            logger.warning(
                "platform live settings could not be read (%s); falling back to "
                "the deployment lane allowlist",
                marker,
            )
        return None
    if row is None:
        return None
    return PersistedLaneSettings(
        lanes=normalise_lanes(row.lanes),
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _write_once(
    session: Any,
    lanes: Dict[str, bool],
    *,
    actor_id: str,
    reason: Optional[str],
    moment: datetime,
) -> Dict[str, bool]:
    row = session.execute(
        select(PlatformLiveSetting).where(
            PlatformLiveSetting.settings_id == LIVE_SETTINGS_SINGLETON_ID
        )
    ).scalar_one_or_none()
    previous = normalise_lanes(row.lanes) if row is not None else {}
    if row is None:
        session.add(
            PlatformLiveSetting(
                settings_id=LIVE_SETTINGS_SINGLETON_ID,
                lanes=lanes,
                updated_by=actor_id,
                updated_at=moment,
            )
        )
    else:
        row.lanes = lanes
        row.updated_by = actor_id
        row.updated_at = moment
    session.add(
        PlatformLiveSettingAudit(
            actor_id=actor_id,
            reason=(str(reason).strip() or None) if reason is not None else None,
            previous_lanes=previous,
            lanes=lanes,
            created_at=moment,
        )
    )
    session.commit()
    return previous


def update_live_settings(
    lanes: Mapping[str, Any],
    *,
    actor_id: str,
    reason: Optional[str] = None,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
) -> PersistedLaneSettings:
    """Persist the lane map and append its audit row, atomically.

    Two owners saving at once would otherwise both see "no row" and both insert;
    the loser's write is retried as an update once, so the singleton stays a
    singleton and the last committed write wins rather than erroring at the
    operator.
    """
    settings = normalise_lanes(lanes)
    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("actor_id is required to change live lane settings")
    moment = now or datetime.now(timezone.utc)
    factory = platform_session_factory(session_factory)

    for attempt in (1, 2):
        try:
            with factory() as session:
                _write_once(
                    session,
                    settings,
                    actor_id=actor,
                    reason=reason,
                    moment=moment,
                )
            break
        except IntegrityError:
            if attempt == 2:
                raise
            logger.info(
                "platform live settings insert lost a race; retrying as an update"
            )

    return PersistedLaneSettings(lanes=settings, updated_at=moment, updated_by=actor)


__all__ = [
    "LIVE_LANE_KEYS",
    "PersistedLaneSettings",
    "normalise_lanes",
    "platform_session_factory",
    "read_live_settings",
    "update_live_settings",
]
