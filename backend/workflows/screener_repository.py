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
nothing at all.

Attachment transitions are not precomputed. The caller supplies an
:class:`AttachmentPlan` (the run's ranked results plus the attachment
specifications — everything the pipeline already produced); ``finalize_run``
acquires the per-attachment PostgreSQL advisory transaction lock, reads the
baseline UNDER that lock, and only then derives the events and state updates
from the current baseline, publishing them in the same transaction. A stale
baseline snapshot can therefore never reach publication: whatever the
baseline is at lock time is what the transitions are computed against, even
when the baseline is still empty and there are no rows to lock. The
chronology gate remains as a second guard — a run whose ``scheduled_for`` is
strictly older than the run owning the locked baseline is suppressed whole,
because its transitions describe a moment that has already been superseded.

Runs are never deleted by the scheduler; history stays attributable to the
exact workflow revision and universe revision that produced it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
    "AttachmentTask",
    "AttachmentPlan",
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
    """One computed attachment signal event + outbox fan-out.

    Derived inside the publication transaction, from the baseline read under
    the attachment lock — never carried in from outside.
    """

    attachment_id: str
    occurrence_key: str
    fired_at: datetime
    evidence: dict
    channel_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AttachmentStateUpdate:
    """One computed baseline row upsert."""

    attachment_id: str
    instrument_key: str
    present: bool
    rank: Optional[int]
    consecutive_absent: int


