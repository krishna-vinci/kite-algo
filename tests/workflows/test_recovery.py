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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


def _cond_key(document: WorkflowDocument, stage_id: str) -> str:
    """Canonical predicate key of a stage's first condition (new shape)."""
    from backend.alerts.predicates import cond_key

    stage = next(s for s in document.stages if s.id == stage_id)
    return cond_key(stage.conditions[0])


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
    cond = next(
        c for s in _fixture_document().stages if s.id == "px" for c in s.conditions
    )
    from backend.alerts.predicates import cond_key as _ck

    cond_evidence = events[0].evidence[_ck(cond)]
    assert cond_evidence["level"] == 3000.0
    assert cond_evidence["ltp"] == 3001.0
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
    cross_key = _cond_key(_candle_document(level=100.0), "bar")
    repo.save_checkpoint(
        sub.id,
        sub.instrument_key,
        "candle",
        {
            "initialized": True,
            "epoch_id": "candle",
            "conds": {cross_key: {"prev": 99.0}},
        },
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
    # loser's checkpoint write rolled back: prev is still 99.0 in the
    # condition's partitioned sub-state
    assert rolled_back_state["conds"][cross_key]["prev"] == 99.0


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


# ---------------------------------------------------------------------------
# fault 1 + 7: entrypoint delivery task, resolver, token env
# ---------------------------------------------------------------------------


class FakeAdapter:
    """NotificationAdapter double: records sends, always accepts."""

    def __init__(self):
        self.sent = []

    async def send(self, destination, subject, body):
        self.sent.append((dict(destination), subject, body))
        from backend.notifications.adapters import DeliveryOutcome

        return DeliveryOutcome(status="accepted", detail="fake ok")


class StubEvalWorker:
    """Duck-typed worker whose run() blocks until cancelled."""

    def __init__(self):
        self.stopped = False

    async def run(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped = True
            raise


def _seed_pending_delivery(session_factory, sub, channel_id, *, when=T0):
    """Insert a signal_event + pending delivery row; returns the delivery id."""
    import uuid as _uuid

    event_id = str(_uuid.uuid4())
    delivery_id = str(_uuid.uuid4())
    with session_factory() as session:
        session.add(
            SignalEvent(
                id=event_id,
                subscription_id=sub.id,
                occurrence_key=f"seed:{delivery_id}",
                fired_at=when,
                evidence={"seeded": True},
                created_at=when,
            )
        )
        session.add(
            Delivery(
                id=delivery_id,
                event_id=event_id,
                channel_id=channel_id,
                status="pending",
                attempts=0,
            )
        )
        session.commit()
    return delivery_id


def test_subscription_loader_returns_pinned_context(session_factory, notif_repo, channel_id):
    import backend.workflows.worker_entry as entry

    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    loader = entry.build_subscription_loader(session_factory)
    context = loader(sub.id)
    assert context is not None
    assert set(context.keys()) == {
        "instrument_key", "alert_id", "message", "expires_at", "workflow_name",
    }
    assert context["instrument_key"] == "NSE:RELIANCE"
    assert context["alert_id"] == "breakout"
    assert context["workflow_name"]  # joined from workflows.name

    assert loader("no-such-subscription") is None


def test_delivery_resolver_fallback_resolves_delivery_context(session_factory, notif_repo, channel_id):
    import backend.workflows.worker_entry as entry

    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)
    delivery_id = _seed_pending_delivery(session_factory, sub, channel_id)

    loader = entry.build_subscription_loader(session_factory)
    resolver = entry.build_delivery_resolver(notif_repo, loader)
    context = resolver(delivery_id)
    assert context is not None
    assert set(context.keys()) == {
        "provider", "destination", "subject", "body", "expires_at",
    }
    assert context["provider"] == "telegram"
    # the chat id is preserved; the pinned make_resolver additionally merges
    # the channel's secret_env override into the destination
    assert context["destination"]["chat_id"] == "12345"
    assert context["subject"]
    assert context["body"]
    assert resolver("missing-delivery") is None


def test_delivery_enabled_env_kill_switch(monkeypatch):
    import backend.workflows.worker_entry as entry

    monkeypatch.delenv("ALERTS_DELIVERY_ENABLED", raising=False)
    assert entry.delivery_enabled() is True  # default on
    monkeypatch.setenv("ALERTS_DELIVERY_ENABLED", "1")
    assert entry.delivery_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("ALERTS_DELIVERY_ENABLED", off)
        assert entry.delivery_enabled() is False


def test_delivery_task_runs_and_processes_pending_outbox(session_factory, notif_repo, channel_id):
    import backend.workflows.worker_entry as entry
    from backend.notifications.worker import DeliveryWorker

    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _fixture_document())
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)
    delivery_id = _seed_pending_delivery(session_factory, sub, channel_id)

    adapter = FakeAdapter()
    loader = entry.build_subscription_loader(session_factory)
    resolver = entry.build_delivery_resolver(notif_repo, loader)
    delivery_worker = DeliveryWorker(
        notif_repo, adapter_factory=lambda provider: adapter, resolver=resolver
    )

    async def scenario():
        stop = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(0.5, stop.set_result(None))
        # make the pending row due immediately even if backoff was seeded
        results = await entry.supervise(
            StubEvalWorker(), delivery_worker, stop=stop, delivery_poll_interval_s=0.01
        )
        return results

    asyncio.run(scenario())

    assert len(adapter.sent) == 1
    rows = _deliveries(session_factory)
    assert len(rows) == 1
    assert rows[0].id == delivery_id
    assert rows[0].status == "delivered"


