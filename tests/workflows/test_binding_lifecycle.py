"""C1 acceptance: the complete instrument-binding lifecycle.

Exercises the PRODUCTION wiring — EvaluationWorker refresh passes driving
source factories, the shared binding registry, PgCandleHistory lookups, and
the market-runtime renewal snapshot — not the resolver helper in isolation.

Scenarios (handoff C1):
- registry diff semantics (added / replaced / removed);
- startup with zero tokens/workflows, then first activation binds and
  subscribes without restart, and the renewal callable sees the new snapshot;
- A -> A+B membership growth binds both; A+B -> B shrink releases A;
- broker token replacement rebuilds the source (fresh epoch) while durable
  trigger state (checkpoint) is preserved;
- retirement/authoritative rejection stops the source and dispatch;
- PgCandleHistory reads the CURRENT binding, not a startup snapshot.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.compiler import compile_document
from backend.workflows.instrument_bindings import InstrumentBindingRegistry
from backend.workflows.repository import AlertSubscription, Base, SqlAlchemyWorkflowRepository
from backend.workflows.runtime import EvaluationWorker, PgCandleHistory


def _document_dict(name, symbols, trigger="once"):
    return {
        "version": 1,
        "name": name,
        "instruments": [
            {"symbol": s.split(":", 1)[1], "exchange": s.split(":", 1)[0]}
            for s in symbols
        ],
        "session": "nse_equity",
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [
                        {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 3000}}
                    ]
                },
            }
        ],
        "alerts": [{"id": "breakout", "source": "px", "trigger": trigger, "channels": []}],
    }


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


class _FakeTickSource:
    def __init__(self, epoch_id, token=None):
        self.epoch_id = epoch_id
        self.token = token
        self.stopped = False

    async def start(self):
        return None

    async def next_observation(self):
        return None

    async def stop(self):
        self.stopped = True


class _FakeHistory:
    def __init__(self, registry):
        self._registry = registry

    def recent_bars(self, key, timeframe, limit):
        return []

    def previous_session_levels(self, key, at):
        return None


class _Harness:
    """Worker + controllable catalog resolution state (production wiring)."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.repo = SqlAlchemyWorkflowRepository(session_factory)
        self.bindings = InstrumentBindingRegistry()
        # What the "catalog" currently answers: key -> token; missing = reject.
        self.catalog_answers: dict = {}
        self.resolve_calls: list = []
        self.source_epoch = 0
        self.built_sources: list = []
        self.renewal_snapshots: list = []

    def _resolver(self, keys):
        self.resolve_calls.append(set(keys))
        resolved, rejected = {}, {}
        for key in keys:
            token = self.catalog_answers.get(key)
            if token is None:
                rejected[key] = "not_found"
            else:
                resolved[key] = token
        return resolved, rejected

    def _tick_factory(self, instrument_key):
        self.source_epoch += 1
        source = _FakeTickSource(f"boot-{self.source_epoch}", token=self.bindings.get(instrument_key))
        self.built_sources.append(source)
        return source

    async def _renewal(self):
        self.renewal_snapshots.append(self.bindings.snapshot())

    def build_worker(self) -> EvaluationWorker:
        self.worker = EvaluationWorker(
            self.repo,
            self.session_factory,
            channel_resolver=None,
            tick_source_factory=self._tick_factory,
            candle_source_factory=lambda ik, tf: _FakeTickSource("candle"),
            candle_history=_FakeHistory(self.bindings),
            poll_interval_s=0.01,
            instrument_resolver=self._resolver,
            binding_registry=self.bindings,
            renewal=self._renewal,
            renewal_interval_s=30.0,
        )
        return self.worker

    def activate(self, name, symbols, trigger="once"):
        from backend.workflows.parser import parse_workflow_dict

        compiled = compile_document(parse_workflow_dict(_document_dict(name, symbols, trigger)))
        workflow, revision = self.repo.create_workflow(
            "owner-1", name, compiled.document.to_document_dict(), compiled.canonical_hash,
        )
        self.repo.activate_revision(workflow.id, revision.id)
        created = self.worker.service.ensure_subscriptions(
            self.repo.get_active_revision(workflow.id)
        )
        return workflow, created


