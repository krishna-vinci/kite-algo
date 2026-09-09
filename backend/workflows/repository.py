"""SQLAlchemy persistence for the alerts platform (Task 5).

Defines the shared declarative ``Base`` for both alerts-platform repository
modules (``backend.notifications.repository`` builds its tables on this same
``Base``) plus the workflow-side tables:

- ``workflows`` / ``workflow_revisions`` / ``alert_subscriptions``
- ``evaluation_checkpoints`` (per subscription/instrument/epoch state)
- ``signal_events`` (durable, idempotent signal outbox source)

The matching Postgres migration is
``backend/alembic/versions/20260908_000011_alerts_platform_phase1.py``.

This module imports only the standard library and SQLAlchemy (plus a lazy
import of the notifications repository inside ``record_signal`` so the delivery
fan-out stays in the caller's transaction); it is safe to import from any
process without redis.

Repositories take a ``session_factory`` (a ``sessionmaker`` or any callable
returning a :class:`sqlalchemy.orm.Session`), mirroring
``backend.api.repositories.algo_worker_repo``. Every public method runs in its
own transaction unless a ``db`` session is passed in, in which case the caller
owns the transaction (used to compose checkpoint + signal + deliveries
atomically).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base

__all__ = [
    "Base",
    "Workflow",
    "WorkflowRevision",
    "AlertSubscription",
    "EvaluationCheckpoint",
    "SignalEvent",
    "ActiveSubscription",
    "SqlAlchemyWorkflowRepository",
    "DomainConflict",
    "RevisionConflict",
    "IdempotencyConflict",
    "LeaseConflict",
]

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(session: Any) -> bool:
    bind = getattr(session, "bind", None)
    return bind is not None and getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


# ---------------------------------------------------------------------------
# domain errors
# ---------------------------------------------------------------------------


class DomainConflict(Exception):
    """A domain invariant was violated (duplicate hash, illegal transition...)."""


class RevisionConflict(DomainConflict):
    """An expected_revision optimistic-concurrency check failed."""


class IdempotencyConflict(DomainConflict):
    """An idempotency_key was replayed with a different name."""


class LeaseConflict(Exception):
    """A checkpoint compare-and-swap lost: the stored owner_epoch moved on."""


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(String(36), primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    idempotency_key = Column(String(255), unique=True, nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_workflows_owner_name"),
    )


class WorkflowRevision(Base):
    __tablename__ = "workflow_revisions"

    id = Column(String(36), primary_key=True, default=_uuid)
    workflow_id = Column(String(36), ForeignKey("workflows.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    canonical_hash = Column(String(64), nullable=False, index=True)
    document = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="draft")  # draft|active|archived
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    activated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("workflow_id", "revision", name="uq_workflow_revisions_workflow_revision"),
        UniqueConstraint("workflow_id", "canonical_hash", name="uq_workflow_revisions_workflow_hash"),
    )


class AlertSubscription(Base):
    __tablename__ = "alert_subscriptions"

    id = Column(String(36), primary_key=True, default=_uuid)
    revision_id = Column(String(36), ForeignKey("workflow_revisions.id"), nullable=False)
    alert_id = Column(String(255), nullable=False)
    stage_id = Column(String(255), nullable=False)
    instrument_symbol = Column(String(64), nullable=False)
    instrument_exchange = Column(String(32), nullable=False)
    instrument_key = Column(String(128), nullable=False, index=True)  # e.g. "NSE:RELIANCE"
    trigger = Column(String(32), nullable=False)
    # cooldown_s, rearm_level, rearm_direction, reminder_interval_s,
    # notify_if_already_true, expires_at, channels, message
    config = Column(JSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default="active")  # active|paused|expired|completed
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "revision_id", "alert_id", "instrument_key",
            name="uq_alert_subscriptions_revision_alert_instrument",
        ),
    )


class EvaluationCheckpoint(Base):
    __tablename__ = "evaluation_checkpoints"

    subscription_id = Column(String(36), primary_key=True)
    instrument_key = Column(String(128), primary_key=True)
    epoch_id = Column(String(128), primary_key=True)
    state = Column(JSON, nullable=False, default=dict)
    owner_epoch = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    subscription_id = Column(String(36), ForeignKey("alert_subscriptions.id"), nullable=False)
    occurrence_key = Column(String(512), nullable=False, unique=True, index=True)
    fired_at = Column(DateTime(timezone=True), nullable=False)
    evidence = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


@dataclass(frozen=True)
class ActiveSubscription:
    """An active alert subscription joined with its owner and revision document."""

    id: str
    revision_id: str
    alert_id: str
    stage_id: str
    instrument_symbol: str
    instrument_exchange: str
    instrument_key: str
    trigger: str
    config: dict
    state: str
    created_at: Optional[datetime]
    owner_id: str
    workflow_id: str
    document: dict


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


class SqlAlchemyWorkflowRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def _session(self) -> Session:
        session = self.session_factory()
        # Keep returned ORM objects usable after commit + close.
        session.expire_on_commit = False
        return session

    # -- workflows ----------------------------------------------------------

    def create_workflow(
        self,
        owner_id: str,
        name: str,
        document_dict: dict,
        canonical_hash: str,
        idempotency_key: Optional[str] = None,
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Create a workflow with its first draft revision.

        Replays the original ``(workflow, latest revision)`` when
        ``idempotency_key`` matches; raises :class:`IdempotencyConflict` when
        the key exists for a different name.
        """
        if db is not None:
            return self._create_workflow(db, owner_id, name, document_dict, canonical_hash, idempotency_key, now)
        session = self._session()
        try:
            result = self._create_workflow(session, owner_id, name, document_dict, canonical_hash, idempotency_key, now)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_workflow(self, session, owner_id, name, document_dict, canonical_hash, idempotency_key, now):
        if idempotency_key:
            existing = session.execute(
                select(Workflow).where(Workflow.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.name != name:
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} already used by workflow "
                        f"{existing.name!r}, refusing to create {name!r}"
                    )
                latest = session.execute(
                    select(WorkflowRevision)
                    .where(WorkflowRevision.workflow_id == existing.id)
                    .order_by(WorkflowRevision.revision.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest is None:
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} matches workflow {existing.id} without revisions"
                    )
                return existing, latest

        timestamp = now or _utcnow()
        workflow = Workflow(
            id=_uuid(),
            owner_id=owner_id,
            name=name,
            idempotency_key=idempotency_key,
            created_at=timestamp,
            updated_at=timestamp,
        )
        revision = WorkflowRevision(
            id=_uuid(),
            workflow_id=workflow.id,
            revision=1,
            canonical_hash=canonical_hash,
            document=dict(document_dict or {}),
            status="draft",
            created_at=timestamp,
        )
        session.add(workflow)
        session.add(revision)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DomainConflict(
                f"workflow {name!r} already exists for owner {owner_id!r}"
            ) from exc
        return workflow, revision

    def add_draft_revision(
        self,
        workflow_id: str,
        document_dict: dict,
        canonical_hash: str,
        expected_revision: Optional[int] = None,
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Append a new draft revision; duplicate canonical_hash -> DomainConflict.

        When ``expected_revision`` is given, the workflow's current max
        revision is re-read (and locked with ``FOR UPDATE`` on Postgres)
        inside the SAME transaction as the insert and a mismatch raises
        :class:`RevisionConflict` before any row is written — the
        optimistic-concurrency check is atomic with the insert.
        """
        if db is not None:
            return self._add_draft_revision(db, workflow_id, document_dict, canonical_hash, now, expected_revision)
        session = self._session()
        try:
            revision = self._add_draft_revision(session, workflow_id, document_dict, canonical_hash, now, expected_revision)
            session.commit()
            return revision
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _add_draft_revision(self, session, workflow_id, document_dict, canonical_hash, now, expected_revision=None):
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        if expected_revision is not None:
            # Serialize concurrent appends per workflow on Postgres: the row
            # lock is taken BEFORE reading the max revision so the
            # compare-and-insert below is one atomic unit. SQLite ignores the
            # lock; the same-transaction check is the correctness fix.
            if _is_postgres(session):
                session.execute(select(Workflow.id).where(Workflow.id == workflow_id).with_for_update())
        latest = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id)
            .order_by(WorkflowRevision.revision.desc())
            .limit(1)
        ).scalar_one_or_none()
        if expected_revision is not None:
            current = int(latest.revision) if latest is not None else 0
            if current != int(expected_revision):
                raise RevisionConflict(
                    f"workflow {workflow_id}: expected_revision {expected_revision} "
                    f"does not match current revision {current}"
                )
        revision = WorkflowRevision(
            id=_uuid(),
            workflow_id=workflow_id,
            revision=(latest.revision if latest is not None else 0) + 1,
            canonical_hash=canonical_hash,
            document=dict(document_dict or {}),
            status="draft",
            created_at=now or _utcnow(),
        )
        session.add(revision)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DomainConflict(
                f"canonical_hash {canonical_hash!r} already stored for workflow {workflow_id}"
            ) from exc
        return revision

    def activate_revision(
        self,
        workflow_id: str,
        revision_id: str,
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Activate one revision, archiving every other active revision.

        Accepts revisions in status ``draft`` **or** ``archived``: rolling back
        to an older revision re-activates it. An already-active revision (or a
        revision of another workflow) raises :class:`DomainConflict`; the
        single-active invariant is always preserved. Activating also un-archives
        the workflow itself.
        """
        if db is not None:
            return self._activate_revision(db, workflow_id, revision_id, now)
        session = self._session()
        try:
            revision = self._activate_revision(session, workflow_id, revision_id, now)
            session.commit()
            return revision
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _activate_revision(self, session, workflow_id, revision_id, now):
        timestamp = now or _utcnow()
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        revision = session.get(WorkflowRevision, revision_id)
        if revision is None or revision.workflow_id != workflow_id:
            raise DomainConflict(
                f"revision {revision_id} does not belong to workflow {workflow_id}"
            )
        if revision.status not in ("draft", "archived"):
            raise DomainConflict(
                f"revision {revision_id} is {revision.status!r}; only draft or "
                "archived revisions can be activated"
            )
        # Serialize concurrent activations per workflow on Postgres.
        if _is_postgres(session):
            session.execute(select(Workflow.id).where(Workflow.id == workflow_id).with_for_update())
        session.execute(
            update(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
            .values(status="archived")
        )
        revision.status = "active"
        revision.activated_at = timestamp
        # Reactivation (e.g. rollback after archive) un-archives the workflow.
        workflow.archived_at = None
        workflow.updated_at = timestamp
        session.flush()
        return revision

    def get_workflow(self, workflow_id: str, *, db: Optional[Session] = None):
        if db is not None:
            return db.get(Workflow, workflow_id)
        session = self._session()
        try:
            return session.get(Workflow, workflow_id)
        finally:
            session.close()

    def list_workflows(self, owner_id: str, *, db: Optional[Session] = None):
        if db is not None:
            return self._list_workflows(db, owner_id)
        session = self._session()
        try:
            return self._list_workflows(session, owner_id)
        finally:
            session.close()

    def _list_workflows(self, session, owner_id):
        return list(
            session.execute(
                select(Workflow)
                .where(Workflow.owner_id == owner_id)
                .order_by(Workflow.created_at.desc(), Workflow.id.desc())
            ).scalars().all()
        )

    def get_active_revision(self, workflow_id: str, *, db: Optional[Session] = None):
        if db is not None:
            return self._get_active_revision(db, workflow_id)
        session = self._session()
        try:
            return self._get_active_revision(session, workflow_id)
        finally:
            session.close()

    def _get_active_revision(self, session, workflow_id):
        return session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
            .order_by(WorkflowRevision.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # -- subscriptions ------------------------------------------------------

    def list_active_subscriptions(self, *, db: Optional[Session] = None):
        """Active subscriptions joined with owner_id and the revision document."""
        if db is not None:
            return self._list_active_subscriptions(db)
        session = self._session()
        try:
            return self._list_active_subscriptions(session)
        finally:
            session.close()

    def _list_active_subscriptions(self, session):
        # Only subscriptions of an ACTIVE revision of an un-archived workflow
        # are eligible: superseded (archived) revisions and archived workflows
        # must never be evaluated, even when their rows still say 'active'.
        rows = session.execute(
            select(
                AlertSubscription,
                Workflow.owner_id,
                Workflow.id.label("workflow_id"),
                WorkflowRevision.document,
            )
            .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
            .join(Workflow, WorkflowRevision.workflow_id == Workflow.id)
            .where(
                AlertSubscription.state == "active",
                WorkflowRevision.status == "active",
                Workflow.archived_at.is_(None),
            )
            .order_by(AlertSubscription.created_at.asc(), AlertSubscription.id.asc())
        ).all()
        return [
            ActiveSubscription(
                id=sub.id,
                revision_id=sub.revision_id,
                alert_id=sub.alert_id,
                stage_id=sub.stage_id,
                instrument_symbol=sub.instrument_symbol,
                instrument_exchange=sub.instrument_exchange,
                instrument_key=sub.instrument_key,
                trigger=sub.trigger,
                config=dict(sub.config or {}),
                state=sub.state,
                created_at=sub.created_at,
                owner_id=owner_id,
                workflow_id=workflow_id,
                document=dict(document or {}),
            )
            for sub, owner_id, workflow_id, document in rows
        ]

    # -- checkpoints --------------------------------------------------------

    def load_checkpoint(
        self,
        subscription_id: str,
        instrument_key: str,
        epoch_id: str,
        *,
        db: Optional[Session] = None,
    ):
        """Return ``(state dict, owner_epoch)`` or ``None`` when absent."""
        if db is not None:
            return self._load_checkpoint(db, subscription_id, instrument_key, epoch_id)
        session = self._session()
        try:
            return self._load_checkpoint(session, subscription_id, instrument_key, epoch_id)
        finally:
            session.close()

    def _load_checkpoint(self, session, subscription_id, instrument_key, epoch_id):
        row = session.get(EvaluationCheckpoint, (subscription_id, instrument_key, epoch_id))
        if row is None:
            return None
        return dict(row.state or {}), int(row.owner_epoch or 0)

    def save_checkpoint(
        self,
        subscription_id: str,
        instrument_key: str,
        epoch_id: str,
        state: dict,
        expected_owner_epoch: Optional[int],
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Atomic compare-and-swap of a checkpoint.

        The save is a single guarded ``UPDATE ... WHERE owner_epoch =
        :expected`` (rowcount 0 -> guarded insert-if-missing -> retried CAS ->
        :class:`LeaseConflict` when the stored owner_epoch moved on). On
        success the stored owner_epoch becomes ``expected + 1``; concurrent
        writers can never interleave a lost update between load and save.
        """
        if db is not None:
            return self._save_checkpoint(db, subscription_id, instrument_key, epoch_id, state, expected_owner_epoch, now)
        session = self._session()
        try:
            row = self._save_checkpoint(session, subscription_id, instrument_key, epoch_id, state, expected_owner_epoch, now)
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _cas_update(self, session, subscription_id, instrument_key, epoch_id, state, expected, timestamp):
        """One guarded UPDATE; returns the statement result (check rowcount)."""
        stmt = update(EvaluationCheckpoint).where(
            EvaluationCheckpoint.subscription_id == subscription_id,
            EvaluationCheckpoint.instrument_key == instrument_key,
            EvaluationCheckpoint.epoch_id == epoch_id,
        )
        if expected is not None:
            stmt = stmt.where(EvaluationCheckpoint.owner_epoch == expected)
        return session.execute(
            stmt.values(
                state=dict(state or {}),
                owner_epoch=EvaluationCheckpoint.owner_epoch + 1,
                updated_at=timestamp,
            )
        )

    def _save_checkpoint(self, session, subscription_id, instrument_key, epoch_id, state, expected_owner_epoch, now):
        timestamp = now or _utcnow()
        expected = None if expected_owner_epoch is None else int(expected_owner_epoch)

        result = self._cas_update(session, subscription_id, instrument_key, epoch_id, state, expected, timestamp)
        if result.rowcount:
            return self._refresh_checkpoint(session, subscription_id, instrument_key, epoch_id)

        # rowcount == 0: the row is missing, OR a racing writer already moved
        # owner_epoch past our expectation. Try a guarded insert (savepoint so
        # an IntegrityError never poisons the caller's transaction); when the
        # insert loses the race, retry the CAS once — losing THAT is a real
        # lease conflict.
        try:
            with session.begin_nested():
                session.add(
                    EvaluationCheckpoint(
                        subscription_id=subscription_id,
                        instrument_key=instrument_key,
                        epoch_id=epoch_id,
                        state=dict(state or {}),
                        owner_epoch=(0 if expected is None else expected) + 1,
                        updated_at=timestamp,
                    )
                )
                session.flush()
        except IntegrityError:
            retry = self._cas_update(session, subscription_id, instrument_key, epoch_id, state, expected, timestamp)
            if retry.rowcount == 0:
                stored = session.get(EvaluationCheckpoint, (subscription_id, instrument_key, epoch_id))
                stored_epoch = 0 if stored is None else int(stored.owner_epoch or 0)
                raise LeaseConflict(
                    f"checkpoint {subscription_id}/{instrument_key}/{epoch_id}: "
                    f"expected owner_epoch {expected}, stored {stored_epoch}"
                )
        return self._refresh_checkpoint(session, subscription_id, instrument_key, epoch_id)

    @staticmethod
    def _refresh_checkpoint(session, subscription_id, instrument_key, epoch_id):
        row = session.get(EvaluationCheckpoint, (subscription_id, instrument_key, epoch_id))
        if row is not None:
            session.refresh(row)  # the CAS UPDATE bypasses the identity map
        return row

    # -- signals ------------------------------------------------------------

    def record_signal(
        self,
        subscription_id: str,
        occurrence_key: str,
        fired_at: datetime,
        evidence: dict,
        channel_ids: Sequence[str],
        *,
        db: Optional[Session] = None,
        now: Optional[datetime] = None,
    ):
        """Atomically insert a signal event + one pending delivery per channel.

        Returns the :class:`SignalEvent`, or ``None`` when ``occurrence_key``
        already exists (unique violation -> nothing written). When ``db`` is
        given, the caller owns the transaction and IntegrityError propagates.
        """
        if db is not None:
            return self._record_signal(db, subscription_id, occurrence_key, fired_at, evidence, channel_ids, now)
        session = self._session()
        try:
            event = self._record_signal(session, subscription_id, occurrence_key, fired_at, evidence, channel_ids, now)
            session.commit()
            return event
        except IntegrityError:
            # occurrence_key already exists (including a racing writer that
            # won the unique constraint): nothing must be written.
            session.rollback()
            return None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _record_signal(self, session, subscription_id, occurrence_key, fired_at, evidence, channel_ids, now):
        # Deferred import: notifications.repository imports Base from this
        # module, so resolve it lazily to avoid a circular import.
        from backend.notifications.repository import Delivery

        timestamp = now or _utcnow()
        event = SignalEvent(
            id=_uuid(),
            subscription_id=subscription_id,
            occurrence_key=occurrence_key,
            fired_at=fired_at,
            evidence=dict(evidence or {}),
            created_at=timestamp,
        )
        session.add(event)
        session.flush()  # IntegrityError on duplicate occurrence_key propagates
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
        session.flush()
        return event

    def list_events(
        self,
        subscription_ids: Sequence[str],
        limit: int = 50,
        offset: int = 0,
        *,
        db: Optional[Session] = None,
    ):
        """Paginated signal events, newest first."""
        limit = int(limit)
        offset = int(offset)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        ids = [str(value) for value in (subscription_ids or [])]
        if db is not None:
            return self._list_events(db, ids, limit, offset)
        session = self._session()
        try:
            return self._list_events(session, ids, limit, offset)
        finally:
            session.close()

    def _list_events(self, session, subscription_ids, limit, offset):
        if not subscription_ids:
            return []
        return list(
            session.execute(
                select(SignalEvent)
                .where(SignalEvent.subscription_id.in_(subscription_ids))
                .order_by(SignalEvent.fired_at.desc(), SignalEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars().all()
        )
