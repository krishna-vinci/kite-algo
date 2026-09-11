"""Phase 6 6A.0 acceptance: LTP freshness.

The failure this closes: the market runtime keeps re-publishing the last tick
after a session closes, so the alerts worker evaluated a FROZEN exchange
snapshot as if it were live and could alert on it. Nothing compared the
exchange's event time against the receipt, and nothing aged the feed when it
went silent — so an illiquid or closed instrument looked busy forever
(spec E-27 wants "health shows stale data age; no signals; no error storms").

Covered here:
- the tick-level bounds (age + future) and the frozen-snapshot signature;
- the silence gap and its PERSISTED continuity invalidation;
- durability across a restart;
- staleness visible in health with NO tick arriving;
- the kill switch.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.repository import ChannelReference, Delivery  # noqa: F401
from backend.workflows import advanced_repository  # noqa: F401
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.runtime import EvaluationWorker, RedisTickSource
from backend.workflows.service import EvaluationService

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
TOKEN = 123668231
INSTRUMENT = "MCX:GOLD26OCTFUT"
BOUND = 300.0


# ---------------------------------------------------------------------------
# fake redis pub/sub for the real RedisTickSource
# ---------------------------------------------------------------------------


class _FakePubSub:
    def __init__(self, messages):
        self._messages = list(messages)
        self.closed = False

    async def subscribe(self, channel):
        return None

    async def unsubscribe(self, channel):
        return None

    async def aclose(self):
        self.closed = True

    async def get_message(self, ignore_subscribe_messages=False, timeout=1.0):
        if not self._messages:
            return None
        return {"type": "message", "data": json.dumps(self._messages.pop(0))}


class _FakeRedis:
    def __init__(self, messages):
        self._pubsub = _FakePubSub(messages)

    def pubsub(self):
        return self._pubsub


def _tick(
    *,
    exchange_ts: datetime,
    received_at: Optional[datetime] = None,
    ltp: float = 100.0,
    last_trade_time: Optional[datetime] = None,
) -> dict:
    payload = {
        "instrument_token": TOKEN,
        "last_price": ltp,
        "exchange_timestamp": exchange_ts.isoformat(),
    }
    if received_at is not None:
        payload["received_at"] = received_at.isoformat()
    if last_trade_time is not None:
        payload["last_trade_time"] = last_trade_time.isoformat()
    return payload


def _source(messages, *, now=T0, max_age=None, max_future=None, clock=None):
    return RedisTickSource(
        _FakeRedis(messages),
        {TOKEN: INSTRUMENT},
        max_tick_age_s=max_age,
        max_future_skew_s=max_future,
        clock=clock or (lambda: now),
    )


async def _next(source):
    await source.start()
    return await source.next_observation()


# ---------------------------------------------------------------------------
# 1. tick-level bounds
# ---------------------------------------------------------------------------


def test_frozen_snapshot_is_rejected_despite_a_fresh_receipt():
    """The exact observed failure: stale exchange time, fresh receipt.

    This is the signature the market runtime produces when it re-publishes the
    last snapshot — so a receive-age-only check (the existing
    ``DEFAULT_TICK_STALE_MS`` precedent) would pass it as fresh. The age bound
    compares the exchange timestamp against the receipt and refuses it.
    """
    frozen_exchange = T0 - timedelta(seconds=5760)  # the observed 96-minute skew
    source = _source(
        [_tick(exchange_ts=frozen_exchange, received_at=T0)],
        max_age=BOUND,
    )
    assert asyncio.run(_next(source)) is None
    assert source.rejected["stale_tick"] == 1
    assert source.rejected["future_tick"] == 0


def test_fresh_tick_passes_and_carries_receive_time():
    source = _source(
        [_tick(exchange_ts=T0 - timedelta(seconds=2), received_at=T0)],
        max_age=BOUND,
    )
    obs = asyncio.run(_next(source))
    assert obs is not None
    assert obs.ts == T0 - timedelta(seconds=2)
    assert obs.received_at == T0
    assert obs.ltp == 100.0
    assert source.rejected["stale_tick"] == 0


def test_excessive_future_timestamp_is_rejected():
    """Clock skew must not authorise a "fresh" tick."""
    source = _source(
        [_tick(exchange_ts=T0 + timedelta(seconds=3600), received_at=T0)],
        max_age=BOUND,
        max_future=BOUND,
    )
    assert asyncio.run(_next(source)) is None
    assert source.rejected["future_tick"] == 1


def test_receive_time_is_used_when_present_for_the_age_bound():
    """A tick received long after the exchange stamped it is stale too.

    Delivery delay is bounded by the same rule: ``received_at - ts`` is checked
    even when the exchange time looks recent relative to the source clock.
    """
    source = _source(
        [
            _tick(
                exchange_ts=T0,
                received_at=T0 + timedelta(seconds=600),
            )
        ],
        now=T0 + timedelta(seconds=600),
        max_age=BOUND,
    )
    assert asyncio.run(_next(source)) is None
    assert source.rejected["stale_tick"] == 1


def test_untimed_tick_is_counted_not_silently_dropped():
    source = _source([{"instrument_token": TOKEN, "last_price": 100.0}], max_age=BOUND)
    assert asyncio.run(_next(source)) is None
    assert source.rejected["untimed"] == 1


def test_no_bounds_configured_preserves_historical_behavior():
    """An unconfigured source makes no freshness claim (kill-switch path)."""
    source = _source(
        [_tick(exchange_ts=T0 - timedelta(days=1), received_at=T0)]
    )
    obs = asyncio.run(_next(source))
    assert obs is not None, "without bounds the source must behave as before"
    assert sum(source.rejected.values()) == 0


# ---------------------------------------------------------------------------
# service harness (silence gap + persistence + health)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory():
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


def _ltp_document(trigger: str = "on_transition"):
    return {
        "version": 1,
        "name": "ltp-freshness",
        "session": "mcx_commodity",
        "instruments": [INSTRUMENT],
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [
                        {"left": {"field": "ltp"}, "op": "crosses_above",
                         "right": {"value": 100.0}}
                    ]
                },
            }
        ],
        "alerts": [
            {"id": "a1", "source": "px", "trigger": trigger, "channels": ["chan-1"]}
        ],
    }


def _activate(session_factory, doc=None):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(doc or _ltp_document()))
    workflow, revision = repo.create_workflow(
        "owner-1", (doc or _ltp_document())["name"],
        compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    service = EvaluationService(repo, session_factory, owner_id="worker-1")
    service.ensure_subscriptions(active)
    return repo, service, revision


def _single_sub(repo):
    subs = repo.list_active_subscriptions()
    assert len(subs) == 1
    return subs[0]


def _tick_obs(ltp, ts, *, received_at=None, epoch="boot-1"):
    return Observation(
        ts=ts, epoch_id=epoch, ltp=ltp, received_at=received_at or ts
    )


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def _checkpoint_state(session_factory, sub_id, instrument_key):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    latest = repo.load_latest_checkpoint(sub_id, instrument_key)
    if latest is None:
        return None
    _epoch, state, _owner_epoch = latest
    return state


# ---------------------------------------------------------------------------
# 2. silence gap
# ---------------------------------------------------------------------------


def test_silence_gap_invalidates_continuity_and_does_not_fabricate_a_crossing(
    session_factory,
):
    """A tick after a long silence must not fire against pre-silence state.

    Sequence: initialize below the level, then wait far longer than the bound,
    then arrive above it. Without the gap rule that second tick is a legitimate
    crossing and would notify — but it would be a crossing computed across a
    period the worker never observed.
    """
    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)

    first = service.handle_observation(
        sub, _tick_obs(99.0, T0, received_at=T0)
    )
    assert (first.fired, first.emitted) == (False, False)

    late_ts = T0 + timedelta(seconds=BOUND * 4)
    second = service.handle_observation(
        sub, _tick_obs(101.0, late_ts, received_at=late_ts)
    )

    assert second.fired is False, "no crossing may be manufactured across a silence"
    assert second.emitted is False
    assert second.suppression_reason == "ltp_gap"
    assert _events(session_factory) == []

    state = _checkpoint_state(session_factory, sub.id, sub.instrument_key)
    assert state.get("continuity_invalidation_reason") == "ltp_gap"
    assert state.get("continuity_invalidated_at") is not None
    # Continuity was re-initialized by the post-gap tick: the condition records
    # the gap tick as its FIRST observation (prev == its own value) instead of
    # carrying the pre-silence `prev`, which is what would have fired.
    cond = next(
        sub_state
        for key, sub_state in (state.get("conds") or {}).items()
        if key.startswith("crosses_above:field:ltp")
    )
    assert cond["prev"] == 101.0, "the gap tick must start a fresh baseline"
    assert state.get("last_tick_received_at") == late_ts.isoformat()


def test_gap_within_the_bound_still_fires_normally(session_factory):
    """The bound must not suppress ordinary evaluation."""
    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)

    service.handle_observation(sub, _tick_obs(99.0, T0, received_at=T0))
    soon = T0 + timedelta(seconds=BOUND / 2)
    cross = service.handle_observation(
        sub, _tick_obs(101.0, soon, received_at=soon)
    )
    assert cross.fired is True
    assert cross.emitted is True
    assert cross.suppression_reason is None
    assert len(_events(session_factory)) == 1


def test_gap_does_not_clear_durable_trigger_bookkeeping(session_factory):
    """A spent ``once`` rule is not re-armed by a stale interval.

    Continuity invalidation must not resurrect lifecycle state: clearing
    ``fired_once`` would let a stale interval re-notify a rule that has already
    completed.
    """
    repo, service, _revision = _activate(
        session_factory, {**_ltp_document(trigger="once")}
    )
    sub = _single_sub(repo)

    service.handle_observation(sub, _tick_obs(99.0, T0, received_at=T0))
    cross = service.handle_observation(
        sub, _tick_obs(101.0, T0 + timedelta(seconds=60), received_at=T0 + timedelta(seconds=60))
    )
    assert cross.emitted is True
    assert cross.rule_completed is True

    state = _checkpoint_state(session_factory, sub.id, sub.instrument_key)
    assert state.get("fired_once") is True, "bookkeeping must survive"

    # Long silence, then a fresh crossing signal: still no second notification.
    late = T0 + timedelta(seconds=BOUND * 4)
    after = service.handle_observation(sub, _tick_obs(99.0, late, received_at=late))
    assert after.emitted is False
    late2 = late + timedelta(seconds=BOUND * 4)
    again = service.handle_observation(sub, _tick_obs(105.0, late2, received_at=late2))
    assert again.emitted is False
    assert len(_events(session_factory)) == 1


def test_kill_switch_disables_the_gap_rule(session_factory):
    """``ltp_freshness_enabled=False`` restores the previous behavior."""
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(_ltp_document()))
    workflow, revision = repo.create_workflow(
        "owner-1", "kill-switch", compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    service = EvaluationService(
        repo, session_factory, owner_id="worker-1", ltp_freshness_enabled=False
    )
    service.ensure_subscriptions(active)
    sub = _single_sub(repo)
    assert service.ltp_freshness_enabled is False

    service.handle_observation(sub, _tick_obs(99.0, T0, received_at=T0))
    late = T0 + timedelta(seconds=BOUND * 4)
    tick = service.handle_observation(sub, _tick_obs(101.0, late, received_at=late))
    # With the rule disabled the crossing fires exactly as it did before.
    assert tick.suppression_reason is None
    assert len(_events(session_factory)) == 1


def test_observations_without_receive_time_never_invent_a_gap(session_factory):
    """Synthetic observations carry no receipt; no gap may be inferred."""
    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)

    bare = Observation(ts=T0, epoch_id="boot-1", ltp=99.0)
    service.handle_observation(sub, bare)
    far = Observation(ts=T0 + timedelta(days=1), epoch_id="boot-1", ltp=101.0)
    result = service.handle_observation(sub, far)
    assert result.suppression_reason != "ltp_gap"
    assert result.emitted is True


# ---------------------------------------------------------------------------
# 3. durability across a restart
# ---------------------------------------------------------------------------


def test_continuity_invalidation_is_durable_across_restart(session_factory):
    """A restart between detection and the next tick must not resurrect continuity.

    The invalidation is committed in its own transaction BEFORE evaluation, so
    it survives independently of what the evaluation does — including the case
    that matters most for durability, a failing evaluation. Otherwise the next
    tick could be judged against pre-silence continuity, which is exactly the
    fabricated-crossing hazard the reset exists to prevent.
    """
    from backend.workflows.repository import LeaseConflict

    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)
    service.handle_observation(sub, _tick_obs(99.0, T0, received_at=T0))

    # Fail the EVALUATION's checkpoint save (the second call); the invalidation
    # (the first) must already be committed and must not roll back with it.
    real_save = repo.save_checkpoint
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise LeaseConflict("injected: evaluation save failed after invalidation")
        return real_save(*args, **kwargs)

    repo.save_checkpoint = _flaky
    late = T0 + timedelta(seconds=BOUND * 4)
    try:
        result = service.handle_observation(
            sub, _tick_obs(101.0, late, received_at=late)
        )
    finally:
        repo.save_checkpoint = real_save

    assert result.suppression_reason == "lease_lost"
    assert _events(session_factory) == []

    # The invalidation survived the failed evaluation. A fresh process (as after
    # a restart) sees it and cannot replay pre-silence continuity.
    repo2 = SqlAlchemyWorkflowRepository(session_factory)
    sub2 = _single_sub(repo2)
    state = _checkpoint_state(session_factory, sub2.id, sub2.instrument_key)
    assert state.get("continuity_invalidation_reason") == "ltp_gap"
    assert "conds" not in state, (
        "no condition continuity may remain after an invalidated, failed evaluation"
    )
    assert state.get("last_tick_received_at") == late.isoformat()

    # And the recovery tick cannot fire a crossing against pre-silence state.
    service2 = EvaluationService(repo2, session_factory, owner_id="worker-1")
    recovery = late + timedelta(seconds=30)
    result2 = service2.handle_observation(
        sub2, _tick_obs(105.0, recovery, received_at=recovery)
    )
    assert result2.fired is False
    assert result2.emitted is False


# ---------------------------------------------------------------------------
# 4. staleness observable with NO tick arriving
# ---------------------------------------------------------------------------


class _FakeSource:
    async def start(self):
        return None

    async def stop(self):
        return None

    async def next_observation(self):
        return None


class _EmptyHistory:
    def recent_bars(self, key, timeframe, limit):
        return []

    def previous_session_levels(self, key, at):
        return None


def _worker(session_factory, **overrides):
    params = dict(
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=_EmptyHistory(),
        poll_interval_s=0.01,
        instrument_resolver=lambda keys: ({k: TOKEN for k in keys}, set()),
        renewal=None,
        owner_id="worker-1",
    )
    params.update(overrides)
    return EvaluationWorker(
        SqlAlchemyWorkflowRepository(session_factory), session_factory, **params
    )


def test_health_reports_growing_tick_age_with_zero_ticks(session_factory):
    """The whole point: silence must be visible without any tick arriving.

    The age is derived by the health timer from the last accepted receipt, so
    an operator sees the feed has gone quiet even though nothing has been
    evaluated (and therefore no counter could have moved).
    """
    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)

    # One accepted tick, stamped an hour before "now".
    old = T0 - timedelta(hours=1)
    service.handle_observation(sub, _tick_obs(99.0, old, received_at=old))

    worker = _worker(session_factory)
    asyncio.run(worker.start())  # seeds tick ages from the checkpoint

    snapshot = worker.health_snapshot()
    assert snapshot["ltp_freshness_enabled"] is True
    assert snapshot["ltp_max_gap_s"] == BOUND
    assert snapshot["stale_tick_instruments"] == 1
    detail = snapshot["stale_tick_detail"]
    assert INSTRUMENT in detail
    assert detail[INSTRUMENT] > BOUND
    # No tick arrived during any of this — the staleness came from the clock.
    assert worker.health["evaluations"] == 0
    asyncio.run(worker.stop())


def test_health_clears_staleness_once_a_tick_is_accepted(session_factory):
    repo, service, _revision = _activate(session_factory)
    sub = _single_sub(repo)
    service.handle_observation(sub, _tick_obs(99.0, T0, received_at=T0))

    worker = _worker(session_factory)
    asyncio.run(worker.start())

    # An accepted tick refreshes the age to "now".
    fresh = datetime.now(timezone.utc)
    worker._remember_accepted_tick(
        sub.instrument_key,
        Observation(ts=fresh, epoch_id="e", ltp=100.0, received_at=fresh),
    )
    snapshot = worker.health_snapshot()
    assert snapshot["stale_tick_instruments"] == 0
    assert snapshot["stale_tick_detail"] == {}
    asyncio.run(worker.stop())


class _ScriptedSource:
    """A feed source that yields a fixed sequence of observations."""

    def __init__(self, observations):
        self._observations = list(observations)
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return self._observations.pop(0) if self._observations else None


def test_poll_once_resets_the_staleness_clock(session_factory):
    """Guard the WIRING: an accepted tick must reset the age in poll_once.

    Exercising the helper directly is not enough — without this, removing the
    poll-loop call would leave health aging forever and reporting a stale feed
    while data is actually flowing.
    """
    _repo, _service, _revision = _activate(session_factory)
    fresh = datetime.now(timezone.utc)
    source = _ScriptedSource([
        Observation(ts=fresh, epoch_id="boot-1", ltp=99.0, received_at=fresh)
    ])
    worker = _worker(session_factory, tick_source_factory=lambda key: source)
    asyncio.run(worker.start())

    # Nothing has ticked yet, so the instrument reads as stale.
    assert worker.health_snapshot()["never_ticked_instruments"] == 1

    asyncio.run(worker.poll_once())

    snapshot = worker.health_snapshot()
    assert snapshot["never_ticked_instruments"] == 0
    assert snapshot["stale_tick_instruments"] == 0
    asyncio.run(worker.stop())


def test_health_counts_a_never_ticked_instrument_as_stale(session_factory):
    """A subscription that has never seen a tick is stale, not absent."""
    _repo, _service, _revision = _activate(session_factory)
    worker = _worker(session_factory)
    asyncio.run(worker.start())
    snapshot = worker.health_snapshot()
    assert snapshot["never_ticked_instruments"] == 1
    assert snapshot["stale_tick_instruments"] == 1
    assert snapshot["stale_tick_detail"][INSTRUMENT] is None
    asyncio.run(worker.stop())


def test_rejected_tick_counters_surface_in_health(session_factory):
    """A refused tick is reported: it never becomes an observation."""
    _repo, _service, _revision = _activate(session_factory)
    source = _source(
        [_tick(exchange_ts=T0 - timedelta(days=1), received_at=T0)], max_age=BOUND
    )
    worker = _worker(session_factory, tick_source_factory=lambda key: source)
    asyncio.run(worker.start())
    asyncio.run(worker.poll_once())
    snapshot = worker.health_snapshot()
    assert snapshot["rejected_ticks"]["stale_tick"] == 1
    asyncio.run(worker.stop())