def test_registry_apply_reports_diffs():
    registry = InstrumentBindingRegistry({"NSE:A": 1})
    change = registry.apply({"NSE:A": 1, "NSE:B": 2}, set())
    assert change.added == {"NSE:B": 2} and not change.changed

    change = registry.apply({"NSE:A": 9, "NSE:B": 2}, set())
    assert change.changed == {"NSE:A": 9}

    change = registry.apply({"NSE:A": 9}, {"NSE:B"})
    assert change.removed == {"NSE:B"}
    assert registry.snapshot() == {"NSE:A": 9}
    assert registry.revision == 3


def test_startup_empty_then_activation_binds_without_restart(session_factory):
    harness = _Harness(session_factory)
    worker = harness.build_worker()
    asyncio.run(worker.start())
    # zero workflows, zero bindings at startup: no sources, renewal idle
    assert worker._tick_sources == {}
    assert harness.renewal_snapshots == []

    harness.catalog_answers["NSE:RELIANCE"] = 738561
    _, created = harness.activate("reliance", ["NSE:RELIANCE"])
    assert created == 1

    summary = asyncio.run(worker.refresh_subscriptions())
    assert summary["added"] == 1

    # the new binding reached the actual source factory, registry, and health
    source = worker._tick_sources["NSE:RELIANCE"]
    assert source.token == 738561
    assert harness.bindings.snapshot() == {"NSE:RELIANCE": 738561}
    assert worker.health["unresolved_instruments"] == 0
    assert worker.health["binding_revisions"] == 1

    asyncio.run(harness._renewal())
    assert harness.renewal_snapshots[-1] == {"NSE:RELIANCE": 738561}
    asyncio.run(worker.stop())


def test_membership_growth_then_shrink(session_factory):
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111, "NSE:B": 222}
    worker = harness.build_worker()
    harness.worker = worker
    harness.activate("pair", ["NSE:A", "NSE:B"], trigger="on_transition")
    asyncio.run(worker.start())

    assert set(worker._tick_sources) == {"NSE:A", "NSE:B"}
    tokens = {key: worker._tick_sources[key].token for key in worker._tick_sources}
    assert tokens == {"NSE:A": 111, "NSE:B": 222}

    # A+B -> B: pause A; refresh must release A's subscription and source
    with session_factory() as session:
        row = (
            session.query(AlertSubscription)
            .filter(AlertSubscription.instrument_key == "NSE:A")
            .first()
        )
        row.state = "paused"
        session.commit()

    summary = asyncio.run(worker.refresh_subscriptions())
    assert summary["removed"] == 1
    assert set(worker._tick_sources) == {"NSE:B"}
    released = [s for s in harness.built_sources if s.token == 111]
    assert released and all(s.stopped for s in released)
    asyncio.run(worker.stop())


def test_token_replacement_rebuilds_source_keeps_durable_state(session_factory):
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111}
    worker = harness.build_worker()
    harness.activate("single", ["NSE:A"])
    asyncio.run(worker.start())

    sub = worker._subscriptions[0]
    harness.repo.save_checkpoint(
        sub.id, sub.instrument_key, "boot-1",
        {"fired_once": True, "initialized": True, "epoch_id": "boot-1"}, 0,
    )

    old_source = worker._tick_sources["NSE:A"]
    harness.catalog_answers["NSE:A"] = 999888  # broker rotates the token
    asyncio.run(worker.refresh_subscriptions())

    new_source = worker._tick_sources["NSE:A"]
    assert new_source is not old_source
    assert new_source.token == 999888
    assert old_source.stopped is True
    # start() adds the initial binding (revision 1), the replacement is 2
    assert worker.health["binding_revisions"] == 2
    # observation continuity resets (fresh epoch) but durable state is intact
    saved, _epoch = harness.repo.load_checkpoint(sub.id, sub.instrument_key, "boot-1")
    assert saved["fired_once"] is True
    asyncio.run(worker.stop())