@dataclass(frozen=True)
class AttachmentTask:
    """One attachment to evaluate inside the publication transaction.

    ``spec`` is the parsed attachment (trigger, thresholds, channels, message,
    ``initial_match``); ``results`` is this run's ranked pipeline output.
    Both are produced OUTSIDE the transaction — the expensive screener
    evaluation never runs under the attachment lock.
    """

    attachment_id: str
    spec: Any
    results: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class AttachmentPlan:
    """What ``finalize_run`` needs to derive and publish attachment side effects.

    Carries no baseline state: the baseline is read inside the publication
    transaction, after the per-attachment lock is held, so the transitions
    reflect the current baseline rather than a snapshot taken during
    preparation.
    """

    owner_id: str
    workflow_id: str
    revision_id: str
    run_id: str
    scheduled_for: datetime
    screener_name: str
    tasks: Tuple[AttachmentTask, ...] = ()
    channel_resolver: Optional[Callable[[str, Sequence[str]], Dict[str, str]]] = None
    max_events: int = 100


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
        attachment_plan: Optional[AttachmentPlan] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Publish the run and ALL its side effects in ONE fenced transaction.

        Order inside the transaction:

        1. compare-and-swap ``screener_run`` (id + ``lease_owner`` +
           ``status='running'``): rowcount 0 — stale owner whose lease was
           taken over, or an already-finalized run — aborts the WHOLE
           publication before anything becomes visible;
        2. delete + insert the member rows;
        3. per attachment task: acquire the attachment's advisory transaction
           lock, read the baseline UNDER the lock, derive the events and
           state updates from that baseline, then insert the signal events +
           pending deliveries (occurrence-key pre-checked) and upsert the
           baseline rows. A task whose ``scheduled_for`` is older than the
           run owning the locked baseline is suppressed whole;
        4. fold the published attachment counters into ``coverage``.

        The only attachment work done before this method is the ranked
        pipeline evaluation (``AttachmentPlan.tasks[*].results``); the
        baseline-dependent reasoning happens here, under the lock, so no
        baseline snapshot can go stale between preparation and publication.

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
            if attachment_plan is not None:
                summary = self._publish_attachments(session, attachment_plan, timestamp)
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

    def _publish_attachments(
        self,
        session: Session,
        plan: AttachmentPlan,
        timestamp: datetime,
    ) -> Dict[str, int]:
        """Derive and publish every attachment task's side effects.

        Per task, in order:

        1. acquire the PostgreSQL advisory transaction lock keyed on
           (owner, workflow, revision, attachment) — this is what serializes
           overlapping publications of the same baseline, including the first
           one, which has no baseline rows for a row lock to hold;
        2. read the baseline rows through THIS transaction, under the lock;
        3. chronology gate: if the baseline is owned by a run with a strictly
           newer ``scheduled_for``, this occurrence has been superseded — the
           task is suppressed whole (no events, no state writes);
        4. compute the transitions against the just-read baseline, insert the
           signal events + pending deliveries (occurrence-key deduplicated),
           then upsert the baseline rows.

        Returns the counters folded into the run's coverage.
        """
        applied: Dict[str, int] = {"attachment_events_published": 0}
        for task in plan.tasks:
            _lock_attachment_baseline(
                session,
                owner_id=plan.owner_id,
                workflow_id=plan.workflow_id,
                revision_id=plan.revision_id,
                attachment_id=task.attachment_id,
            )
            states = _read_attachment_states(
                session,
                owner_id=plan.owner_id,
                workflow_id=plan.workflow_id,
                revision_id=plan.revision_id,
                attachment_id=task.attachment_id,
            )
            if _baseline_supersedes(session, states, plan.scheduled_for):
                applied["attachment_events_stale_suppressed"] = (
                    applied.get("attachment_events_stale_suppressed", 0) + 1
                )
                continue
            events, state_updates, suppressed = _compute_attachment_transitions(
                plan=plan, task=task, states=states, timestamp=timestamp
            )
            applied["attachment_events_suppressed"] = (
                applied.get("attachment_events_suppressed", 0) + suppressed
            )
            applied["attachment_events_published"] += _insert_attachment_events(
                session, plan, events, timestamp
            )
            for update in state_updates:
                _upsert_attachment_state_row(
                    session,
                    owner_id=plan.owner_id,
                    workflow_id=plan.workflow_id,
                    revision_id=plan.revision_id,
                    attachment_id=update.attachment_id,
                    instrument_key=update.instrument_key,
                    present=update.present,
                    run_id=plan.run_id,
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
        """Committed baseline for one attachment (read-only inspection)."""
        session = self._sessions()
        try:
            return _read_attachment_states(
                session,
                owner_id=owner_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                attachment_id=attachment_id,
            )
        finally:
            session.close()


def _read_attachment_states(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    attachment_id: str,
) -> Dict[str, ScreenerAttachmentState]:
    """Read one attachment's baseline through the caller's transaction."""
    rows = session.execute(
        select(ScreenerAttachmentState).where(
            ScreenerAttachmentState.owner_id == owner_id,
            ScreenerAttachmentState.workflow_id == workflow_id,
            ScreenerAttachmentState.workflow_revision_id == revision_id,
            ScreenerAttachmentState.attachment_id == attachment_id,
        )
    ).scalars().all()
    return {row.instrument_key: row for row in rows}


def _lock_attachment_baseline(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    attachment_id: str,
) -> None:
    """Serialize overlapping publications of one attachment's baseline.

    A PostgreSQL advisory transaction lock (same pattern as universe
    resolution) rather than a row lock: the FIRST publication of an
    attachment has no baseline rows to lock, and two first publications
    racing on an empty baseline are exactly the interleaving that must not
    interleave. SQLite (unit tests) is a single writer.
    """
    if not _is_postgres(session):
        return
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtext('screener-attachment:' || :lock_key))"
        ),
        {
            "lock_key": (
                f"{owner_id}/{workflow_id}/{revision_id}/{attachment_id}"
            )
        },
    )


