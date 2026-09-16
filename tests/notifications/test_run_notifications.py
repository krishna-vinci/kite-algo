"""Run-scoped notification tests (SQLite): atomic outbox, idempotency, ownership.

No real providers, no redis. Uses the real repository and the delivery resolver
so the loader/rendering adapter for ``strategy_run`` events is exercised.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.message import build_run_message
from backend.notifications.repository import (
    Delivery,
    RunNotificationError,
    SqlAlchemyNotificationRepository,
)
from backend.notifications.worker import make_resolver
from backend.workflows.repository import Base, SignalEvent

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
OWNER = "app:admin"


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def repo(factory):
    return SqlAlchemyNotificationRepository(factory)


def _channel(repo, *, owner=OWNER, name="ops", provider="ntfy", enabled=True):
    return repo.upsert_channel(owner, name, provider, {"url": "https://example.invalid"}, None, enabled)


def _events(factory):
    with factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def test_enqueue_is_atomic_and_owner_bound(repo, factory):
    _channel(repo, name="ops")
    _channel(repo, name="ops2")
    result = repo.enqueue_run_notification(
        owner_id=OWNER,
        run_id="run_1",
        channel_names=["ops", "ops2"],
        text="hello",
        idempotency_key="k1",
        occurred_at=NOW,
    )
    assert result["status"] == "accepted" and result["delivery_count"] == 2

    events = _events(factory)
    assert len(events) == 1
    event = events[0]
    assert event.source_kind == "strategy_run"
    assert event.owner_id == OWNER
    assert event.run_id == "run_1"
    assert event.subscription_id is None
    with factory() as session:
        deliveries = list(session.execute(select(Delivery).where(Delivery.event_id == event.id)).scalars())
    assert len(deliveries) == 2 and {d.status for d in deliveries} == {"pending"}


def test_same_key_same_content_deduplicates(repo, factory):
    _channel(repo)
    first = repo.enqueue_run_notification(
        owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hello", idempotency_key="k1", occurred_at=NOW
    )
    second = repo.enqueue_run_notification(
        owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hello", idempotency_key="k1", occurred_at=NOW
    )
    assert first["status"] == "accepted"
    assert second["status"] == "deduped"
    assert second["event_id"] == first["event_id"]
    assert len(_events(factory)) == 1
    with factory() as session:
        count = len(list(session.execute(select(Delivery)).scalars()))
    assert count == 1


def test_same_key_different_content_conflicts(repo, factory):
    _channel(repo)
    repo.enqueue_run_notification(
        owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hello", idempotency_key="k1", occurred_at=NOW
    )
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="different", idempotency_key="k1", occurred_at=NOW
        )
    assert exc.value.status_code == 409 and exc.value.code == "idempotency_conflict"
    assert len(_events(factory)) == 1


def test_unknown_channel_is_explicit_and_writes_nothing(repo, factory):
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_1", channel_names=["nope"], text="hi", idempotency_key="k1", occurred_at=NOW
        )
    assert exc.value.status_code == 422 and exc.value.code == "unknown_channel"
    assert _events(factory) == []


def test_unauthorized_channel_is_refused_without_partial_write(repo, factory):
    # A channel with the requested name exists, but for a different owner.
    _channel(repo, owner="app:other", name="ops")
    _channel(repo, owner=OWNER, name="mine")
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_1", channel_names=["mine", "ops"], text="hi", idempotency_key="k1", occurred_at=NOW
        )
    assert exc.value.status_code == 403 and exc.value.code == "channel_not_authorized"
    assert _events(factory) == []
    with factory() as session:
        assert list(session.execute(select(Delivery)).scalars()) == []


def test_disabled_channel_is_refused(repo, factory):
    _channel(repo, name="ops", enabled=False)
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hi", idempotency_key="k1", occurred_at=NOW
        )
    assert exc.value.code == "channel_disabled"
    assert _events(factory) == []


def test_account_scope_is_not_notification_ownership(repo):
    # A channel owned by the *account scope* string must not resolve for the
    # hosted app owner.
    _channel(repo, owner="kite:paper", name="ops")
    with pytest.raises(RunNotificationError) as exc:
        repo.enqueue_run_notification(
            owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hi", idempotency_key="k1", occurred_at=NOW
        )
    assert exc.value.code == "channel_not_authorized"


def test_history_is_run_scoped(repo, factory):
    _channel(repo)
    repo.enqueue_run_notification(owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="a", idempotency_key="k1", occurred_at=NOW)
    repo.enqueue_run_notification(owner_id=OWNER, run_id="run_2", channel_names=["ops"], text="b", idempotency_key="k2", occurred_at=NOW)
    run1 = repo.list_run_notifications(OWNER, "run_1")
    assert [e.run_id for e in run1] == ["run_1"]
    assert repo.list_run_notifications("app:other", "run_1") == []


def test_resolver_renders_run_event(repo, factory):
    _channel(repo, provider="ntfy")
    result = repo.enqueue_run_notification(
        owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="target hit", idempotency_key="k1", occurred_at=NOW
    )
    with factory() as session:
        delivery = session.execute(
            select(Delivery).where(Delivery.event_id == result["event_id"])
        ).scalars().first()
    resolver = make_resolver(repo, lambda subscription_id: None)
    resolved = resolver(delivery.id)
    assert resolved is not None
    assert resolved["provider"] == "ntfy"
    assert "target hit" in resolved["body"]
    assert resolved["expires_at"] is None


def test_build_run_message_shape():
    subject, body = build_run_message(run_id="run_1", text="done", fired_at=NOW, event_id="evt-1")
    assert "run_1" in subject
    assert "done" in body and "evt-1" in body
