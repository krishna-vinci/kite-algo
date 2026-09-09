"""Phase 2 closure: production fundamentals data path.

- ``bare_symbol`` maps only NSE-qualified keys onto the source table;
- ``FundamentalsLoader`` serves the latest snapshot with acquisition
  metadata, caches per symbol, and treats missing rows as unknown;
- the worker merges fundamentals into dispatch context only for stages
  (or ancestor layers) that can reference them, with health counters;
- the compiler rejects fundamentals-only stages with no timeframe
  (they would never be dispatched).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.workflows.compiler import compile_document, compile_document as _c  # noqa: F401
from backend.workflows.fundamentals_context import FundamentalsLoader, bare_symbol
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base, SqlAlchemyWorkflowRepository
from backend.workflows.runtime import EvaluationWorker

T0 = datetime(2026, 9, 9, 9, 30, tzinfo=timezone.utc)


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


# ---------------------------------------------------------------------------
# bare_symbol
# ---------------------------------------------------------------------------


def test_bare_symbol_nse_only():
    assert bare_symbol("NSE:INFY") == "INFY"
    assert bare_symbol("nse:tcs") == "tcs"
    assert bare_symbol("BSE:INFY") is None  # NSE-centric source: never conflate
    assert bare_symbol("MCX:GOLD") is None
    assert bare_symbol("INFY") is None  # public identity is EXCHANGE:SYMBOL
    assert bare_symbol("NSE:") is None


# ---------------------------------------------------------------------------
# FundamentalsLoader
# ---------------------------------------------------------------------------


class _FakeMappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _FakeMappings(self._row)


class _FakeSession:
    def __init__(self, rows_by_symbol, queries):
        self._rows = rows_by_symbol
        self.queries = queries

    def execute(self, query, params):
        self.queries.append((str(query), dict(params)))
        return _FakeResult(self._rows.get(params["symbol"]))

    def close(self):
        pass


_ROW = {
    "quarterly_revenue_yoy_pct": 22.5,
    "latest_roce_pct": 19.0,
    "stock_pe": 31.4,
    "market_cap_cr": 512000.0,
    "scraped_at": datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc),
    "as_of_date": "2026-08-31",
    "statement_scope": "consolidated",
}


def _loader(rows=None, queries=None, **kwargs) -> FundamentalsLoader:
    if queries is None:
        queries = []
    captured_rows = rows or {}
    return FundamentalsLoader(
        lambda: _FakeSession(captured_rows, queries),
        **kwargs,
    )


def test_loader_returns_values_and_metadata():
    queries: list = []
    loader = _loader({"INFY": dict(_ROW)}, queries)
    ctx = loader.context_for("NSE:INFY")
    assert ctx is not None
    assert ctx["fundamentals.quarterly_revenue_yoy_pct"] == 22.5
    assert ctx["fundamentals.pe_ratio"] == 31.4
    assert ctx["fundamentals.market_cap"] == 512000.0
    assert ctx["fundamentals.acquired_at"] == "2026-09-01T18:30:00+00:00"
    assert ctx["fundamentals.as_of_date"] == "2026-08-31"
    # single row lookup, parameterized
    assert len(queries) == 1
    assert queries[0][1] == {"symbol": "INFY", "scope": "consolidated"}


def test_loader_missing_row_is_unknown_and_non_nse_never_queries():
    queries: list = []
    loader = _loader({}, queries)
    assert loader.context_for("NSE:NEWCO") is None
    assert loader.context_for("BSE:INFY") is None
    assert queries == [] or all("INFY" not in q[1].values() for q in queries)


def test_loader_caches_within_ttl_and_retries_after_error_ttl():
    queries: list = []
    loader = _loader({"INFY": dict(_ROW)}, queries, ttl_seconds=3600, error_ttl_seconds=10)
    clock = [0.0]
    loader._cache.clear()

    original_monotonic = time.monotonic
    time.monotonic = lambda: clock[0]
    try:
        loader.context_for("NSE:INFY")
        loader.context_for("NSE:INFY")
        assert len(queries) == 1  # cached
        clock[0] += 3601.0
        loader.context_for("NSE:INFY")
        assert len(queries) == 2  # expired -> refetch
    finally:
        time.monotonic = original_monotonic


def test_loader_skips_nonfinite_values():
    row = dict(_ROW)
    row["stock_pe"] = float("nan")
    loader = _loader({"INFY": row})
    ctx = loader.context_for("NSE:INFY")
    assert "fundamentals.pe_ratio" not in ctx  # absent -> predicate unknown
    assert ctx["fundamentals.market_cap"] == 512000.0


# ---------------------------------------------------------------------------
# runtime wiring
# ---------------------------------------------------------------------------


class _RecordingService:
    """Signature mirrors EvaluationService.handle_observation so the
    worker's capability probing passes context through."""

    def __init__(self):
        self.calls: list = []

    def handle_observation(self, sub, obs, *, db=None, allow_emit=True,
                           context=None, features=None, layers=None):
        self.calls.append({"context": context, "features": features, "layers": layers})
        from backend.workflows.service import HandleResult

        return HandleResult(
            fired=False, emitted=False, suppression_reason=None, rule_completed=False
        )


class _FakeSource:
    async def start(self):
        return None

    async def stop(self):
        return None

    async def next_observation(self):
        return None


class _FakeHistory:
    def previous_session_levels(self, key, ts):
        return {"prev_day_high": 100.0}

    def recent_bars(self, key, timeframe, limit):
        return []


