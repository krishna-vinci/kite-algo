"""Phase 4 F10 acceptance: breadth through the REAL worker dispatch path.

These tests do NOT call ``evaluate_breadth`` (or any other component
directly). They author a canonical breadth workflow, activate it through the
service, and then drive completed bars through ``EvaluationWorker``'s dispatch
path — exactly what production does — asserting the observable outcomes a user
cares about: one workflow-level event, one outbox fan-out, no duplicate
crossing, no resurrected participation after re-entry, state that survives a
restart, and an all-or-nothing rollback.

The direct component tests (``tests/integration/test_alerts_phase4_postgres.py``,
``tests/workflows/test_phase4_schema.py``) remain as the low-level coverage;
these are the integration contract on top of them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import Delivery  # noqa: F401 (registers tables)
from backend.workflows import advanced_repository  # noqa: F401 (registers tables)
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    AlertSubscription,
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.runtime import EvaluationWorker

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
FIVEMIN = timedelta(minutes=5)
MEMBERS = ("NSE:A", "NSE:B", "NSE:C", "NSE:D")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _breadth_document(*, threshold=2, members=MEMBERS, channels=("chan-1",)):
    """The canonical Phase 4 breadth workflow used throughout this module."""
    return {
        "version": 1,
        "name": "breadth-acceptance",
        "session": "nse_equity",
        "instruments": list(members),
        "stages": [
            {
                "id": "breadth2",
                "type": "breadth",
                "clock": "candle_close",
                "timeframe": "5minute",
                "breadth": {
                    "condition": {
                        "all": [{"left": {"field": "close"}, "op": "gt",
                                 "right": {"value": 100}}]
                    },
                    "distinct_instruments": threshold,
                    "window": "30m",
                },
            }
        ],
        "alerts": [
            {"id": "ba", "source": "breadth2", "trigger": "on_transition",
             "channels": list(channels)},
        ],
    }


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


class _FakeSource:
    def __init__(self):
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return None


class _EmptyHistory:
    def recent_bars(self, key, timeframe, limit):
        return []

    def previous_session_levels(self, key, at):
        return None


def _activate(session_factory, doc, *, name=None):
    """Author -> activate -> materialize subscriptions, the production order."""
    from backend.workflows.service import EvaluationService

    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", name or doc["name"], compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)
    return repo, active


class _FixedSession:
    """A session provider that returns one identity (tests set the boundary)."""

    def __init__(self, session_id):
        self.session_id = session_id

    def __call__(self, session_name, instrument_key, ts):
        return True, self.session_id


def _make_worker(session_factory, *, owner_id="worker-1", session_provider=None):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    return EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=_EmptyHistory(),
        poll_interval_s=0.01,
        instrument_resolver=lambda keys: ({k: 1 for k in keys}, set()),
        renewal=None,
        session_provider=session_provider,
        owner_id=owner_id,
    )


def _bar(ts, close):
    from backend.alerts.predicates import Observation

    return Observation(
        ts=ts, epoch_id="candle", ltp=close, open=close, high=close, low=close,
        close=close, volume=1000.0, final=True,
    )


def _dispatch_bar(worker, instrument_key, ts, close):
    """Send one completed bar through the worker's real dispatch path.

    Goes through ``worker._dispatch`` — the same entry point a live candle
    completion uses — so indexing, breadth membership assembly, the ownership
    fence and the publication transaction are all exercised.
    """
    sub = next(s for s in worker._subscriptions if s.instrument_key == instrument_key)
    worker._dispatch(sub, _bar(ts, close))


async def _start(worker):
    await worker.start()


def _rows(session_factory, model):
    with session_factory() as session:
        return list(session.execute(select(model)).scalars().all())


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def _deliveries(session_factory):
    with session_factory() as session:
        return list(session.execute(select(Delivery)).scalars().all())


def _breadth_state(session_factory):
    with session_factory() as session:
        return session.execute(text(
            "select satisfied, crossing_seq, last_count, member_count "
            "from alert_breadth_state"
        )).fetchall()


def _session_counters(session_factory):
    with session_factory() as session:
        return session.execute(text(
            "select session_id, count from alert_session_counters order by session_id"
        )).fetchall()


def _suppression_counters(session_factory):
    with session_factory() as session:
        return session.execute(text(
            "select reason, count from alert_suppression_counters order by reason"
        )).fetchall()


def _contributions(session_factory):
    with session_factory() as session:
        return sorted(
            session.execute(text(
                "select instrument_key from alert_breadth_triggers"
            )).scalars().all()
        )


# ---------------------------------------------------------------------------
# 1. K distinct contributors -> ONE workflow-level event and outbox fan-out
# ---------------------------------------------------------------------------


def test_k_distinct_contributors_publish_one_workflow_level_event(session_factory):
    """Through activation and dispatch, K contributors yield exactly one event."""
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    # Four members all satisfy the condition on the SAME completed bar.
    for key in MEMBERS:
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)

    events = _events(session_factory)
    assert len(events) == 1, "exactly one workflow-level event per crossing"
    event = events[0]
    # Workflow-level: no subscription, so it can never render as a per-symbol
    # alert, and the evidence says what it is.
    assert event.subscription_id is None
    assert event.evidence["message_kind"] == "breadth"
    assert event.evidence["count"] == 2
    assert event.evidence["threshold"] == 2
    assert sorted(event.evidence["instruments"]) == ["NSE:A", "NSE:B"]
    assert event.evidence["members"] == len(MEMBERS)
    assert event.evidence["workflow_name"] == "breadth-acceptance"

    deliveries = _deliveries(session_factory)
    assert len(deliveries) == 1, "one delivery per channel, not per contributor"
    assert deliveries[0].channel_id == "chan-1"
    assert deliveries[0].event_id == event.id

    # The crossing itself was minted at count=2 (asserted in the evidence
    # above); the aggregate then continues counting, so the final stored count
    # reflects every member that contributed.
    state = _breadth_state(session_factory)
    assert state == [(True, 1, len(MEMBERS), len(MEMBERS))]
    assert _contributions(session_factory) == sorted(MEMBERS)


def test_non_matching_members_do_not_contribute(session_factory):
    """Unknown/missed conditions never count: only true observations do."""
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=3))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    for key in MEMBERS:
        _dispatch_bar(worker, key, T0 + FIVEMIN, 50.0)  # below the threshold

    assert _events(session_factory) == []
    assert _contributions(session_factory) == []
    assert _breadth_state(session_factory) == [(False, 0, 0, len(MEMBERS))]


# ---------------------------------------------------------------------------
# 2. repeated contributions do not duplicate the crossing
# ---------------------------------------------------------------------------


def test_repeated_contributions_do_not_duplicate_the_crossing(session_factory):
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    for key in MEMBERS:
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1

    # Later bars keep satisfying the condition: the threshold is already
    # satisfied, so no new crossing exists and nothing may be re-sent.
    for offset in (2, 3, 4):
        for key in MEMBERS:
            _dispatch_bar(worker, key, T0 + offset * FIVEMIN, 150.0)

    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1
    assert _breadth_state(session_factory)[0][1] == 1  # crossing_seq unchanged


def test_rearming_after_the_window_lapses_notifies_again(session_factory):
    """A genuine second crossing is a new event with a new identity."""
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    for key in MEMBERS:
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1

    # Everything ages out of the 30m window, which re-arms the threshold.
    late = T0 + timedelta(hours=2)
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, late, 150.0)

    events = _events(session_factory)
    assert len(events) == 2
    keys = {event.occurrence_key for event in events}
    assert len(keys) == 2, "distinct crossings must have distinct identities"
    assert sorted(event.evidence["crossing_seq"] for event in events) == [1, 2]
    assert len(_deliveries(session_factory)) == 2


# ---------------------------------------------------------------------------
# 3. removal / re-entry
# ---------------------------------------------------------------------------


def test_removal_and_reentry_does_not_restore_an_old_contribution(session_factory):
    """A member that leaves and rejoins must contribute again from scratch.

    Driven through the REAL membership lifecycle (a universe-backed document
    plus the worker's refresh pass), because that is the only path that
    actually admits, departs and re-admits members.
    """
    doc = {
        "version": 1,
        "name": "breadth-universe",
        "session": "nse_equity",
        "universe": {"union": [{"universe": "wl"}], "deduplicate": True},
        "stages": [{
            "id": "breadth2", "type": "breadth", "clock": "candle_close",
            "timeframe": "5minute",
            "breadth": {
                "condition": {"all": [{"left": {"field": "close"}, "op": "gt",
                                       "right": {"value": 100}}]},
                "distinct_instruments": 3, "window": "30m",
            },
        }],
        "alerts": [{"id": "ba", "source": "breadth2", "channels": ["chan-1"]}],
    }
    _repo, _revision = _activate(session_factory, doc)

    class _Universe:
        def __init__(self, members):
            self.members = set(members)

        def latest_revision(self, owner_id, name):
            return {"revision": 7, "members": sorted(self.members)}

        def preview_membership(self, *a):
            raise AssertionError

    universe = _Universe({"NSE:A", "NSE:B"})
    worker = _make_worker(session_factory)
    worker.universe_service = universe
    worker.universe_resolve_interval_s = 0.0  # re-resolve every pass
    asyncio.run(_start(worker))
    asyncio.run(worker.refresh_subscriptions())
    assert {s.instrument_key for s in worker._subscriptions} == {"NSE:A", "NSE:B"}

    # A and B contribute, but 3 are needed so nothing crosses yet.
    _dispatch_bar(worker, "NSE:A", T0 + FIVEMIN, 150.0)
    _dispatch_bar(worker, "NSE:B", T0 + FIVEMIN, 150.0)
    assert _events(session_factory) == []
    assert _contributions(session_factory) == ["NSE:A", "NSE:B"]

    # The universe drops NSE:A; NSE:C joins. A's contribution is retained as
    # history (it is not deleted while the member is merely absent).
    universe.members = {"NSE:B", "NSE:C"}
    asyncio.run(worker.refresh_subscriptions())
    assert {s.instrument_key for s in worker._subscriptions} == {"NSE:B", "NSE:C"}
    assert _contributions(session_factory) == ["NSE:A", "NSE:B"]

    # NSE:A rejoins: re-admission clears its retained contribution, so it
    # cannot count toward the threshold until it triggers again.
    universe.members = {"NSE:A", "NSE:B", "NSE:C"}
    asyncio.run(worker.refresh_subscriptions())
    assert {s.instrument_key for s in worker._subscriptions} == {
        "NSE:A", "NSE:B", "NSE:C"
    }
    assert "NSE:A" not in _contributions(session_factory)
    with session_factory() as session:
        row = session.execute(
            select(AlertSubscription).where(
                AlertSubscription.instrument_key == "NSE:A"
            )
        ).scalar_one()
        assert row.state == "active"
        assert not row.config.get("universe_departed")

    # B and C together are only TWO contributors. Had A's stale contribution
    # survived, this would already be a crossing (3) — it must not be.
    _dispatch_bar(worker, "NSE:C", T0 + 2 * FIVEMIN, 150.0)
    assert _events(session_factory) == []
    assert _breadth_state(session_factory)[0][2] == 2  # B and C only

    # A genuinely triggering again is what completes the aggregate, and the
    # evidence names only instruments that actually triggered in the window.
    _dispatch_bar(worker, "NSE:A", T0 + 2 * FIVEMIN, 150.0)
    events = _events(session_factory)
    assert len(events) == 1
    assert sorted(events[0].evidence["instruments"]) == ["NSE:A", "NSE:B", "NSE:C"]
    assert events[0].evidence["count"] == 3


# ---------------------------------------------------------------------------
# 4. restart / takeover preserves state
# ---------------------------------------------------------------------------


def test_restart_preserves_aggregate_state_and_does_not_re_notify(session_factory):
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1

    # Restart: a brand-new worker instance (fresh in-memory dispatch tables,
    # same durable state).
    restarted = _make_worker(session_factory)
    asyncio.run(_start(restarted))
    assert len(restarted._subscriptions) == len(MEMBERS)

    # The window is still satisfied, so the crossing must not be re-minted.
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(restarted, key, T0 + 2 * FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1
    assert _breadth_state(session_factory)[0][1] == 1


def test_a_second_worker_cannot_evaluate_a_live_owned_subscription(session_factory):
    """The ownership fence applies to breadth exactly as to signal alerts.

    While one worker holds an UNEXPIRED evaluation lease, another worker must
    not be able to evaluate the subscription — so it can neither duplicate a
    crossing nor move the aggregate. (Lease expiry and takeover are covered
    against real PostgreSQL in tests/integration/test_alerts_phase4_postgres.py
    and test_alerts_postgres_hardening.py, where the lease clock is real.)
    """
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    owner = _make_worker(session_factory, owner_id="worker-owner")
    asyncio.run(_start(owner))
    bar_time = T0 + FIVEMIN
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(owner, key, bar_time, 150.0)
    assert len(_events(session_factory)) == 1
    state_before = _breadth_state(session_factory)
    contributions_before = _contributions(session_factory)

    # A different worker evaluates the SAME subscriptions at the SAME
    # observation time, so the owner's lease is unexpired.
    other = _make_worker(session_factory, owner_id="worker-other")
    asyncio.run(_start(other))
    # Same instruments the owner has already claimed: ownership is per
    # (subscription, instrument), so a different instrument would be a
    # legitimate first claim rather than a fence violation.
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(other, key, bar_time, 150.0)

    assert other.health["suppressed"].get("not_owner", 0) >= 1
    assert len(_events(session_factory)) == 1
    assert _breadth_state(session_factory) == state_before
    assert _contributions(session_factory) == contributions_before


# ---------------------------------------------------------------------------
# 5. injected failure rolls back everything together
# ---------------------------------------------------------------------------


def test_injected_failure_rolls_back_the_whole_publication(session_factory, monkeypatch):
    """Checkpoint, contribution, aggregate, event and outbox roll back together.

    The failure is injected at the very LAST write of the publication
    transaction (the checkpoint CAS), which is after the contribution, the
    aggregate state, the event and its deliveries have all been issued — so a
    passing assertion means nothing at all was left behind.
    """
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    from backend.workflows.repository import LeaseConflict

    def _fail(*args, **kwargs):
        raise LeaseConflict("injected: ownership lost mid-publication")

    monkeypatch.setattr(worker.workflow_repo, "save_checkpoint", _fail)
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    monkeypatch.undo()

    # Nothing survived: no event, no delivery, no contribution, no aggregate
    # row, and no checkpoint written for either member.
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []
    assert _contributions(session_factory) == []
    assert _breadth_state(session_factory) == []
    with session_factory() as session:
        assert session.execute(text(
            "select count(*) from evaluation_checkpoints"
        )).scalar() == 0

    # The members are then evaluated normally: the run is retryable, and the
    # crossing is published exactly once (never twice).
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1
    assert _breadth_state(session_factory) == [(True, 1, 2, len(MEMBERS))]


# ---------------------------------------------------------------------------
# 6. the notification renders as an aggregate, not a per-symbol alert
# ---------------------------------------------------------------------------


def test_breadth_delivery_renders_as_an_aggregate_message(session_factory):
    """The resolver must produce a workflow-level message, not a symbol alert."""
    from backend.notifications.worker import make_resolver
    from backend.notifications.repository import ChannelReference

    _repo, _revision = _activate(session_factory, _breadth_document(threshold=2))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)

    from backend.notifications.repository import SqlAlchemyNotificationRepository

    with session_factory() as session:
        session.add(
            ChannelReference(
                id="chan-1", owner_id="owner-1", name="chan-1", provider="ntfy",
                destination={"url": "https://ntfy.example/topic"},
            )
        )
        session.commit()
        delivery = session.execute(select(Delivery)).scalar_one()
        notification_repo = SqlAlchemyNotificationRepository(session_factory)
        resolver = make_resolver(notification_repo, lambda _sub_id: None)
        resolved = resolver(delivery.id)

    assert resolved is not None
    assert "[Breadth]" in resolved["subject"]
    assert "NSE:A" in resolved["body"]
    assert "distinct instruments" in resolved["body"]
    # It is an aggregate: there is no single-symbol line describing it.
    assert "symbol: NSE:A" not in resolved["body"]


# ---------------------------------------------------------------------------
# 7. out-of-order contributions, session caps, and their rollback
# ---------------------------------------------------------------------------


def test_out_of_order_contribution_completing_k_still_publishes(session_factory):
    """A permitted crossing publishes even when a contribution arrived late.

    The newest observation arrives FIRST, setting the aggregation watermark;
    two older eligible contributions then complete the threshold. The aggregate
    is evaluated against the watermark, so this is a legitimate crossing — an
    informational reason must not suppress it. (Suppressing would be
    unrecoverable: the state commits as satisfied, and a crossing is never
    re-minted, so the notification would be lost forever.)
    """
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=3))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    # Both inside the 30-minute window: the newer at +20m, the older at +5m.
    newer = T0 + timedelta(minutes=20)
    older = T0 + timedelta(minutes=5)

    # The newest contribution lands first and opens the window at its time.
    _dispatch_bar(worker, "NSE:B", newer, 150.0)
    assert _events(session_factory) == []
    assert _breadth_state(session_factory)[0][2] == 1

    # Older contributions now complete the threshold. Each is evaluated
    # against the watermark (the newer time), not against its own bar time.
    _dispatch_bar(worker, "NSE:A", older, 150.0)
    assert _events(session_factory) == []
    _dispatch_bar(worker, "NSE:C", older, 150.0)

    events = _events(session_factory)
    assert len(events) == 1, "the crossing must publish, not be suppressed"
    event = events[0]
    assert event.subscription_id is None
    assert event.evidence["message_kind"] == "breadth"
    assert event.evidence["count"] == 3
    assert sorted(event.evidence["instruments"]) == ["NSE:A", "NSE:B", "NSE:C"]
    # The out-of-order participation is recorded as provenance, never as a
    # reason to drop the notification.
    assert event.evidence["aggregate_note"] == "breadth_stale_observation"
    # Both times are recorded separately and they genuinely differ.
    assert event.evidence["event_time"] == older.isoformat()
    assert event.evidence["evaluated_at"] == newer.isoformat()
    assert event.evidence["evaluated_at"] != event.evidence["event_time"]
    assert event.evidence["contributing_instrument"] == "NSE:C"

    assert len(_deliveries(session_factory)) == 1
    assert _breadth_state(session_factory)[0][1] == 1


def test_out_of_order_crossing_repeat_and_restart_do_not_duplicate(session_factory):
    """The out-of-order crossing is published exactly once, ever."""
    _repo, _revision = _activate(session_factory, _breadth_document(threshold=3))
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))
    # Both inside the 30-minute window: the newer at +20m, the older at +5m.
    newer = T0 + timedelta(minutes=20)
    older = T0 + timedelta(minutes=5)
    for key, when in (("NSE:B", newer), ("NSE:A", older), ("NSE:C", older)):
        _dispatch_bar(worker, key, when, 150.0)
    assert len(_events(session_factory)) == 1

    # Replaying the same bars must not re-mint the crossing...
    for key, when in (("NSE:B", newer), ("NSE:A", older), ("NSE:C", older)):
        _dispatch_bar(worker, key, when, 150.0)
    assert len(_events(session_factory)) == 1

    # ...and neither must a restart, which rebuilds dispatch state from the
    # durable aggregate.
    restarted = _make_worker(session_factory)
    asyncio.run(_start(restarted))
    for key, when in (("NSE:A", older), ("NSE:C", older)):
        _dispatch_bar(restarted, key, when, 150.0)
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1


def test_breadth_session_cap_advances_state_and_records_durable_suppression(
    session_factory,
):
    """A capped crossing advances the aggregate but creates no event or outbox."""
    doc = _breadth_document(threshold=2)
    doc["alerts"][0]["max_per_session"] = 1
    doc["alerts"][0]["session_cap_reset"] = "session"
    _repo, _revision = _activate(session_factory, doc)
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    # First crossing: the single permitted notification.
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1
    assert _session_counters(session_factory)[0][1] == 1

    # The window lapses, which re-arms the threshold; a genuine second
    # crossing then arrives.
    later = T0 + timedelta(hours=2)
    _dispatch_bar(worker, "NSE:A", later, 150.0)  # observed count < K -> rearm
    assert _breadth_state(session_factory)[0][0] == 0  # satisfied = False
    for key in ("NSE:B",):
        _dispatch_bar(worker, key, later, 150.0)

    # The crossing MUST still advance state; only the notification is capped.
    state = _breadth_state(session_factory)
    assert state[0][0] == 1, "state advances even when the cap suppresses"
    assert state[0][1] == 2, "crossing_seq advances to the new crossing"

    assert len(_events(session_factory)) == 1, "no second event was created"
    assert len(_deliveries(session_factory)) == 1, "no second outbox row"
    # The skip is durably inspectable, not silent.
    assert _suppression_counters(session_factory) == [("session_cap", 1)]
    # A capped attempt does not consume a slot, so the cap cannot drift.
    assert _session_counters(session_factory)[0][1] == 1


def test_a_new_session_permits_another_breadth_notification(session_factory):
    """The cap is per session: a new session boundary resets it."""
    doc = _breadth_document(threshold=2)
    doc["alerts"][0]["max_per_session"] = 1
    doc["alerts"][0]["session_cap_reset"] = "session"
    _repo, _revision = _activate(session_factory, doc)
    session = _FixedSession("NSE:CM:2026-09-11")
    worker = _make_worker(session_factory, session_provider=session)
    asyncio.run(_start(worker))

    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1

    # Same session: capped.
    later = T0 + timedelta(hours=2)
    _dispatch_bar(worker, "NSE:A", later, 150.0)
    for key in ("NSE:B",):
        _dispatch_bar(worker, key, later, 150.0)
    assert len(_events(session_factory)) == 1
    assert _suppression_counters(session_factory) == [("session_cap", 1)]

    # A NEW session id re-arms the cap, so the next crossing notifies again.
    session.session_id = "NSE:CM:2026-09-12"
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, later + timedelta(hours=2), 150.0)
    assert len(_events(session_factory)) == 2
    counters = dict(_session_counters(session_factory))
    assert counters == {"NSE:CM:2026-09-11": 1, "NSE:CM:2026-09-12": 1}


def test_injected_publication_failure_rolls_back_the_cap_and_aggregate(
    session_factory, monkeypatch
):
    """A failure after the cap is reserved rolls the slot back with the aggregate.

    The slot is claimed BEFORE the event is written, so a crash between the two
    must not leave a consumed slot: otherwise a failed crossing would silently
    expend one of the session's notifications while publishing nothing.
    """
    doc = _breadth_document(threshold=2)
    doc["alerts"][0]["max_per_session"] = 1
    _repo, _revision = _activate(session_factory, doc)
    worker = _make_worker(session_factory)
    asyncio.run(_start(worker))

    # First contribution commits normally: the crossing is not reached yet, so
    # no publication is attempted and this is a legitimate, completed write.
    _dispatch_bar(worker, "NSE:A", T0 + FIVEMIN, 150.0)
    state_before = _breadth_state(session_factory)
    contributions_before = _contributions(session_factory)
    assert state_before == [(0, 0, 1, len(MEMBERS))]
    assert contributions_before == ["NSE:A"]

    # The second contribution completes the threshold, so the publication is
    # attempted — and fails AFTER the cap slot has been reserved.
    def _boom(*args, **kwargs):
        raise RuntimeError("injected: publication failed after the slot was reserved")

    monkeypatch.setattr(worker.service, "_publish_breadth_crossing", _boom, raising=True)
    _dispatch_bar(worker, "NSE:B", T0 + FIVEMIN, 150.0)
    monkeypatch.undo()

    # The failed transaction left NOTHING behind: not the second contribution,
    # not the advanced aggregate, not the cap slot, and no event or outbox row.
    assert _breadth_state(session_factory) == state_before
    assert _contributions(session_factory) == contributions_before
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []
    assert _session_counters(session_factory) == []
    assert _suppression_counters(session_factory) == []
    with session_factory() as session:
        checkpoints = session.execute(text(
            "select instrument_key from evaluation_checkpoints order by 1"
        )).scalars().all()
    assert checkpoints == ["NSE:A"], "the failed member's checkpoint rolled back too"

    # The session's notification is therefore still available, and the
    # crossing publishes exactly once on the retry.
    for key in ("NSE:A", "NSE:B"):
        _dispatch_bar(worker, key, T0 + FIVEMIN, 150.0)
    assert len(_events(session_factory)) == 1
    assert _session_counters(session_factory)[0][1] == 1
    assert _breadth_state(session_factory)[0][1] == 1
