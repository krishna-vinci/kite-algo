"""Phase 3C: screener worker API (runs, run detail, events, preview).

- routes live under /api/worker/screeners (worker-token boundary);
- ownership: another owner's workflow/run is 404, never leaked;
- preview is a pure dry-run (no run rows, no outbox rows);
- manual trigger honors the idempotency key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

from backend.api.routers import worker_screeners as screeners_router  # noqa: E402
from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.notifications.repository import Delivery  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    Base,
    SqlAlchemyWorkflowRepository,
    Workflow,
    WorkflowRevision,
)
from backend.workflows.screener_repository import (  # noqa: E402
    ScreenerRun,
    ScreenerRunRepository,
)

RAW_TOKEN = "worker-secret-token"
HEADERS = {"Authorization": f"Bearer {RAW_TOKEN}"}
SC = "/api/worker/screeners"

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


class _StubWorkerTokenRepository:
    def __init__(self, token: WorkerToken, *, raw_token: str = RAW_TOKEN):
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


def _token(actions=None, *, account_scope="kite:paper-a") -> WorkerToken:
    return WorkerToken(
        token_id="worker-1",
        name="test-worker",
        account_scope=account_scope,
        allowed_modes=["paper", "dry_run"],
        allowed_actions=sorted(DEFAULT_WORKER_ACTIONS if actions is None else actions),
        allowed_templates=[],
    )


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


def _screener_doc(name="scr"):
    return {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "universe": {"union": [{"universe": "u"}]},
        "stages": [
            {
                "id": "scan",
                "type": "filter",
                "clock": "candle_close",
                "timeframe": "1d",
                "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]},
            }
        ],
        "alerts": [],
        "screener": {"schedule": {"every": "1d", "at": "session_close"}},
    }


def _seed_screener(session_factory, owner_id, name, *, activate=True):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(_screener_doc(name)))
    workflow, revision = repo.create_workflow(
        owner_id, name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    if activate:
        repo.activate_revision(workflow.id, revision.id)
    return workflow


def _seed_run(session_factory, workflow, *, owner_id="kite:paper-a", bucket=T0, status="complete", members=None):
    from backend.workflows.repository import WorkflowRevision
    from sqlalchemy import select

    session = session_factory()
    try:
        revision_id = session.execute(
            select(WorkflowRevision.id).where(WorkflowRevision.workflow_id == workflow.id)
        ).scalars().first()
    finally:
        session.close()
    repo = ScreenerRunRepository(session_factory)
    run = repo.claim_run(
        owner_id=owner_id,
        workflow_id=workflow.id,
        revision_id=revision_id,
        occurrence_key=f"{workflow.id}:{int(bucket.timestamp())}",
        scheduled_for=bucket,
        lease_owner="w",
        lease_ttl_s=300,
        now=bucket,
    )
    repo.finalize_run(
        run.id, "w",
        status=status,
        as_of=bucket,
        coverage={"expected": len(members or []), "complete": status == "complete"},
        data_freshness={"as_of": bucket.isoformat()},
        members=members or [],
        now=bucket,
    )
    return run


def _client(session_factory, token):
    app = FastAPI()
    app.state.alerts_session_factory = session_factory
    app.include_router(screeners_router.router, prefix="/api")
    app.state.algo_worker_repository = _StubWorkerTokenRepository(token)
    return TestClient(app)


def test_production_mount_keeps_screeners_under_worker_boundary(session_factory):
    app = FastAPI()
    app.include_router(screeners_router.router, prefix="/api")
    paths = {
        getattr(route, "path", "")
        for route in app.routes
        if "/screeners" in getattr(route, "path", "")
    }
    assert paths
    assert all(p.startswith("/api/worker/screeners") for p in paths)


def test_runs_list_and_detail_owned_scoped(session_factory):
    mine = _seed_screener(session_factory, "kite:paper-a", "scr")
    _seed_run(session_factory, mine, members=[
        {"instrument_key": "NSE:A", "passed": True, "rank": 1, "score": 9.0, "values": {"close": 10.0}},
        {"instrument_key": "NSE:B", "passed": False, "exclusion_reason": "condition_filter"},
    ])
    _seed_screener(session_factory, "kite:other", "scr-other")
    client = _client(session_factory, _token())

    response = client.get(f"{SC}/{mine.id}/runs", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["runs"][0]["status"] == "complete"
    assert body["runs"][0]["coverage"]["complete"] is True

    run_id = body["runs"][0]["run_id"]
    detail = client.get(f"{SC}/runs/{run_id}", headers=HEADERS)
    assert detail.status_code == 200
    members = detail.json()["members"]
    assert members[0]["instrument_key"] == "NSE:A"
    assert members[0]["rank"] == 1

    # other owner's run: 404, never leaked
    other = _seed_screener(session_factory, "kite:other", "scr2")
    other_run = _seed_run(session_factory, other, owner_id="kite:other")
    assert client.get(f"{SC}/runs/{other_run.id}", headers=HEADERS).status_code == 404
    # other owner's workflow runs list: 404
    assert client.get(f"{SC}/{other.id}/runs", headers=HEADERS).status_code == 404


def test_runs_list_requires_read_permission(session_factory):
    mine = _seed_screener(session_factory, "kite:paper-a", "scr")
    client = _client(session_factory, _token(actions=["notifications:test"]))
    response = client.get(f"{SC}/{mine.id}/runs", headers=HEADERS)
    assert response.status_code == 403


def test_manual_trigger_honors_idempotency_key(session_factory):
    # explicit instruments: no universe resolution needed for the scan
    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = _screener_doc("manual-scr")
    doc["instruments"] = [
        {"symbol": "NSE:AAA", "exchange": "NSE"},
        {"symbol": "NSE:BBB", "exchange": "NSE"},
    ]
    del doc["universe"]
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "kite:paper-a", "manual-scr", compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    mine = repo.get_workflow(workflow.id)

    class _StubPipeline:
        def evaluate(self, document, members, *, as_of, context_loader=None, member_limit=None):
            from backend.screeners.runner import MemberResult

            return {
                "members": [
                    MemberResult(instrument_key=k, matched=True, exclusion_reason=None,
                                 values={"close": 10.0}, score=5.0, rank=i, passed=True)
                    for i, k in enumerate(sorted(members), start=1)
                ],
                "coverage": {"expected": len(members), "evaluated": len(members),
                             "unavailable": 0, "unknown_conditions": 0,
                             "rank_value_missing": 0, "qualifying": len(members),
                             "complete": True},
                "data_freshness": {"as_of": as_of.isoformat()},
                "status": "complete",
            }

    class _NoUniverses:
        def latest_revision(self, *a):
            return None

    scheduler = screeners_router._scheduler.__wrapped__ if hasattr(screeners_router._scheduler, "__wrapped__") else None
    client = _client(session_factory, _token())
    from backend.screeners.scheduler import ScreenerScheduler
    from backend.screeners.runner import ScreenerPipeline
    from backend.workflows.runtime import PgCandleHistory

    class _BarHistory:
        def recent_bars(self, key, tf, limit):
            class B:
                ts = T0
                epoch_id = "h"
                open = high = low = close = 110.0
                volume = 1000.0
                final = True

            return [B(), B()]

    app_scheduler = ScreenerScheduler(
        session_factory=session_factory,
        workflow_repo=None,
        run_repo=ScreenerRunRepository(session_factory),
        pipeline=ScreenerPipeline(candle_history=_BarHistory()),
        universe_service=None,
        owner_id="api-manual",
    )
    client.app.state.screener_scheduler = app_scheduler

    first = client.post(
        f"{SC}/{mine.id}/runs", params={"idempotency_key": "op-1"}, headers=HEADERS
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "complete"
    run_id = first.json()["run_id"]
    replay = client.post(
        f"{SC}/{mine.id}/runs", params={"idempotency_key": "op-1"}, headers=HEADERS
    )
    assert replay.json()["status"] == "already_finalized"
    assert replay.json()["run_id"] == run_id

    runs = client.get(f"{SC}/{mine.id}/runs", headers=HEADERS).json()
    assert runs["total_count"] == 1  # no duplicate logical run
    assert runs["runs"][0]["coverage"]["expected"] == 2  # explicit members scanned


def test_preview_is_pure_dry_run(session_factory):
    _seed_screener(session_factory, "kite:paper-a", "scr")
    client = _client(session_factory, _token())

    from backend.screeners.runner import ScreenerPipeline

    class _History:
        def recent_bars(self, key, tf, limit):
            class B:
                ts = T0
                epoch_id = "p"
                open = high = low = close = 110.0
                volume = 1000.0
                final = True

            return [B()]

    client.app.state.alerts_engine = object()  # unused by the stub map path
    # inject a pipeline-backed preview by faking catalog token resolution:
    # simplest is to monkeypatch the lazy candle history builder
    import backend.api.routers.worker_screeners as mod

    class _FakeHistory:
        def __init__(self, *a, **k):
            self._h = _History()

        def recent_bars(self, key, tf, limit):
            return _History.recent_bars(self._h, key, tf, limit)

    original = mod._candle_history
    mod._candle_history = lambda request: _FakeHistory()
    try:
        response = client.post(
            f"{SC}/preview",
            headers=HEADERS,
            json={
                "document": _screener_doc("preview-doc"),
                "member_limit": 10,
            },
        )
    finally:
        mod._candle_history = original
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evaluation"] == "dry_run"
    # nothing persisted: no runs, no outbox rows
    with session_factory() as session:
        runs = session.query(ScreenerRun).count()
        deliveries = session.query(Delivery).count()
    assert runs == 0 and deliveries == 0


def _request_for(app):
    """A minimal Request carrying the app state `_scheduler` reads."""
    from starlette.requests import Request

    scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}
    return Request(scope)


def test_manual_and_scheduled_runs_use_the_same_history_window(session_factory):
    """A screener must evaluate identically however it was triggered.

    Regression (live): the worker's scheduler defaulted `window_bars` to 120
    while the API's manual-run scheduler defaulted to 30, so the SAME definition
    reported `partial` when the schedule ran it and `complete` when an operator
    pressed Run now. Both now take the pipeline's own default.
    """
    from backend.api.routers import worker_screeners as screeners_router
    from backend.screeners.runner import DEFAULT_WINDOW_BARS

    client = _client(session_factory, _token())
    app = client.app  # type: ignore[attr-defined]
    app.state.alerts_session_factory = session_factory
    app.state.screener_scheduler = None
    scheduler = screeners_router._scheduler(_request_for(app))
    assert scheduler.pipeline.window_bars == DEFAULT_WINDOW_BARS
    assert scheduler.pipeline.warmer is not None
    assert scheduler.pipeline.warmer.required_bars == DEFAULT_WINDOW_BARS
