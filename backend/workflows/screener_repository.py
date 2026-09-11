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

Publication protocol: run status + members + attachment signal events +
outbox deliveries + attachment baseline upserts are written by
:meth:`ScreenerRunRepository.finalize_run` in ONE transaction that opens
with the ownership compare-and-swap — a rejected or stale owner publishes
nothing at all. Attachment baseline transitions are serialized per
(owner, workflow, revision, attachment) with a PostgreSQL advisory
transaction lock and chronologically gated: a run may only advance a
baseline whose stored ``last_complete_run`` is not strictly newer than
itself, so an older overlapping run can never overwrite newer comparison
state — even when the baseline is still empty and there are no rows to
lock.

Runs are never deleted by the scheduler; history stays attributable to the
exact workflow revision and universe revision that produced it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    text,
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
    "AttachmentEvent",
    "AttachmentStateUpdate",
    "AttachmentTransition",
    "ScreenerRunRepository",
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """SQLite round-trips datetimes naive; treat naive as UTC for compares."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _is_postgres(session: Any) -> bool:
    bind = getattr(session, "bind", None)
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


@dataclass(frozen=True)
class AttachmentEvent:
    """One prepared attachment signal event + outbox fan-out (not yet written)."""

    attachment_id: str
    occurrence_key: str
    fired_at: datetime
    evidence: dict
    channel_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AttachmentStateUpdate:
    """One prepared baseline row upsert (not yet written)."""

    attachment_id: str
    instrument_key: str
    present: bool
    rank: Optional[int]
    consecutive_absent: int


@dataclass(frozen=True)
class AttachmentTransition:
    """Everything one attachment would publish for a run.

    Prepared by the scheduler BEFORE finalization and applied inside
    ``finalize_run``'s fenced transaction — never written earlier.
    """

    owner_id: str
    workflow_id: str
    revision_id: str
    attachment_id: str
    events: Tuple[AttachmentEvent, ...] = ()
    state_updates: Tuple[AttachmentStateUpdate, ...] = ()


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
        attachments: Sequence[AttachmentTransition] = (),
        now: Optional[datetime] = None,
    ) -> bool:
        """Publish the run and ALL its side effects in ONE fenced transaction.

        Order inside the transaction:

        1. compare-and-swap ``screener_run`` (id + ``lease_owner`` +
           ``status='running'``): rowcount 0 — stale owner whose lease was
           taken over, or an already-finalized run — aborts the WHOLE
           publication before anything becomes visible;
        2. delete + insert the member rows;
        3. per attachment transition: acquire the attachment's advisory
           transaction lock (serializes every overlapping publication of the
           same attachment — including the first one, when there are no
           baseline rows to lock), apply the chronology gate (a stored
           ``last_complete_run`` strictly newer than this run suppresses the
           whole transition — an older run never overwrites newer comparison
           state), then insert the signal events + pending deliveries
           (occurrence-key pre-checked) and upsert the baseline rows;
        4. fold the published attachment summary into ``coverage``.

        Fence semantics vs lease expiry: the fence is OWNERSHIP, not time.
        A lease that expired but was never taken over still finalizes (the
        worker is still the sole owner; blocking it would lose a completed
        run's notifications). Once another worker takes the run over, the
        original owner is permanently fenced by the CAS.
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

            summary: Dict[str, int] = {}
            for transition in attachments:
                applied = self._apply_attachment_transition(
                    session, transition, run_id, as_of, timestamp
                )
                for key, value in applied.items():
                    summary[key] = summary.get(key, 0) + value
            if summary:
                coverage = dict(coverage or {})
                coverage.update(summary)
                session.execute(
                    update(ScreenerRun)
                    .where(ScreenerRun.id == run_id)
                    .values(coverage=coverage, updated_at=timestamp)
                )
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _apply_attachment_transition(
        self,
        session: Session,
        transition: AttachmentTransition,
        run_id: str,
        run_scheduled_for: datetime,
        timestamp: datetime,
    ) -> Dict[str, int]:
        """Apply one attachment's prepared transitions inside the publication
        transaction. Returns the published/suppressed counters.

        - A PostgreSQL advisory transaction lock keyed on
          (owner, workflow, revision, attachment) serializes every
          overlapping publication of the same baseline, taken BEFORE the
          baseline read. Unlike a row lock it also covers the first
          publication, when no baseline rows exist yet. SQLite (tests) is a
          single writer.
        - chronology gate: if any baseline row was last written by a run
          with a strictly newer ``scheduled_for``, this transition is
          entirely suppressed — its events would describe transitions away
          from a baseline that no longer exists, and its state writes would
          overwrite newer comparison state.
        """
        from backend.notifications.repository import Delivery

        from backend.workflows.repository import SignalEvent

        applied: Dict[str, int] = {
            "attachment_events_published": 0,
        }
        if _is_postgres(session):
            session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('screener-attachment:' || :lock_key))"
                ),
                {
                    "lock_key": (
                        f"{transition.owner_id}/{transition.workflow_id}/"
                        f"{transition.revision_id}/{transition.attachment_id}"
                    )
                },
            )
        lock_rows = session.execute(
            select(ScreenerAttachmentState.last_complete_run_id)
            .where(
                ScreenerAttachmentState.owner_id == transition.owner_id,
                ScreenerAttachmentState.workflow_id == transition.workflow_id,
                ScreenerAttachmentState.workflow_revision_id == transition.revision_id,
                ScreenerAttachmentState.attachment_id == transition.attachment_id,
            )
        ).scalars().all()
        baseline_run_ids = {value for value in lock_rows if value}
        if baseline_run_ids:
            stored = session.execute(
                select(ScreenerRun.scheduled_for).where(
                    ScreenerRun.id.in_(baseline_run_ids)
                )
            ).scalars().all()
            newest_baseline = max(_as_utc(value) for value in stored)
            if _as_utc(run_scheduled_for) < newest_baseline:
                applied["attachment_events_stale_suppressed"] = 1
                return applied

        for event in transition.events:
            duplicate = session.execute(
                select(SignalEvent.id).where(
                    SignalEvent.occurrence_key == event.occurrence_key
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                continue  # replay: this logical event already exists
            signal = SignalEvent(
                id=_uuid(),
                subscription_id=None,
                workflow_id=str(transition.workflow_id),
                occurrence_key=event.occurrence_key,
                fired_at=event.fired_at,
                evidence=dict(event.evidence or {}),
                created_at=timestamp,
            )
            session.add(signal)
            session.flush()
            for channel_id in dict.fromkeys(event.channel_ids or ()):
                session.add(
                    Delivery(
                        id=_uuid(),
                        event_id=signal.id,
                        channel_id=channel_id,
                        status="pending",
                        attempts=0,
                        next_attempt_at=timestamp,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            applied["attachment_events_published"] += 1

        for update in transition.state_updates:
            _upsert_attachment_state_row(
                session,
                owner_id=transition.owner_id,
                workflow_id=transition.workflow_id,
                revision_id=transition.revision_id,
                attachment_id=update.attachment_id,
                instrument_key=update.instrument_key,
                present=update.present,
                run_id=run_id,
                rank=update.rank,
                consecutive_absent=update.consecutive_absent,
                now=timestamp,
            )
        return applied

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


def _upsert_attachment_state_row(
    session: Session,
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
    now: datetime,
) -> None:
    """Insert-or-update one baseline row in the caller's transaction.

    Uses a dialect ``ON CONFLICT DO UPDATE`` upsert so two overlapping
    publications of the same attachment can never collide on the unique
    constraint (the collision would poison a PostgreSQL transaction).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    values = dict(
        owner_id=owner_id,
        workflow_id=workflow_id,
        workflow_revision_id=revision_id,
        attachment_id=attachment_id,
        instrument_key=instrument_key,
        present=bool(present),
        last_complete_run_id=run_id,
        last_rank=rank,
        consecutive_absent=int(consecutive_absent),
        updated_at=now,
    )
    insert = pg_insert if _is_postgres(session) else sqlite_insert
    session.execute(
        insert(ScreenerAttachmentState)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                ScreenerAttachmentState.owner_id,
                ScreenerAttachmentState.workflow_id,
                ScreenerAttachmentState.workflow_revision_id,
                ScreenerAttachmentState.attachment_id,
                ScreenerAttachmentState.instrument_key,
            ],
            set_={
                "present": values["present"],
                "last_complete_run_id": values["last_complete_run_id"],
                "last_rank": values["last_rank"],
                "consecutive_absent": values["consecutive_absent"],
                "updated_at": values["updated_at"],
            },
        )
    )


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

