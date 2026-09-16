"""Persistence contract tests for the alerts platform (Task 5).

Every test gets a fresh in-memory SQLite engine (pattern from
tests/api/test_algo_worker_api.py::_sqlite_algo_worker_repo) with the shared
alerts-platform ``Base.metadata.create_all``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import Delivery
from backend.workflows.compiler import compile_document
from backend.workflows.models import (
    AlertSpec,
    Condition,
    InstrumentRef,
    Operand,
    Stage,
    WorkflowDocument,
)
from backend.workflows.repository import (
    ActiveSubscription,
    AlertSubscription,
    Base,
    DomainConflict,
    EvaluationCheckpoint,
    IdempotencyConflict,
    LeaseConflict,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
    WorkflowRevision,
)


def _utc(value):
    """SQLite round-trips datetimes without tzinfo; treat naive as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _document(name: str = "reliance-breakout", level: float = 3000.0) -> WorkflowDocument:
    return WorkflowDocument(
        version=1,
        name=name,
        instruments=(InstrumentRef(symbol="RELIANCE", exchange="NSE"),),
        stages=(
            Stage(
                id="px",
                type="signal",
                clock="ltp",
                timeframe=None,
                conditions=(
                    Condition(
                        Operand(kind="field", name="ltp"),
                        "crosses_above",
                        Operand(kind="value", value=level),
                    ),
                ),
            ),
        ),
        alerts=(
            AlertSpec(id="breakout", source="px", trigger="once", channels=("telegram_primary",)),
        ),
    )


def _compiled(doc: WorkflowDocument):
    compiled = compile_document(doc)
    return compiled.document.to_document_dict(), compiled.canonical_hash


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _create_workflow(repo, **overrides):
    doc = overrides.pop("doc", None) or _document()
    document_dict, canonical_hash = _compiled(doc)
    kwargs = {
        "owner_id": "owner-1",
        "name": doc.name,
        "document_dict": document_dict,
        "canonical_hash": canonical_hash,
    }
    kwargs.update(overrides)
    return repo.create_workflow(**kwargs)


def _add_subscription(
    session_factory,
    revision_id: str,
    *,
    state: str = "active",
    alert_id: str = "breakout",
    instrument_symbol: str = "RELIANCE",
    instrument_key: str = "NSE:RELIANCE",
    channels=("telegram_primary",),
):
    with session_factory() as session:
        sub = AlertSubscription(
            id=str(uuid.uuid4()),
            revision_id=revision_id,
            alert_id=alert_id,
            stage_id="px",
            instrument_symbol=instrument_symbol,
            instrument_exchange="NSE",
            instrument_key=instrument_key,
            trigger="once",
            config={
                "cooldown_s": None,
                "rearm_level": None,
                "rearm_direction": None,
                "reminder_interval_s": None,
                "notify_if_already_true": False,
                "expires_at": None,
                "channels": list(channels),
                "message": None,
            },
            state=state,
        )
        session.add(sub)
        session.commit()
        return sub.id


# ---------------------------------------------------------------------------
# workflows + revisions
# ---------------------------------------------------------------------------