def _baseline_supersedes(
    session: Session,
    states: Dict[str, ScreenerAttachmentState],
    scheduled_for: datetime,
) -> bool:
    """True when the baseline was written by a strictly newer occurrence.

    With the transitions computed under the lock this is a pure ordering
    guard: a superseded occurrence would derive its "current" state from a
    moment that no longer exists, so it publishes nothing for the attachment
    (its run results still publish) and never overwrites newer state.
    """
    baseline_run_ids = {
        state.last_complete_run_id for state in states.values() if state.last_complete_run_id
    }
    if not baseline_run_ids:
        return False
    stored = session.execute(
        select(ScreenerRun.scheduled_for).where(ScreenerRun.id.in_(baseline_run_ids))
    ).scalars().all()
    newest_baseline = max(_as_utc(value) for value in stored)
    return _as_utc(scheduled_for) < newest_baseline


def _insert_attachment_events(
    session: Session,
    plan: AttachmentPlan,
    events: Sequence[AttachmentEvent],
    timestamp: datetime,
) -> int:
    """Insert signal events + their pending deliveries; returns how many fired.

    The occurrence key makes a replay a no-op: the same logical event can
    never produce a second notification.
    """
    from backend.notifications.repository import Delivery

    from backend.workflows.repository import SignalEvent

    published = 0
    for event in events:
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
            workflow_id=str(plan.workflow_id),
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
        published += 1
    return published


