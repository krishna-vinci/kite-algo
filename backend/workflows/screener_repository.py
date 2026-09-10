"""Screener run persistence and scheduling repository (Phase 3 F9).

Tables (migration 20260910_000015):

- ``screener_run`` — one scheduled (or manual) execution of a screener
  workflow revision. ``occurrence_key`` is the idempotency contract:
  ``{workflow_id}:{bucket_epoch}`` — the unique constraint makes concurrent
  claim attempts resolve to exactly one logical run. ``lease_owner`` /
  ``lease_expires_at`` fence a running execution: a stale owner cannot
  finalize after another worker took over (compare-and-swap on finalize).
- ``screener_run_member`` — per-instrument outcome with values, exclusion
  reason and deterministic rank; written once, in the same transaction as
  the run's final status (atomic publication).
- ``screener_attachment_state`` — durable entry/exit/rank state per
  (owner, workflow, revision, attachment, instrument) so restart never
  resets baselines or hysteresis (E-17).

Runs are never deleted by the scheduler; history stays attributable to the
exact workflow revision and universe revision that produced it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.workflows.repository import Base
from backend.workflows.universes import GUID

__all__ = [
    "ScreenerRun",
    "ScreenerRunMember",
    "ScreenerAttachmentState",
    "ScreenerRunRepository",
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScreenerRun(Base):
    __tablename__ = "screener_run"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False, index=True)
    workflow_id = Column(GUID, nullable=False, index=True)
    workflow_revision_id = Column(GUID, nullable=False)
    occurrence_key = Column(String(255), nullable=False, unique=True, index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    triggered_by = Column(String(16), nullable=False, default="schedule")  # schedule|manual
    status = Column(String(16), nullable=False, default="running")  # running|complete|partial|failed
    universe_revision = Column(Integer, nullable=True)
    as_of = Column(DateTime(timezone=True), nullable=True)
    coverage = Column(JSON, nullable=False, default=dict)
    data_freshness = Column(JSON, nullable=False, default=dict)
    failure_reason = Column(Text, nullable=True)
    lease_owner = Column(String(120), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ScreenerRunMember(Base):
    __tablename__ = "screener_run_member"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(GUID, ForeignKey("screener_run.id", ondelete="CASCADE"), nullable=False)
    instrument_key = Column(String(128), nullable=False)
    passed = Column(Boolean, nullable=False, default=False)
    exclusion_reason = Column(String(64), nullable=True)
    values = Column(JSON, nullable=False, default=dict)
    rank = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)


class ScreenerAttachmentState(Base):
    __tablename__ = "screener_attachment_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(String(255), nullable=False)
    workflow_id = Column(GUID, nullable=False)
    workflow_revision_id = Column(GUID, nullable=False)
    attachment_id = Column(String(255), nullable=False)
    instrument_key = Column(String(128), nullable=False)
    present = Column(Boolean, nullable=False, default=False)
    last_complete_run_id = Column(GUID, nullable=True)
    last_rank = Column(Integer, nullable=True)
    consecutive_absent = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "workflow_id", "workflow_revision_id", "attachment_id", "instrument_key",
            name="uq_screener_attachment_state_revision_attachment_instrument",
        ),
    )


class ScreenerRunRepository:
    """All screener run/attachment-state persistence."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- claiming (idempotency + fencing) ----------------------------------

    def claim_run(
        self,
        *,
        owner_id: str,
        workflow_id: str,
        revision_id: str,
        occurrence_key: str,
        scheduled_for: datetime,
        lease_owner: str,
        lease_ttl_s: float,
        triggered_by: str = "schedule",
        now: Optional[datetime] = None,
    ) -> Optional[ScreenerRun]:
        """Atomically claim a scheduled occurrence.

        - New occurrence: INSERT the running row (unique occurrence_key
          resolves concurrent claims to exactly one winner).
        - Existing RUNNING row with an EXPIRED lease (stale owner — crash or
          lease loss): take over by compare-and-swap on ``lease_expires_at``.
        - Existing row otherwise (owned elsewhere or finalized): return None.
        """
        timestamp = now or _utcnow()
        lease_expires = timestamp + timedelta(seconds=float(lease_ttl_s))
        session = self._sessions()
        try:
            existing = session.execute(
                select(ScreenerRun).where(ScreenerRun.occurrence_key == occurrence_key)
            ).scalar_one_or_none()
            if existing is None:
                run = ScreenerRun(
                    id=_uuid(),
                    owner_id=owner_id,
                    workflow_id=workflow_id,
                    workflow_revision_id=revision_id,
                    occurrence_key=occurrence_key,
                    scheduled_for=scheduled_for,
                    triggered_by=triggered_by,
                    status="running",
                    lease_owner=lease_owner,
                    lease_expires_at=lease_expires,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(run)
                try:
                    session.commit()
                    return run
                except IntegrityError:
                    # Lost the unique race: the other worker owns it.
                    session.rollback()
                    return None
            if existing.status == "running" and existing.lease_expires_at is not None:
                expires = existing.lease_expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= timestamp:
                    # Expired lease only: a live owner's run is never stolen.
                    result = session.execute(
                        update(ScreenerRun)
                        .where(
                            ScreenerRun.id == existing.id,
                            ScreenerRun.status == "running",
                            ScreenerRun.lease_expires_at == existing.lease_expires_at,
                        )
                        .values(
                            lease_owner=lease_owner,
                            lease_expires_at=lease_expires,
                            updated_at=timestamp,
                        )
                    )
                    session.commit()
                    if result.rowcount == 1:
                        session.refresh(existing)
                        return existing
            return None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def refresh_lease(self, run_id: str, lease_owner: str, lease_ttl_s: float, *, now=None) -> bool:
        timestamp = now or _utcnow()
        session = self._sessions()
        try:
            result = session.execute(
                update(ScreenerRun)
                .where(
                    ScreenerRun.id == run_id,
                    ScreenerRun.lease_owner == lease_owner,
                    ScreenerRun.status == "running",
                )
                .values(
                    lease_expires_at=timestamp + timedelta(seconds=float(lease_ttl_s)),
                    updated_at=timestamp,
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    # -- finalization (atomic publication) ---------------------------------

    def finalize_run(
        self,
        run_id: str,
        lease_owner: str,
        *,
        status: str,
        as_of: datetime,
        coverage: dict,
        data_freshness: dict,
        members: Sequence[dict],
        failure_reason: Optional[str] = None,
        universe_revision: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Publish status + all members + freshness in ONE transaction.

        The compare-and-swap on ``lease_owner`` guarantees a stale owner
        (whose lease was taken over) cannot finalize anything: rowcount 0
        aborts the whole transaction.
        """
        timestamp = now or _utcnow()
        session = self._sessions()
        try:
            claimed = session.execute(
                update(ScreenerRun)
                .where(
                    ScreenerRun.id == run_id,
                    ScreenerRun.lease_owner == lease_owner,
                    ScreenerRun.status == "running",
                )
                .values(
                    status=status,
                    as_of=as_of,
                    coverage=coverage,
                    data_freshness=data_freshness,
                    failure_reason=failure_reason,
                    universe_revision=universe_revision,
                    completed_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                return False
            session.execute(
                delete(ScreenerRunMember).where(ScreenerRunMember.run_id == run_id)
            )
            for member in members:
                session.add(
                    ScreenerRunMember(
                        run_id=run_id,
                        instrument_key=member["instrument_key"],
                        passed=bool(member.get("passed")),
                        exclusion_reason=member.get("exclusion_reason"),
                        values=dict(member.get("values") or {}),
                        rank=member.get("rank"),
                        score=member.get("score"),
                    )
                )
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- reads --------------------------------------------------------------

    def latest_complete_run(self, owner_id: str, workflow_id: str) -> Optional[ScreenerRun]:
        session = self._sessions()
        try:
            return session.execute(
                select(ScreenerRun)
                .where(
                    ScreenerRun.owner_id == owner_id,
                    ScreenerRun.workflow_id == workflow_id,
                    ScreenerRun.status.in_(("complete", "partial")),
                )
                .order_by(ScreenerRun.scheduled_for.desc())
                .limit(1)
            ).scalar_one_or_none()
        finally:
            session.close()

    def latest_finalized_run(self, owner_id: str, workflow_id: str) -> Optional[ScreenerRun]:
        session = self._sessions()
        try:
            return session.execute(
                select(ScreenerRun)
                .where(
                    ScreenerRun.owner_id == owner_id,
                    ScreenerRun.workflow_id == workflow_id,
                    ScreenerRun.status != "running",
                )
                .order_by(ScreenerRun.scheduled_for.desc())
                .limit(1)
            ).scalar_one_or_none()
        finally:
            session.close()

    def run_members(self, run_id: str) -> List[ScreenerRunMember]:
        session = self._sessions()
        try:
            return list(
                session.execute(
                    select(ScreenerRunMember)
                    .where(ScreenerRunMember.run_id == run_id)
                    .order_by(
                        ScreenerRunMember.rank.asc().nullslast(),
                        ScreenerRunMember.instrument_key.asc(),
                    )
                ).scalars().all()
            )
        finally:
            session.close()

    def get_run(self, run_id: str) -> Optional[ScreenerRun]:
        session = self._sessions()
        try:
            return session.execute(
                select(ScreenerRun).where(ScreenerRun.id == run_id)
            ).scalar_one_or_none()
        finally:
            session.close()

    def get_run_by_occurrence(
        self, owner_id: str, occurrence_key: str
    ) -> Optional[ScreenerRun]:
        session = self._sessions()
        try:
            return session.execute(
                select(ScreenerRun).where(
                    ScreenerRun.owner_id == owner_id,
                    ScreenerRun.occurrence_key == occurrence_key,
                )
            ).scalar_one_or_none()
        finally:
            session.close()

    def list_runs(
        self, owner_id: str, workflow_id: str, *, limit: int = 20, offset: int = 0
    ) -> List[ScreenerRun]:
        session = self._sessions()
        try:
            return list(
                session.execute(
                    select(ScreenerRun)
                    .where(
                        ScreenerRun.owner_id == owner_id,
                        ScreenerRun.workflow_id == workflow_id,
                    )
                    .order_by(ScreenerRun.scheduled_for.desc())
                    .offset(max(0, int(offset)))
                    .limit(max(1, min(int(limit), 200)))
                ).scalars().all()
            )
        finally:
            session.close()

    # -- attachment state ----------------------------------------------------

    def attachment_states(
        self, owner_id: str, workflow_id: str, revision_id: str, attachment_id: str
    ) -> Dict[str, ScreenerAttachmentState]:
        session = self._sessions()
        try:
            rows = session.execute(
                select(ScreenerAttachmentState).where(
                    ScreenerAttachmentState.owner_id == owner_id,
                    ScreenerAttachmentState.workflow_id == workflow_id,
                    ScreenerAttachmentState.workflow_revision_id == revision_id,
                    ScreenerAttachmentState.attachment_id == attachment_id,
                )
            ).scalars().all()
            return {row.instrument_key: row for row in rows}
        finally:
            session.close()

    def upsert_attachment_state(
        self,
        *,
        owner_id: str,
        workflow_id: str,
        revision_id: str,
        attachment_id: str,
        instrument_key: str,
        present: bool,
        run_id: str,
        rank: Optional[int],
        consecutive_absent: int,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ) -> None:
        timestamp = now or _utcnow()

        def _write(session: Session) -> None:
            existing = session.execute(
                select(ScreenerAttachmentState).where(
                    ScreenerAttachmentState.owner_id == owner_id,
                    ScreenerAttachmentState.workflow_id == workflow_id,
                    ScreenerAttachmentState.workflow_revision_id == revision_id,
                    ScreenerAttachmentState.attachment_id == attachment_id,
                    ScreenerAttachmentState.instrument_key == instrument_key,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ScreenerAttachmentState(
                        owner_id=owner_id,
                        workflow_id=workflow_id,
                        workflow_revision_id=revision_id,
                        attachment_id=attachment_id,
                        instrument_key=instrument_key,
                        present=present,
                        last_complete_run_id=run_id,
                        last_rank=rank,
                        consecutive_absent=consecutive_absent,
                        updated_at=timestamp,
                    )
                )
            else:
                existing.present = present
                existing.last_complete_run_id = run_id
                existing.last_rank = rank
                existing.consecutive_absent = consecutive_absent
                existing.updated_at = timestamp
            session.flush()

        if db is not None:
            _write(db)
            return
        session = self._sessions()
        try:
            _write(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def record_attachment_event(
    session_factory: Any,
    *,
    workflow_id: str,
    occurrence_key: str,
    fired_at: datetime,
    evidence: dict,
    channel_ids: Sequence[str],
    now: Optional[datetime] = None,
):
    """Atomically insert a screener attachment signal event + one pending
    delivery per channel (subscription_id NULL; delivery context comes from
    evidence). Returns None when the occurrence already exists (idempotent
    retry / duplicate attachment evaluation can never double-send).
    """
    from backend.notifications.repository import Delivery

    from backend.workflows.repository import SignalEvent

    timestamp = now or _utcnow()
    session = session_factory()
    try:
        event = SignalEvent(
            id=_uuid(),
            subscription_id=None,
            workflow_id=str(workflow_id),
            occurrence_key=occurrence_key,
            fired_at=fired_at,
            evidence=dict(evidence or {}),
            created_at=timestamp,
        )
        session.add(event)
        session.flush()
        for channel_id in dict.fromkeys(channel_ids or ()):
            session.add(
                Delivery(
                    id=_uuid(),
                    event_id=event.id,
                    channel_id=channel_id,
                    status="pending",
                    attempts=0,
                    next_attempt_at=timestamp,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        session.commit()
        return event
    except IntegrityError:
        session.rollback()
        return None
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def list_workflow_events(
    session_factory: Any,
    workflow_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
):
    """Paginated screener attachment events for one workflow, newest first."""
    from backend.workflows.repository import SignalEvent

    session = session_factory()
    try:
        return list(
            session.execute(
                select(SignalEvent)
                .where(
                    SignalEvent.workflow_id == str(workflow_id),
                    SignalEvent.subscription_id.is_(None),
                )
                .order_by(SignalEvent.fired_at.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit), 200)))
            ).scalars().all()
        )
    finally:
        session.close()

