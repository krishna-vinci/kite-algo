"""Task 6 tests: DeliveryWorker — outbox claim -> send -> record -> backoff.

Every test gets a fresh in-memory SQLite engine (pattern from
tests/notifications/test_outbox.py) and a FakeAdapter registered through
``register_adapter`` (the real registry entries are restored afterwards).
Async worker calls run through ``asyncio.run`` inside sync tests, matching
tests/notifications/test_adapters.py.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.adapters import (
    DeliveryOutcome,
    register_adapter,
)
from backend.notifications.repository import (
    Delivery,
    DeliveryAttempt,
    SqlAlchemyNotificationRepository,
)
from backend.notifications.worker import DeliveryWorker
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

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
# Safely in the past for the run_forever test, which uses wall-clock time.
LONG_AGO = datetime(2020, 1, 1, 9, 0, tzinfo=timezone.utc)

SUMMARY_KEYS = {"claimed", "delivered", "retrying", "failed", "expired"}


def _utc(value):
    """SQLite round-trips datetimes without tzinfo; treat naive as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fake adapters + registry hygiene
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Records send() calls and replays a scripted result.

    ``outcomes`` may be a DeliveryOutcome, a callable(destination, subject, body)
    -> DeliveryOutcome, or a list consumed in order (the last entry repeats).
    """

    provider = "fake"

    def __init__(self, outcomes=None, exc: Exception | None = None):
        self._script = outcomes
        self.exc = exc
        self.calls: list[dict] = []

    async def send(self, destination: dict, subject: str, body: str) -> DeliveryOutcome:
        self.calls.append(
            {"destination": dict(destination or {}), "subject": subject, "body": body}
        )
        if self.exc is not None:
            raise self.exc
        if isinstance(self._script, list):
            outcome = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        else:
            outcome = self._script
        if callable(outcome):
            return outcome(destination, subject, body)
        if outcome is None:
            return DeliveryOutcome(status="accepted", detail="ok")
        return outcome


_REGISTERED: dict[str, object] = {}


def _install_fake(provider: str, adapter: FakeAdapter) -> FakeAdapter:
    """Register ``adapter`` under ``provider`` (restored by the autouse fixture)."""
    _REGISTERED[provider] = register_adapter(provider, lambda a=adapter: a)
    return adapter


@pytest.fixture(autouse=True)
def _restore_adapter_registry():
    yield
    for provider, previous in _REGISTERED.items():
        register_adapter(provider, previous)
    _REGISTERED.clear()


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


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
            AlertSpec(id="breakout", source="px", trigger="once", channels=("primary",)),
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


def _seed_event(
    session_factory,
    notification_repo,
    *,
    occurrence: str,
    channel_specs,
    fired_at=NOW,
    expires_at=None,
):
    """Create workflow + subscription + channels, then one signal event.

    ``channel_specs`` is a sequence of ``(provider, name)`` tuples. Returns
    ``(event, {channel_name: channel})``.
    """
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
            instrument_key="NSE:RELIANCE",
            trigger="once",
            config={
                "expires_at": expires_at,
                "channels": [name for _provider, name in channel_specs],
            },
        )
        session.add(sub)
        session.commit()
        sub_id = sub.id

    channels = {}
    channel_ids = []
    for provider, name in channel_specs:
        channel = notification_repo.upsert_channel(
            owner_id="owner-1",
            name=name,
            provider=provider,
            destination={"chat_id": "4242"},
        )
        channels[name] = channel
        channel_ids.append(channel.id)

    event = workflow_repo.record_signal(
        sub_id,
        occurrence,
        fired_at,
        {"ltp": 3002.5, "level": 3000.0},
        channel_ids,
        now=fired_at,
    )
    assert event is not None
    return event, channels


def _deliveries_by_channel(session_factory, channels):
    with session_factory() as session:
        rows = list(session.execute(select(Delivery)).scalars().all())
    by_channel = {row.channel_id: row for row in rows}
    return {name: by_channel[channel.id] for name, channel in channels.items()}


def _delivery(session_factory, delivery_id):
    with session_factory() as session:
        return session.get(Delivery, delivery_id)


def _attempts(session_factory, delivery_id):
    with session_factory() as session:
        return list(
            session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_no.asc())
            ).scalars().all()
        )


def _context(**overrides):
    context = {
        "rule_name": "breakout",
        "instrument_key": "NSE:RELIANCE",
        "template": None,
        "expires_at": None,
        "evidence": {"ltp": 3002.5, "level": 3000.0},
        "fired_at": NOW,
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# delivered
# ---------------------------------------------------------------------------


def test_pending_claimed_sent_delivered_with_attempt_row(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    event, channels = _seed_event(
        session_factory,
        notification_repo,
        occurrence="occ-delivered",
        channel_specs=[("fake-ok", "primary")],
    )
    rows = _deliveries_by_channel(session_factory, channels)
    # future expiry as a datetime proves non-string expiry parsing keeps the event alive
    contexts = {rows["primary"].id: _context(expires_at=NOW + timedelta(hours=1))}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert set(summary) == SUMMARY_KEYS
    assert summary == {"claimed": 1, "delivered": 1, "retrying": 0, "failed": 0, "expired": 0}

    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "delivered"
    assert delivery.attempts == 1
    assert _utc(delivery.delivered_at) == NOW
    assert delivery.lease_until is None
    assert _utc(delivery.next_attempt_at) is None

    attempts = _attempts(session_factory, delivery.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "accepted")]

    (call,) = adapter.calls
    assert call["destination"] == {"chat_id": "4242"}
    # the worker rendered the message from the event context
    assert call["subject"] == "[Alert] breakout: NSE:RELIANCE"
    assert "ltp=3002.5" in call["body"]
    assert event.id in call["body"]

    # terminal status: a second pass claims nothing and sends nothing
    second = asyncio.run(worker.run_once(now=NOW + timedelta(seconds=5)))
    assert second == {"claimed": 0, "delivered": 0, "retrying": 0, "failed": 0, "expired": 0}
    assert len(adapter.calls) == 1


def test_precomputed_subject_body_passthrough(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-precomputed", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context(subject="pre subject", body="pre body")}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    asyncio.run(worker.run_once(now=NOW))

    (call,) = adapter.calls
    assert call["subject"] == "pre subject"
    assert call["body"] == "pre body"


def test_template_override_rendered_by_worker(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-template", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context(template="${symbol} crossed ${ltp}")}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    asyncio.run(worker.run_once(now=NOW))

    (call,) = adapter.calls
    assert call["subject"] == "[Alert] breakout: NSE:RELIANCE"  # default subject kept
    assert call["body"] == "NSE:RELIANCE crossed 3002.5"


def test_default_resolver_still_delivers(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-no-resolver", channel_specs=[("fake-ok", "primary")]
    )
    worker = DeliveryWorker(notification_repo)  # no resolver at all

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary["delivered"] == 1
    (call,) = adapter.calls
    assert call["subject"] == "[Alert] alert: -"  # defaults, never crashes


# ---------------------------------------------------------------------------
# retrying / failed
# ---------------------------------------------------------------------------


def test_retryable_outcome_backs_off_with_jitter(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake(
        "fake-ok",
        FakeAdapter(outcomes=DeliveryOutcome(status="retryable", retry_after_s=30, detail="provider 500")),
    )
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-retry", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context()}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary == {"claimed": 1, "delivered": 0, "retrying": 1, "failed": 0, "expired": 0}
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "retrying"
    assert delivery.attempts == 1
    assert delivery.lease_until is None
    assert delivery.last_error == "provider 500"

    delay = _utc(delivery.next_attempt_at) - NOW
    # 30s hint + jitter in [0, 0.2 * 30]
    assert timedelta(seconds=30) <= delay <= timedelta(seconds=36)

    attempts = _attempts(session_factory, delivery.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "retryable")]

    # backoff suppresses a second claim until it elapses
    early = asyncio.run(worker.run_once(now=NOW + timedelta(seconds=10)))
    assert early["claimed"] == 0
    due = asyncio.run(worker.run_once(now=NOW + timedelta(seconds=37)))
    assert due["claimed"] == 1
    assert len(adapter.calls) == 2


def test_unknown_outcome_retries_with_default_backoff(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake(
        "fake-ok",
        FakeAdapter(outcomes=DeliveryOutcome(status="unknown", detail="timeout")),
    )
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-unknown", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context()}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary["retrying"] == 1
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "retrying"
    assert delivery.attempts == 1
    assert delivery.last_error == "timeout"

    delay = _utc(delivery.next_attempt_at) - NOW
    # default 60s backoff + jitter in [0, 0.2 * 60]
    assert timedelta(seconds=60) <= delay <= timedelta(seconds=72)

    attempts = _attempts(session_factory, delivery.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "unknown")]


def test_permanent_outcome_fails_terminal(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake(
        "fake-ok",
        FakeAdapter(outcomes=DeliveryOutcome(status="permanent", detail="chat not found")),
    )
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-permanent", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context()}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary == {"claimed": 1, "delivered": 0, "retrying": 0, "failed": 1, "expired": 0}
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "failed"
    assert delivery.attempts == 1
    assert delivery.lease_until is None
    assert delivery.last_error == "chat not found"

    # terminal: never re-claimed, adapter never hit again
    second = asyncio.run(worker.run_once(now=NOW + timedelta(minutes=10)))
    assert second["claimed"] == 0
    assert len(adapter.calls) == 1


def test_max_attempts_exhaustion_marks_failed(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake(
        "fake-ok",
        FakeAdapter(outcomes=DeliveryOutcome(status="retryable", retry_after_s=5, detail="still down")),
    )
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-exhaust", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context()}
    worker = DeliveryWorker(
        notification_repo,
        resolver=lambda delivery_id: contexts.get(delivery_id),
        max_attempts=2,
    )

    first = asyncio.run(worker.run_once(now=NOW))
    assert first["retrying"] == 1
    assert _delivery(session_factory, rows["primary"].id).attempts == 1

    second = asyncio.run(worker.run_once(now=NOW + timedelta(seconds=10)))
    assert second == {"claimed": 1, "delivered": 0, "retrying": 0, "failed": 1, "expired": 0}
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "failed"
    assert delivery.attempts == 2
    assert delivery.last_error == "max attempts exceeded"
    assert delivery.next_attempt_at is None

    attempts = _attempts(session_factory, delivery.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "retryable"), (2, "retryable")]

    third = asyncio.run(worker.run_once(now=NOW + timedelta(hours=1)))
    assert third["claimed"] == 0
    assert len(adapter.calls) == 2


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------


def test_expired_event_expires_without_calling_adapter(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    expired_at = NOW - timedelta(seconds=1)
    _event, channels = _seed_event(
        session_factory,
        notification_repo,
        occurrence="occ-expired",
        channel_specs=[("fake-ok", "primary")],
        expires_at=expired_at.isoformat(),
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context(expires_at=expired_at.isoformat())}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary == {"claimed": 1, "delivered": 0, "retrying": 0, "failed": 0, "expired": 1}
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "expired"
    assert delivery.attempts == 1
    assert delivery.lease_until is None
    assert adapter.calls == []  # never contacted the provider

    attempts = _attempts(session_factory, delivery.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "expired")]

    # terminal: no further claims
    second = asyncio.run(worker.run_once(now=NOW + timedelta(minutes=5)))
    assert second["claimed"] == 0


# ---------------------------------------------------------------------------
# sibling independence
# ---------------------------------------------------------------------------


def test_failing_adapter_does_not_block_sibling_delivery(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    bad = _install_fake("fake-bad", FakeAdapter(exc=RuntimeError("boom")))
    ok = _install_fake("fake-good", FakeAdapter())
    _event, channels = _seed_event(
        session_factory,
        notification_repo,
        occurrence="occ-siblings",
        channel_specs=[("fake-bad", "bad"), ("fake-good", "good")],
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {row.id: _context() for row in rows.values()}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary["claimed"] == 2
    assert summary["delivered"] == 1
    assert summary["retrying"] == 1

    good = _delivery(session_factory, rows["good"].id)
    assert good.status == "delivered"
    assert good.attempts == 1
    assert len(ok.calls) == 1

    bad_row = _delivery(session_factory, rows["bad"].id)
    assert bad_row.status == "retrying"
    assert bad_row.attempts == 1
    attempts = _attempts(session_factory, bad_row.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "unknown")]
    assert "boom" in attempts[0].detail
    assert len(bad.calls) == 1  # attempted exactly once, exception contained


def test_resolver_failure_does_not_block_sibling_delivery(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    ok = _install_fake("fake-good", FakeAdapter())
    _event, channels = _seed_event(
        session_factory,
        notification_repo,
        occurrence="occ-resolver",
        channel_specs=[("fake-good", "good"), ("fake-good", "good2")],
    )
    rows = _deliveries_by_channel(session_factory, channels)
    broken_id = rows["good"].id
    contexts = {rows["good2"].id: _context()}

    def flaky_resolver(delivery_id):
        if delivery_id == broken_id:
            raise KeyError("context unavailable")
        return contexts.get(delivery_id)

    worker = DeliveryWorker(notification_repo, resolver=flaky_resolver)

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary["claimed"] == 2
    assert summary["delivered"] == 1
    assert summary["retrying"] == 1

    good2 = _delivery(session_factory, rows["good2"].id)
    assert good2.status == "delivered"
    assert len(ok.calls) == 1

    broken = _delivery(session_factory, broken_id)
    assert broken.status == "retrying"
    attempts = _attempts(session_factory, broken.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "unknown")]
    assert "context unavailable" in attempts[0].detail


# ---------------------------------------------------------------------------
# lease / crash recovery
# ---------------------------------------------------------------------------


def test_crashed_worker_stale_lease_is_reclaimed_and_completed(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-crash", channel_specs=[("fake-ok", "primary")]
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context()}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    # simulate a worker that claimed, then crashed before sending
    claimed = notification_repo.claim_deliveries(NOW, limit=1)
    assert [row.id for row in claimed] == [rows["primary"].id]
    with session_factory() as session:
        row = session.get(Delivery, rows["primary"].id)
        row.lease_until = NOW - timedelta(seconds=1)
        session.commit()

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary == {"claimed": 1, "delivered": 1, "retrying": 0, "failed": 0, "expired": 0}
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "delivered"
    assert delivery.attempts == 1  # reclaim does not double-count attempts
    assert len(adapter.calls) == 1


def test_unexpired_lease_is_not_claimed(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory, notification_repo, occurrence="occ-lease", channel_specs=[("fake-ok", "primary")]
    )
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: None)

    claimed = notification_repo.claim_deliveries(NOW, limit=1)
    assert len(claimed) == 1  # another (live) worker holds the lease

    summary = asyncio.run(worker.run_once(now=NOW))

    assert summary["claimed"] == 0
    assert adapter.calls == []


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------


def test_run_forever_processes_and_stops_on_cancel(session_factory):
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    adapter = _install_fake("fake-ok", FakeAdapter())
    _event, channels = _seed_event(
        session_factory,
        notification_repo,
        occurrence="occ-forever",
        channel_specs=[("fake-ok", "primary")],
        fired_at=LONG_AGO,
    )
    rows = _deliveries_by_channel(session_factory, channels)
    contexts = {rows["primary"].id: _context(fired_at=LONG_AGO)}
    worker = DeliveryWorker(notification_repo, resolver=lambda delivery_id: contexts.get(delivery_id))

    async def scenario():
        task = asyncio.create_task(worker.run_forever(poll_interval_s=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        return await asyncio.gather(task)

    results = asyncio.run(scenario())

    assert results == [None]  # loop returned cleanly on cancellation
    delivery = _delivery(session_factory, rows["primary"].id)
    assert delivery.status == "delivered"
    assert len(adapter.calls) >= 1
