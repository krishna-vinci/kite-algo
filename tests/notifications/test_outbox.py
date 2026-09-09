"""Outbox claim/attempt contract tests for the notifications repository (Task 5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import (
    Delivery,
    DeliveryAttempt,
    SqlAlchemyNotificationRepository,
)
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
    AlertSubscription,
    Base,
    SqlAlchemyWorkflowRepository,
)


def _utc(value):
    """SQLite round-trips datetimes without tzinfo; treat naive as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _document(name: str = "reliance-breakout") -> WorkflowDocument:
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
                        Operand(kind="value", value=3000.0),
                    ),
                ),
            ),
        ),
        alerts=(
            AlertSpec(id="breakout", source="px", trigger="once", channels=("telegram_primary",)),
        ),
    )


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


def _subscription_id(session_factory, *, instrument_key: str = "NSE:RELIANCE") -> str:
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(_document())
    _wf, rev = workflow_repo.create_workflow(
        owner_id="owner-1",
        name=compiled.document.name,
        document_dict=compiled.document.to_document_dict(),
        canonical_hash=compiled.canonical_hash,
    )
    with session_factory() as session:
        sub = AlertSubscription(
            id=str(uuid.uuid4()),
            revision_id=rev.id,
            alert_id="breakout",
            stage_id="px",
            instrument_symbol="RELIANCE",
            instrument_exchange="NSE",
            instrument_key=instrument_key,
            trigger="once",
            config={"channels": ["c1", "c2"]},
        )
        session.add(sub)
        session.commit()
        return sub.id


def _seed_event(workflow_repo, subscription_id: str, occurrence: str, channel_ids, fired_at):
    event = workflow_repo.record_signal(
        subscription_id, occurrence, fired_at, {"ltp": 1.0}, channel_ids, now=fired_at
    )
    assert event is not None
    return event


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def test_upsert_channel_create_then_update(session_factory):
    repo = SqlAlchemyNotificationRepository(session_factory)
    now = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)

    channel = repo.upsert_channel(
        owner_id="owner-1",
        name="telegram_primary",
        provider="telegram",
        destination={"chat_id": "111"},
        secret_env="TELEGRAM_BOT_TOKEN",
        enabled=True,
        now=now,
    )
    assert channel.provider == "telegram"
    assert channel.destination == {"chat_id": "111"}
    assert channel.secret_env == "TELEGRAM_BOT_TOKEN"
    assert channel.enabled is True

    updated = repo.upsert_channel(
        owner_id="owner-1",
        name="telegram_primary",
        provider="telegram",
        destination={"chat_id": "222"},
        secret_env="TELEGRAM_BOT_TOKEN",
        enabled=False,
        now=now,
    )
    assert updated.id == channel.id
    assert updated.destination == {"chat_id": "222"}
    assert updated.enabled is False

    assert [c.id for c in repo.list_channels("owner-1")] == [channel.id]
    assert repo.list_channels("nobody") == []
    fetched = repo.get_channel(channel.id)
    assert fetched is not None and fetched.name == "telegram_primary"
    assert repo.get_channel("missing") is None


# ---------------------------------------------------------------------------
# claim machinery
# ---------------------------------------------------------------------------


def test_claim_deliveries_are_disjoint(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-a", ["c1", "c2"], now)

    first = notification_repo.claim_deliveries(now, limit=1)
    assert len(first) == 1
    claimed = first[0]
    assert claimed.status == "delivering"
    assert claimed.attempts == 0  # claiming must not increment attempts
    assert _utc(claimed.lease_until) > now

    second = notification_repo.claim_deliveries(now, limit=5)
    assert len(second) == 1
    assert second[0].id != claimed.id

    assert notification_repo.claim_deliveries(now, limit=5) == []


def test_claim_skips_delivery_with_unexpired_lease(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-lease", ["c1"], now)
    claimed = notification_repo.claim_deliveries(now, limit=5)
    assert len(claimed) == 1

    assert notification_repo.claim_deliveries(now, limit=5) == []


def test_claim_reclaims_stale_lease(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-crash", ["c1"], now)
    claimed = notification_repo.claim_deliveries(now, limit=1)[0]

    # simulate a crashed worker: the lease expires without an attempt recorded
    with session_factory() as session:
        row = session.get(Delivery, claimed.id)
        row.lease_until = now - timedelta(seconds=1)
        session.commit()

    reclaimed = notification_repo.claim_deliveries(now, limit=5)
    assert [d.id for d in reclaimed] == [claimed.id]
    assert _utc(reclaimed[0].lease_until) > now


def test_claim_respects_next_attempt_at(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-backoff", ["c1"], now)
    claimed = notification_repo.claim_deliveries(now, limit=1)[0]
    notification_repo.record_attempt(
        claimed.id,
        1,
        "retryable",
        "provider 500",
        "retrying",
        next_attempt_at=now + timedelta(seconds=60),
        last_error="provider 500",
        now=now,
    )

    assert notification_repo.claim_deliveries(now, limit=5) == []
    due_later = notification_repo.claim_deliveries(now + timedelta(seconds=61), limit=5)
    assert [d.id for d in due_later] == [claimed.id]


def test_record_attempt_updates_delivery_and_logs_row(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-attempt", ["c1"], now)
    claimed = notification_repo.claim_deliveries(now, limit=1)[0]

    delivered = notification_repo.record_attempt(
        claimed.id,
        1,
        "accepted",
        "telegram sendMessage 200",
        "delivered",
        delivered_at=now,
        now=now,
    )
    assert delivered.status == "delivered"
    assert delivered.attempts == 1
    assert _utc(delivered.delivered_at) == now
    assert delivered.lease_until is None
    assert delivered.last_error is None

    retried = notification_repo.record_attempt(
        claimed.id,
        2,
        "retryable",
        "timeout",
        "retrying",
        next_attempt_at=now + timedelta(seconds=30),
        last_error="timeout",
        now=now,
    )
    assert retried.status == "retrying"
    assert retried.attempts == 2
    assert retried.last_error == "timeout"
    assert _utc(retried.next_attempt_at) == now + timedelta(seconds=30)

    with session_factory() as session:
        attempts = session.execute(select(DeliveryAttempt).order_by(DeliveryAttempt.attempt_no)).scalars().all()
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "accepted"), (2, "retryable")]
    assert attempts[0].delivery_id == claimed.id

    with pytest.raises(KeyError):
        notification_repo.record_attempt("missing-delivery", 1, "accepted", "", "delivered")


def test_get_due_and_stale_partition(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    sub_id = _subscription_id(session_factory)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    _seed_event(workflow_repo, sub_id, "occ-partition", ["c1", "c2"], now)
    claimed = notification_repo.claim_deliveries(now, limit=1)[0]

    with session_factory() as session:
        rows = session.execute(select(Delivery)).scalars().all()
        pending = next(row for row in rows if row.status == "pending")

    due, stale = notification_repo.get_due_and_stale(now)
    assert [d.id for d in due] == [pending.id]
    assert stale == []

    with session_factory() as session:
        row = session.get(Delivery, claimed.id)
        row.lease_until = now - timedelta(seconds=1)
        session.commit()

    due, stale = notification_repo.get_due_and_stale(now)
    assert [d.id for d in due] == [pending.id]
    assert [d.id for d in stale] == [claimed.id]