def test_create_workflow_and_draft_revision(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    document_dict, canonical_hash = _compiled(_document())
    wf, rev = _create_workflow(repo)

    assert wf.owner_id == "owner-1"
    assert wf.name == "reliance-breakout"
    assert wf.idempotency_key is None
    assert wf.archived_at is None
    uuid.UUID(wf.id)

    assert rev.workflow_id == wf.id
    assert rev.revision == 1
    assert rev.status == "draft"
    assert rev.activated_at is None
    assert rev.document == document_dict
    assert rev.canonical_hash == canonical_hash

    assert repo.get_workflow(wf.id).id == wf.id
    assert repo.get_workflow("missing") is None
    assert [w.id for w in repo.list_workflows("owner-1")] == [wf.id]
    assert repo.list_workflows("nobody") == []
    assert repo.get_active_revision(wf.id) is None


def test_add_draft_revision_duplicate_canonical_hash_conflict(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, _rev = _create_workflow(repo)
    document_dict, canonical_hash = _compiled(_document())

    with pytest.raises(DomainConflict):
        repo.add_draft_revision(wf.id, document_dict, canonical_hash)

    other_doc, other_hash = _compiled(_document(name="reliance-breakout-v2", level=3100.0))
    rev2 = repo.add_draft_revision(wf.id, other_doc, other_hash)
    assert rev2.revision == 2
    assert rev2.status == "draft"

    with pytest.raises(KeyError):
        repo.add_draft_revision("missing-workflow", document_dict, canonical_hash)


def test_add_draft_revision_expected_revision_checked_in_transaction(session_factory):
    """expected_revision must be verified inside the SAME transaction as the
    insert (compare-and-insert), so a stale expectation conflicts before any
    row is written."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, _rev1 = _create_workflow(repo)
    doc2, hash2 = _compiled(_document(name="reliance-breakout-v2", level=3100.0))
    rev2 = repo.add_draft_revision(wf.id, doc2, hash2)
    assert rev2.revision == 2

    doc3, hash3 = _compiled(_document(name="reliance-breakout-v3", level=3200.0))

    # stale expectation (latest is 2, caller thinks 1): conflict, no revision
    with pytest.raises(DomainConflict):
        repo.add_draft_revision(wf.id, doc3, hash3, expected_revision=1)
    with session_factory() as session:
        revisions = session.execute(
            select(WorkflowRevision).where(WorkflowRevision.workflow_id == wf.id)
        ).scalars().all()
    assert sorted(revision.revision for revision in revisions) == [1, 2]

    # matching expectation inserts; unknown workflow still KeyErrors
    rev3 = repo.add_draft_revision(wf.id, doc3, hash3, expected_revision=2)
    assert rev3.revision == 3
    with pytest.raises(KeyError):
        repo.add_draft_revision("missing-workflow", doc3, hash3, expected_revision=0)


def test_activate_revision_single_active_invariant(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, rev1 = _create_workflow(repo)
    doc2, hash2 = _compiled(_document(name="reliance-breakout-v2", level=3100.0))
    rev2 = repo.add_draft_revision(wf.id, doc2, hash2)

    activated = repo.activate_revision(wf.id, rev2.id)
    assert activated.status == "active"
    assert _utc(activated.activated_at) is not None

    active = repo.get_active_revision(wf.id)
    assert active is not None and active.id == rev2.id

    with session_factory() as session:
        statuses = {row.id: row.status for row in session.execute(select(WorkflowRevision)).scalars()}
    assert statuses[rev1.id] == "draft"  # never active, untouched
    assert statuses[rev2.id] == "active"

    # a previously-active revision is archived by the next activation
    doc3, hash3 = _compiled(_document(name="reliance-breakout-v3", level=3200.0))
    rev3 = repo.add_draft_revision(wf.id, doc3, hash3)
    repo.activate_revision(wf.id, rev3.id)
    with session_factory() as session:
        statuses = {row.id: row.status for row in session.execute(select(WorkflowRevision)).scalars()}
    assert statuses[rev1.id] == "draft"
    assert statuses[rev2.id] == "archived"
    assert statuses[rev3.id] == "active"
    assert repo.get_active_revision(wf.id).id == rev3.id

    # an already-active revision cannot be activated again
    with pytest.raises(DomainConflict):
        repo.activate_revision(wf.id, rev3.id)
    with pytest.raises(DomainConflict):
        repo.activate_revision(wf.id, str(uuid.uuid4()))  # unknown revision

    # rollback: activating the ARCHIVED rev2 re-activates it and re-archives
    # the currently-active rev3 (single-active invariant preserved)
    repo.activate_revision(wf.id, rev2.id)
    with session_factory() as session:
        statuses = {row.id: row.status for row in session.execute(select(WorkflowRevision)).scalars()}
    assert statuses[rev1.id] == "draft"
    assert statuses[rev2.id] == "active"
    assert statuses[rev3.id] == "archived"
    assert repo.get_active_revision(wf.id).id == rev2.id

    # activating the remaining draft re-archives the current active revision
    repo.activate_revision(wf.id, rev1.id)
    assert repo.get_active_revision(wf.id).id == rev1.id
    with pytest.raises(DomainConflict):
        repo.activate_revision(wf.id, rev1.id)  # no longer draft/archived (active)

    # a revision belonging to a different workflow is rejected
    wf_b, rev_b = _create_workflow(repo, doc=_document(name="other-breakout"))
    assert wf_b.id != wf.id
    with pytest.raises(DomainConflict):
        repo.activate_revision(wf.id, rev_b.id)

    with pytest.raises(KeyError):
        repo.activate_revision("missing-workflow", rev_b.id)


def test_rollback_reactivates_older_revision_and_archives_current(session_factory):
    """activate rev1 -> add+activate rev2 -> activate rev1 again: rev1 active,
    rev2 archived, exactly one active revision."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, rev1 = _create_workflow(repo)
    doc2, hash2 = _compiled(_document(name="reliance-breakout-v2", level=3100.0))
    rev2 = repo.add_draft_revision(wf.id, doc2, hash2)

    repo.activate_revision(wf.id, rev1.id)
    repo.activate_revision(wf.id, rev2.id)
    assert repo.get_active_revision(wf.id).id == rev2.id

    rolled_back = repo.activate_revision(wf.id, rev1.id)
    assert rolled_back.status == "active"

    active = repo.get_active_revision(wf.id)
    assert active is not None and active.id == rev1.id
    with session_factory() as session:
        statuses = {row.id: row.status for row in session.execute(select(WorkflowRevision)).scalars()}
    assert statuses[rev1.id] == "active"
    assert statuses[rev2.id] == "archived"


def test_activate_revision_unarchives_an_archived_workflow(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, rev = _create_workflow(repo)
    repo.activate_revision(wf.id, rev.id)
    with session_factory() as session:
        workflow = session.get(WorkflowModel, wf.id)
        workflow.archived_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        session.execute(
            update(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == wf.id)
            .values(status="archived")
        )
        session.commit()

    repo.activate_revision(wf.id, rev.id)

    with session_factory() as session:
        workflow = session.get(WorkflowModel, wf.id)
        revision = session.get(WorkflowRevision, rev.id)
    assert workflow.archived_at is None  # reactivation un-archives
    assert revision.status == "active"


def test_create_workflow_idempotency_key_replay_and_conflict(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, rev = _create_workflow(repo, idempotency_key="idem-1")

    wf_again, rev_again = _create_workflow(repo, idempotency_key="idem-1")
    assert wf_again.id == wf.id
    assert rev_again.id == rev.id
    assert wf_again.idempotency_key == "idem-1"

    with pytest.raises(IdempotencyConflict):
        _create_workflow(repo, idempotency_key="idem-1", name="different-name")

    # owner/name uniqueness still enforced when no idempotency key is given
    with pytest.raises(DomainConflict):
        _create_workflow(repo)


def test_list_active_subscriptions_exposes_owner_and_document(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    wf, rev = _create_workflow(repo)
    repo.activate_revision(wf.id, rev.id)

    active_id = _add_subscription(
        session_factory,
        rev.id,
        state="active",
        channels=("telegram_primary", "ntfy_secondary"),
    )
    _add_subscription(
        session_factory,
        rev.id,
        state="paused",
        alert_id="breakout",
        instrument_symbol="TCS",
        instrument_key="NSE:TCS",
    )

    subs = repo.list_active_subscriptions()
    assert [s.id for s in subs] == [active_id]
    row: ActiveSubscription = subs[0]
    assert row.owner_id == "owner-1"
    assert row.workflow_id == wf.id
    assert row.revision_id == rev.id
    assert row.alert_id == "breakout"
    assert row.stage_id == "px"
    assert row.instrument_symbol == "RELIANCE"
    assert row.instrument_exchange == "NSE"
    assert row.instrument_key == "NSE:RELIANCE"
    assert row.trigger == "once"
    assert row.state == "active"
    assert row.config["channels"] == ["telegram_primary", "ntfy_secondary"]
    assert row.document["name"] == "reliance-breakout"
    assert row.document["stages"][0]["id"] == "px"


def test_list_active_subscriptions_filters_superseded_and_archived(session_factory):
    """Superseded revisions (archived) and archived workflows must never stay
    eligible for evaluation, even when their subscription rows say 'active'."""
    repo = SqlAlchemyWorkflowRepository(session_factory)

    # workflow A: rev1 activated + subscription, then superseded by rev2
    wf_a, rev1_a = _create_workflow(repo, doc=_document(name="flow-a"))
    repo.activate_revision(wf_a.id, rev1_a.id)
    superseded_sub = _add_subscription(session_factory, rev1_a.id)
    doc_a2, hash_a2 = _compiled(_document(name="flow-a-v2", level=3100.0))
    rev2_a = repo.add_draft_revision(wf_a.id, doc_a2, hash_a2)
    repo.activate_revision(wf_a.id, rev2_a.id)
    active_sub = _add_subscription(session_factory, rev2_a.id)

    # workflow B: activated + subscription, then archived entirely
    wf_b, rev_b = _create_workflow(repo, doc=_document(name="flow-b"))
    repo.activate_revision(wf_b.id, rev_b.id)
    _add_subscription(session_factory, rev_b.id, alert_id="breakout",
                      instrument_key="NSE:TCS")
    with session_factory() as session:
        workflow_b = session.get(WorkflowModel, wf_b.id)
        workflow_b.archived_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        session.commit()

    subs = repo.list_active_subscriptions()
    assert [sub.id for sub in subs] == [active_sub], (
        "only subscriptions of the active revision of an un-archived workflow "
        "stay eligible; superseded/archived ones must be filtered out"
    )
    assert superseded_sub not in {sub.id for sub in subs}
    assert all(sub.workflow_id != wf_b.id for sub in subs)
    assert all(sub.revision_id == rev2_a.id for sub in subs)


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------


def test_checkpoint_compare_and_swap(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id)
    key = "NSE:RELIANCE"

    assert repo.load_checkpoint(sub_id, key, "epoch-1") is None

    cp = repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 99.0}, 0)
    assert cp.owner_epoch == 1
    assert repo.load_checkpoint(sub_id, key, "epoch-1") == ({"prev": 99.0}, 1)

    cp = repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 101.0}, 1)
    assert cp.owner_epoch == 2

    # stale expectation: stored owner_epoch moved to 2
    with pytest.raises(LeaseConflict):
        repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 999.0}, 1)
    assert repo.load_checkpoint(sub_id, key, "epoch-1") == ({"prev": 101.0}, 2)

    # other instruments/epochs are independent
    repo.save_checkpoint(sub_id, "NSE:TCS", "epoch-1", {}, 0)
    repo.save_checkpoint(sub_id, key, "epoch-2", {"fresh": True}, 0)
    assert repo.load_checkpoint(sub_id, "NSE:TCS", "epoch-1") == ({}, 1)
    assert repo.load_checkpoint(sub_id, key, "epoch-2") == ({"fresh": True}, 1)


