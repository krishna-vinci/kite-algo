"""Phase 3B: screener-sourced dynamic universes.

- kind validates and requires an owner-scoped source workflow;
- members come from the latest COMPLETE run (top_n respected);
- no complete run or an expired freshness limit raises UniverseSourceUnavailable
  (downstream alerts degrade to unknown instead of silently stale — E-18);
- dependency cycles are rejected;
- another owner's workflow is never resolvable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base
from backend.workflows.screener_repository import ScreenerRunRepository
from backend.workflows.universes import (
    Universe,
    UniverseRevision,
    UniverseService,
    UniverseSourceUnavailable,
    UniverseValidationError,
)

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


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


class _PassCatalog:
    """Accepts every key as an active instrument (unit test stub)."""

    class _Descriptor:
        lifecycle_status = "active"
        generation = "gen-test"
        instrument_id = "stub"

    def resolve_public_key(self, key):
        return self._Descriptor()


def _service(session_factory) -> UniverseService:
    return UniverseService(session_factory, catalog=_PassCatalog())


def _seed_run(session_factory, workflow, *, status="complete", scheduled_for=T0,
              members=("NSE:A", "NSE:B"), owner_id="owner-1", ranks=None):
    from backend.workflows.repository import WorkflowRevision
    from sqlalchemy import select

    from backend.workflows.screener_repository import ScreenerRun

    session = session_factory()
    try:
        revision_id = session.execute(
            select(WorkflowRevision.id).where(WorkflowRevision.workflow_id == workflow.id)
        ).scalar_one()
        run = ScreenerRun(
            id="run-1",
            owner_id=owner_id,
            workflow_id=workflow.id,
            workflow_revision_id=revision_id,
            occurrence_key=f"{workflow.id}:{int(scheduled_for.timestamp())}",
            scheduled_for=scheduled_for,
            status="running",  # finalize CAS transitions it
            universe_revision=7,
            as_of=scheduled_for,
            coverage={"expected": len(members), "complete": status == "complete"},
            data_freshness={},
            lease_owner="w",
            lease_expires_at=scheduled_for,
        )
        session.add(run)
        session.commit()
        repo = ScreenerRunRepository(session_factory)
        repo.finalize_run(
            run.id, "w",
            status=status,
            as_of=scheduled_for,
            coverage={"expected": len(members), "complete": status == "complete"},
            data_freshness={},
            members=[
                {
                    "instrument_key": key,
                    "passed": True,
                    "rank": (ranks or {}).get(key, index + 1),
                    "score": 10.0 - index,
                    "values": {"close": 100.0},
                }
                for index, key in enumerate(members)
            ],
            universe_revision=7,
            now=scheduled_for,
        )
        return run.id
    finally:
        session.close()


def _seed_workflow(session_factory, name, owner_id="owner-1", *, universe=None, kind="screener"):
    from sqlalchemy import select

    from backend.workflows.repository import Workflow, WorkflowRevision
    from backend.workflows.screener_repository import ScreenerRun  # noqa: F401

    document = {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "stages": [{"id": "s", "type": "filter", "clock": "candle_close", "timeframe": "1d",
                    "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]}}],
        "alerts": [],
        "screener": {"schedule": {"every": "1d", "at": "session_close"}},
    }
    if universe is not None:
        document["universe"] = universe
    session = session_factory()
    try:
        workflow = Workflow(id="wf-" + name, owner_id=owner_id, name=name)
        session.add(workflow)
        session.flush()
        revision = WorkflowRevision(
            workflow_id=workflow.id, revision=1,
            canonical_hash=f"hash-{name}", document=document, status="active",
        )
        session.add(revision)
        session.commit()
        return session.execute(
            select(Workflow).where(Workflow.id == workflow.id)
        ).scalar_one()
    finally:
        session.close()


def test_screener_universe_requires_workflow_config(session_factory):
    service = _service(session_factory)
    with pytest.raises(UniverseValidationError):
        service.create_universe("owner-1", "dyn", "screener", {})


def test_screener_universe_resolves_latest_complete_run_with_top_n(session_factory):
    workflow = _seed_workflow(session_factory, "mom-scan")
    _seed_run(session_factory, workflow, members=("NSE:A", "NSE:B", "NSE:C"),
              ranks={"NSE:A": 1, "NSE:B": 2, "NSE:C": 3})
    service = _service(session_factory)
    service.create_universe(
        "owner-1", "dyn-top2", "screener", {"workflow": "mom-scan", "top_n": 2}
    )
    detail = service.resolve_membership("owner-1", "dyn-top2")
    assert detail["members"] == ["NSE:A", "NSE:B"]
    freshness = detail["coverage"]["source_freshness"]
    assert freshness["source_run_id"] == "run-1"
    assert freshness["source_run_universe_revision"] == 7


def test_no_complete_run_is_source_unavailable(session_factory):
    workflow = _seed_workflow(session_factory, "empty-scan")
    _seed_run(session_factory, workflow, status="running")
    service = _service(session_factory)
    service.create_universe("owner-1", "dyn", "screener", {"workflow": "empty-scan"})
    with pytest.raises(UniverseSourceUnavailable):
        service.resolve_membership("owner-1", "dyn")


def test_stale_complete_run_expires_visibly(session_factory):
    workflow = _seed_workflow(session_factory, "old-scan")
    _seed_run(
        session_factory, workflow,
        scheduled_for=T0 - timedelta(days=10),  # beyond the 3d default
    )
    service = _service(session_factory)
    service.create_universe("owner-1", "dyn", "screener", {"workflow": "old-scan"})
    with pytest.raises(UniverseSourceUnavailable) as exc:
        service.resolve_membership("owner-1", "dyn")
    assert "stale" in str(exc.value)


def test_freshness_limit_extends_usability(session_factory):
    workflow = _seed_workflow(session_factory, "old-scan")
    _seed_run(session_factory, workflow, scheduled_for=T0 - timedelta(days=10))
    service = _service(session_factory)
    service.create_universe(
        "owner-1", "dyn", "screener",
        {"workflow": "old-scan", "freshness_limit_s": 30 * 24 * 3600},
    )
    detail = service.resolve_membership("owner-1", "dyn")
    assert "NSE:A" in detail["members"]


def test_cross_owner_workflow_reference_rejected(session_factory):
    _seed_workflow(session_factory, "secret-scan", owner_id="owner-2")
    service = _service(session_factory)
    service.create_universe("owner-1", "dyn", "screener", {"workflow": "secret-scan"})
    with pytest.raises(UniverseSourceUnavailable):
        service.resolve_membership("owner-1", "dyn")


def test_dependency_cycle_rejected(session_factory):
    # W_b's universe references universe "cyc-a"; creating a universe sourced
    # from W_a whose document references "cyc-b" which sources W_b whose
    # document references "cyc-a" would loop — the bounded walk rejects it.
    doc_a = {
        "union": [{"universe": "cyc-b"}],
        "deduplicate": True,
    }
    workflow_a = _seed_workflow(session_factory, "scan-a", universe=doc_a)
    workflow_b = _seed_workflow(
        session_factory, "scan-b",
        universe={"union": [{"universe": "cyc-a"}], "deduplicate": True},
    )
    service = _service(session_factory)
    # universe cyc-b is sourced from scan-b
    service.create_universe("owner-1", "cyc-b", "screener", {"workflow": "scan-b"})
    # now creating cyc-a sourced from scan-a: scan-a references cyc-b -> scan-b
    # references cyc-a (the universe being created) -> cycle
    with pytest.raises(UniverseValidationError) as exc:
        service.create_universe("owner-1", "cyc-a", "screener", {"workflow": "scan-a"})
    assert "cycle" in str(exc.value).lower()


def test_acyclic_chain_is_allowed(session_factory):
    workflow_b = _seed_workflow(
        session_factory, "scan-b2",
        universe={"union": [{"index": "nifty50"}], "deduplicate": True},
    )
    service = _service(session_factory)
    service.create_universe("owner-1", "mid", "screener", {"workflow": "scan-b2"})
    _seed_workflow(session_factory, "scan-a2",
                   universe={"union": [{"universe": "mid"}], "deduplicate": True})
    service.create_universe("owner-1", "top", "screener", {"workflow": "scan-a2"})
    # resolution of scan-a2's cycle walk passes (no cycle): creation succeeded
    assert service.list_universes("owner-1") is not None
