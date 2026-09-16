"""C6 acceptance: the binding an API-ACTIVATED workflow is evaluated under.

Regression this pins: ``activate_workflow`` carried its own copy of the
subscription-materialization loop, which drifted from
``EvaluationService.ensure_subscriptions`` and omitted the catalog binding.
Subscriptions created through the API therefore evaluated without catalog
provenance, and their events went out with no ``instrument_binding`` in
evidence — the C6 contract was silently weaker for exactly the rows a user
creates through the API, while rows materialized by the worker's refresh or by
universe membership (both of which DO resolve the binding) carried it.

The tests drive the real HTTP activation route and then the real worker
dispatch path, so they pin behavior end to end rather than the helper:

- activation persists the binding, identity and generation included;
- a catalog generation/token move is reflected on re-materialization;
- an event produced by an API-activated subscription carries the binding that
  was in force at evaluation time;
- an authoritative retirement (expired/retired/not-found) stops the instrument
  from evaluating at all.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers import worker_workflows as worker_workflows_router  # noqa: E402
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.notifications.repository import Delivery  # noqa: F401,E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows import advanced_repository  # noqa: F401,E402
from backend.workflows.repository import (  # noqa: E402
    AlertSubscription,
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    WorkflowRevision,
)
from backend.workflows.runtime import EvaluationWorker  # noqa: E402
from backend.workflows.service import EvaluationService  # noqa: E402

RAW_TOKEN = "binding-secret-token"
HEADERS = {"Authorization": f"Bearer {RAW_TOKEN}"}
WF = "/api/worker/workflows"
INSTRUMENT = "NSE:RELIANCE"
T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
FIVEMIN = timedelta(minutes=5)

BINDING_V1 = {
    "instrument_id": "instrument-1",
    "public_key": INSTRUMENT,
    "broker": "kite",
    "broker_token": 111,
    "catalog_generation": "generation-1",
    "lifecycle_status": "active",
}
BINDING_V2 = {
    "instrument_id": "instrument-1",
    "public_key": INSTRUMENT,
    "broker": "kite",
    "broker_token": 222,
    "catalog_generation": "generation-2",
    "lifecycle_status": "active",
}

DOCUMENT = {
    "version": 1,
    "name": "activation-binding",
    "session": "nse_equity",
    "instruments": [INSTRUMENT],
    "stages": [
        {
            "id": "px",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "5minute",
            "conditions": {
                "all": [
                    {"left": {"field": "close"}, "op": "crosses_above",
                     "right": {"value": 3000}}
                ]
            },
        }
    ],
    "alerts": [
        {"id": "breakout", "source": "px", "trigger": "on_transition",
         "channels": []}
    ],
}


class _StubWorkerTokenRepository:
    def __init__(self, token, *, raw_token=RAW_TOKEN):
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


def _client():
    """TestClient over the real workflows router with an in-memory alerts DB."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    token = WorkerToken(
        token_id="worker-1",
        name="test-worker",
        account_scope="kite:paper-a",
        allowed_modes=["paper", "dry_run"],
        allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
        allowed_templates=[],
    )
    app = FastAPI()
    app.include_router(worker_workflows_router.router, prefix="/api")
    app.dependency_overrides[worker_workflows_router._alerts_db] = lambda: factory
    app.state.algo_worker_repository = _StubWorkerTokenRepository(token)
    return TestClient(app), factory, engine


def _activate_via_api(client):
    created = client.post(
        WF, json={"name": DOCUMENT["name"], "document": DOCUMENT}, headers=HEADERS
    )
    assert created.status_code == 200, created.text
    workflow_id = created.json()["workflow_id"]
    activated = client.post(f"{WF}/{workflow_id}/activate", headers=HEADERS)
    assert activated.status_code == 200, activated.text
    return workflow_id, activated.json()


def _subscription(factory, workflow_id):
    with factory() as session:
        row = session.execute(
            select(AlertSubscription)
            .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
            .where(WorkflowRevision.workflow_id == workflow_id)
        ).scalar_one_or_none()
    assert row is not None, "activation must materialize a subscription"
    return row


@pytest.fixture()
def catalog(monkeypatch):
    """A controllable catalog answer for the binding resolver."""
    state = {"binding": dict(BINDING_V1)}

    monkeypatch.setattr(
        EvaluationService,
        "_resolve_catalog_binding",
        staticmethod(lambda key, session: dict(state["binding"]) if state["binding"] else None),
    )
    return state


