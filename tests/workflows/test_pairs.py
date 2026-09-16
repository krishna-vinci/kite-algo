"""Cross-instrument pair computation (Phase 4 F10).

The rules that stop an unavailable ratio from being reported as a valid one:
head alignment, lookback endpoint alignment, freshness, zero denominators, and
same-period returns when a leg has a gap.
"""

from __future__ import annotations

import pytest

from backend.alerts.predicates import pair_operand_id
from backend.workflows import registry
from backend.workflows.models import Operand
from backend.workflows.pairs import PairResult, collect_pair_operands, resolve_pair
from backend.workflows.parser import parse_workflow_dict

from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


class _Bar:
    def __init__(self, ts, close):
        self.ts = ts
        self.close = close


class _History:
    """Minimal candle history: ``{instrument: {ts: close}}``."""

    def __init__(self, series):
        self.series = series

    def recent_bars(self, instrument_key, timeframe, limit):
        bars = sorted(self.series.get(instrument_key, {}).items())[-limit:]
        return [_Bar(ts, close) for ts, close in bars]


def _series(values, *, start=T0, step=STEP, skip=()):
    out = {}
    ts = start
    for index, value in enumerate(values):
        if index not in skip:
            out[start + index * step] = value
        ts += step
    return out


def _operand(kind="pair_ratio", **params):
    return Operand(kind="pair", name=kind, params=params)


def _grid(count, base=100.0):
    return [base + index for index in range(count)]


# ---------------------------------------------------------------------------
# pair_ratio
# ---------------------------------------------------------------------------


def test_pair_ratio_uses_the_same_completed_bar():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        "NSE:B": _series([50, 50, 50, 50, 100]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    assert result.value == pytest.approx(110 / 100)
    assert result.head_ts == head


def test_pair_ratio_reports_misaligned_heads():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        # B has no bar at the newest grid point: heads differ by one bar.
        "NSE:B": _series([50, 50, 50, 50, 60], skip=(4,)),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_misaligned"


def test_pair_ratio_tolerates_the_configured_skew():
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102, 103, 110]),
        "NSE:B": _series([50, 50, 50, 60, 70], skip=(4,)),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B", max_skew_bars=1),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    assert result.value == pytest.approx(110 / 60)
    assert result.skew_bars == 1


def test_pair_ratio_zero_denominator_is_unknown():
    head = T0 + 2 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102]),
        "NSE:B": _series([50, 50, 0]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_zero_denominator"


def test_pair_ratio_missing_leg_is_unknown():
    history = _History({"NSE:A": _series([100, 101, 102])})
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=T0 + 2 * STEP,
    )
    assert result.reason == "pair_missing"


def test_pair_never_consumes_a_future_bar():
    history = _History({
        "NSE:A": _series([100, 101, 102, 103]),
        "NSE:B": _series([50, 50, 50, 50]),
    })
    # Cutoff at the third bar: the fourth must be invisible.
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=T0 + 2 * STEP,
    )
    assert result.value == pytest.approx(102 / 50)


def test_pair_stale_head_is_unknown():
    head = T0 + 40 * STEP
    history = _History({
        "NSE:A": _series([100, 101, 102]),
        "NSE:B": _series([50, 51, 52]),
    })
    result = resolve_pair(
        _operand(instrument="NSE:A", reference="NSE:B"),
        history=history, timeframe="15minute", cutoff=head,
        bar_age_limit_s=2 * registry.timeframe_seconds("15minute"),
    )
    assert result.value is None
    assert result.reason == "pair_stale"


# ---------------------------------------------------------------------------
# relative_strength
# ---------------------------------------------------------------------------


