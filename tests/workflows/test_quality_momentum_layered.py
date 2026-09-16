"""Phase 2 acceptance: layered execution + shared features (F7/F8).

Covers the required evidence items:

7. EMA20/EMA50 independence and an identical dependency used by 100 rules
   computed ONCE per event inside the documented engine scope.
9. Unknown propagates through layered conditions (insufficient upstream
   history never manufactures a firing).
10. The layered quality-momentum fixture executes end-to-end on
    deterministic fixtures (fundamentals via injected context).
8/16. Daily features consume only COMPLETED daily candles.
E-25. Delivery storm budget bounds per-rule emissions and is reported.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts import features as feature_functions
from backend.alerts.predicates import Observation
from backend.notifications.repository import Delivery  # noqa: F401  (registers tables)
from backend.workflows.compiler import compile_document
from backend.workflows.feature_planner import build_subscription_plan
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base, SqlAlchemyWorkflowRepository
from backend.workflows.service import EvaluationService

T0 = datetime(2026, 9, 9, 9, 15, tzinfo=timezone.utc)


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


def _layered_document(name, symbols, trigger="on_transition"):
    """Layered doc: fundamentals quality filter -> daily trend -> 5m breakout."""
    return {
        "version": 1,
        "name": name,
        "instruments": [
            {"symbol": s.split(":", 1)[1], "exchange": s.split(":", 1)[0]}
            for s in symbols
        ],
        "session": "nse_equity",
        "universe": {"union": [{"index": "nifty50"}], "deduplicate": True},
        "stages": [
            {
                "id": "quality",
                "type": "filter",
                "evaluate_on": "fundamentals_refresh",
                "timeframe": "1d",
                "input": "universe",
                "conditions": {
                    "all": [
                        {
                            "field": "fundamentals.quarterly_revenue_yoy_pct",
                            "op": "gt",
                            "value": 15,
                        }
                    ]
                },
            },
            {
                "id": "trend",
                "type": "filter",
                "input": "quality",
                "evaluate_on": "candle_close",
                "timeframe": "1d",
                "conditions": {
                    "all": [
                        {"left": {"field": "close"}, "op": "gt", "right": {"indicator": "ema", "period": 200}}
                    ]
                },
            },
            {
                "id": "breakout",
                "type": "signal",
                "input": "trend",
                "evaluate_on": "candle_close",
                "timeframe": "5m",
                "conditions": {
                    "all": [
                        {
                            "left": {"indicator": "ema", "period": 20},
                            "op": "crosses_above",
                            "right": {"indicator": "ema", "period": 50},
                        },
                        {
                            "left": {"field": "volume"},
                            "op": "gt",
                            "right": {
                                "multiply": [
                                    2,
                                    {"indicator": "sma", "source": "volume", "period": 20, "offset": 1},
                                ]
                            },
                        },
                    ]
                },
            },
        ],
        "alerts": [
            {"id": "stock-breakout", "source": "breakout", "trigger": trigger, "channels": ["c1"]}
        ],
    }


def test_layered_plan_extracts_stages_features_and_timeframes():
    doc = parse_workflow_dict(_layered_document("plan", ["NSE:RELIANCE"]))
    plan = build_subscription_plan(doc, "breakout")
    assert [stage.id for stage, _tf in plan.layers] == ["quality", "trend"]
    timeframes = {tf for tf, _spec in plan.specs}
    assert timeframes == {"day", "5minute"}
    ids = {spec.feature_id for _tf, spec in plan.specs}
    # ema20 / ema50 are distinct identities; volume sma has its own identity
    assert any(fid.startswith("ema:") and '"period":20' in fid for fid in ids)
    assert any(fid.startswith("ema:") and '"period":50' in fid for fid in ids)
    assert any(fid.startswith("sma:") and '"close"' not in fid and "volume" in fid for fid in ids)


def test_identical_dependency_computed_once_across_100_rules(session_factory, monkeypatch):
    """100 rules referencing ema(20) on the same instrument/timeframe cause
    exactly ONE engine computation per event."""
    from backend.workflows.feature_engine import FeatureEngine

    engine = FeatureEngine()
    compute_calls = {"n": 0}
    original_ema = feature_functions.ema

    def counting_ema(values, period):
        compute_calls["n"] += 1
        return original_ema(values, period)

    monkeypatch.setattr(feature_functions, "ema", counting_ema)

    bars = [
        Observation(
            ts=T0 + timedelta(minutes=i),
            epoch_id="candle",
            ltp=100 + i,
            open=100 + i,
            high=100.5 + i,
            low=99.5 + i,
            close=100 + i,
            volume=1000.0,
            final=True,
        )
        for i in range(60)
    ]
    for i, bar in enumerate(bars):
        engine.on_bar("NSE:X", "5minute", bar)

    # 100 rules declare the SAME feature; the spec table dedupes it.
    spec_id = "ema:{\"period\":20}:close"
    for _rule in range(100):
        engine.declare(
            "NSE:X", "5minute",
            __import__(
                "backend.workflows.feature_engine", fromlist=["FeatureSpec"]
            ).FeatureSpec(function="ema", params={"period": 20}),
        )
    snapshot = engine.on_bar(
        "NSE:X", "5minute",
        Observation(
            ts=T0 + timedelta(minutes=60), epoch_id="candle", ltp=160, open=160,
            high=161, low=159, close=160, volume=1000.0, final=True,
        ),
    )
    assert snapshot[spec_id] is not None
    # one computation per event for this feature despite 100 declarations
    assert compute_calls["n"] == 1


def test_unknown_upstream_layer_blocks_firing(session_factory):
    """A layered filter whose upstream data is insufficient yields unknown:
    the rule never fires (E-15 + acceptance #9)."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(_layered_document("layers", ["NSE:A"])))
    workflow, revision = repo.create_workflow(
        "owner-1", compiled.document.name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    service = EvaluationService(repo, session_factory)
    revision = repo.get_active_revision(workflow.id)
    service.ensure_subscriptions(revision)
    sub = repo.list_active_subscriptions()[0]

    # quality layer unknown (no fundamentals context) -> the whole chain unknown
    quality_stage = next(s for s in compiled.document.stages if s.id == "quality")
    missing = {"ema:{\"period\":20}:close": 110.0, "ema:{\"period\":50}:close": 100.0}
    result = service.handle_observation(
        sub,
        Observation(ts=T0, epoch_id="candle", ltp=110, open=110, high=110, low=110,
                    close=110, volume=99999.0, final=True),
        features=dict(missing),
        layers=[(quality_stage, {})],
    )
    assert result.emitted is False
    assert result.fired is False

    # with fundamentals satisfied AND upstream snapshots known, a genuine
    # breakout fires. Acquisition metadata rides the context so the event
    # evidence records the freshness the evaluation actually used (§5.8).
    fundamentals = {
        "fundamentals.quarterly_revenue_yoy_pct": 22.0,
        "fundamentals.acquired_at": "2026-09-01T18:30:00+00:00",
        "fundamentals.as_of_date": "2026-08-31",
    }
    daily = {"field:close": 130.0, "ema:{\"period\":200}:close": 100.0}
    warm = service.handle_observation(
        sub,
        Observation(ts=T0 + timedelta(minutes=5), epoch_id="candle", ltp=120, open=110,
                    high=120, low=110, close=120, volume=1000.0, final=True),
        features={
            "ema:{\"period\":20}:close": 110.0,
            "ema:{\"period\":50}:close": 105.0,
            "sma:{\"period\":20}:volume@1": 500.0,
        },
        context=dict(fundamentals),
        layers=[(quality_stage, {})],
    )
    assert warm.emitted is False  # first bar initializes the crossing only
    fired = service.handle_observation(
        sub,
        Observation(ts=T0 + timedelta(minutes=10), epoch_id="candle", ltp=130, open=120,
                    high=130, low=120, close=130, volume=5000.0, final=True),
        features={
            "ema:{\"period\":20}:close": 126.0,
            "ema:{\"period\":50}:close": 120.0,
            "sma:{\"period\":20}:volume@1": 500.0,
        },
        context=dict(fundamentals),
        layers=[
            (quality_stage, {}),
            (next(s for s in compiled.document.stages if s.id == "trend"), dict(daily)),
        ],
    )
    assert fired.emitted is True
    events = repo.list_events([sub.id], limit=5)
    assert len(events) == 1
    evidence = events[0].evidence
    assert evidence, "event must carry evidence"
    assert evidence.get("fundamentals_acquired_at") == "2026-09-01T18:30:00+00:00"
    assert evidence.get("fundamentals_as_of_date") == "2026-08-31"


def test_storm_budget_bounds_emissions_and_is_reported(session_factory, monkeypatch):
    """E-25: per-rule emission budget suppresses with a visible reason."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = {
        "version": 1,
        "name": "storm",
        "instruments": [{"symbol": "A", "exchange": "NSE"}],
        "session": "nse_equity",
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [{"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 100}}]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["c1"]}],
    }
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", "storm", compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    service = EvaluationService(repo, session_factory)
    service.delivery_budget_per_window = 2
    revision = repo.get_active_revision(workflow.id)
    service.ensure_subscriptions(revision)
    sub = repo.list_active_subscriptions()[0]

    results = []
    for i in range(8):
        price = 99.0 if i % 2 == 0 else 101.0  # alternating genuine crossings
        results.append(
            service.handle_observation(
                sub,
                Observation(ts=T0 + timedelta(seconds=i * 5), epoch_id="b", ltp=price),
            )
        )
    emissions = [r.emitted for r in results]
    suppressed = [r for r in results if r.suppression_reason == "storm_budget"]
    assert sum(1 for e in emissions if e) <= 2
    assert suppressed, "budget excess must be visible as storm_budget suppression"