def test_retirement_stops_source_and_dispatch(session_factory):
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111}
    worker = harness.build_worker()
    harness.activate("retire", ["NSE:A"], trigger="on_transition")
    asyncio.run(worker.start())
    assert "NSE:A" in worker._tick_sources

    # catalog authoritatively retires the instrument on the next import
    harness.catalog_answers.clear()
    asyncio.run(worker.refresh_subscriptions())

    assert "NSE:A" not in worker._tick_sources
    assert not worker._ltp_subs.get("NSE:A")
    stopped = [s for s in harness.built_sources if s.token == 111]
    assert stopped and all(s.stopped for s in stopped)
    assert worker.health["unresolved_instruments"] == 1

    # a live observation for the retired instrument must not dispatch anywhere
    evaluations_before = worker.health["evaluations"]
    asyncio.run(worker.poll_once())
    assert worker.health["evaluations"] == evaluations_before
    asyncio.run(worker.stop())


def test_pg_history_reads_current_binding_not_snapshot():
    registry = InstrumentBindingRegistry()
    history = PgCandleHistory(engine=None, instrument_tokens=registry)
    assert history._token_for("NSE:A") is None
    registry.apply({"NSE:A": 4242}, set())
    assert history._token_for("NSE:A") == 4242


def test_pg_history_accepts_a_get_only_provider():
    """A provider that exposes only ``get()`` is still a provider.

    Regression: the constructor required ``snapshot()`` too, so the API
    process's lazy catalog token map fell into the static-mapping branch and
    died on ``.items()`` — every manual screener run and preview 500'd in the
    deployed stack.
    """

    class _GetOnly:
        def get(self, key):
            return {"NSE:RELIANCE": 738561}.get(key)

    history = PgCandleHistory(engine=None, instrument_tokens=_GetOnly())
    assert history._token_for("NSE:RELIANCE") == 738561
    assert history._token_for("NSE:ABSENT") is None


def test_registry_retain_only_releases_unneeded_bindings():
    """A binding nobody requires any more must not be retained.

    ``apply`` removes a key ONLY when the catalog explicitly rejects it, so
    without an explicit release the registry keeps the binding of an archived
    subscription forever. That leaks market-data quota: the renewal callback
    publishes the registry snapshot, so a retained key is re-subscribed to the
    market-runtime on every lease refresh for an instrument nothing evaluates.
    """
    registry = InstrumentBindingRegistry({"NSE:A": 1, "NSE:B": 2, "NSE:C": 3})
    assert registry.snapshot() == {"NSE:A": 1, "NSE:B": 2, "NSE:C": 3}

    # Nothing to release: the required set is unchanged.
    assert registry.retain_only({"NSE:A", "NSE:B", "NSE:C"}) == set()
    assert registry.revision == 0

    released = registry.retain_only({"NSE:B"})
    assert released == {"NSE:A", "NSE:C"}
    assert registry.snapshot() == {"NSE:B": 2}, "only the required key survives"
    assert registry.revision == 1, "consumers must see the binding set moved"

    # Idempotent, and case/whitespace-insensitive like the rest of the class.
    assert registry.retain_only({" nse:b "}) == set()
    assert registry.snapshot() == {"NSE:B": 2}


