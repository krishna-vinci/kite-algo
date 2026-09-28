"""Persisted, live-applied option-chain runtime settings."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.platform.models import (
    OPTIONS_SETTINGS_SINGLETON_ID,
    PlatformOptionsSetting,
    PlatformOptionsSettingAudit,
)
from backend.platform.settings import platform_session_factory

logger = logging.getLogger(__name__)

AVAILABLE_OPTION_UNDERLYINGS = (
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
    "FINNIFTY",
    "MIDCPNIFTY",
    "BANKEX",
)


@dataclass(frozen=True)
class OptionsSettings:
    always_on: list[str]
    cadence_sec: int
    tick_driven: bool
    min_interval_sec: float
    idle_stop_minutes: int
    source: str
    updated_at: Optional[datetime]
    updated_by: Optional[str]


def _default_always_on() -> list[str]:
    raw = os.environ.get("OPTIONS_AUTOSTART_UNDERLYINGS")
    values = raw.split(",") if raw is not None else ["NIFTY"]
    normalized: list[str] = []
    for value in values:
        symbol = str(value or "").strip().upper()
        if (
            symbol in AVAILABLE_OPTION_UNDERLYINGS
            and symbol not in normalized
        ):
            normalized.append(symbol)
    return normalized


def _defaults() -> OptionsSettings:
    return OptionsSettings(
        always_on=_default_always_on(),
        cadence_sec=5,
        tick_driven=True,
        min_interval_sec=1.0,
        idle_stop_minutes=15,
        source="default",
        updated_at=None,
        updated_by=None,
    )


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def validate_options_settings(
    *,
    always_on: Sequence[str],
    cadence_sec: int,
    tick_driven: bool,
    min_interval_sec: float,
    idle_stop_minutes: int,
) -> tuple[list[str], int, bool, float, int]:
    if isinstance(always_on, (str, bytes)):
        raise ValueError("always_on must be a list")
    normalized: list[str] = []
    for value in always_on:
        symbol = str(value or "").strip().upper()
        if symbol not in AVAILABLE_OPTION_UNDERLYINGS:
            raise ValueError(f"unsupported option underlying: {symbol or value}")
        if symbol not in normalized:
            normalized.append(symbol)
    if len(normalized) > 3:
        raise ValueError("always_on may contain at most 3 underlyings")
    if isinstance(cadence_sec, bool) or not isinstance(cadence_sec, int) or not 1 <= cadence_sec <= 10:
        raise ValueError("cadence_sec must be an integer between 1 and 10")
    if not isinstance(tick_driven, bool):
        raise ValueError("tick_driven must be a boolean")
    minimum = float(min_interval_sec)
    if not 0.25 <= minimum <= 10.0:
        raise ValueError("min_interval_sec must be between 0.25 and 10")
    if minimum > cadence_sec:
        raise ValueError("min_interval_sec must be less than or equal to cadence_sec")
    if (
        isinstance(idle_stop_minutes, bool)
        or not isinstance(idle_stop_minutes, int)
        or not 0 <= idle_stop_minutes <= 390
    ):
        raise ValueError("idle_stop_minutes must be an integer between 0 and 390")
    return normalized, cadence_sec, tick_driven, minimum, idle_stop_minutes


def read_options_settings(
    session_factory: Optional[Callable[[], Any]] = None,
) -> OptionsSettings:
    try:
        factory = platform_session_factory(session_factory)
        with factory() as session:
            row = session.execute(
                select(PlatformOptionsSetting).where(
                    PlatformOptionsSetting.settings_id == OPTIONS_SETTINGS_SINGLETON_ID
                )
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - pre-migration/unavailable DB uses defaults
        logger.warning(
            "platform option settings could not be read (%s); using defaults",
            type(exc).__name__,
        )
        return _defaults()
    if row is None:
        return _defaults()
    always_on, cadence, tick_driven, minimum, idle = validate_options_settings(
        always_on=row.always_on,
        cadence_sec=row.cadence_sec,
        tick_driven=row.tick_driven,
        min_interval_sec=float(row.min_interval_sec),
        idle_stop_minutes=row.idle_stop_minutes,
    )
    return OptionsSettings(
        always_on=always_on,
        cadence_sec=cadence,
        tick_driven=tick_driven,
        min_interval_sec=minimum,
        idle_stop_minutes=idle,
        source="db",
        updated_at=_as_utc(row.updated_at),
        updated_by=row.updated_by,
    )


def _write_once(
    session: Any,
    settings: OptionsSettings,
    *,
    actor_id: str,
    reason: Optional[str],
    moment: datetime,
) -> None:
    row = session.execute(
        select(PlatformOptionsSetting).where(
            PlatformOptionsSetting.settings_id == OPTIONS_SETTINGS_SINGLETON_ID
        )
    ).scalar_one_or_none()
    previous = (
        OptionsSettings(
            always_on=list(row.always_on),
            cadence_sec=int(row.cadence_sec),
            tick_driven=bool(row.tick_driven),
            min_interval_sec=float(row.min_interval_sec),
            idle_stop_minutes=int(row.idle_stop_minutes),
            source="db",
            updated_at=_as_utc(row.updated_at),
            updated_by=row.updated_by,
        )
        if row is not None
        else _defaults()
    )
    if row is None:
        row = PlatformOptionsSetting(settings_id=OPTIONS_SETTINGS_SINGLETON_ID)
        session.add(row)
    row.always_on = list(settings.always_on)
    row.cadence_sec = settings.cadence_sec
    row.tick_driven = settings.tick_driven
    row.min_interval_sec = settings.min_interval_sec
    row.idle_stop_minutes = settings.idle_stop_minutes
    row.updated_by = actor_id
    row.updated_at = moment
    session.add(
        PlatformOptionsSettingAudit(
            actor_id=actor_id,
            reason=(str(reason).strip() or None) if reason is not None else None,
            previous_always_on=list(previous.always_on),
            always_on=list(settings.always_on),
            previous_cadence_sec=previous.cadence_sec,
            cadence_sec=settings.cadence_sec,
            previous_tick_driven=previous.tick_driven,
            tick_driven=settings.tick_driven,
            previous_min_interval_sec=previous.min_interval_sec,
            min_interval_sec=settings.min_interval_sec,
            previous_idle_stop_minutes=previous.idle_stop_minutes,
            idle_stop_minutes=settings.idle_stop_minutes,
            created_at=moment,
        )
    )
    session.commit()


def update_options_settings(
    *,
    always_on: Sequence[str],
    cadence_sec: int,
    tick_driven: bool,
    min_interval_sec: float,
    idle_stop_minutes: int,
    actor_id: str,
    reason: Optional[str] = None,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
) -> OptionsSettings:
    values = validate_options_settings(
        always_on=always_on,
        cadence_sec=cadence_sec,
        tick_driven=tick_driven,
        min_interval_sec=min_interval_sec,
        idle_stop_minutes=idle_stop_minutes,
    )
    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("actor_id is required to change option settings")
    moment = now or datetime.now(timezone.utc)
    settings = OptionsSettings(
        always_on=values[0],
        cadence_sec=values[1],
        tick_driven=values[2],
        min_interval_sec=values[3],
        idle_stop_minutes=values[4],
        source="db",
        updated_at=moment,
        updated_by=actor,
    )
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
            logger.info("platform option settings insert lost a race; retrying")
    return settings


__all__ = [
    "AVAILABLE_OPTION_UNDERLYINGS",
    "OptionsSettings",
    "read_options_settings",
    "update_options_settings",
    "validate_options_settings",
]