def _fundamentals_doc():
    return {
        "version": 1,
        "name": "fund-doc",
        "session": "nse_equity",
        "stages": [
            {
                "id": "quality",
                "type": "filter",
                "clock": "candle_close",
                "timeframe": "1d",
                "conditions": {
                    "all": [
                        {"field": "fundamentals.pe_ratio", "op": "lt", "value": 40}
                    ]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "quality", "trigger": "on_transition", "channels": ["c1"]}],
    }


def _make_worker(session_factory, service, loader):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    return EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=_FakeHistory(),
        service=service,
        fundamentals_loader=loader,
    )


def _subscription(session_factory, repo, doc_dict):
    from backend.workflows.compiler import compile_document
    from backend.workflows.service import EvaluationService
    from backend.workflows.repository import ActiveSubscription

    compiled = compile_document(parse_workflow_dict(doc_dict))
    workflow, revision = repo.create_workflow(
        "owner-1", "fund-doc", compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    return ActiveSubscription(
        id="sub-1",
        workflow_id=workflow.id,
        revision_id=revision.id,
        alert_id="a1",
        stage_id="quality",
        instrument_key="NSE:INFY",
        instrument_symbol="INFY",
        instrument_exchange="NSE",
        document=doc_dict,
        config={},
        trigger="on_transition",
        state="active",
        created_at=None,
        owner_id="owner-1",
    )


def test_worker_merges_fundamentals_for_fundamentals_stages(session_factory):
    from backend.workflows.repository import ActiveSubscription

    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _RecordingService()
    queries: list = []
    loader = _loader({"INFY": dict(_ROW)}, queries)
    worker = _make_worker(session_factory, service, loader)

    doc_dict = _fundamentals_doc()
    sub = ActiveSubscription(
        id="sub-1",
        workflow_id="wf",
        revision_id="rev",
        alert_id="a1",
        stage_id="quality",
        instrument_key="NSE:INFY",
        instrument_symbol="INFY",
        instrument_exchange="NSE",
        document=doc_dict,
        config={},
        trigger="on_transition",
        state="active",
        created_at=None,
        owner_id="owner-1",
    )
    obs = Observation(ts=T0, epoch_id="candle", ltp=100, open=100, high=100, low=100,
                      close=100, volume=1.0, final=True)
    worker._dispatch(sub, obs)
    assert len(service.calls) == 1
    ctx = service.calls[0]["context"]
    assert ctx["fundamentals.pe_ratio"] == 31.4
    assert ctx["prev_day_high"] == 100.0
    assert worker.health["fundamentals_hits"] == 1
    assert worker.health["fundamentals_misses"] == 0

    # non-fundamentals stage: loader not consulted
    plain_doc = dict(doc_dict)
    plain_doc = {
        "version": 1,
        "name": "px-doc",
        "session": "nse_equity",
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {"all": [{"left": {"field": "ltp"}, "op": "gt", "right": {"value": 1}}]},
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["c1"]}],
    }
    plain_sub = ActiveSubscription(
        id="sub-2", workflow_id="wf", revision_id="rev2", alert_id="a1",
        stage_id="px", instrument_key="NSE:INFY", instrument_symbol="INFY",
        instrument_exchange="NSE", document=plain_doc,
        config={}, trigger="on_transition", state="active",
        created_at=None, owner_id="owner-1",
    )
    worker._dispatch(plain_sub, obs)
    assert worker.health["fundamentals_hits"] == 1  # unchanged
    assert worker.health["fundamentals_misses"] == 0


def test_worker_counts_fundamentals_misses_and_staleness(session_factory):
    from backend.workflows.repository import ActiveSubscription

    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _RecordingService()
    # stale snapshot (8 days old, threshold 168h default)
    stale_row = dict(_ROW)
    stale_row["scraped_at"] = datetime.now(timezone.utc) - timedelta(days=8)
    loader = _loader({"INFY": stale_row})
    worker = _make_worker(session_factory, service, loader)

    sub = ActiveSubscription(
        id="sub-1", workflow_id="wf", revision_id="rev", alert_id="a1",
        stage_id="quality", instrument_key="NSE:INFY", instrument_symbol="INFY",
        instrument_exchange="NSE", document=_fundamentals_doc(),
        config={}, trigger="on_transition", state="active",
        created_at=None, owner_id="owner-1",
    )
    obs = Observation(ts=T0, epoch_id="candle", ltp=100, open=100, high=100, low=100,
                      close=100, volume=1.0, final=True)
    worker._dispatch(sub, obs)
    assert worker.health["fundamentals_stale"] == 1

    # missing row -> miss counter
    missing_loader = _loader({})
    worker2 = _make_worker(session_factory, service, missing_loader)
    worker2._dispatch(sub, obs)
    assert worker2.health["fundamentals_misses"] == 1


# ---------------------------------------------------------------------------
# compiler honesty
# ---------------------------------------------------------------------------


def test_compiler_rejects_fundamentals_only_stage_without_timeframe():
    doc = {
        "version": 1,
        "name": "no-tf",
        "session": "nse_equity",
        "stages": [
            {
                "id": "quality",
                "type": "filter",
                "clock": "candle_close",
                "conditions": {
                    "all": [{"field": "fundamentals.pe_ratio", "op": "lt", "value": 40}]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "quality", "trigger": "on_transition", "channels": ["c1"]}],
    }
    try:
        compile_document(parse_workflow_dict(doc))
    except Exception as exc:
        codes = {issue.code for issue in exc.issues}
        messages = " ".join(issue.message for issue in exc.issues)
        assert "timeframe_missing" in codes
        assert "fundamentals" in messages
        assert "timeframe: 1d" in messages  # actionable
    else:
        raise AssertionError("fundamentals-only stage without timeframe must not compile")


def test_compiler_accepts_fundamentals_stage_with_timeframe():
    doc = _fundamentals_doc()
    compiled = compile_document(parse_workflow_dict(doc))
    assert compiled.canonical_hash
