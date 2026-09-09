"""SQLAlchemy persistence for notification channels and the delivery outbox.

Tables (sharing the alerts-platform ``Base`` from
``backend.workflows.repository``):

- ``channel_references`` — owner-scoped Telegram/ntfy channel definitions
  (destinations hold no secrets; the secret lives in ``secret_env``)
- ``deliveries`` — outbox rows, one per (signal event, channel), claimed under
  a time-based lease
- ``delivery_attempts`` — per-attempt audit log

The matching Postgres migration is
``backend/alembic/versions/20260908_000011_alerts_platform_phase1.py``.

Only stdlib + SQLAlchemy imports at module load; safe to import anywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.workflows.repository import Base, LeaseConflict

__all__ = [
    "ChannelReference",
    "Delivery",
    "DeliveryAttempt",
    "DELIVERY_STATUSES",
    "DEFAULT_LEASE_SECONDS",
    "LeaseConflict",
    "SqlAlchemyNotificationRepository",
]

DELIVERY_STATUSES = ("pending", "delivering", "delivered", "retrying", "failed", "expired")
DEFAULT_LEASE_SECONDS = 120


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChannelReference(Base):
    __tablename__ = "channel_references"

    id = Column(String(36), primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    provider = Column(String(32), nullable=False)  # telegram|ntfy
    destination = Column(JSON, nullable=False, default=dict)  # no secrets here
    secret_env = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_channel_references_owner_name"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(String(36), primary_key=True, default=_uuid)
    event_id = Column(String(36), ForeignKey("signal_events.id"), nullable=False)
    channel_id = Column(String(36), ForeignKey("channel_references.id"), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("event_id", "channel_id", name="uq_deliveries_event_channel"),
        Index("ix_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    delivery_id = Column(String(36), ForeignKey("deliveries.id"), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    outcome = Column(String(32), nullable=False)  # accepted|retryable|permanent|unknown
    detail = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SqlAlchemyNotificationRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def _session(self) -> Session:
        session = self.session_factory()
        # Keep returned ORM objects usable after commit + close.
        session.expire_on_commit = False
        return session

    # -- channels -----------------------------------------------------------

    def upsert_channel(
        self,
        owner_id: str,
        name: str,
        provider: str,
        destination: dict,
        secret_env: Optional[str] = None,
        enabled: bool = True,
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        if db is not None:
            return self._upsert_channel(db, owner_id, name, provider, destination, secret_env, enabled, now)
        session = self._session()
        try:
            channel = self._upsert_channel(session, owner_id, name, provider, destination, secret_env, enabled, now)
            session.commit()
            return channel
        except IntegrityError:
            # Lost a create race against a concurrent upsert of the same
            # (owner_id, name): retry as a plain update of the winner.
            session.rollback()
            try:
                channel = self._update_channel(session, owner_id, name, provider, destination, secret_env, enabled)
                session.commit()
                return channel
            except Exception:
                session.rollback()
                raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _upsert_channel(self, session, owner_id, name, provider, destination, secret_env, enabled, now):
        existing = self._get_channel_by_name(session, owner_id, name)
        if existing is not None:
            return self._apply_channel_update(existing, provider, destination, secret_env, enabled)
        timestamp = now or _utcnow()
        channel = ChannelReference(
            id=_uuid(),
            owner_id=owner_id,
            name=name,
            provider=provider,
            destination=dict(destination or {}),
            secret_env=secret_env,
            enabled=bool(enabled),
            created_at=timestamp,
        )
        session.add(channel)
        session.flush()
        return channel

    def _update_channel(self, session, owner_id, name, provider, destination, secret_env, enabled):
        existing = self._get_channel_by_name(session, owner_id, name)
        if existing is None:
            raise KeyError(f"channel {owner_id}/{name} disappeared during upsert")
        return self._apply_channel_update(existing, provider, destination, secret_env, enabled)

    @staticmethod
    def _get_channel_by_name(session, owner_id, name):
        return session.execute(
            select(ChannelReference).where(
                ChannelReference.owner_id == owner_id,
                ChannelReference.name == name,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _apply_channel_update(channel, provider, destination, secret_env, enabled):
        channel.provider = provider
        channel.destination = dict(destination or {})
        channel.secret_env = secret_env
        channel.enabled = bool(enabled)
        return channel

    def list_channels(self, owner_id: str, *, db: Optional[Session] = None):
        if db is not None:
            return self._list_channels(db, owner_id)
        session = self._session()
        try:
            return self._list_channels(session, owner_id)
        finally:
            session.close()

    def _list_channels(self, session, owner_id):
        return list(
            session.execute(
                select(ChannelReference)
                .where(ChannelReference.owner_id == owner_id)
                .order_by(ChannelReference.created_at.asc(), ChannelReference.id.asc())
            ).scalars().all()
        )

    def get_channel(self, channel_id: str, *, db: Optional[Session] = None):
        if db is not None:
            return db.get(ChannelReference, channel_id)
        session = self._session()
        try:
            return session.get(ChannelReference, channel_id)
        finally:
            session.close()

    def get_delivery(self, delivery_id: str, *, db: Optional[Session] = None):
        """Return the ``Delivery`` row or ``None`` when unknown."""
        if db is not None:
            return db.get(Delivery, delivery_id)
        session = self._session()
        try:
            return session.get(Delivery, delivery_id)
        finally:
            session.close()

    # -- outbox -------------------------------------------------------------

    @staticmethod
    def _claimable_guard(now: datetime):
        """Rows a worker may claim at ``now``.

        - pending/retrying rows whose backoff elapsed and whose lease (if any)
          expired, plus
        - delivering rows whose lease expired (crashed worker reclaim).
        """
        return or_(
            and_(
                Delivery.status.in_(("pending", "retrying")),
                or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= now),
                or_(Delivery.lease_until.is_(None), Delivery.lease_until <= now),
            ),
            and_(
                Delivery.status == "delivering",
                Delivery.lease_until.is_not(None),
                Delivery.lease_until <= now,
            ),
        )

    def claim_deliveries(
        self,
        now: datetime,
        limit: int = 10,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        db: Optional[Session] = None,
    ):
        """Claim up to ``limit`` due deliveries under a time-based lease.

        Claimed rows move to ``status='delivering'`` with
        ``lease_until = now + lease_seconds``; attempts are not incremented.
        Two claims never return the same delivery. Uses
        ``FOR UPDATE SKIP LOCKED`` on Postgres; elsewhere a guarded
        ``UPDATE ... WHERE`` per candidate row with a rowcount check.
        """
        limit = int(limit)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if db is not None:
            return self._claim_deliveries(db, now, limit, int(lease_seconds))
        session = self._session()
        try:
            claimed = self._claim_deliveries(session, now, limit, int(lease_seconds))
            session.commit()
            return claimed
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _claim_deliveries(self, session, now, limit, lease_seconds):
        lease_until = now + timedelta(seconds=max(1, lease_seconds))
        guard = self._claimable_guard(now)
        order_by = (Delivery.created_at.asc(), Delivery.id.asc())

        bind = session.bind
        if bind is not None and getattr(bind.dialect, "name", "") == "postgresql":
            rows = session.execute(
                select(Delivery)
                .where(guard)
                .order_by(*order_by)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars().all()
            claimed: List[Delivery] = []
            for row in rows:
                row.status = "delivering"
                row.lease_until = lease_until
                row.updated_at = now
                claimed.append(row)
            session.flush()
            return claimed

        candidate_ids = session.execute(
            select(Delivery.id).where(guard).order_by(*order_by).limit(limit)
        ).scalars().all()
        claimed = []
        for delivery_id in candidate_ids:
            if len(claimed) >= limit:
                break
            result = session.execute(
                update(Delivery)
                .where(Delivery.id == delivery_id, guard)
                .values(status="delivering", lease_until=lease_until, updated_at=now)
            )
            if result.rowcount == 1:
                claimed.append(session.get(Delivery, delivery_id))
        return claimed

    def record_attempt(
        self,
        delivery_id: str,
        attempt_no: int,
        outcome: str,
        detail: str,
        new_status: str,
        *,
        next_attempt_at: Optional[datetime] = None,
        delivered_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
        lease_until: Optional[datetime] = None,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Log one attempt and update the delivery row under lease fencing.

        The write only applies while this worker still owns the delivery:
        the guarded UPDATE requires ``status='delivering'`` AND a still-fresh
        lease (``lease_until`` IS NULL or ``> now``). When ``lease_until``
        (the lease value the caller saw on its claimed row) is given, the
        stored lease must also still equal it — so a record arriving after
        another worker reclaimed the delivery is rejected too. Rowcount 0
        raises :class:`LeaseConflict` and nothing is written (no audit row,
        no status change). Non-``delivering`` statuses clear the lease and
        apply the optional ``next_attempt_at`` / ``delivered_at`` /
        ``last_error``.
        """
        if new_status not in DELIVERY_STATUSES:
            raise ValueError(f"unknown delivery status {new_status!r}")
        if db is not None:
            return self._record_attempt(
                db, delivery_id, attempt_no, outcome, detail, new_status,
                next_attempt_at, delivered_at, last_error, lease_until, now,
            )
        session = self._session()
        try:
            delivery = self._record_attempt(
                session, delivery_id, attempt_no, outcome, detail, new_status,
                next_attempt_at, delivered_at, last_error, lease_until, now,
            )
            session.commit()
            return delivery
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _record_attempt(self, session, delivery_id, attempt_no, outcome, detail, new_status,
                        next_attempt_at, delivered_at, last_error, expected_lease_until, now):
        delivery = session.get(Delivery, delivery_id)
        if delivery is None:
            raise KeyError(delivery_id)
        timestamp = now or _utcnow()

        # Lease-fenced completion (fault 2): a stale worker whose lease
        # expired — or whose delivery was reclaimed by another worker — must
        # not overwrite the row. Guarded UPDATE; rowcount 0 -> LeaseConflict.
        guard = [
            Delivery.id == delivery_id,
            Delivery.status == "delivering",
            or_(Delivery.lease_until.is_(None), Delivery.lease_until > timestamp),
        ]
        if expected_lease_until is not None:
            guard.append(Delivery.lease_until == expected_lease_until)

        values = {
            "status": new_status,
            "attempts": max(int(delivery.attempts or 0), int(attempt_no)),
            "next_attempt_at": next_attempt_at,
            "updated_at": timestamp,
        }
        if delivered_at is not None:
            values["delivered_at"] = delivered_at
        if last_error is not None:
            values["last_error"] = last_error
        if new_status != "delivering":
            values["lease_until"] = None

        result = session.execute(
            update(Delivery)
            .where(*guard)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            raise LeaseConflict(
                f"delivery {delivery_id}: lease expired or reclaimed; attempt discarded"
            )

        session.add(
            DeliveryAttempt(
                delivery_id=delivery_id,
                attempt_no=int(attempt_no),
                outcome=str(outcome),
                detail=str(detail or ""),
                created_at=timestamp,
            )
        )
        session.flush()
        session.refresh(delivery)  # re-sync the ORM row with the guarded UPDATE
        return delivery

    def count_attempts(
        self,
        delivery_id: str,
        outcome: Optional[str] = None,
        *,
        db: Optional[Session] = None,
    ) -> int:
        """Number of recorded attempts for a delivery, optionally per outcome."""
        if db is not None:
            return self._count_attempts(db, delivery_id, outcome)
        session = self._session()
        try:
            return self._count_attempts(session, delivery_id, outcome)
        finally:
            session.close()

    @staticmethod
    def _count_attempts(session, delivery_id, outcome) -> int:
        stmt = (
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
        )
        if outcome is not None:
            stmt = stmt.where(DeliveryAttempt.outcome == str(outcome))
        return int(session.execute(stmt).scalar_one())

    def get_due_and_stale(self, now: datetime, *, db: Optional[Session] = None):
        """Return ``(due, stale)`` deliveries at ``now``.

        ``due``: pending/retrying rows claimable right now.
        ``stale``: delivering rows whose lease expired (crashed workers).
        """
        if db is not None:
            return self._get_due_and_stale(db, now)
        session = self._session()
        try:
            return self._get_due_and_stale(session, now)
        finally:
            session.close()

    def _get_due_and_stale(self, session, now):
        due = list(
            session.execute(
                select(Delivery)
                .where(
                    Delivery.status.in_(("pending", "retrying")),
                    or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= now),
                    or_(Delivery.lease_until.is_(None), Delivery.lease_until <= now),
                )
                .order_by(Delivery.created_at.asc(), Delivery.id.asc())
            ).scalars().all()
        )
        stale = list(
            session.execute(
                select(Delivery)
                .where(
                    Delivery.status == "delivering",
                    Delivery.lease_until.is_not(None),
                    Delivery.lease_until <= now,
                )
                .order_by(Delivery.created_at.asc(), Delivery.id.asc())
            ).scalars().all()
        )
        return due, stale
