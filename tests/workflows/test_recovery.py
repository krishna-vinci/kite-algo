"""Recovery + runtime semantics for the evaluation service and worker (Task 7).

Covers spec v2 §4 F2/F3 acceptance and §6 E-5/E-6/E-7/E-9 at the
service/worker boundary using an in-memory SQLite database and fake feed
sources: no network, no redis, no postgres.

Scenarios:
- activation creates alert_subscriptions per (alert, instrument), idempotently
- ltp crossing fires exactly once with a pending delivery for the channel id
- ltp restart (new epoch) never fires (fresh state, E-5/E-9)
- candle gap declares a new epoch and never fires from the stale bar (E-6)
- duplicate completed candle produces exactly one signal event (E-7)
- checkpoint CAS conflict rolls back the whole evaluation transaction (E-3)
- worker warmup: insufficient history stays silent; warmed state lets the
  first live bar fire (F2 warmup-before-live)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.repository import Delivery, SqlAlchemyNotificationRepository
from backend.workflows.compiler import compile_document
from backend.workflows.models import (
    AlertSpec,
    Condition,
    InstrumentRef,
    Operand,
    Stage,
    WorkflowDocument,
)
from backend.workflows.parser import parse_workflow_yaml
from backend.workflows.repository import (
    ActiveSubscription,
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    WorkflowRevision,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "workflows" / "basic-price.yaml"

T0 = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeSource:
    """TickSource protocol double: queued observations, never blocks."""

    def __init__(self, observations=None):
        self.queue = list(observations or [])

    async def start(self):
        return None

    async def next_observation(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    async def stop(self):
        return None


class FakeHistory:
    """CandleHistory protocol double keyed by (instrument_key, timeframe)."""

    def __init__(self, bars=None):
        self.bars = dict(bars or {})

    def recent_bars(self, instrument_key, timeframe, limit):
        bars = self.bars.get((instrument_key, timeframe), [])
        return list(bars[-limit:])


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


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
def notif_repo(session_factory):
    return SqlAlchemyNotificationRepository(session_factory)


@pytest.fixture()
def channel_id(notif_repo):
    channel = notif_repo.upsert_channel(
        "owner-1",
        "telegram_primary",
        "telegram",
        {"chat_id": "12345"},
        secret_env="TELEGRAM_BOT_TOKEN",
    )
    return channel.id


def _resolver(notif_repo):
    """Channel-name -> channel-id resolver as the worker builds it."""

    def resolve(owner_id, names):
        return {
            c.name: c.id for c in notif_repo.list_channels(owner_id) if c.enabled
        }

    return resolve


def _service(session_factory, repo, notif_repo, **kwargs):
    from backend.workflows.service import EvaluationService

    return EvaluationService(
        repo,
        session_factory,
        channel_resolver=_resolver(notif_repo),
        **kwargs,
    )


def _activate(repo, doc: WorkflowDocument):
    compiled = compile_document(doc)
    workflow, revision = repo.create_workflow(
        "owner-1",
        doc.name,
        compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    return workflow, revision


def _fixture_document() -> WorkflowDocument:
    return parse_workflow_yaml(FIXTURE.read_text())


def _candle_document(level: float = 100.0, trigger: str = "once") -> WorkflowDocument:
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
        alerts=(AlertSpec(id="cross", source="bar", trigger=trigger, channels=("telegram_primary",)),),
    )


def _tick_obs(epoch: str, ltp: float, ts: datetime = T0) -> Observation:
    return Observation(ts=ts, epoch_id=epoch, ltp=ltp)


def _bar(minutes: float, close: float, epoch: str = "candle") -> Observation:
    from datetime import timedelta

    ts = T0 + timedelta(minutes=minutes)
    return Observation(
        ts=ts,
        epoch_id=epoch,
        ltp=close,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
        final=True,
    )


def _single_sub(repo) -> ActiveSubscription:
    subs = repo.list_active_subscriptions()
    assert len(subs) == 1
    return subs[0]


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def _deliveries(session_factory):
    with session_factory() as session:
        return list(session.execute(select(Delivery)).scalars().all())


def _subscription_rows(session_factory):
    from backend.workflows.repository import AlertSubscription

    with session_factory() as session:
        return list(session.execute(select(AlertSubscription)).scalars().all())


# ---------------------------------------------------------------------------
# 1 + 8: activation -> ensure_subscriptions, idempotent
# ---------------------------------------------------------------------------


def test_activation_creates_subscriptions_from_fixture(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    doc = _fixture_document()
    workflow, revision = _activate(repo, doc)

    created = service.ensure_subscriptions(revision)
    assert created == 1

    rows = _subscription_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.revision_id == revision.id
    assert row.alert_id == "breakout"
    assert row.stage_id == "px"
    assert row.trigger == "once"
    assert row.instrument_symbol == "RELIANCE"
    assert row.instrument_exchange == "NSE"
    assert row.instrument_key == "NSE:RELIANCE"
    assert row.state == "active"
    assert row.config["channels"] == ["telegram_primary"]

    subs = repo.list_active_subscriptions()
    assert len(subs) == 1
    assert subs[0].owner_id == "owner-1"
    assert subs[0].workflow_id == workflow.id


def test_ensure_subscriptions_is_idempotent(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    doc = _candle_document()
    _workflow, revision = _activate(repo, doc)

    assert service.ensure_subscriptions(revision) == 1
    assert service.ensure_subscriptions(revision) == 0
    assert service.ensure_subscriptions(revision) == 0

    rows = _subscription_rows(session_factory)
    keys = {(r.alert_id, r.instrument_key) for r in rows}
    assert keys == {("cross", "NSE:RELIANCE")}
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 2: ltp crossing fires once
# ---------------------------------------------------------------------------


def test_ltp_crossing_fires_once_with_pending_delivery(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    below = service.handle_observation(sub, _tick_obs("boot-1", 2999.0))
    assert (below.fired, below.emitted, below.rule_completed) == (False, False, False)
    assert _events(session_factory) == []

    cross = service.handle_observation(sub, _tick_obs("boot-1", 3001.0, T0.replace(minute=1)))
    assert cross.fired is True
    assert cross.emitted is True
    assert cross.suppression_reason is None
    assert cross.rule_completed is True

    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].evidence["level"] == 3000.0
    assert events[0].evidence["epoch_id"] == "boot-1"
    assert events[0].evidence["stage_id"] == "px"

    deliveries = _deliveries(session_factory)
    assert len(deliveries) == 1
    assert deliveries[0].event_id == events[0].id
    assert deliveries[0].channel_id == channel_id  # resolved from the channel NAME
    assert deliveries[0].status == "pending"

    # trigger "once" completed the rule: the subscription is marked completed
    rows = _subscription_rows(session_factory)
    assert rows[0].state == "completed"

    after = service.handle_observation(sub, _tick_obs("boot-1", 3002.0, T0.replace(minute=2)))
    assert (after.fired, after.emitted) == (False, False)
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1


# ---------------------------------------------------------------------------
# 3: ltp restart / new epoch never fires
# ---------------------------------------------------------------------------


def test_ltp_restart_new_epoch_never_fires(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # establish the epoch below the level
    first = service.handle_observation(sub, _tick_obs("boot-1", 2995.0))
    assert first.emitted is False

    # worker restart: a new epoch id arrives; even far above the level the
    # first observation only initializes (fresh state, no phantom crossing).
    restarted = service.handle_observation(sub, _tick_obs("boot-2", 3005.0, T0.replace(minute=5)))
    assert restarted.fired is False
    assert restarted.emitted is False
    assert restarted.suppression_reason == "already_true_at_activation"
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []


# ---------------------------------------------------------------------------
# 4: candle gap declares a new epoch, stale bar never fires
# ---------------------------------------------------------------------------


def test_candle_gap_opens_new_epoch_without_firing(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _candle_document(level=100.0))
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    assert service.handle_observation(sub, _bar(0, 95.0)).emitted is False
    assert service.handle_observation(sub, _bar(1, 99.0)).emitted is False

    # 10:01 -> 10:09 is an 8-minute jump on a 1-minute timeframe (> 2.5 bars):
    # gap detected, fresh state, the stale 101 bar must not fire.
    stale = service.handle_observation(sub, _bar(9, 101.0))
    assert stale.fired is False
    assert stale.emitted is False
    assert stale.suppression_reason == "feed_gap"
    assert _events(session_factory) == []

    # continuity restarts from the stale bar: 102 above 101 is no crossing
    after = service.handle_observation(sub, _bar(10, 102.0))
    assert after.fired is False
    assert after.emitted is False
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []


# ---------------------------------------------------------------------------
# 5: duplicate completed candle -> exactly one event
# ---------------------------------------------------------------------------


def test_duplicate_final_candle_creates_single_event(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    workflow, revision = _activate(repo, _candle_document(level=100.0))
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    service.handle_observation(sub, _bar(0, 95.0))
    service.handle_observation(sub, _bar(1, 99.0))
    fired = service.handle_observation(sub, _bar(2, 101.0))
    assert fired.emitted is True
    assert len(_events(session_factory)) == 1

    # the feed re-delivers the same completed bar (same ts, same epoch):
    # the predicate does not re-fire (prev == cur), so no second event.
    duplicate = service.handle_observation(sub, _bar(2, 101.0))
    assert duplicate.emitted is False
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1

    # E-2 loser: another worker already committed the occurrence key for a
    # fresh crossing (bar ts 10:04). This worker's evaluation must detect the
    # collision at commit time, roll everything back and skip without crash.
    loser_ts = _bar(4, 101.0).ts
    occurrence = f"{workflow.id}:cross:NSE:RELIANCE:candle:{loser_ts.isoformat()}"
    with session_factory() as session:
        session.add(
            SignalEvent(
                subscription_id=sub.id,
                occurrence_key=occurrence,
                fired_at=loser_ts,
                evidence={"seeded": "racing-writer"},
                created_at=loser_ts,
            )
        )
        session.commit()

    # rewind candle state to just below the level so the crossing genuinely
    # fires in-process (as if this worker had never seen the other's commit)
    _state, owner_epoch = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    repo.save_checkpoint(
        sub.id,
        sub.instrument_key,
        "candle",
        {"initialized": True, "epoch_id": "candle", "prev": 99.0},
        owner_epoch,
    )

    loser = service.handle_observation(sub, _bar(4, 101.0))
    assert loser.fired is True
    assert loser.emitted is False
    assert loser.suppression_reason == "duplicate_occurrence"
    # still only the original event + the racing writer's; no third event and
    # no delivery for the loser's rolled-back commit
    assert len(_events(session_factory)) == 2
    assert len(_deliveries(session_factory)) == 1
    rolled_back_state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    assert rolled_back_state["prev"] == 99.0  # loser's checkpoint write rolled back


# ---------------------------------------------------------------------------
# 6: checkpoint CAS conflict rolls back event + checkpoint
# ---------------------------------------------------------------------------


class _LeaseRacerRepo(SqlAlchemyWorkflowRepository):
    """Repository double that moves owner_epoch just before the service saves."""

    def __init__(self, session_factory, race: dict):
        super().__init__(session_factory)
        self._race = race  # {"armed": bool}

    def save_checkpoint(self, subscription_id, instrument_key, epoch_id, state, expected_owner_epoch, *, db=None, now=None):
        if self._race.get("armed") and db is not None:
            from sqlalchemy import update

            from backend.workflows.repository import EvaluationCheckpoint

            db.execute(
                update(EvaluationCheckpoint)
                .where(
                    EvaluationCheckpoint.subscription_id == subscription_id,
                    EvaluationCheckpoint.instrument_key == instrument_key,
                    EvaluationCheckpoint.epoch_id == epoch_id,
                )
                .values(owner_epoch=99)
            )
            db.flush()
            self._race["armed"] = False
        return super().save_checkpoint(
            subscription_id, instrument_key, epoch_id, state, expected_owner_epoch, db=db, now=now
        )


def test_lease_conflict_rolls_back_event_and_checkpoint(session_factory, notif_repo, channel_id):
    from backend.workflows.repository import EvaluationCheckpoint

    repo = SqlAlchemyWorkflowRepository(session_factory)
    racer = _LeaseRacerRepo(session_factory, {"armed": False})
    service = _service(session_factory, racer, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # seed a checkpoint as if the epoch had already seen ltp 2999
    repo.save_checkpoint(
        sub.id, sub.instrument_key, "boot-1",
        {"initialized": True, "epoch_id": "boot-1", "prev": 2999.0},
        0,
    )
    assert repo.load_checkpoint(sub.id, sub.instrument_key, "boot-1")[1] == 1

    # the losing worker evaluates a real crossing, but the lease moved on:
    # the whole transaction (event + deliveries + checkpoint) must roll back.
    racer._race["armed"] = True
    result = service.handle_observation(sub, _tick_obs("boot-1", 3001.0, T0.replace(minute=1)))
    assert result.emitted is False
    assert result.suppression_reason == "lease_lost"

    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []

    state, owner_epoch = repo.load_checkpoint(sub.id, sub.instrument_key, "boot-1")
    assert owner_epoch == 1  # the racing bump was rolled back too
    assert state["prev"] == 2999.0
    assert state.get("fired_once") is None


# ---------------------------------------------------------------------------
# 7: worker warmup before live
# ---------------------------------------------------------------------------


def _make_worker(session_factory, repo, notif_repo, history, tick_sources, candle_sources):
    from backend.workflows.runtime import EvaluationWorker

    return EvaluationWorker(
        repo,
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda instrument_key: tick_sources.setdefault(instrument_key, FakeSource()),
        candle_source_factory=lambda instrument_key, timeframe: candle_sources.setdefault(
            (instrument_key, timeframe), FakeSource()
        ),
        candle_history=history,
        poll_interval_s=0.01,
    )


def test_worker_warmup_insufficient_history_does_not_fire(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = _candle_document(level=100.0)
    _activate(repo, doc)

    # no usable history at all: warmup stays silent (E-15: rule is unknown,
    # no fire, no error), and a lone live bar must not manufacture a crossing
    history = FakeHistory({("NSE:RELIANCE", "minute"): []})
    tick_sources, candle_sources = {}, {}
    worker = _make_worker(session_factory, repo, notif_repo, history, tick_sources, candle_sources)

    asyncio.run(worker.start())

    # start() created the subscription rows; nothing to replay
    assert len(_subscription_rows(session_factory)) == 1
    assert worker.health["warmups"] == 0
    assert worker.health["evaluations"] == 0
    assert worker.health["emitted"] == 0
    assert _events(session_factory) == []

    # sources for the candle group exist; a lone live bar cannot fire either
    assert ("NSE:RELIANCE", "minute") in candle_sources
    candle_sources[("NSE:RELIANCE", "minute")].queue.append(_bar(1, 105.0))
    asyncio.run(worker.poll_once())
    assert _events(session_factory) == []  # no prev: initialize, never fire
    assert worker.health["evaluations"] == 1
    assert worker.health["suppressed"]["already_true_at_activation"] == 1

    asyncio.run(worker.stop())


def test_worker_warmup_then_live_candle_fires_once(session_factory, notif_repo, channel_id):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, _candle_document(level=100.0))

    history = FakeHistory(
        {("NSE:RELIANCE", "minute"): [_bar(0, 95.0), _bar(1, 99.0)]}
    )
    tick_sources, candle_sources = {}, {}
    worker = _make_worker(session_factory, repo, notif_repo, history, tick_sources, candle_sources)

    asyncio.run(worker.start())
    assert worker.health["warmups"] == 2
    assert _events(session_factory) == []  # warmup establishes prev, fires nothing

    source = candle_sources[("NSE:RELIANCE", "minute")]
    source.queue.append(_bar(2, 101.0))
    assert asyncio.run(worker.poll_once()) is True

    events = _events(session_factory)
    assert len(events) == 1
    deliveries = _deliveries(session_factory)
    assert len(deliveries) == 1
    assert deliveries[0].channel_id == channel_id
    assert deliveries[0].status == "pending"

    assert worker.health["emitted"] == 1
    assert worker.health["evaluations"] == 3  # 2 warmup bars + 1 live bar
    assert worker.health["last_evaluated_at"] is not None

    # a quiet poll reports no progress
    assert asyncio.run(worker.poll_once()) is False

    asyncio.run(worker.stop())


# ---------------------------------------------------------------------------
# entry import safety
# ---------------------------------------------------------------------------


def test_worker_entry_imports_cleanly_and_parses_tokens(monkeypatch):
    import sys

    for name in [m for m in list(sys.modules) if m.startswith("backend.workflows.worker_entry")]:
        del sys.modules[name]
    import backend.workflows.worker_entry as entry

    assert callable(entry.main)

    monkeypatch.delenv("ALERTS_INSTRUMENT_TOKENS", raising=False)
    assert entry.build_instrument_tokens() == {}

    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKENS", json.dumps({"NSE:RELIANCE": 738561}))
    assert entry.build_instrument_tokens() == {"NSE:RELIANCE": 738561}

    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKENS", "not-json")
    assert entry.build_instrument_tokens() == {}