def test_checkpoint_cas_update_is_atomic_compare_and_swap(session_factory):
    """The save must be a single guarded UPDATE: a concurrent owner_epoch bump
    between load and save makes the CAS lose (LeaseConflict) and the stored
    state stays exactly as the winner wrote it."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id)
    key = "NSE:RELIANCE"

    repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 99.0}, 0)

    # simulate a racing writer moving owner_epoch behind our back
    with session_factory() as session:
        session.execute(
            update(EvaluationCheckpoint)
            .where(
                EvaluationCheckpoint.subscription_id == sub_id,
                EvaluationCheckpoint.instrument_key == key,
                EvaluationCheckpoint.epoch_id == "epoch-1",
            )
            .values(state={"prev": 100.0}, owner_epoch=99)
        )
        session.commit()

    with pytest.raises(LeaseConflict):
        repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 101.0}, 1)
    assert repo.load_checkpoint(sub_id, key, "epoch-1") == ({"prev": 100.0}, 99)


def test_checkpoint_insert_race_loses_to_existing_newer_epoch(session_factory):
    """rowcount==0 on the CAS UPDATE means missing row OR moved epoch; the
    guarded insert path must detect the existing row and end in LeaseConflict
    instead of clobbering it."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id)
    key = "NSE:RELIANCE"

    # row exists with owner_epoch=5; caller still believes the row is absent
    with session_factory() as session:
        session.add(
            EvaluationCheckpoint(
                subscription_id=sub_id,
                instrument_key=key,
                epoch_id="epoch-1",
                state={"prev": 42.0},
                owner_epoch=5,
                updated_at=datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

    with pytest.raises(LeaseConflict):
        repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 43.0}, 0)
    assert repo.load_checkpoint(sub_id, key, "epoch-1") == ({"prev": 42.0}, 5)

    # matching the real stored epoch succeeds and increments by exactly one
    row = repo.save_checkpoint(sub_id, key, "epoch-1", {"prev": 44.0}, 5)
    assert row.owner_epoch == 6
    assert repo.load_checkpoint(sub_id, key, "epoch-1") == ({"prev": 44.0}, 6)