def test_relative_strength_compares_the_same_period():
    head = T0 + 3 * STEP
    history = _History({
        # A: +10% over two bars; B: +2% over the same two bars.
        "NSE:A": _series([100, 105, 108, 110]),
        "NSE:B": _series([200, 200, 202, 204]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    # head is index 3, anchor is head - 2 bars = index 1
    assert result.value == pytest.approx((110 / 105 - 1) * 100 - (204 / 200 - 1) * 100)
    assert result.anchor_ts == head - 2 * STEP


def test_relative_strength_rejects_mismatched_lookback_endpoints():
    """A leg missing a bar at the anchor is rejected, not silently shortened."""
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 108, 110, 112]),
        # B is missing the anchor bar (index 2) but present at the head.
        "NSE:B": _series([200, 200, 202, 204, 206], skip=(2,)),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_lookback_misaligned"


def test_interior_gaps_do_not_shift_either_window():
    """The formula reads only the endpoints, so an interior gap is harmless."""
    head = T0 + 4 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 108, 110, 112]),
        "NSE:B": _series([200, 200, 202, 204, 206], skip=(3,)),  # interior gap
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.reason is None
    # head is index 4; the anchor is head - 2 bars = index 2 on BOTH legs,
    # even though B has an interior gap at index 3.
    assert result.value == pytest.approx((112 / 108 - 1) * 100 - (206 / 202 - 1) * 100)


def test_relative_strength_insufficient_history_is_unknown():
    history = _History({
        "NSE:A": _series([100, 101]),
        "NSE:B": _series([50, 51]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5),
        history=history, timeframe="15minute", cutoff=T0 + STEP,
    )
    assert result.value is None
    assert result.reason in ("pair_lookback_misaligned", "pair_insufficient_history")


def test_relative_strength_zero_anchor_is_unknown():
    head = T0 + 2 * STEP
    history = _History({
        "NSE:A": _series([100, 105, 110]),
        "NSE:B": _series([0, 200, 204]),
    })
    result = resolve_pair(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=2),
        history=history, timeframe="15minute", cutoff=head,
    )
    assert result.value is None
    assert result.reason == "pair_zero_denominator"


# ---------------------------------------------------------------------------
# identity and collection
# ---------------------------------------------------------------------------


def test_pair_identity_separates_legs_lookback_and_skew():
    base = _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5)
    other_leg = _operand("relative_strength", instrument="NSE:A", reference="NSE:C", lookback=5)
    other_lookback = _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=6)
    other_skew = _operand(
        "relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5, max_skew_bars=1
    )
    identities = {
        pair_operand_id(base),
        pair_operand_id(other_leg),
        pair_operand_id(other_lookback),
        pair_operand_id(other_skew),
    }
    assert len(identities) == 4
    assert pair_operand_id(base) == pair_operand_id(
        _operand("relative_strength", instrument="NSE:A", reference="NSE:B", lookback=5)
    )


def test_collect_pair_operands_finds_nested_references():
    doc = parse_workflow_dict({
        "version": 1, "name": "pairs", "session": "nse_equity",
        "instruments": ["NSE:A"],
        "stages": [{
            "id": "s", "type": "signal", "clock": "candle_close", "timeframe": "15minute",
            "conditions": {"all": [
                {"left": {"pair_ratio": {"instrument": "NSE:A", "reference": "NSE:B"}},
                 "op": "gt", "right": {"value": 1.0}},
            ], "any": [
                {"left": {"relative_strength": {"instrument": "NSE:A", "reference": "NSE:B",
                                                "lookback": 5}},
                 "op": "gt", "right": {"value": 1.0}},
            ]},
        }],
        "alerts": [],
    })
    stage = doc.stages[0]
    operands = collect_pair_operands(
        list(stage.conditions) + list(stage.any_conditions) + list(stage.not_conditions)
    )
    assert len(operands) == 2
    assert {operand.name for operand in operands} == {"pair_ratio", "relative_strength"}


# ---------------------------------------------------------------------------
# worker dispatch path
# ---------------------------------------------------------------------------


def _pair_workflow_document():
    """A stage whose only condition reads a cross-instrument pair ratio."""
    return {
        "version": 1,
        "name": "pair-dispatch",
        "session": "nse_equity",
        "instruments": ["NSE:AAA", "NSE:BBB"],
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "candle_close",
                "timeframe": "minute",
                "conditions": {
                    "all": [
                        {
                            "left": {"pair_ratio": {"instrument": "NSE:AAA",
                                                    "reference": "NSE:BBB"}},
                            "op": "crosses_above",
                            "right": {"value": 1.0},
                        }
                    ]
                },
            }
        ],
        "alerts": [
            {"id": "a", "source": "px", "trigger": "on_transition", "channels": []}
        ],
    }


