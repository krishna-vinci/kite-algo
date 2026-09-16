"""Deterministic alerts smoke: observation -> event -> outbox -> providers.

This test is deliberately isolated from Redis, market hours, Telegram, and
ntfy. The live shell smoke remains destination-explicit and bounded.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.adapters import DeliveryOutcome
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
from backend.workflows.repository import Base, SignalEvent, SqlAlchemyWorkflowRepository
from backend.workflows.service import EvaluationService


class _Provider:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.calls: list[tuple[str, str]] = []

    async def send(self, destination, subject, body):
        self.calls.append((subject, body))
        return DeliveryOutcome(
            status="accepted" if self.accepted else "permanent",
            detail="mock accepted" if self.accepted else "mock provider failure",
        )


def test_isolated_tick_to_delivery_fanout_with_one_provider_failure():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    workflow_repo = SqlAlchemyWorkflowRepository(sessions)
    notification_repo = SqlAlchemyNotificationRepository(sessions)
    good = notification_repo.upsert_channel("owner", "telegram", "telegram", {})
    bad = notification_repo.upsert_channel("owner", "ntfy", "ntfy", {})

    document = WorkflowDocument(
        version=1,
        name="isolated-smoke",
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
                        Operand(kind="value", value=100.0),
                    ),
                ),
            ),
        ),
        alerts=(
            AlertSpec(
                id="breakout",
                source="px",
                trigger="on_transition",
                channels=("telegram", "ntfy"),
            ),
        ),
    )
    compiled = compile_document(document)
    workflow, revision = workflow_repo.create_workflow(
        "owner",
        document.name,
        compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    workflow_repo.activate_revision(workflow.id, revision.id)

    resolver = lambda owner, names: {
        row.name: row.id
        for row in notification_repo.list_channels(owner)
        if row.enabled and row.name in names
    }
    service = EvaluationService(
        workflow_repo,
        sessions,
        channel_resolver=resolver,
        owner_id="isolated-smoke-worker",
    )
    service.ensure_subscriptions(revision)
    sub = workflow_repo.list_active_subscriptions()[0]
    ts = datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)
    service.handle_observation(sub, Observation(ts=ts, epoch_id="epoch", ltp=99.0))
    result = service.handle_observation(
        sub,
        Observation(ts=ts.replace(second=1), epoch_id="epoch", ltp=101.0),
    )
    assert result.emitted is True

    providers = {"telegram": _Provider(True), "ntfy": _Provider(False)}

    def resolve(delivery_id):
        with sessions() as session:
            delivery = session.get(Delivery, delivery_id)
            event = session.get(SignalEvent, delivery.event_id)
            channel = session.get(type(good), delivery.channel_id)
        return {
            "provider": channel.provider,
            "destination": {},
            "rule_name": "isolated-smoke:breakout",
            "instrument_key": "NSE:RELIANCE",
            "evidence": dict(event.evidence),
            "fired_at": event.fired_at,
        }

    worker = DeliveryWorker(
        notification_repo,
        adapter_factory=lambda provider: providers[provider],
        resolver=resolve,
        jitter_fraction=0,
    )
    summary = asyncio.run(worker.run_once(now=ts.replace(second=2)))
    assert summary["delivered"] == 1, summary
    assert summary["failed"] == 1
    with sessions() as session:
        deliveries = list(session.execute(select(Delivery)).scalars())
        attempts = list(session.execute(select(DeliveryAttempt)).scalars())
    assert len(deliveries) == 2
    assert len(attempts) == 2
    assert len(providers["telegram"].calls) == 1
    assert len(providers["ntfy"].calls) == 1
    engine.dispose()