def test_delivery_task_disabled_by_env(monkeypatch, session_factory, notif_repo, channel_id):
    import backend.workflows.worker_entry as entry
    from backend.notifications.worker import DeliveryWorker

    monkeypatch.setenv("ALERTS_DELIVERY_ENABLED", "0")
    assert entry.delivery_enabled() is False

    delivery_worker = DeliveryWorker(notif_repo, resolver=lambda delivery_id: {})
    calls = []

    async def spy_run_forever(interval):
        calls.append(interval)

    delivery_worker.run_forever = spy_run_forever

    async def scenario():
        stop = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(0.05, stop.set_result(None))
        return await entry.supervise(
            StubEvalWorker(), delivery_worker, stop=stop, delivery_poll_interval_s=0.01
        )

    asyncio.run(scenario())
    assert calls == []  # delivery task never started


def test_alerts_instrument_tokens_warn_when_empty(monkeypatch, caplog):
    import backend.workflows.worker_entry as entry

    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKENS", json.dumps({"NSE:RELIANCE": 738561}))
    assert entry.build_instrument_tokens() == {"NSE:RELIANCE": 738561}

    # JSON array instead of object is rejected loudly
    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKENS", '["NSE:RELIANCE"]')
    assert entry.build_instrument_tokens() == {}

    # non-numeric token value is rejected
    monkeypatch.setenv("ALERTS_INSTRUMENT_TOKENS", '{"NSE:RELIANCE": "abc"}')
    assert entry.build_instrument_tokens() == {}

    with caplog.at_level("WARNING", logger="backend.workflows.worker_entry"):
        monkeypatch.delenv("ALERTS_INSTRUMENT_TOKENS", raising=False)
        entry.warn_if_no_instruments(entry.build_instrument_tokens())
        assert any("ALERTS_INSTRUMENT_TOKENS" in record.message for record in caplog.records)

    caplog.clear()
    with caplog.at_level("WARNING", logger="backend.workflows.worker_entry"):
        entry.warn_if_no_instruments({"NSE:RELIANCE": 738561})
        assert not [r for r in caplog.records if "no instruments" in r.message.lower()]


def test_build_renewal_uses_same_client_on_interval(session_factory, notif_repo):
    import backend.workflows.worker_entry as entry

    class FakeMarketClient:
        def __init__(self):
            self.calls = []

        async def set_owner_subscriptions(self, owner_id, tokens):
            self.calls.append((owner_id, dict(tokens)))
            return {}

    client = FakeMarketClient()
    renew = entry.build_renewal(client, "alerts-worker:test", {"NSE:RELIANCE": 738561})

    from backend.workflows.runtime import EvaluationWorker

    worker = EvaluationWorker(
        SqlAlchemyWorkflowRepository(session_factory),
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: FakeSource(),
        candle_source_factory=lambda ik, tf: FakeSource(),
        candle_history=FakeHistory(),
        renewal=renew,
        renewal_interval_s=0.01,
        poll_interval_s=0.01,
        health_interval_s=3600,
    )

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert len(client.calls) >= 2  # renewed repeatedly on the interval
    owner, tokens = client.calls[0]
    assert owner == "alerts-worker:test"
    assert tokens == {738561: "full"}
    assert worker.health["renewal_failures"] == 0


