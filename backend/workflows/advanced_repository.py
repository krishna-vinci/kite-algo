"""Durable state for Phase 4 F10 advanced conditions (advanced_repository).

Owns exactly the objects introduced by migration ``20260911_000016`` that
evaluation needs:

- ``alert_breadth_state`` / ``alert_breadth_triggers`` — workflow-level
  aggregation state (never per-subscription checkpoints: a single
  workflow-level event cannot be governed by N per-instrument copies of
  ``satisfied``), serialized per stage with an advisory transaction lock.
- ``alert_session_counters`` / ``alert_suppression_counters`` — the
  per-(alert, session) notification cap shared atomically across instruments,
  and the durable record of what the cap suppressed.

Every method takes the CALLER's session and never commits on its own: these
writes must ride the same fenced publication transaction as the checkpoint,
signal event and outbox rows, so an injected failure rolls back all of them
together.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session

from backend.workflows.repository import Base
from backend.workflows.universes import GUID

__all__ = [
    "AlertBreadthState",
    "AlertBreadthTrigger",
    "AlertSessionCounter",
    "AlertSuppressionCounter",
    "lock_breadth_stage",
    "read_breadth_state",
    "upsert_breadth_state",
    "upsert_breadth_contribution",
    "count_breadth_contributions",
    "evict_breadth_contribution",
    "reserve_session_slot",
    "record_suppression",
    "session_count",
    "suppression_summary",
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(session: Any) -> bool:
    bind = getattr(session, "bind", None)
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


class AlertBreadthState(Base):
    """One threshold row per (owner, workflow, revision, stage)."""

    __tablename__ = "alert_breadth_state"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False)
    workflow_id = Column(GUID, nullable=False)
    revision_id = Column(GUID, nullable=False)
    stage_id = Column(String(255), nullable=False)
    # Starts FALSE: the first legitimate crossing must not be suppressed.
    satisfied = Column(Boolean, nullable=False, default=False)
    # Monotonic durable crossing identity — never a timestamp, so two
    # transitions sharing an event timestamp cannot collide.
    crossing_seq = Column(BigInteger, nullable=False, default=0)
    satisfied_since_ts = Column(DateTime(timezone=True), nullable=True)
    last_fired_ts = Column(DateTime(timezone=True), nullable=True)
    last_count = Column(Integer, nullable=True)
    member_count = Column(Integer, nullable=True)
    # Greatest event time ever evaluated for this stage; never moves backwards
    # and is what makes the aggregate arrival-order independent.
    aggregation_watermark = Column(DateTime(timezone=True), nullable=True)
    membership_resolved_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AlertBreadthTrigger(Base):
    """One contribution row per (stage, instrument): latest qualifying trigger."""

    __tablename__ = "alert_breadth_triggers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    owner_id = Column(String(255), nullable=False)
    workflow_id = Column(GUID, nullable=False)
    revision_id = Column(GUID, nullable=False)
    stage_id = Column(String(255), nullable=False)
    instrument_key = Column(String(128), nullable=False)
    last_trigger_ts = Column(DateTime(timezone=True), nullable=False)
    last_bar_ts = Column(DateTime(timezone=True), nullable=True)
    universe_revision = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AlertSessionCounter(Base):
    """Per-(owner, workflow, revision, alert, session) notification count."""

    __tablename__ = "alert_session_counters"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False)
    workflow_id = Column(GUID, nullable=False)
    revision_id = Column(GUID, nullable=False)
    alert_id = Column(String(255), nullable=False)
    session_id = Column(String(128), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    first_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AlertSuppressionCounter(Base):
    """Durable record of a suppressed notification (reason + count)."""

    __tablename__ = "alert_suppression_counters"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False)
    workflow_id = Column(GUID, nullable=False)
    revision_id = Column(GUID, nullable=False)
    alert_id = Column(String(255), nullable=False)
    session_id = Column(String(128), nullable=True)
    reason = Column(String(64), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    first_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_instrument_key = Column(String(128), nullable=True)
    last_stage_id = Column(String(255), nullable=True)


# ---------------------------------------------------------------------------
# breadth
# ---------------------------------------------------------------------------


def lock_breadth_stage(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
) -> None:
    """Serialize every publication of one breadth stage.

    A PostgreSQL advisory transaction lock (the pattern already used by
    universe resolution and screener attachments) rather than a row lock: the
    FIRST evaluation of a stage has no state row yet, and two first
    evaluations racing on an absent row are exactly the interleaving that must
    not interleave. SQLite (unit tests) is a single writer.
    """
    if not _is_postgres(session):
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('breadth:' || :lock_key))"),
        {"lock_key": f"{owner_id}/{workflow_id}/{revision_id}/{stage_id}"},
    )


def read_breadth_state(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    for_update: bool = False,
):
    """Read one stage's threshold row (optionally locking it)."""
    stmt = select(AlertBreadthState).where(
        AlertBreadthState.owner_id == owner_id,
        AlertBreadthState.workflow_id == workflow_id,
        AlertBreadthState.revision_id == revision_id,
        AlertBreadthState.stage_id == stage_id,
    )
    if for_update and _is_postgres(session):
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def upsert_breadth_state(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    satisfied: bool,
    crossing_seq: int,
    satisfied_since_ts: Optional[datetime],
    last_fired_ts: Optional[datetime],
    count: Optional[int],
    member_count: Optional[int],
    aggregation_watermark: Optional[datetime],
    membership_resolved_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> None:
    """Insert-or-update the stage's threshold row in the caller's transaction."""
    timestamp = now or _utcnow()
    row = read_breadth_state(
        session,
        owner_id=owner_id,
        workflow_id=workflow_id,
        revision_id=revision_id,
        stage_id=stage_id,
        for_update=True,
    )
    if row is None:
        session.add(
            AlertBreadthState(
                id=_uuid(),
                owner_id=owner_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                stage_id=stage_id,
                satisfied=satisfied,
                crossing_seq=crossing_seq,
                satisfied_since_ts=satisfied_since_ts,
                last_fired_ts=last_fired_ts,
                last_count=count,
                member_count=member_count,
                aggregation_watermark=aggregation_watermark,
                membership_resolved_at=membership_resolved_at,
                updated_at=timestamp,
            )
        )
        session.flush()
        return
    row.satisfied = satisfied
    row.crossing_seq = crossing_seq
    row.satisfied_since_ts = satisfied_since_ts
    row.last_fired_ts = last_fired_ts
    row.last_count = count
    row.member_count = member_count
    row.aggregation_watermark = aggregation_watermark
    if membership_resolved_at is not None:
        row.membership_resolved_at = membership_resolved_at
    row.updated_at = timestamp
    session.flush()


def upsert_breadth_contribution(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    instrument_key: str,
    trigger_ts: datetime,
    bar_ts: Optional[datetime],
    universe_revision: Optional[int],
    now: Optional[datetime] = None,
) -> bool:
    """Record one instrument's latest qualifying trigger.

    The write is MONOTONIC: an older observation can never overwrite a newer
    recorded contribution for the same instrument, and a duplicate is a no-op.
    That guard is what makes the aggregate independent of cross-instrument
    arrival order — a replayed or late bar cannot rewind another instrument's
    participation. Returns True when a row was written.
    """
    timestamp = now or _utcnow()
    if _is_postgres(session):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(AlertBreadthTrigger).values(
            owner_id=owner_id,
            workflow_id=workflow_id,
            revision_id=revision_id,
            stage_id=stage_id,
            instrument_key=instrument_key,
            last_trigger_ts=trigger_ts,
            last_bar_ts=bar_ts,
            universe_revision=universe_revision,
            updated_at=timestamp,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                AlertBreadthTrigger.owner_id,
                AlertBreadthTrigger.workflow_id,
                AlertBreadthTrigger.revision_id,
                AlertBreadthTrigger.stage_id,
                AlertBreadthTrigger.instrument_key,
            ],
            set_={
                "last_trigger_ts": stmt.excluded.last_trigger_ts,
                "last_bar_ts": stmt.excluded.last_bar_ts,
                "universe_revision": stmt.excluded.universe_revision,
                "updated_at": stmt.excluded.updated_at,
            },
            where=AlertBreadthTrigger.last_trigger_ts < stmt.excluded.last_trigger_ts,
        )
        result = session.execute(stmt)
        return bool(result.rowcount)
    # SQLite (unit tests): single writer, so a guarded read-then-write is safe
    # and keeps the same monotonic semantics as the PostgreSQL upsert.
    existing = session.execute(
        select(AlertBreadthTrigger).where(
            AlertBreadthTrigger.owner_id == owner_id,
            AlertBreadthTrigger.workflow_id == workflow_id,
            AlertBreadthTrigger.revision_id == revision_id,
            AlertBreadthTrigger.stage_id == stage_id,
            AlertBreadthTrigger.instrument_key == instrument_key,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            AlertBreadthTrigger(
                owner_id=owner_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                stage_id=stage_id,
                instrument_key=instrument_key,
                last_trigger_ts=trigger_ts,
                last_bar_ts=bar_ts,
                universe_revision=universe_revision,
                updated_at=timestamp,
            )
        )
        session.flush()
        return True
    if existing.last_trigger_ts is not None and existing.last_trigger_ts >= trigger_ts:
        return False
    existing.last_trigger_ts = trigger_ts
    existing.last_bar_ts = bar_ts
    existing.universe_revision = universe_revision
    existing.updated_at = timestamp
    session.flush()
    return True


def count_breadth_contributions(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    window_start: datetime,
    evaluated_at: datetime,
    members: Optional[Sequence[str]] = None,
    limit: int = 1000,
) -> Tuple[int, List[Tuple[str, datetime]]]:
    """Count distinct members whose latest trigger falls in the window.

    Both bounds are inclusive and the UPPER bound is what excludes a
    contribution stamped after this evaluation (only reachable when bars
    arrive out of order across instruments). Rows from instruments outside
    ``members`` are history: retained, never counted.

    The member filter is applied IN SQL, before the LIMIT. Filtering in Python
    after the LIMIT would let non-member rows consume the row budget and
    silently drop valid contributions — and because contributions commonly
    share a timestamp, which rows survived would be nondeterministic.
    """
    stmt = (
        select(
            AlertBreadthTrigger.instrument_key,
            AlertBreadthTrigger.last_trigger_ts,
        )
        .where(
            AlertBreadthTrigger.owner_id == owner_id,
            AlertBreadthTrigger.workflow_id == workflow_id,
            AlertBreadthTrigger.revision_id == revision_id,
            AlertBreadthTrigger.stage_id == stage_id,
            AlertBreadthTrigger.last_trigger_ts >= window_start,
            AlertBreadthTrigger.last_trigger_ts <= evaluated_at,
        )
        .order_by(AlertBreadthTrigger.last_trigger_ts.asc())
        .limit(max(1, int(limit)))
    )
    if members is not None:
        allowed = list(dict.fromkeys(members))
        if not allowed:
            return 0, []
        stmt = stmt.where(AlertBreadthTrigger.instrument_key.in_(allowed))
        # Concrete bound: an overflow would mean more members than the
        # configured capacity, which the caller already reports as unknown.
        stmt = stmt.limit(max(1, min(int(limit), len(allowed))))
    rows = session.execute(stmt).all()
    return len(rows), [(row[0], row[1]) for row in rows]


def evict_breadth_contribution(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    instrument_key: str,
) -> None:
    """Clear one instrument's contribution (membership re-entry).

    Called when an instrument is re-admitted to the member set: its retained
    row is history, not participation, so the rule stays "an instrument
    contributes after admission" rather than "contributes because it once
    did". The window can outlive a departure, so without this a readmitted
    symbol would silently carry a pre-departure trigger.
    """
    session.execute(
        delete(AlertBreadthTrigger).where(
            AlertBreadthTrigger.owner_id == owner_id,
            AlertBreadthTrigger.workflow_id == workflow_id,
            AlertBreadthTrigger.revision_id == revision_id,
            AlertBreadthTrigger.stage_id == stage_id,
            AlertBreadthTrigger.instrument_key == instrument_key,
        )
    )


# ---------------------------------------------------------------------------
# session caps
# ---------------------------------------------------------------------------


def session_count(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    alert_id: str,
    session_id: str,
) -> int:
    row = session.execute(
        select(AlertSessionCounter.count).where(
            AlertSessionCounter.owner_id == owner_id,
            AlertSessionCounter.workflow_id == workflow_id,
            AlertSessionCounter.revision_id == revision_id,
            AlertSessionCounter.alert_id == alert_id,
            AlertSessionCounter.session_id == session_id,
        )
    ).scalar_one_or_none()
    return int(row or 0)


def reserve_session_slot(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    alert_id: str,
    session_id: str,
    maximum: int,
    now: Optional[datetime] = None,
) -> bool:
    """Atomically claim one notification slot for this session.

    Returns True when a slot was reserved, False when the cap is already
    reached. The claim is a single guarded upsert, so two workers owning
    different instruments of the same alert cannot both take the last slot;
    a capped attempt does NOT increment, so suppressed attempts can never
    exhaust the cap. The row is keyed by ``session_id``, so a new session
    simply starts a fresh row and no reset job is needed.
    """
    timestamp = now or _utcnow()
    if _is_postgres(session):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(AlertSessionCounter).values(
            id=_uuid(),
            owner_id=owner_id,
            workflow_id=workflow_id,
            revision_id=revision_id,
            alert_id=alert_id,
            session_id=session_id,
            count=1,
            first_at=timestamp,
            updated_at=timestamp,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                AlertSessionCounter.owner_id,
                AlertSessionCounter.workflow_id,
                AlertSessionCounter.revision_id,
                AlertSessionCounter.alert_id,
                AlertSessionCounter.session_id,
            ],
            set_={
                "count": AlertSessionCounter.__table__.c.count + 1,
                "updated_at": timestamp,
            },
            where=AlertSessionCounter.__table__.c.count < maximum,
        )
        return bool(session.execute(stmt).rowcount)
    # SQLite (unit tests): single writer, so read-then-guarded-write is safe.
    row = session.execute(
        select(AlertSessionCounter).where(
            AlertSessionCounter.owner_id == owner_id,
            AlertSessionCounter.workflow_id == workflow_id,
            AlertSessionCounter.revision_id == revision_id,
            AlertSessionCounter.alert_id == alert_id,
            AlertSessionCounter.session_id == session_id,
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            AlertSessionCounter(
                id=_uuid(),
                owner_id=owner_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                alert_id=alert_id,
                session_id=session_id,
                count=1,
                first_at=timestamp,
                updated_at=timestamp,
            )
        )
        session.flush()
        return True
    if row.count >= maximum:
        return False
    row.count += 1
    row.updated_at = timestamp
    session.flush()
    return True


def record_suppression(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    alert_id: str,
    session_id: Optional[str],
    reason: str,
    instrument_key: Optional[str] = None,
    stage_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Upsert the durable suppression counter for one reason.

    A skipped notification must be inspectable, so the reason, count and last
    observation are persisted in the SAME transaction that skipped the event.
    """
    timestamp = now or _utcnow()
    row = session.execute(
        select(AlertSuppressionCounter).where(
            AlertSuppressionCounter.owner_id == owner_id,
            AlertSuppressionCounter.workflow_id == workflow_id,
            AlertSuppressionCounter.revision_id == revision_id,
            AlertSuppressionCounter.alert_id == alert_id,
            AlertSuppressionCounter.session_id == session_id,
            AlertSuppressionCounter.reason == reason,
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            AlertSuppressionCounter(
                id=_uuid(),
                owner_id=owner_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                alert_id=alert_id,
                session_id=session_id,
                reason=reason,
                count=1,
                first_at=timestamp,
                last_at=timestamp,
                last_instrument_key=instrument_key,
                last_stage_id=stage_id,
            )
        )
    else:
        row.count += 1
        row.last_at = timestamp
        row.last_instrument_key = instrument_key
        row.last_stage_id = stage_id
    session.flush()


def suppression_summary(
    session: Session, *, workflow_id: str, revision_id: Optional[str] = None
) -> Dict[str, int]:
    """Durable suppression counts per reason (for the health response)."""
    stmt = select(
        AlertSuppressionCounter.reason, AlertSuppressionCounter.count
    ).where(AlertSuppressionCounter.workflow_id == workflow_id)
    if revision_id is not None:
        stmt = stmt.where(AlertSuppressionCounter.revision_id == revision_id)
    out: Dict[str, int] = {}
    for reason, count in session.execute(stmt).all():
        out[str(reason)] = out.get(str(reason), 0) + int(count or 0)
    return out