# ---------------------------------------------------------------------------
# signals + events
# ---------------------------------------------------------------------------


def test_record_signal_idempotent_second_call_writes_nothing(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id, channels=("c1", "c2"))
    fired_at = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
    occurrence = "NSE:RELIANCE|breakout|2026-09-08T10:00:00+00:00"

    event = repo.record_signal(sub_id, occurrence, fired_at, {"ltp": 3002.5, "level": 3000.0}, ["c1", "c2"], now=fired_at)
    assert event is not None
    assert event.subscription_id == sub_id
    assert event.occurrence_key == occurrence
    assert _utc(event.fired_at) == fired_at
    assert event.evidence == {"ltp": 3002.5, "level": 3000.0}

    duplicate = repo.record_signal(sub_id, occurrence, fired_at + timedelta(seconds=1), {"ltp": 3003.0}, ["c1", "c2"])
    assert duplicate is None

    with session_factory() as session:
        events = session.execute(select(SignalEvent)).scalars().all()
        deliveries = session.execute(select(Delivery)).scalars().all()
    assert len(events) == 1
    assert len(deliveries) == 2  # untouched by the duplicate call


def test_record_signal_fanout_two_channels_unique_constraint(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id, channels=("c1", "c2"))
    fired_at = datetime(2026, 9, 8, 10, 30, tzinfo=timezone.utc)

    event = repo.record_signal(sub_id, "occ-fanout", fired_at, {"ltp": 1.0}, ["c1", "c2"], now=fired_at)
    assert event is not None

    with session_factory() as session:
        deliveries = session.execute(select(Delivery).order_by(Delivery.channel_id)).scalars().all()
    assert sorted(d.channel_id for d in deliveries) == ["c1", "c2"]
    for delivery in deliveries:
        assert delivery.event_id == event.id
        assert delivery.status == "pending"
        assert delivery.attempts == 0
        assert delivery.lease_until is None
        assert _utc(delivery.next_attempt_at) == fired_at

    # unique(event_id, channel_id) enforced by the database
    with session_factory() as session:
        session.add(
            Delivery(id=str(uuid.uuid4()), event_id=event.id, channel_id="c1")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_list_events_newest_first_pagination(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _wf, rev = _create_workflow(repo)
    sub_id = _add_subscription(session_factory, rev.id)
    base = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    for index in range(3):
        result = repo.record_signal(sub_id, f"occ-{index}", base + timedelta(minutes=index), {"i": index}, [])
        assert result is not None

    page1 = repo.list_events([sub_id], limit=2, offset=0)
    assert [e.evidence["i"] for e in page1] == [2, 1]
    page2 = repo.list_events([sub_id], limit=2, offset=2)
    assert [e.evidence["i"] for e in page2] == [0]
    assert repo.list_events([], limit=5) == []
    with pytest.raises(ValueError):
        repo.list_events([sub_id], limit=0)