def _compute_attachment_transitions(
    *,
    plan: AttachmentPlan,
    task: AttachmentTask,
    states: Dict[str, ScreenerAttachmentState],
    timestamp: datetime,
) -> Tuple[List[AttachmentEvent], List[AttachmentStateUpdate], int]:
    """Derive one attachment's events and baseline upserts from a locked baseline.

    Pure: ``states`` is the baseline as read under the lock, ``task.results``
    is this run's ranked pipeline output. Returns
    ``(events, state_updates, suppressed_event_count)``.

    Baseline semantics: the first complete run of a (revision, attachment)
    initializes state silently unless ``initial_match`` is set. Partial runs
    never reach this function (no exits, no baseline advance — E-18).

    Hysteresis: ``top_n`` enters at rank <= entry_rank and only exits when
    rank > exit_rank (entry_rank < exit_rank by validation, E-17);
    ``entry``/``exit`` triggers use ``exit_after`` consecutive absences
    (default 1). ``rank_delta`` fires on |current_rank - last_rank| >=
    threshold using the previous complete run's rank (a distinct concept
    from hysteresis bands — it compares ranks, it does not gate presence).
    """
    spec = task.spec
    results = task.results
    run_id = plan.run_id
    baseline = not states
    current = {m.instrument_key: m for m in results}
    raw_events: List[Tuple[str, dict]] = []
    state_updates: List[AttachmentStateUpdate] = []

    for key, member in sorted(current.items()):
        previous = states.get(key)
        prev_rank = previous.last_rank if previous is not None else None
        was_present = bool(previous.present) if previous is not None else False

        if spec.trigger == "top_n":
            enters = member.passed and member.rank is not None and member.rank <= (spec.entry_rank or 0)
            if was_present and (not member.passed or member.rank is None or member.rank > (spec.exit_rank or 0)):
                # still inside the hysteresis band: neither exit nor entry
                stays = member.passed and member.rank is not None and member.rank <= (spec.exit_rank or 0)
                if stays:
                    state_updates.append(_state_update(spec, key, True, member.rank, 0))
                    continue
                state_updates.append(_state_update(spec, key, False, member.rank, 0))
                raw_events.append((key, {"action": "exit", "rank": member.rank, "prev_rank": prev_rank}))
                continue
            if enters and not was_present:
                state_updates.append(_state_update(spec, key, True, member.rank, 0))
                raw_events.append((key, {"action": "entry", "rank": member.rank, "prev_rank": prev_rank}))
                continue
            state_updates.append(_state_update(spec, key, bool(enters or was_present), member.rank, 0))
            continue

        if spec.trigger in ("entry", "exit"):
            present = bool(member.passed)
            absent_streak = 0 if present else ((previous.consecutive_absent if previous else 0) + 1)
            threshold = spec.exit_after or 1
            if present and not was_present:
                raw_events.append((key, {"action": "entry", "rank": member.rank, "prev_rank": prev_rank}))
            if was_present and not present and absent_streak >= threshold:
                raw_events.append((key, {"action": "exit", "rank": member.rank, "prev_rank": prev_rank}))
            state_updates.append(_state_update(spec, key, present, member.rank, absent_streak))
            continue

        if spec.trigger == "rank_delta":
            if member.passed and member.rank is not None and prev_rank is not None:
                delta = abs(member.rank - prev_rank)
                if delta >= (spec.rank_delta or 0):
                    raw_events.append((
                        key,
                        {
                            "action": "rank_change",
                            "rank": member.rank,
                            "prev_rank": prev_rank,
                            "delta": delta,
                            "direction": "up" if member.rank < prev_rank else "down",
                        },
                    ))
            state_updates.append(_state_update(spec, key, bool(member.passed), member.rank, 0))
            continue

    # instruments present in prior state but absent from this run's
    # results (universe departure): they cannot be ranked any more —
    # treat as absent for entry/exit triggers, exited for top_n bands.
    for key in sorted(set(states) - set(current)):
        previous = states[key]
        if spec.trigger == "top_n":
            if previous.present:
                # a departed instrument can no longer hold a rank band
                raw_events.append((key, {"action": "exit", "rank": None, "prev_rank": previous.last_rank}))
                state_updates.append(_state_update(spec, key, False, None, 0))
            continue
        if spec.trigger in ("entry", "exit"):
            # streak advances once per complete run; the exit fires at
            # the exact crossing (streak == threshold), never repeats
            streak = (previous.consecutive_absent or 0) + 1
            threshold = spec.exit_after or 1
            if streak == threshold:
                raw_events.append((key, {"action": "exit", "rank": None, "prev_rank": previous.last_rank}))
            state_updates.append(_state_update(spec, key, False, None, streak))

    if baseline and not spec.initial_match:
        # first complete run: initialize state, notify nothing
        state_updates = [
            _state_update(spec, key, bool(current[key].passed), current[key].rank, 0)
            for key in current
        ]
        raw_events = []

    events: List[AttachmentEvent] = []
    suppressed = 0
    for key, payload in raw_events:
        if len(events) >= plan.max_events:
            suppressed += 1
            continue
        member = current.get(key)
        member_values = dict(member.values) if member is not None else {}
        channel_ids = (
            plan.channel_resolver(plan.owner_id, list(spec.channels)) or {}
            if plan.channel_resolver is not None
            else {}
        )
        events.append(
            AttachmentEvent(
                attachment_id=spec.id,
                occurrence_key=f"{plan.workflow_id}:{plan.revision_id}:{spec.id}:{run_id}:{key}",
                fired_at=plan.scheduled_for or timestamp,
                evidence={
                    "screener": plan.screener_name,
                    "attachment_id": spec.id,
                    "trigger": spec.trigger,
                    "action": payload.get("action"),
                    "instrument_key": key,
                    "rank": payload.get("rank"),
                    "prev_rank": payload.get("prev_rank"),
                    "rank_delta": payload.get("delta"),
                    "direction": payload.get("direction"),
                    "run_id": run_id,
                    "scheduled_for": plan.scheduled_for.isoformat() if plan.scheduled_for else None,
                    "message": spec.message,
                    "values": {k: v for k, v in member_values.items() if k in ("close", "change_pct", "turnover", "score", "candle_ts")},
                    "message_kind": "screener_attachment",
                },
                channel_ids=tuple(dict.fromkeys(channel_ids.values())),
            )
        )
    return events, state_updates, suppressed


def _state_update(
    spec: Any, key: str, present: bool, rank: Optional[int], absent_streak: int
) -> AttachmentStateUpdate:
    return AttachmentStateUpdate(
        attachment_id=spec.id,
        instrument_key=key,
        present=present,
        rank=rank,
        consecutive_absent=absent_streak,
    )


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

