"""C6 acceptance: provenance describes the binding actually used.

- new subscriptions persist catalog binding provenance at creation;
- EXISTING subscriptions created before catalog data existed receive a
  controlled binding backfill on the next materialization pass;
- every signal event carries the binding snapshot in force at evaluation
  time, and rebinding later does not rewrite old events' meaning;
- stale/partial bindings are refreshed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.repository import Delivery  # noqa: F401  (registers tables)
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
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.service import EvaluationService

T0 = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

BINDING_V1 = {
    "instrument_id": "instrument-1",
    "public_key": "NSE:RELIANCE",
    "broker": "kite",
    "broker_token": 111,
    "catalog_generation": "generation-1",
    "lifecycle_status": "active",
}
BINDING_V2 = {
    "instrument_id": "instrument-1",
    "public_key": "NSE:RELIANCE",
    "broker": "kite",
    "broker_token": 222,
    "catalog_generation": "generation-2",
    "lifecycle_status": "active",
}


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


@pytest.fixture()
def repo(session_factory):
    return SqlAlchemyWorkflowRepository(session_factory)


def _candle_doc(level: float = 100.0) -> WorkflowDocument:
    return WorkflowDocument(
        version=1,
        name="candle-cross",
        instruments=(InstrumentRef(symbol="RELIANCE", exchange="NSE"),),
        stages=(
            Stage(
                id="bar",
                type="signal",
                clock="candle_close",
                timeframe="minute",
                conditions=(
                    Condition(
                        Operand(kind="field", name="close"),
                        "crosses_above",
                        Operand(kind="value", value=level),
                    ),
                ),
            ),
        ),
        alerts=(AlertSpec(id="cross", source="bar", trigger="on_transition", channels=("c1",)),),
    )


def _activate(repo, doc):
    compiled = compile_document(doc)
    workflow, revision = repo.create_workflow(
        "owner-1", doc.name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    return repo.get_active_revision(workflow.id)


def _bar(minutes: float, close: float) -> Observation:
    ts = T0 + timedelta(minutes=minutes)
    return Observation(
        ts=ts, epoch_id="candle", ltp=close, open=close, high=close,
        low=close, close=close, volume=1000.0, final=True,
    )


def _sub_config(session_factory, sub_id):
    with session_factory() as session:
        row = session.get(AlertSubscription, sub_id)
        return dict(row.config or {})


def _set_sub_config(session_factory, sub_id, config):
    with session_factory() as session:
        row = session.get(AlertSubscription, sub_id)
        row.config = dict(config)
        session.commit()


def test_new_subscription_persists_binding_at_creation(repo, session_factory, monkeypatch):
    monkeypatch.setattr(EvaluationService, "_resolve_catalog_binding", staticmethod(lambda key, session: dict(BINDING_V1)))
    service = EvaluationService(repo, session_factory)
    revision = _activate(repo, _candle_doc())
    created = service.ensure_subscriptions(revision)
    assert created == 1

    sub = repo.list_active_subscriptions()[0]
    config = _sub_config(session_factory, sub.id)
    assert config["instrument_binding"] == BINDING_V1


def test_existing_subscription_receives_binding_when_catalog_becomes_available(
    repo, session_factory, monkeypatch
):
    # materialized BEFORE catalog data existed: no binding at all
    service = EvaluationService(repo, session_factory)
    revision = _activate(repo, _candle_doc())
    service.ensure_subscriptions(revision)
    sub = repo.list_active_subscriptions()[0]
    assert "instrument_binding" not in _sub_config(session_factory, sub.id)

    # catalog becomes available; the next materialization pass backfills
    monkeypatch.setattr(
        EvaluationService, "_resolve_catalog_binding",
        staticmethod(lambda key, session: dict(BINDING_V1)),
    )
    service.ensure_subscriptions(revision)
    assert _sub_config(session_factory, sub.id)["instrument_binding"] == BINDING_V1

    # a stale binding (partial metadata) is refreshed too
    stale = {"instrument_id": "", "broker_token": 111}
    _set_sub_config(session_factory, sub.id, {"instrument_binding": stale, "message": "keep"})
    service.ensure_subscriptions(revision)
    refreshed = _sub_config(session_factory, sub.id)
    assert refreshed["instrument_binding"] == BINDING_V1
    assert refreshed["message"] == "keep"  # unrelated config keys are preserved


def test_event_evidence_keeps_binding_snapshot_across_rebinding(
    repo, session_factory, monkeypatch
):
    monkeypatch.setattr(
        EvaluationService, "_resolve_catalog_binding",
        staticmethod(lambda key, session: dict(BINDING_V1)),
    )
    service = EvaluationService(repo, session_factory)
    revision = _activate(repo, _candle_doc())
    service.ensure_subscriptions(revision)
    sub = repo.list_active_subscriptions()[0]

    # fire on a real crossing: 99 -> 101 crosses the 100 level
    service.handle_observation(sub, _bar(0, 99.0))
    fired = service.handle_observation(sub, _bar(1, 101.0))
    assert fired.emitted is True

    events = list(session_factory().execute(select(SignalEvent)).scalars())
    assert len(events) == 1
    evidence = events[0].evidence
    assert evidence["instrument_binding"] == BINDING_V1

    # the broker rotates the token and the worker rebinds (generation-2)
    monkeypatch.setattr(
        EvaluationService, "_resolve_catalog_binding",
        staticmethod(lambda key, session: dict(BINDING_V2)),
    )
    service.ensure_subscriptions(revision)
    assert _sub_config(session_factory, sub.id)["instrument_binding"] == BINDING_V2

    # the OLD event still references the OLD binding — rebinding must not
    # overwrite the meaning of historical events
    events = list(session_factory().execute(select(SignalEvent)).scalars())
    assert events[0].evidence["instrument_binding"] == BINDING_V1
