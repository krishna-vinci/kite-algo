"""ORM tables for the platform live-lane setting and its audit trail.

Migration ``20260926_000052_platform_live_settings`` creates these tables and
``20260926_000053_account_daily_loss_cap`` adds the account-wide day-loss cap
columns. The ORM uses ``JSON`` (portable to the SQLite test database) while the
migrations and ``schema.sql`` use ``JSONB`` - the established pattern in this
repo.

``platform_live_settings`` is a SINGLE row: ``settings_id`` is a check-enforced
singleton, so "the platform has one lane policy" is a database fact rather than
a convention two callers can disagree about.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    Text,
    func,
)

from backend.workflows.repository import Base

#: The only ``settings_id`` the singleton row may carry.
LIVE_SETTINGS_SINGLETON_ID = 1


class PlatformLiveSetting(Base):
    """The operator's per-lane answer for NEW live exposure.

    This row does **not** arm live trading: ``HOSTED_LIVE_ENABLED`` still gates
    all live execution, and a lane map that opens nothing is a valid, safe row.
    An ABSENT row means the deployment env allowlist decides - never "open".
    """

    __tablename__ = "platform_live_settings"

    settings_id = Column(Integer, primary_key=True, default=LIVE_SETTINGS_SINGLETON_ID)
    #: ``{"cnc": bool, "mis": bool, "futures": bool, "options": bool}``.
    lanes = Column(JSON, nullable=False, default=dict)
    #: The account-wide day-loss cap in INR, or NULL for "no cap configured".
    #: Enforced at admission against the broker's own day P&L, never here.
    account_daily_loss_cap_inr = Column(Numeric(18, 2), nullable=True)
    #: The server-derived actor (``app:<username>``) of the last change.
    updated_by = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"settings_id = {LIVE_SETTINGS_SINGLETON_ID}",
            name="ck_platform_live_settings_singleton",
        ),
    )


class PlatformLiveSettingAudit(Base):
    """Append-only audit of live-lane changes.

    One row per write, carrying the actor, the reason, and the lane map before
    and after. The migration installs an insert-only trigger, so a change can
    never be rewritten or erased after the fact.
    """

    __tablename__ = "platform_live_settings_audit"

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    previous_lanes = Column(JSON, nullable=False, default=dict)
    lanes = Column(JSON, nullable=False, default=dict)
    previous_account_daily_loss_cap_inr = Column(Numeric(18, 2), nullable=True)
    account_daily_loss_cap_inr = Column(Numeric(18, 2), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_platform_live_settings_audit_created", "created_at"),)


__all__ = [
    "LIVE_SETTINGS_SINGLETON_ID",
    "PlatformLiveSetting",
    "PlatformLiveSettingAudit",
]
