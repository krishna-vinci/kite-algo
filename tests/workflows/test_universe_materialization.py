"""Phase 2 F7 acceptance: universe membership lifecycle in the worker.

- membership expansion admits members WITHOUT restart; admission is warmup-
  gated (new members start silent);
- membership shrink departs members and releases their subscriptions;
- events retain the membership revision in force at evaluation time;
- a failing resolution keeps the last valid membership (explicit freshness).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import Delivery  # noqa: F401  (registers tables)
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base, SqlAlchemyWorkflowRepository
from backend.workflows.runtime import EvaluationWorker


def _universe_document():
    return {
        "version": 1,
        "name": "uni-breakout",
        "session": "nse_equity",
        "universe": {"union": [{"universe": "my-watchlist"}], "deduplicate": True},
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [{"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 3000}}]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["c1"]}],
    }


class _FakeUniverseService:
    def __init__(self, members):
        self.members_by_name = members
        self.resolve_calls = 0

    def latest_revision(self, owner_id, name):
        self.resolve_calls += 1
        members = self.members_by_name.get(name)
        if members is None:
            return None
        return {"revision": 3, "members": sorted(members), "resolved_at": "now"}

    def preview_membership(self, owner_id, kind, source_config):
        raise AssertionError("index refs are not used in this test")


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _activate(repo, session_factory, name, doc):
    from backend.workflows.service import EvaluationService

    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    service = EvaluationService(repo, session_factory)
    service.ensure_subscriptions(active)
    return active


def _make_worker(session_factory, universe_service):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    worker = EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=_FakeHistory(),
        poll_interval_s=0.01,
        instrument_resolver=lambda keys: ({k: 1 for k in keys}, set()),
        renewal=None,
    )
    worker.universe_service = universe_service
    worker.universe_resolve_interval_s = 0.0  # re-resolve every pass
    return worker


class _FakeSource:
    def __init__(self):
        self.stopped = False

    async def start(self):
        return None

    async def next_observation(self):
        return None

    async def stop(self):
        self.stopped = True


class _FakeHistory:
    def recent_bars(self, key, timeframe, limit):
        return []

    def previous_session_levels(self, key, at):
        return None


def test_membership_expansion_and_departure_without_restart(session_factory):
    from backend.workflows.service import EvaluationService

    fake = _FakeUniverseService({"my-watchlist": {"NSE:A", "NSE:B"}})
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, session_factory, "uni-breakout", _universe_document())
    worker = _make_worker(session_factory, fake)

    asyncio.run(worker.start())
    keys = {s.instrument_key for s in worker._subscriptions}
    assert keys == {"NSE:A", "NSE:B"}

    # the universe grows: a third member is admitted on the next refresh pass
    fake.members_by_name["my-watchlist"] = {"NSE:A", "NSE:B", "NSE:C"}
    asyncio.run(worker.refresh_subscriptions())
    keys = {s.instrument_key for s in worker._subscriptions}
    assert keys == {"NSE:A", "NSE:B", "NSE:C"}
    # admitted members carry the membership revision in their config
    member_sub = next(s for s in worker._subscriptions if s.instrument_key == "NSE:C")
    assert member_sub.config.get("universe_member") is True
    assert member_sub.config.get("universe_revision") == 3

    # the universe shrinks: NSE:A departs and its evaluation stops
    fake.members_by_name["my-watchlist"] = {"NSE:B", "NSE:C"}
    asyncio.run(worker.refresh_subscriptions())
    active_keys = {
        s.instrument_key for s in worker._subscriptions if s.state == "active"
    }
    assert active_keys == {"NSE:B", "NSE:C"}
    # the departed member's DB row is paused with a visible reason (Phase 1
    # dispatch semantics remove paused rows from the worker's tables)
    from backend.workflows.repository import AlertSubscription

    with session_factory() as session:
        departed = (
            session.query(AlertSubscription)
            .filter(AlertSubscription.instrument_key == "NSE:A")
            .first()
        )
        assert departed.state == "paused"
        assert departed.config.get("universe_departed") is True
    asyncio.run(worker.stop())


def test_event_evidence_carries_universe_revision(session_factory):
    from backend.alerts.predicates import Observation

    fake = _FakeUniverseService({"my-watchlist": {"NSE:A"}})
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, session_factory, "uni-breakout", _universe_document())
    worker = _make_worker(session_factory, fake)
    asyncio.run(worker.start())
    sub = worker._subscriptions[0]
    assert sub.config.get("universe_revision") == 3

    service = worker.service
    service.handle_observation(
        sub, Observation(ts=T0(), epoch_id="b1", ltp=2995.0)
    )
    result = service.handle_observation(
        sub, Observation(ts=T0(1), epoch_id="b1", ltp=3005.0)
    )
    assert result.emitted is True
    from sqlalchemy import select

    with session_factory() as session:
        from backend.workflows.repository import SignalEvent

        events = session.execute(select(SignalEvent)).scalars().all()
        assert len(events) == 1
        assert events[0].evidence.get("universe_revision") == 3
    asyncio.run(worker.stop())


def test_failed_resolution_keeps_last_valid_membership(session_factory):
    class _FlakyUniverseService(_FakeUniverseService):
        def latest_revision(self, owner_id, name):
            if self.fail:
                raise RuntimeError("index source down")
            return super().latest_revision(owner_id, name)

    fake = _FlakyUniverseService({"my-watchlist": {"NSE:A"}})
    fake.fail = False
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, session_factory, "uni-breakout", _universe_document())
    worker = _make_worker(session_factory, fake)
    asyncio.run(worker.start())
    assert {s.instrument_key for s in worker._subscriptions} == {"NSE:A"}

    # source fails AND the cached resolution is expired: last membership holds
    fake.fail = True
    worker._universe_cache.clear()  # force a resolution attempt
    asyncio.run(worker.refresh_subscriptions())
    from backend.workflows.repository import AlertSubscription

    with session_factory() as session:
        states = {
            row.instrument_key: row.state
            for row in session.query(AlertSubscription).all()
        }
    assert states.get("NSE:A") == "active"
    assert worker._universe_health["resolution_failures"] >= 1
    asyncio.run(worker.stop())


from datetime import datetime, timedelta, timezone


def T0(offset_minutes=0):
    return datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)