# ---------------------------------------------------------------------------
# 1. activation persists the binding
# ---------------------------------------------------------------------------


def test_api_activation_persists_catalog_binding(catalog):
    """The route that creates subscriptions for a user must record the binding."""
    client, factory, engine = _client()
    try:
        workflow_id, body = _activate_via_api(client)
        assert body["subscriptions_created"] == 1

        row = _subscription(factory, workflow_id)
        assert row.config.get("instrument_binding") == BINDING_V1, (
            "API activation must persist the catalog binding, not just the "
            "alert's own config — otherwise every event from an API-created "
            "subscription loses its provenance"
        )
        # Identity AND generation, so a later generation move is detectable.
        binding = row.config["instrument_binding"]
        assert binding["instrument_id"] == "instrument-1"
        assert binding["broker_token"] == 111
        assert binding["catalog_generation"] == "generation-1"
    finally:
        engine.dispose()


def test_activation_without_a_catalog_still_materializes(catalog):
    """A missing catalog is not fatal: the row exists, the worker recovers it."""
    catalog["binding"] = None
    client, factory, engine = _client()
    try:
        workflow_id, body = _activate_via_api(client)
        assert body["subscriptions_created"] == 1
        row = _subscription(factory, workflow_id)
        assert row.state == "active"
        assert "instrument_binding" not in (row.config or {})
    finally:
        engine.dispose()


def test_api_activation_rebinds_when_the_catalog_moves(catalog):
    """A token/generation replacement is picked up on re-materialization."""
    client, factory, engine = _client()
    try:
        workflow_id, _ = _activate_via_api(client)
        assert _subscription(factory, workflow_id).config["instrument_binding"] == BINDING_V1

        # The catalog moves: same instrument, new broker token and generation.
        catalog["binding"] = dict(BINDING_V2)
        with factory() as session:
            revision = session.execute(
                select(WorkflowRevision).where(
                    WorkflowRevision.workflow_id == workflow_id
                )
            ).scalar_one()
        EvaluationService(
            SqlAlchemyWorkflowRepository(factory), factory
        ).ensure_subscriptions(revision)

        binding = _subscription(factory, workflow_id).config["instrument_binding"]
        assert binding == BINDING_V2, "a replaced token must re-bind the row"
        assert binding["broker_token"] == 222
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. evaluation after API activation carries the binding into evidence
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self):
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return None


class _EmptyHistory:
    def recent_bars(self, key, timeframe, limit):
        return []

    def previous_session_levels(self, key, at):
        return None


def _make_worker(factory, *, resolver, registry=None):
    return EvaluationWorker(
        SqlAlchemyWorkflowRepository(factory),
        factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=_EmptyHistory(),
        poll_interval_s=0.01,
        instrument_resolver=resolver,
        binding_registry=registry,
        renewal=None,
        owner_id="worker-1",
    )


def _bar(ts, close):
    from backend.alerts.predicates import Observation

    return Observation(
        ts=ts, epoch_id="candle", ltp=close, open=close, high=close, low=close,
        close=close, volume=1000.0, final=True,
    )


def _dispatch(worker, ts, close):
    sub = next(s for s in worker._subscriptions if s.instrument_key == INSTRUMENT)
    worker._dispatch(sub, _bar(ts, close))


def test_worker_evaluation_carries_the_activation_binding_into_evidence(catalog):
    """An event from an API-activated subscription records the binding used."""
    client, factory, engine = _client()
    try:
        workflow_id, _ = _activate_via_api(client)

        worker = _make_worker(
            factory, resolver=lambda keys: ({INSTRUMENT: 111}, {})
        )
        asyncio.run(worker.start())

        # First bar initializes; the second crosses and publishes.
        _dispatch(worker, T0 + FIVEMIN, 2900.0)
        _dispatch(worker, T0 + 2 * FIVEMIN, 3100.0)

        with factory() as session:
            events = list(session.execute(select(SignalEvent)).scalars().all())
        assert len(events) == 1, "one crossing, one event"
        evidence = events[0].evidence
        assert evidence["instrument_binding"] == BINDING_V1, (
            "the event must carry the binding in force at evaluation time"
        )
        # Identity and generation, not just a token.
        assert evidence["instrument_binding"]["catalog_generation"] == "generation-1"

        # A later re-bind must NOT rewrite the meaning of the old event: the
        # snapshot is copied into the immutable row.
        catalog["binding"] = dict(BINDING_V2)
        with factory() as session:
            revision = session.execute(
                select(WorkflowRevision).where(
                    WorkflowRevision.workflow_id == workflow_id
                )
            ).scalar_one()
        EvaluationService(
            SqlAlchemyWorkflowRepository(factory), factory
        ).ensure_subscriptions(revision)
        with factory() as session:
            again = session.execute(select(SignalEvent)).scalars().all()
        assert again[0].evidence["instrument_binding"] == BINDING_V1
    finally:
        engine.dispose()