def test_removed_subscription_releases_its_token_from_the_lease(session_factory):
    """Dropping a subscription must stop paying for its feed.

    Drives the production refresh path: after a subscription goes away the
    released instrument must disappear from the renewal snapshot (which is what
    the market-runtime lease is built from) and its feed source must be stopped
    — otherwise the runtime keeps streaming a token nothing consumes until the
    worker restarts.
    """
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111, "NSE:B": 222}
    worker = harness.build_worker()
    harness.worker = worker
    harness.activate("pair", ["NSE:A", "NSE:B"], trigger="on_transition")
    asyncio.run(worker.start())
    assert worker.instrument_tokens == {"NSE:A": 111, "NSE:B": 222}

    # A+B -> B: pause A, exactly as archiving or a paused workflow does.
    with session_factory() as session:
        row = (
            session.query(AlertSubscription)
            .filter(AlertSubscription.instrument_key == "NSE:A")
            .first()
        )
        row.state = "paused"
        session.commit()

    harness.renewal_snapshots.clear()
    asyncio.run(worker.refresh_subscriptions())

    # The released binding is gone from the registry...
    assert worker.instrument_tokens == {"NSE:B": 222}, (
        "a released instrument must not stay bound"
    )
    # ...so the renewal payload — the market-runtime lease — no longer includes
    # its token. This is the assertion that fails if teardown is missing.
    asyncio.run(harness._renewal())
    assert harness.renewal_snapshots[-1] == {"NSE:B": 222}
    assert 111 not in harness.renewal_snapshots[-1].values()

    released = [s for s in harness.built_sources if s.token == 111]
    assert released and all(s.stopped for s in released)
    asyncio.run(worker.stop())


def test_transient_resolution_failure_does_not_release_bindings(session_factory):
    """A failed catalog pass must never be read as "no longer required".

    Releasing on a transient resolver error would stop feeds for instruments
    that are still subscribed, so the pass must leave bindings untouched.
    """
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111}
    worker = harness.build_worker()
    harness.worker = worker
    harness.activate("solo", ["NSE:A"], trigger="on_transition")
    asyncio.run(worker.start())
    assert worker.instrument_tokens == {"NSE:A": 111}

    def _explode(keys):
        raise RuntimeError("catalog temporarily unavailable")

    worker.instrument_resolver = _explode
    asyncio.run(worker.refresh_subscriptions())

    assert worker.instrument_tokens == {"NSE:A": 111}, (
        "bindings must survive a transient resolution failure"
    )
    assert set(worker._tick_sources) == {"NSE:A"}
    asyncio.run(worker.stop())


def test_last_subscription_removed_releases_everything(session_factory):
    """Zero subscriptions must release bindings too, not just a smaller set.

    Guards a subtle shape: the resolution pass skips the catalog call when
    nothing is required, so an early return placed before the release would
    leave the LAST binding retained — the worker keeps a live feed for an
    instrument it no longer evaluates, and the lease keeps it alive, until the
    process restarts. The empty request is exactly when the release matters
    most.
    """
    harness = _Harness(session_factory)
    harness.catalog_answers = {"NSE:A": 111}
    worker = harness.build_worker()
    harness.worker = worker
    harness.activate("solo", ["NSE:A"], trigger="on_transition")
    asyncio.run(worker.start())
    assert worker.instrument_tokens == {"NSE:A": 111}

    # The only subscription goes away.
    with session_factory() as session:
        row = (
            session.query(AlertSubscription)
            .filter(AlertSubscription.instrument_key == "NSE:A")
            .first()
        )
        row.state = "archived"
        session.commit()

    asyncio.run(worker.refresh_subscriptions())

    assert worker.instrument_tokens == {}, (
        "the last binding must be released, not retained until a restart"
    )
    # An empty snapshot makes the renewal a no-op, so the market-runtime lease
    # is no longer refreshed and its own TTL janitor releases the owner.
    harness.renewal_snapshots.clear()
    asyncio.run(harness._renewal())
    assert harness.renewal_snapshots[-1] == {}
    released = [s for s in harness.built_sources if s.token == 111]
    assert released and all(s.stopped for s in released)
    asyncio.run(worker.stop())