def test_renewal_failure_does_not_crash_worker(session_factory, notif_repo):
    import backend.workflows.worker_entry as entry

    class FailingClient:
        def __init__(self):
            self.attempts = 0

        async def set_owner_subscriptions(self, owner_id, tokens):
            self.attempts += 1
            raise RuntimeError("market-runtime down")

    renew = entry.build_renewal(FailingClient(), "alerts-worker:test", {"NSE:RELIANCE": 738561})

    from backend.workflows.runtime import EvaluationWorker

    worker = EvaluationWorker(
        SqlAlchemyWorkflowRepository(session_factory),
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: FakeSource(),
        candle_source_factory=lambda ik, tf: FakeSource(),
        candle_history=FakeHistory(),
        renewal=renew,
        renewal_interval_s=0.01,
        poll_interval_s=0.01,
        health_interval_s=3600,
    )

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.08)
        assert not task.done()  # renewal outage must not crash the loop
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert worker.health["renewal_failures"] >= 2
    assert worker.health["last_renewal_at"] is None


# ---------------------------------------------------------------------------
# fault 6: health file
# ---------------------------------------------------------------------------


def test_health_file_written_when_env_set(monkeypatch, session_factory, notif_repo, tmp_path):
    from backend.workflows.runtime import EvaluationWorker

    health_file = tmp_path / "health.json"
    monkeypatch.setenv("ALERTS_HEALTH_FILE", str(health_file))

    worker = EvaluationWorker(
        SqlAlchemyWorkflowRepository(session_factory),
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: FakeSource(),
        candle_source_factory=lambda ik, tf: FakeSource(),
        candle_history=FakeHistory(),
        health_interval_s=0.02,
        poll_interval_s=0.01,
        health_extra=lambda: {"deliveries": {"delivered": 3}},
    )

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert health_file.exists()
    snapshot = json.loads(health_file.read_text())
    for key in (
        "started_at", "last_evaluated_at", "evaluations", "emitted",
        "suppressed", "gaps", "warmups", "unresolved_channels",
        "renewal_failures", "deliveries",
    ):
        assert key in snapshot, f"health snapshot missing {key!r}: {snapshot}"
    assert snapshot["deliveries"] == {"delivered": 3}


# ---------------------------------------------------------------------------
# fault 3: subscription refresh (activation/pause without restart)
# ---------------------------------------------------------------------------


def _candle_document_named(name: str, level: float, symbol: str = "RELIANCE") -> WorkflowDocument:
    return WorkflowDocument(
        version=1,
        name=name,
        instruments=(InstrumentRef(symbol=symbol, exchange="NSE"),),
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
        alerts=(AlertSpec(id="cross", source="bar", trigger="once", channels=("telegram_primary",)),),
    )


def test_subscription_refresh_adds_and_drops_without_restart(
    session_factory, notif_repo, channel_id
):
    from backend.workflows.runtime import EvaluationWorker

    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, _candle_document_named("wf-one", level=100.0))

    history = FakeHistory(
        {
            ("NSE:RELIANCE", "minute"): [_bar(0, 95.0), _bar(1, 99.0)],
            ("NSE:TCS", "minute"): [_bar(0, 1950.0)],
        }
    )
    tick_sources, candle_sources = {}, {}
    worker = EvaluationWorker(
        repo,
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: tick_sources.setdefault(ik, FakeSource()),
        candle_source_factory=lambda ik, tf: candle_sources.setdefault((ik, tf), FakeSource()),
        candle_history=history,
        poll_interval_s=0.01,
    )
    asyncio.run(worker.start())

    subs_before = {s.id for group in worker._candle_subs.values() for s in group}
    assert len(subs_before) == 1

    # activate a second workflow while the worker is running
    _workflow2, revision2 = _activate(repo, _candle_document_named("wf-two", level=2000.0, symbol="TCS"))
    service = _service(session_factory, repo, notif_repo)
    service.ensure_subscriptions(revision2)

    summary = asyncio.run(worker.refresh_subscriptions())
    assert summary["added"] == 1
    assert summary["removed"] == 0

    subs_after = {s.id for group in worker._candle_subs.values() for s in group}
    assert len(subs_after) == 2
    # new subscription was warmed (candle rules) before live dispatch
    warmups_after_add = worker.health["warmups"]
    assert warmups_after_add == 3  # 2 bars RELIANCE + 1 bar TCS
    assert ("NSE:TCS", "minute") in worker._candle_sources

    # pause the first subscription; a refresh must drop it and prune its source
    rows = _subscription_rows(session_factory)
    target = [r for r in rows if r.instrument_key == "NSE:RELIANCE"][0]
    with session_factory() as session:
        from backend.workflows.repository import AlertSubscription

        row = session.get(AlertSubscription, target.id)
        row.state = "paused"
        session.commit()

    summary = asyncio.run(worker.refresh_subscriptions())
    assert summary["removed"] == 1
    assert summary["added"] == 0

    current_ids = {s.id for group in worker._candle_subs.values() for s in group}
    assert target.id not in current_ids
    # source pruned once its group emptied
    assert ("NSE:RELIANCE", "minute") not in worker._candle_sources

    # dispatch consults the current set: a live bar for the paused instrument
    # must not be dispatched to the paused subscription
    evaluations_before = worker.health["evaluations"]
    asyncio.run(worker.poll_once())
    assert worker.health["evaluations"] == evaluations_before

    asyncio.run(worker.stop())


