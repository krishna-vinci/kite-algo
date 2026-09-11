"""Phase 3 F9: scheduler claim/fence idempotency + attachment semantics.

- concurrent claim attempts resolve to ONE logical run (unique occurrence);
- a stale owner cannot finalize (compare-and-swap fencing);
- first complete run is a silent baseline unless initial_match;
- entry/exit use exit_after consecutive absences; top_n uses rank-band
  hysteresis; rank_delta compares against the previous complete rank;
- partial runs never evaluate attachments (no exits, no baseline advance).

Attachment transitions are PREPARED by ``prepare_attachments`` (no writes)
and published by ``finalize_run`` inside the run's single fenced
transaction — the ``_evaluate`` helper below drives that exact pair.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import Delivery  # noqa: F401
from backend.workflows.repository import SignalEvent  # noqa: F401
from backend.screeners.scheduler import ScreenerScheduler, prepare_attachments
from backend.workflows.compiler import compile_document
from backend.workflows.models import AttachmentSpec, ScreenerSpec, ScheduleSpec
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base, SqlAlchemyWorkflowRepository
from backend.workflows.screener_repository import (
    ScreenerRunMember,
    ScreenerRunRepository,
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


class _History:
    def __init__(self, closes):
        self.closes = closes

    def recent_bars(self, key, timeframe, limit):
        return []


def _doc(attachments):
    return {
        "version": 1,
        "name": "scr",
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
        "screener": {
            "schedule": {"every": "1d", "at": "session_close"},
            "rank": {"by": {"field": "change_pct"}, "direction": "desc"},
            "attachments": attachments,
        },
    }


def _activate(session_factory, doc_dict, name="scr"):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(doc_dict))
    workflow, revision = repo.create_workflow(
        "owner-1", name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    return repo.get_workflow(workflow.id), repo.get_active_revision(workflow.id)


def _member(key, *, passed=True, rank=None, score=5.0):
    from backend.screeners.runner import MemberResult

    return MemberResult(
        instrument_key=key,
        matched=True if passed else False,
        exclusion_reason=None if passed else "condition_filter",
        values={"close": 100.0, "score": score},
        score=score if passed else None,
        rank=rank,
        passed=passed,
    )


def _run_row(session_factory, workflow, revision, bucket=T0, status="running"):
    repo = ScreenerRunRepository(session_factory)
    return repo.claim_run(
        owner_id="owner-1",
        workflow_id=workflow.id,
        revision_id=revision.id,
        occurrence_key=f"{workflow.id}:{int(bucket.timestamp())}",
        scheduled_for=bucket,
        lease_owner="worker-a",
        lease_ttl_s=300,
        now=bucket,
    )


def _channels(owner_id, names):
    return {name: f"chan-{name}" for name in names}


# ---------------------------------------------------------------------------
# claim + fence
# ---------------------------------------------------------------------------


def test_concurrent_claims_resolve_to_one_run(session_factory):
    workflow, revision = _activate(session_factory, _doc([]))
    repo = ScreenerRunRepository(session_factory)
    key = f"{workflow.id}:{int(T0.timestamp())}"
    first = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=key, scheduled_for=T0, lease_owner="worker-a",
        lease_ttl_s=300, now=T0,
    )
    second = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=key, scheduled_for=T0, lease_owner="worker-b",
        lease_ttl_s=300, now=T0,
    )
    assert first is not None and second is None
    assert repo.get_run(first.id).lease_owner == "worker-a"


def test_stale_lease_takeover_then_old_owner_cannot_finalize(session_factory):
    workflow, revision = _activate(session_factory, _doc([]))
    repo = ScreenerRunRepository(session_factory)
    key = f"{workflow.id}:{int(T0.timestamp())}"
    run = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=key, scheduled_for=T0, lease_owner="worker-a",
        lease_ttl_s=10, now=T0,
    )
    later = T0 + timedelta(seconds=60)
    taken = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=key, scheduled_for=T0, lease_owner="worker-b",
        lease_ttl_s=300, now=later,
    )
    assert taken is not None and taken.id == run.id
    assert taken.lease_owner == "worker-b"
    # stale owner's finalize must be rejected wholesale
    published = repo.finalize_run(
        run.id, "worker-a",
        status="complete", as_of=T0,
        coverage={"expected": 1}, data_freshness={},
        members=[{"instrument_key": "NSE:A", "passed": True, "rank": 1}],
        now=later,
    )
    assert published is False
    assert repo.run_members(run.id) == []
    # the new owner CAN finalize
    published = repo.finalize_run(
        run.id, "worker-b",
        status="complete", as_of=T0,
        coverage={"expected": 1}, data_freshness={},
        members=[{"instrument_key": "NSE:A", "passed": True, "rank": 1}],
        now=later,
    )
    assert published is True
    members = repo.run_members(run.id)
    assert [m.instrument_key for m in members] == ["NSE:A"]


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


def _workflow_objects(workflow, revision):
    return workflow, revision


def _evaluate(session_factory, workflow, revision, doc, run, results, now=T0):
    """prepare_attachments + the fenced finalize_run publication pair.

    Returns the prepared summary; the run is published as ``complete`` with
    the transitions applied inside the publication transaction."""
    repo = ScreenerRunRepository(session_factory)
    summary, transitions = prepare_attachments(
        workflow=workflow,
        revision=revision,
        document=parse_workflow_dict(doc),
        run=run,
        results=results,
        run_repo=repo,
        channel_resolver=_channels,
        owner_id="owner-1",
        now=now,
    )
    published = repo.finalize_run(
        run.id,
        run.lease_owner,
        status="complete",
        as_of=run.scheduled_for,
        coverage={"expected": len(results)},
        data_freshness={},
        members=[
            {
                "instrument_key": m.instrument_key,
                "passed": m.passed,
                "exclusion_reason": m.exclusion_reason,
                "values": m.values,
                "rank": m.rank,
                "score": m.score,
            }
            for m in results
        ],
        attachments=transitions,
        now=now,
    )
    assert published is True
    return summary


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def _states(session_factory, workflow, revision, attachment_id):
    repo = ScreenerRunRepository(session_factory)
    return repo.attachment_states("owner-1", workflow.id, revision.id, attachment_id)


def test_first_complete_run_is_silent_baseline(session_factory):
    doc = _doc([{"id": "en", "trigger": "entry", "channels": ["telegram_primary"]}])
    workflow, revision = _activate(session_factory, doc)
    run = _run_row(session_factory, workflow, revision)
    results = [_member("NSE:A", rank=1), _member("NSE:B", rank=2)]
    summary = _evaluate(session_factory, workflow, revision, doc, run, results)
    assert summary["events"] == 0
    assert _events(session_factory) == []
    states = _states(session_factory, workflow, revision, "en")
    assert states["NSE:A"].present is True  # baseline recorded silently


def test_initial_match_opt_in_fires_on_baseline(session_factory):
    doc = _doc([{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}])
    workflow, revision = _activate(session_factory, doc)
    run = _run_row(session_factory, workflow, revision)
    results = [_member("NSE:A", rank=1)]
    summary = _evaluate(session_factory, workflow, revision, doc, run, results)
    assert summary["events"] == 1
    events = _events(session_factory)
    assert len(events) == 1
    evidence = events[0].evidence
    assert evidence["message_kind"] == "screener_attachment"
    assert evidence["instrument_key"] == "NSE:A"
    assert evidence["action"] == "entry"
    assert evidence["screener"] == "scr"
    assert evidence["scheduled_for"] is not None  # freshness disclosure
    # deliveries fan out through the existing outbox
    with session_factory() as session:
        deliveries = list(session.execute(select(Delivery)).scalars().all())
    assert len(deliveries) == 1 and deliveries[0].status == "pending"


def test_entry_then_exit_with_consecutive_absence_hysteresis(session_factory):
    doc = _doc([{"id": "en", "trigger": "exit", "channels": ["t"], "exit_after": 2}])
    workflow, revision = _activate(session_factory, doc)
    run1 = _run_row(session_factory, workflow, revision)
    _evaluate(session_factory, workflow, revision, doc, run1, [_member("NSE:A", rank=1)])
    # run 2: A absent once -> no exit yet (threshold 2)
    run2 = _run_row(session_factory, workflow, revision, bucket=T0 + timedelta(days=1))
    _evaluate(session_factory, workflow, revision, doc, run2, [_member("NSE:B", rank=1)],
              now=T0 + timedelta(days=1))
    states = _states(session_factory, workflow, revision, "en")
    assert states["NSE:A"].present is False
    assert states["NSE:A"].consecutive_absent == 1
    assert all(e.evidence["action"] != "exit" for e in _events(session_factory))
    # run 3: absent again -> exit fires
    run3 = _run_row(session_factory, workflow, revision, bucket=T0 + timedelta(days=2))
    _evaluate(session_factory, workflow, revision, doc, run3, [_member("NSE:B", rank=1)],
              now=T0 + timedelta(days=2))
    actions = [e.evidence["action"] for e in _events(session_factory)]
    assert actions.count("exit") == 1


def test_top_n_hysteresis_buffers_boundary_oscillation(session_factory):
    doc = _doc([{"id": "tn", "trigger": "top_n", "top_n": 10, "entry_rank": 10, "exit_rank": 15, "channels": ["t"]}])
    workflow, revision = _activate(session_factory, doc)
    run1 = _run_row(session_factory, workflow, revision)
    _evaluate(session_factory, workflow, revision, doc, run1,
              [_member("NSE:A", rank=8, score=20.0), _member("NSE:B", rank=9, score=19.0)])
    # run 2: A drifts to rank 12 — inside the exit band (<= 15): NO exit
    run2 = _run_row(session_factory, workflow, revision, bucket=T0 + timedelta(days=1))
    _evaluate(session_factory, workflow, revision, doc, run2,
              [_member("NSE:A", rank=12, score=12.0), _member("NSE:B", rank=9, score=19.0)],
              now=T0 + timedelta(days=1))
    assert all(e.evidence["action"] != "exit" for e in _events(session_factory))
    states = _states(session_factory, workflow, revision, "tn")
    assert states["NSE:A"].present is True
    # run 3: A falls to rank 20 — beyond exit band: exit fires
    run3 = _run_row(session_factory, workflow, revision, bucket=T0 + timedelta(days=2))
    _evaluate(session_factory, workflow, revision, doc, run3,
              [_member("NSE:A", rank=20, score=2.0), _member("NSE:B", rank=9, score=19.0)],
              now=T0 + timedelta(days=2))
    exits = [e for e in _events(session_factory) if e.evidence["action"] == "exit"]
    assert len(exits) == 1 and exits[0].evidence["instrument_key"] == "NSE:A"


def test_rank_delta_fires_on_threshold_cross(session_factory):
    doc = _doc([{"id": "rd", "trigger": "rank_delta", "rank_delta": 3, "channels": ["t"], "initial_match": False}])
    workflow, revision = _activate(session_factory, doc)
    run1 = _run_row(session_factory, workflow, revision)
    _evaluate(session_factory, workflow, revision, doc, run1,
              [_member("NSE:A", rank=1, score=30.0), _member("NSE:B", rank=5, score=10.0)])
    # run 2: B improves 5 -> 2 (delta 3): fires; A 1 -> 2 (delta 1): silent
    run2 = _run_row(session_factory, workflow, revision, bucket=T0 + timedelta(days=1))
    summary = _evaluate(
        session_factory, workflow, revision, doc, run2,
        [_member("NSE:A", rank=2, score=20.0), _member("NSE:B", rank=2, score=20.0)],
        now=T0 + timedelta(days=1),
    )
    # B: delta |2-5| = 3 >= 3 fires
    deltas = [e for e in _events(session_factory) if e.evidence["trigger"] == "rank_delta"]
    assert len(deltas) == 1
    assert deltas[0].evidence["instrument_key"] == "NSE:B"
    assert deltas[0].evidence["rank_delta"] == 3
    assert deltas[0].evidence["direction"] == "up"


def test_attachment_occurrence_key_is_idempotent_on_replay(session_factory):
    doc = _doc([{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}])
    workflow, revision = _activate(session_factory, doc)
    run = _run_row(session_factory, workflow, revision)
    results = [_member("NSE:A", rank=1)]
    _evaluate(session_factory, workflow, revision, doc, run, results)
    # a full replay of the SAME run (stale recovery attempt) publishes
    # nothing: the fenced CAS rejects the already-finalized run outright
    repo = ScreenerRunRepository(session_factory)
    _summary, transitions = prepare_attachments(
        workflow=workflow,
        revision=revision,
        document=parse_workflow_dict(doc),
        run=run,
        results=results,
        run_repo=repo,
        channel_resolver=_channels,
        owner_id="owner-1",
    )
    assert all(t.events == () for t in transitions)  # baseline advanced: nothing to fire
    assert repo.finalize_run(
        run.id, run.lease_owner,
        status="complete", as_of=run.scheduled_for,
        coverage={}, data_freshness={}, members=[],
        attachments=transitions,
    ) is False
    assert len(_events(session_factory)) == 1


def test_attachment_event_cap_bounds_storms(session_factory):
    doc = _doc([{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}])
    workflow, revision = _activate(session_factory, doc, name="scr-cap")
    run = _run_row(session_factory, workflow, revision)
    results = [_member(f"NSE:{i:03d}", rank=i, score=100.0 - i) for i in range(1, 151)]
    summary = _evaluate(
        session_factory, workflow, revision, doc, run, results,
        now=T0,  # 150 entrants on the first (initial_match) run
    )
    assert summary["events"] == 100  # hard cap
    assert summary["suppressed_events"] == 50


# ---------------------------------------------------------------------------
# scheduler wiring
# ---------------------------------------------------------------------------


def test_scheduler_executes_due_occurrence_and_finalizes(session_factory):
    doc = _doc([])
    workflow, revision = _activate(session_factory, doc)
    repo = SqlAlchemyWorkflowRepository(session_factory)

    class _Gate:
        # active every day at session close
        def __call__(self, at):
            return True, "session"

    class _UniverseService:
        def latest_revision(self, owner_id, name):
            return {"revision": 4, "members": ["NSE:A", "NSE:B"]}

        def preview_membership(self, owner_id, kind, config):
            raise AssertionError("not used")

        def resolve_membership(self, owner_id, name):
            return {}

    scheduler = ScreenerScheduler(
        session_factory=session_factory,
        workflow_repo=repo,
        run_repo=ScreenerRunRepository(session_factory),
        pipeline=_StubPipeline(),
        universe_service=_UniverseService(),
        session_gate=_Gate(),
        owner_id="worker-a",
        poll_interval_s=30,
        lease_ttl_s=300,
    )
    now = T0 + timedelta(hours=20)
    executed = scheduler.poll_once(now=now)
    assert executed == 1
    runs = ScreenerRunRepository(session_factory).list_runs("owner-1", workflow.id)
    assert len(runs) == 1
    assert runs[0].status == "complete"
    assert runs[0].coverage["expected"] == 2
    assert runs[0].universe_revision == 4
    # second pass: same occurrence -> no duplicate run
    assert scheduler.poll_once(now=now + timedelta(seconds=60)) == 0
    assert len(ScreenerRunRepository(session_factory).list_runs("owner-1", workflow.id)) == 1


class _StubPipeline:
    def evaluate(self, document, members, *, as_of, context_loader=None, member_limit=None):
        from backend.screeners.runner import MemberResult

        results = [
            MemberResult(
                instrument_key=key, matched=True, exclusion_reason=None,
                values={"close": 100.0, "score": 5.0}, score=5.0,
                rank=index, passed=True,
            )
            for index, key in enumerate(members, start=1)
        ]
        return {
            "members": results,
            "coverage": {"expected": len(members), "evaluated": len(members),
                         "unavailable": 0, "unknown_conditions": 0,
                         "rank_value_missing": 0, "qualifying": len(members),
                         "complete": True},
            "data_freshness": {"as_of": as_of.isoformat()},
            "status": "complete",
        }