class _FullBar:
    """A complete bar: feature warmup reads OHLCV, not just the close."""

    def __init__(self, ts, close):
        self.ts = ts
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = 1.0
        self.oi = None


class _StubHistory:
    """Bars keyed by instrument, as ``PgCandleHistory`` presents them."""

    def __init__(self, series):
        self.series = series

    def recent_bars(self, instrument_key, timeframe, limit):
        bars = sorted(self.series.get(instrument_key, {}).items())[-limit:]
        return [_FullBar(ts, close) for ts, close in bars]

    def previous_session_levels(self, instrument_key, at):
        return None


class _FakeSource:
    def __init__(self):
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return None


@pytest.fixture()
def session_factory():
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.notifications.repository import Delivery  # noqa: F401
    from backend.workflows import advanced_repository  # noqa: F401
    from backend.workflows.repository import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_worker_dispatch_resolves_a_pair_operand(session_factory):
    """A pair stage must survive worker startup and dispatch.

    Regression: ``_resolve_pairs`` called ``pair_operand_id``, which
    ``runtime.py`` never imported, so the FIRST dispatch of a stage with a pair
    operand raised ``NameError``. That first dispatch happens during STARTUP
    warmup, so activating a single pair workflow crash-looped the whole worker
    and stopped EVERY alert — not just the pair one. Component tests passed
    throughout because they call ``resolve_pair`` directly and never went
    through dispatch.
    """
    import asyncio

    from backend.workflows.compiler import compile_document
    from backend.workflows.repository import SqlAlchemyWorkflowRepository
    from backend.workflows.runtime import EvaluationWorker
    from backend.workflows.service import EvaluationService

    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = _pair_workflow_document()
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", doc["name"], compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)

    # AAA/BBB = 90/100 = 0.90 on the completed bar being dispatched.
    history = _StubHistory({
        "NSE:AAA": {T0: 90.0},
        "NSE:BBB": {T0: 100.0},
    })
    worker = EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=history,
        poll_interval_s=0.01,
        instrument_resolver=lambda keys: ({k: 1 for k in keys}, set()),
        renewal=None,
        owner_id="worker-1",
    )

    # This is the call that used to raise NameError during warmup.
    asyncio.run(worker.start())

    from datetime import timezone as _tz

    from backend.alerts.predicates import Observation

    sub = next(s for s in worker._subscriptions if s.stage_id == "px")
    obs = Observation(
        ts=T0, epoch_id="candle", ltp=90.0, open=90.0, high=90.0, low=90.0,
        close=90.0, volume=1.0, final=True,
    )
    worker._dispatch(sub, obs)

    # The pair resolved: `crosses_above` records `prev` ONLY when its left
    # operand produced a number, so an unresolved (unknown) pair would leave
    # the condition's substate empty — the same way it does for a missing
    # external value.
    from sqlalchemy import text

    with session_factory() as session:
        rows = session.execute(text("select state from evaluation_checkpoints")).scalars().all()
    assert rows, "dispatch must checkpoint"
    import json as _json

    state = _json.loads(rows[0]) if isinstance(rows[0], str) else rows[0]
    conds = state.get("conds") or {}
    # The canonical key for a pair operand carries the operand's identity, so
    # this also pins that identity through the dispatch path.
    keys = [k for k in conds if k.startswith("crosses_above:indicator:pair_ratio:")]
    assert keys, (
        f"the pair condition must have been evaluated, not unknown; conds={conds}"
    )
    assert "NSE:AAA" in keys[0] and "NSE:BBB" in keys[0]
    assert conds[keys[0]].get("prev") == pytest.approx(0.9), (
        "prev must be the computed pair ratio AAA/BBB"
    )
    asyncio.run(worker.stop())