# ---------------------------------------------------------------------------
# fault 4: redis outage -> rebuild source with a new epoch, loop survives
# ---------------------------------------------------------------------------


class FlakySource:
    """TickSource double: raises on the first N polls, then replays a queue."""

    def __init__(self, epoch_id, observations=None, fail_times=0):
        self.epoch_id = epoch_id
        self.queue = list(observations or [])
        self.fail_times = int(fail_times)
        self.polls = 0
        self.stopped = False

    async def start(self):
        return None

    async def next_observation(self):
        self.polls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"simulated redis outage (epoch {self.epoch_id})")
        if self.queue:
            return self.queue.pop(0)
        return None

    async def stop(self):
        self.stopped = True
        return None


def test_redis_outage_rebuilds_source_new_epoch_no_phantom_fires(
    session_factory, notif_repo, channel_id
):
    from backend.workflows.runtime import EvaluationWorker

    repo = SqlAlchemyWorkflowRepository(session_factory)
    _activate(repo, _fixture_document())  # ltp crossing at 3000, trigger once

    built = {"count": 0}

    def tick_factory(instrument_key):
        built["count"] += 1
        epoch = f"boot-{built['count']}"
        # the first two epochs are mid-outage; boot-3 recovers high (a phantom
        # epoch switch must not fire) then confirms a genuine crossing later
        if built["count"] == 3:
            return FlakySource(epoch, [_tick_obs(epoch, 3005.0, T0.replace(minute=1))])
        return FlakySource(epoch, fail_times=1)

    worker = EvaluationWorker(
        repo,
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=tick_factory,
        candle_source_factory=lambda ik, tf: FakeSource(),
        candle_history=FakeHistory(),
        poll_interval_s=0.01,
        source_rebuild_backoff_s=0.0,
    )
    asyncio.run(worker.start())
    assert built["count"] == 1

    # poll 1: boot-1 raises -> gap, teardown, rebuild queued
    asyncio.run(worker.poll_once())
    assert worker.health["gaps"] == 1
    # poll 2: boot-2 built (new epoch) and raises too -> second gap
    asyncio.run(worker.poll_once())
    assert worker.health["gaps"] == 2
    assert built["count"] == 2

    # poll 3: boot-3 built with a NEW epoch boot id; its first observation is
    # above the level but must only initialize (no phantom fire, spec D2/E-5)
    asyncio.run(worker.poll_once())
    assert built["count"] == 3
    source = worker._tick_sources["NSE:RELIANCE"]
    assert source.epoch_id == "boot-3"
    assert source.stopped is False  # the recovered source stays up
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []

    # the recovered stream completes a genuine crossing: 3005 -> 2990 -> 3001
    source.queue.append(_tick_obs("boot-3", 2990.0, T0.replace(minute=2)))
    asyncio.run(worker.poll_once())
    assert _events(session_factory) == []  # still below

    source.queue.append(_tick_obs("boot-3", 3001.0, T0.replace(minute=3)))
    asyncio.run(worker.poll_once())
    events = _events(session_factory)
    assert len(events) == 1  # real crossing fired exactly once
    assert events[0].evidence["epoch_id"] == "boot-3"
    assert len(_deliveries(session_factory)) == 1
    assert worker.health["rebuilds"] == 2

    asyncio.run(worker.stop())


# ---------------------------------------------------------------------------
# fault 5: warmup never emits historical notifications
# ---------------------------------------------------------------------------


