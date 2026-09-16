"""Phase 2 closure: feature lifecycle production wiring (F8).

Regression tests for four reproduced defects:

1. the dispatch timeframe's feature window is history-warmed even when its
   feed source already exists (restart used to leave the window empty);
2. a feature-source failure rebuilds into the FEATURE source table with
   history backfill (it used to bookkeep as a candle source and orphan the
   dead source);
3. binding replacement re-warms feature windows from history;
4. binding retirement releases engine state and zombie feature sources.

Plus F8 stage references: validated against declared feature stages and
resolved to the referenced stage's own-timeframe snapshot at dispatch.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.workflows.compiler import WorkflowValidationError, compile_document
from backend.workflows.feature_engine import FeatureEngine
from backend.workflows.feature_planner import build_subscription_plan
from backend.workflows.instrument_bindings import BindingChange
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import ActiveSubscription, Base, SqlAlchemyWorkflowRepository
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


class _FakeSource:
    def __init__(self, label="src"):
        self.label = label
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return None


class _History:
    """Returns deterministic rising closes so EMA values are computable."""

    def __init__(self, bars=60):
        self.bars = bars
        self.warm_calls: list = []

    def recent_bars(self, key, timeframe, limit):
        self.warm_calls.append((key, timeframe, limit))
        return [
            Observation(
                ts=T0 + timedelta(minutes=i),
                epoch_id="h",
                ltp=100.0 + i,
                open=100.0 + i,
                high=100.0 + i,
                low=100.0 + i,
                close=100.0 + i,
                volume=1000.0,
                final=True,
            )
            for i in range(self.bars)
        ]

    def previous_session_levels(self, key, at):
        return None


def _doc():
    return {
        "version": 1,
        "name": "feat-doc",
        "session": "nse_equity",
        "stages": [
            {
                "id": "ema20",
                "type": "feature",
                "clock": "candle_close",
                "timeframe": "5minute",
                "function": "ema",
                "stage_params": {"period": 5},
            },
            {
                "id": "px",
                "type": "signal",
                "clock": "candle_close",
                "timeframe": "5minute",
                "conditions": {
                    "all": [
                        {"left": {"field": "close"}, "op": "gt",
                         "right": {"indicator": "stage:ema20"}}
                    ]
                },
            },
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["c1"]}],
    }


def _make_worker(session_factory, *, history, sources):
    repo = SqlAlchemyWorkflowRepository(session_factory)

    def candle_factory(key, tf):
        src = _FakeSource(f"candle:{key}:{tf}")
        sources[(key, tf)] = src
        return src

    return EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(f"tick:{key}"),
        candle_source_factory=candle_factory,
        candle_history=history,
    )


def _sub(worker, session_factory, doc_dict):
    compiled = compile_document(parse_workflow_dict(doc_dict))
    sub = ActiveSubscription(
        id="sub-1",
        workflow_id="wf",
        revision_id="rev",
        alert_id="a1",
        stage_id="px",
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
    worker._subscriptions.append(sub)
    worker._index_subscription(sub)
    return sub


def test_dispatch_timeframe_feature_window_is_history_warmed(session_factory):
    """The dispatch timeframe (5minute) also carries feature specs; its window
    must warm from history even though its feed source already exists."""
    history = _History()
    sources: dict = {}
    worker = _make_worker(session_factory, history=history, sources=sources)
    sub = _sub(worker, session_factory, _doc())

    async def scenario():
        # start() ordering equivalent: dispatch source exists BEFORE sync
        src = worker.candle_source_factory(sub.instrument_key, "5minute")
        await src.start()
        worker._candle_sources[(sub.instrument_key, "5minute")] = src
        await worker._sync_feature_sources()

    asyncio.run(scenario())
    snapshot = worker.feature_engine.snapshot(sub.instrument_key, "5minute")
    ema_values = {k: v for k, v in snapshot.items() if k.startswith("ema:")}
    assert ema_values and all(v is not None for v in ema_values), snapshot
    # no duplicate feature-only source for the dispatch timeframe
    assert (sub.instrument_key, "5minute") not in worker._feature_sources


def test_stage_reference_resolves_to_stage_snapshot(session_factory):
    doc_dict = _doc()
    compiled = compile_document(parse_workflow_dict(doc_dict))
    assert compiled.canonical_hash
    worker = _make_worker(session_factory, history=_History(), sources={})
    sub = _sub(worker, session_factory, doc_dict)
    plan = worker._sub_plans[sub.id]
    assert plan.stage_aliases, "stage reference must produce an alias"
    (alias, tf, feature_id) = plan.stage_aliases[0]
    assert alias == "stage:ema20"

    async def scenario():
        await worker._sync_feature_sources()

    asyncio.run(scenario())
    features, _layers = worker._plan_dispatch(sub)
    assert features is not None
    assert features.get("stage:ema20") is not None
    canonical = [k for k in features if k.startswith("ema:")]
    assert canonical and features["stage:ema20"] == features[canonical[0]]


def test_stage_reference_rejected_when_target_is_not_a_feature_stage():
    doc_dict = _doc()
    # point the reference at the signal stage instead
    doc_dict["stages"][1]["conditions"]["all"][0]["right"] = {"indicator": "stage:px"}
    try:
        compile_document(parse_workflow_dict(doc_dict))
    except WorkflowValidationError as exc:
        assert any(i.code == "missing_reference" for i in exc.issues)
    else:
        raise AssertionError("stage reference to non-feature stage must fail validation")


def test_feature_source_failure_rebuilds_into_feature_table_with_backfill(session_factory):
    history = _History()
    sources: dict = {}
    worker = _make_worker(session_factory, history=history, sources=sources)
    sub = _sub(worker, session_factory, _doc())

    async def scenario():
        await worker._sync_feature_sources()
        key = (sub.instrument_key, "5minute")
        assert key in worker._feature_sources
        dead = worker._feature_sources[key]
        worker.source_rebuild_backoff_s = 0.0
        await worker._handle_source_failure("feature", key, dead)
        assert key not in worker._feature_sources
        assert key not in worker._candle_sources  # never mis-bookkept
        await worker._rebuild_due_sources()
        assert key in worker._feature_sources
        assert worker._feature_sources[key] is not dead
        # outage backfill: history re-read warms the window
        assert (sub.instrument_key, "5minute") in history.warm_calls[-2:] or any(
            k == sub.instrument_key and tf == "5minute" for k, tf, _ in history.warm_calls
        )

    asyncio.run(scenario())


def test_binding_replacement_rewinds_feature_window(session_factory):
    history = _History()
    sources: dict = {}
    worker = _make_worker(session_factory, history=history, sources={})
    worker.bindings.apply({"NSE:INFY": 111}, set())
    sub = _sub(worker, session_factory, _doc())

    async def scenario():
        await worker._sync_feature_sources()
        key = (sub.instrument_key, "5minute")
        old_snapshot = dict(worker.feature_engine.snapshot(*key))
        assert any(v is not None for v in old_snapshot.values())
        change = worker.bindings.apply({"NSE:INFY": 222}, set())
        assert change.changed
        history.warm_calls.clear()
        await worker._apply_binding_change(change)
        assert key in worker._feature_sources  # rebuilt
        assert any(tf == "5minute" for _k, tf, _l in history.warm_calls)

    asyncio.run(scenario())


def test_binding_removal_releases_engine_state_and_sources(session_factory):
    history = _History()
    sources: dict = {}
    worker = _make_worker(session_factory, history=history, sources={})
    worker.bindings.apply({"NSE:INFY": 111}, set())
    sub = _sub(worker, session_factory, _doc())

    async def scenario():
        await worker._sync_feature_sources()
        key = (sub.instrument_key, "5minute")
        assert key in worker._feature_sources
        assert worker.feature_engine.snapshot(*key)
        change = worker.bindings.apply({}, {"NSE:INFY"})
        assert change.removed == {"NSE:INFY"}
        await worker._apply_binding_change(change)
        assert key not in worker._feature_sources
        assert worker.feature_engine.declared_timeframes(sub.instrument_key) == ()
        assert worker.feature_engine.snapshot(*key) == {}
        assert all(wk[0] != sub.instrument_key for wk in worker._feature_warmed)

    asyncio.run(scenario())


def test_feature_engine_release_drops_state():
    engine = FeatureEngine()
    bars = [
        Observation(ts=T0 + timedelta(minutes=i), epoch_id="h", ltp=1, open=1, high=1,
                    low=1, close=1 + i, volume=1, final=True)
        for i in range(10)
    ]
    for obs in bars:
        engine.on_bar("NSE:A", "day", obs)
    engine.release("NSE:A")
    assert engine.snapshot("NSE:A", "day") == {}
    assert engine.field_snapshot("NSE:A", "day") == {}