def test_refresh_rebinds_when_the_catalog_moves_without_a_restart(catalog):
    """A token replaced while the worker runs is reflected in later evidence.

    The registry tracks tokens for feed construction, but the descriptor
    (identity + catalog generation) that events copy lives in
    ``sub.config["instrument_binding"]``. Without a refresh-time re-bind, a
    replacement would leave events reporting the generation they are no longer
    evaluated under — confidently wrong provenance, which is worse than none.
    """
    client, factory, engine = _client()
    try:
        workflow_id, _ = _activate_via_api(client)

        answers = {"token": 111}
        worker = _make_worker(
            factory, resolver=lambda keys: ({INSTRUMENT: answers["token"]}, {})
        )
        asyncio.run(worker.start())
        assert _subscription(factory, workflow_id).config["instrument_binding"] == BINDING_V1

        # The catalog moves while the worker keeps running: the broker token
        # AND the generation both change.
        catalog["binding"] = dict(BINDING_V2)
        answers["token"] = 222
        asyncio.run(worker.refresh_subscriptions())

        # Persisted...
        assert _subscription(factory, workflow_id).config["instrument_binding"] == BINDING_V2
        # ...and in the object dispatch actually uses, which is what evidence
        # is copied from.
        live = next(
            s for s in worker._subscriptions if s.instrument_key == INSTRUMENT
        )
        assert live.config["instrument_binding"] == BINDING_V2

        # A crossing published after the move carries the NEW binding.
        _dispatch(worker, T0 + FIVEMIN, 2900.0)
        _dispatch(worker, T0 + 2 * FIVEMIN, 3100.0)
        with factory() as session:
            events = list(session.execute(select(SignalEvent)).scalars().all())
        assert len(events) == 1
        assert events[0].evidence["instrument_binding"] == BINDING_V2
        asyncio.run(worker.stop())
    finally:
        engine.dispose()


def test_authoritative_retirement_stops_evaluation(catalog):
    """An expired/retired instrument stops consuming feeds and dispatching.

    The authored stage is on the candle clock, so retirement must tear down the
    CANDLE source and dispatch group (tick sources are the LTP path).
    """
    client, factory, engine = _client()
    try:
        workflow_id, _ = _activate_via_api(client)

        answers = {"reject": False}
        built = []

        def _resolver(keys):
            if answers["reject"]:
                # Authoritative refusal: the catalog no longer serves it.
                return {}, {INSTRUMENT: "retired"}
            return {INSTRUMENT: 111}, {}

        def _candle_source(key, timeframe):
            source = _FakeSource()
            built.append(source)
            return source

        worker = _make_worker(factory, resolver=_resolver)
        worker.candle_source_factory = _candle_source
        asyncio.run(worker.start())

        candle_keys = [k for k in worker._candle_sources if k[0] == INSTRUMENT]
        assert candle_keys, "an active candle subscription must have a source"
        active_key = candle_keys[0]
        assert worker._candle_subs.get(active_key), "and a dispatch group"
        active_source = worker._candle_sources[active_key]

        # The catalog retires it on the next import.
        answers["reject"] = True
        asyncio.run(worker.refresh_subscriptions())

        assert all(
            k[0] != INSTRUMENT for k in worker._candle_sources
        ), "a retired instrument must stop holding a candle source"
        assert not worker._candle_subs.get(active_key), (
            "and must stop being dispatched to"
        )
        assert active_source.stopped, "its feed must be stopped, not leaked"

        # And a live observation for it evaluates nowhere.
        before = worker.health["evaluations"]
        asyncio.run(worker.poll_once())
        assert worker.health["evaluations"] == before

        asyncio.run(worker.stop())
    finally:
        engine.dispose()