class _PinnedFakeService:
    """EvaluationService double implementing the pinned handle_observation
    contract (``allow_emit``) for a simple close-crosses-level rule."""

    def __init__(self, session_factory, level=100.0):
        self.session_factory = session_factory
        self.level = level
        self.calls = []  # (sub_id, ts, allow_emit)
        self._state = {}

    def ensure_subscriptions(self, revision):
        return 0  # rows are pre-created by the real service in these tests

    def handle_observation(self, sub, obs, *, db=None, allow_emit=True, context=None):
        self.calls.append((sub.id, obs.ts, allow_emit))
        state = self._state.setdefault(sub.id, {"prev": None, "fired": False})
        prev, state["prev"] = state["prev"], obs.close
        crossed = prev is not None and prev < self.level <= obs.close
        if not allow_emit:
            # warmup: may advance internal state but never commits anything
            return SimpleNamespace(emitted=False, suppression_reason=None, fired=False)
        if crossed and not state["fired"]:
            state["fired"] = True
            with self.session_factory() as session:
                session.add(
                    SignalEvent(
                        subscription_id=sub.id,
                        occurrence_key=f"fake:{sub.id}:{obs.ts.isoformat()}",
                        fired_at=obs.ts,
                        evidence={"level": self.level, "epoch_id": obs.epoch_id},
                        created_at=obs.ts,
                    )
                )
                session.commit()
            return SimpleNamespace(emitted=True, suppression_reason=None, fired=True)
        return SimpleNamespace(emitted=False, suppression_reason="no_crossing", fired=False)


def test_warmup_emits_nothing_then_live_bar_fires_once(session_factory, notif_repo, channel_id):
    from backend.workflows.runtime import EvaluationWorker

    repo = SqlAlchemyWorkflowRepository(session_factory)
    real_service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _candle_document(level=100.0))
    real_service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # 5 historical bars, two of which cross the level: warmup must stay silent
    history = FakeHistory(
        {("NSE:RELIANCE", "minute"): [
            _bar(0, 95.0), _bar(1, 99.0), _bar(2, 101.0), _bar(3, 100.5), _bar(4, 99.0),
        ]}
    )
    fake_service = _PinnedFakeService(session_factory, level=100.0)
    tick_sources, candle_sources = {}, {}
    worker = EvaluationWorker(
        repo,
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: tick_sources.setdefault(ik, FakeSource()),
        candle_source_factory=lambda ik, tf: candle_sources.setdefault((ik, tf), FakeSource()),
        candle_history=history,
        poll_interval_s=0.01,
        service=fake_service,
    )
    asyncio.run(worker.start())

    assert worker.health["warmups"] == 5
    assert _events(session_factory) == []  # zero signal events from warmup
    assert _deliveries(session_factory) == []
    # every warmup call passed allow_emit=False (pinned contract)
    assert len(fake_service.calls) == 5
    assert all(allow is False for _, _, allow in fake_service.calls)
    # warmup bars never touched the live queue
    assert candle_sources[("NSE:RELIANCE", "minute")].queue == []

    # live: a genuine crossing fires exactly once
    source = candle_sources[("NSE:RELIANCE", "minute")]
    source.queue.append(_bar(5, 101.0))
    asyncio.run(worker.poll_once())
    events = _events(session_factory)
    assert len(events) == 1

    source.queue.append(_bar(6, 102.0))
    asyncio.run(worker.poll_once())
    assert len(_events(session_factory)) == 1  # trigger-once semantics
    assert worker.health["emitted"] == 1

    asyncio.run(worker.stop())


def test_warmup_skips_bars_already_processed(session_factory, notif_repo, channel_id):
    from backend.workflows.runtime import EvaluationWorker

    repo = SqlAlchemyWorkflowRepository(session_factory)
    real_service = _service(session_factory, repo, notif_repo)
    _workflow, revision = _activate(repo, _candle_document(level=100.0))
    real_service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # checkpoint says bars up to 10:02 were already processed
    boundary_ts = _bar(2, 101.0).ts
    repo.save_checkpoint(
        sub.id, sub.instrument_key, "candle",
        {"initialized": True, "epoch_id": "candle", "last_bar_ts": boundary_ts.isoformat()},
        0,
    )

    history = FakeHistory(
        {("NSE:RELIANCE", "minute"): [_bar(0, 95.0), _bar(1, 99.0), _bar(2, 101.0), _bar(3, 99.5)]}
    )
    fake_service = _PinnedFakeService(session_factory, level=100.0)
    worker = EvaluationWorker(
        repo,
        session_factory,
        _resolver(notif_repo),
        tick_source_factory=lambda ik: FakeSource(),
        candle_source_factory=lambda ik, tf: FakeSource(),
        candle_history=history,
        poll_interval_s=0.01,
        service=fake_service,
    )
    asyncio.run(worker.start())

    replayed = [ts for _sid, ts, _allow in fake_service.calls]
    assert boundary_ts not in replayed
    assert _bar(3, 99.5).ts in replayed  # strictly newer bars are replayed
    assert len(replayed) == 1
    assert _events(session_factory) == []

    asyncio.run(worker.stop())
