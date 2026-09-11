"""Phase 3 PostgreSQL fault-injection suite (spec E-1/E-2/E-17/E-18/E-19).

Proves on a real PostgreSQL what SQLite cannot: occurrence-claim uniqueness
under concurrency (E-2), stale-owner takeover fencing with atomic run
publication, crash injection between claim and finalize, replay-idempotent
attachment events, and the single fenced publication transaction (run
status/results + attachment baselines + signal events + outbox entries
commit together or not at all). Runs against the ISOLATED disposable
PostgreSQL only:

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_screener_postgres.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.screeners.scheduler import ScreenerScheduler, prepare_attachments
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    Workflow,
    WorkflowRevision,
)
from backend.workflows.screener_repository import (
    AttachmentEvent,
    AttachmentTransition,
    ScreenerAttachmentState,
    ScreenerRun,
    ScreenerRunMember,
    ScreenerRunRepository,
)

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; PostgreSQL screener suite skipped",
)

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pg_session_factory():
    engine = create_engine(PG_URL, poolclass=NullPool)
    # isolate: truncate the screener tables this module owns
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE screener_run_member, screener_run, screener_attachment_state CASCADE"))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def clean_runs(pg_session_factory):
    with pg_session_factory() as session:
        session.execute(text("TRUNCATE screener_run_member, screener_run, screener_attachment_state CASCADE"))
        # real channel rows so delivery FKs hold on PostgreSQL
        session.execute(text(
            "INSERT INTO channel_references (id, owner_id, name, provider, destination) "
            "VALUES ('chan-t', 'owner-1', 't', 'mock', concat('{', chr(34), 'target', chr(34), ': ', chr(34), 'test', chr(34), '}')::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        ))
        # remove leftover seeded screener workflows from earlier attempts
        session.execute(text(
            "DELETE FROM delivery_attempts WHERE delivery_id IN "
            "(SELECT id FROM deliveries)"
        ))
        session.execute(text(
            "DELETE FROM deliveries WHERE event_id IN (SELECT id FROM signal_events)"
        ))
        session.execute(text("DELETE FROM signal_events"))
        session.execute(text(
            "DELETE FROM alert_subscriptions WHERE revision_id IN "
            "(SELECT id FROM workflow_revisions WHERE workflow_id IN "
            "(SELECT id FROM workflows WHERE name LIKE 'pg-%'))"
        ))
        session.execute(text(
            "DELETE FROM workflow_revisions WHERE workflow_id IN "
            "(SELECT id FROM workflows WHERE name LIKE 'pg-%')"
        ))
        session.execute(text("DELETE FROM workflows WHERE name LIKE 'pg-%'"))
        session.commit()
    return pg_session_factory


def _seed_screener(factory, owner_id="owner-1", name="pg-scr", attachments=None):
    repo = SqlAlchemyWorkflowRepository(factory)
    doc = {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "universe": {"union": [{"universe": "u"}]},
        "stages": [
            {"id": "scan", "type": "filter", "clock": "candle_close", "timeframe": "1d",
             "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]}}
        ],
        "alerts": [],
        "screener": {
            "schedule": {"every": "1d", "at": "session_close"},
            "rank": {"by": {"field": "change_pct"}, "direction": "desc"},
            "attachments": attachments or [],
        },
    }
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        owner_id, name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    return repo.get_workflow(workflow.id), repo.get_active_revision(workflow.id)


def _member(key, rank, score):
    from backend.screeners.runner import MemberResult

    return MemberResult(
        instrument_key=key, matched=True, exclusion_reason=None,
        values={"close": 100.0, "score": score}, score=score, rank=rank, passed=True,
    )


def test_concurrent_claims_produce_exactly_one_logical_run(clean_runs):
    """E-2: N workers racing the same occurrence -> one run row, one winner."""
    factory = clean_runs
    workflow, revision = _seed_screener(factory)
    repo = ScreenerRunRepository(factory)
    occurrence_key = f"{workflow.id}:{int(T0.timestamp())}"

    def claim(worker_name):
        return repo.claim_run(
            owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
            occurrence_key=occurrence_key, scheduled_for=T0,
            lease_owner=worker_name, lease_ttl_s=300, now=T0,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, [f"w-{i}" for i in range(6)]))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    with factory() as session:
        count = session.execute(
            select(ScreenerRun).where(ScreenerRun.workflow_id == workflow.id)
        ).scalars().all()
    assert len(count) == 1


def test_crash_after_claim_recovers_via_lease_takeover(clean_runs):
    """Worker dies after claiming (no finalize): the next pass takes over and
    completes the SAME logical run — no duplicate occurrence rows."""
    factory = clean_runs
    workflow, revision = _seed_screener(factory)
    repo = ScreenerRunRepository(factory)
    occurrence_key = f"{workflow.id}:{int(T0.timestamp())}"

    crashed = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=occurrence_key, scheduled_for=T0, lease_owner="dead-worker",
        lease_ttl_s=10, now=T0,
    )
    assert crashed is not None
    # ... worker dies; lease expires ...
    later = T0 + timedelta(seconds=60)
    recovered = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=occurrence_key, scheduled_for=T0, lease_owner="new-worker",
        lease_ttl_s=300, now=later,
    )
    assert recovered is not None and recovered.id == crashed.id
    assert recovered.lease_owner == "new-worker"
    published = repo.finalize_run(
        recovered.id, "new-worker",
        status="complete", as_of=T0,
        coverage={"expected": 2, "complete": True}, data_freshness={},
        members=[
            {"instrument_key": "NSE:A", "passed": True, "rank": 1, "score": 9.0, "values": {}},
            {"instrument_key": "NSE:B", "passed": True, "rank": 2, "score": 8.0, "values": {}},
        ],
        now=later,
    )
    assert published is True
    with factory() as session:
        runs = session.execute(select(ScreenerRun)).scalars().all()
        assert len(runs) == 1
        members = session.execute(
            select(ScreenerRunMember).where(ScreenerRunMember.run_id == runs[0].id)
        ).scalars().all()
        assert len(members) == 2


def test_finalize_is_atomic_members_and_status_together(clean_runs):
    """A failure while writing members must not leave a half-published run:
    status stays running and no member rows survive (E-1 analogue)."""
    factory = clean_runs
    workflow, revision = _seed_screener(factory)
    repo = ScreenerRunRepository(factory)
    run = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=f"{workflow.id}:{int(T0.timestamp())}", scheduled_for=T0,
        lease_owner="w", lease_ttl_s=300, now=T0,
    )
    with pytest.raises(Exception):
        # member payload violates the NOT NULL instrument_key -> the whole
        # finalize transaction (status update + members) rolls back
        repo.finalize_run(
            run.id, "w",
            status="complete", as_of=T0,
            coverage={}, data_freshness={},
            members=[{"instrument_key": None, "passed": True}],
            now=T0,
        )
    with factory() as session:
        row = session.execute(
            select(ScreenerRun).where(ScreenerRun.id == run.id)
        ).scalar_one()
        assert row.status == "running"
        assert row.completed_at is None
        members = session.execute(
            select(ScreenerRunMember).where(ScreenerRunMember.run_id == run.id)
        ).scalars().all()
        assert members == []


def test_attachment_events_idempotent_and_fenced_after_takeover(clean_runs):
    """Replays and stale owners cannot duplicate attachment events; the
    surviving event set is exactly one per (run, instrument)."""
    factory = clean_runs
    attachments = [{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}]
    workflow, revision = _seed_screener(factory, attachments=attachments)
    doc = parse_workflow_dict(
        SqlAlchemyWorkflowRepository(factory).get_active_revision(workflow.id).document
    )
    repo = ScreenerRunRepository(factory)
    run = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=f"{workflow.id}:{int(T0.timestamp())}", scheduled_for=T0,
        lease_owner="w", lease_ttl_s=300, now=T0,
    )
    results = [_member("NSE:A", 1, 9.0), _member("NSE:B", 2, 8.0)]

    def evaluate():
        summary, transitions = prepare_attachments(
            workflow=workflow, revision=revision, document=doc, run=run,
            results=results, run_repo=repo,
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
            owner_id="owner-1", now=T0,
        )
        published = repo.finalize_run(
            run.id, run.lease_owner,
            status="complete", as_of=T0,
            coverage={"expected": 2, "complete": True}, data_freshness={},
            members=[
                {"instrument_key": m.instrument_key, "passed": True,
                 "rank": m.rank, "score": m.score, "values": {}}
                for m in results
            ],
            attachments=transitions, now=T0,
        )
        return summary, published

    first, first_published = evaluate()
    replay, replay_published = evaluate()  # crash-recovery replay of the same logical run
    assert first["events"] == 2 and first_published is True
    assert replay["events"] == 0 and replay_published is False

    with factory() as session:
        events = session.execute(
            select(SignalEvent).where(SignalEvent.workflow_id == workflow.id)
        ).scalars().all()
        assert len(events) == 2
        keys = {e.occurrence_key for e in events}
        assert len(keys) == 2
        deliveries = session.execute(text("SELECT count(*) FROM deliveries")).scalar()
        assert int(deliveries) == 2  # 2 events x 1 channel, no duplicates


def test_scheduler_end_to_end_on_postgres(clean_runs):
    """Scenario 15: scheduler -> stored data stub -> persisted run ->
    attachment -> outbox, all through the production repository path."""
    factory = clean_runs
    attachments = [{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}]
    workflow, revision = _seed_screener(factory, name="pg-e2e", attachments=attachments)

    class _Gate:
        def __call__(self, at):
            return True, "session"

    class _UniverseService:
        def latest_revision(self, owner_id, name):
            return {"revision": 11, "members": ["NSE:A", "NSE:B", "NSE:C"]}

        def preview_membership(self, *a):
            raise AssertionError

        def resolve_membership(self, *a):
            return {}

    class _Pipeline:
        def evaluate(self, document, members, *, as_of, context_loader=None, member_limit=None):
            ranked = sorted(members)
            results = [
                _member(key, index + 1, 10.0 - index)
                for index, key in enumerate(ranked)
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

    scheduler = ScreenerScheduler(
        session_factory=factory,
        workflow_repo=SqlAlchemyWorkflowRepository(factory),
        run_repo=ScreenerRunRepository(factory),
        pipeline=_Pipeline(),
        universe_service=_UniverseService(),
        session_gate=_Gate(),
        owner_id="pg-worker",
    )
    executed = scheduler.poll_once(now=T0 + timedelta(hours=20))
    assert executed == 1
    repo = ScreenerRunRepository(factory)
    runs = repo.list_runs("owner-1", workflow.id)
    assert len(runs) == 1 and runs[0].status == "complete"
    assert runs[0].coverage["expected"] == 3
    assert runs[0].universe_revision == 11
    members = repo.run_members(runs[0].id)
    assert [m.rank for m in members] == [1, 2, 3]

    with factory() as session:
        events = session.execute(
            select(SignalEvent).where(SignalEvent.workflow_id == workflow.id)
        ).scalars().all()
        assert len(events) == 3  # initial_match entrants


# ---------------------------------------------------------------------------
# publication transaction boundary (Phase 3 hardening)
#
# Run status/results + attachment baselines + signal events + outbox
# deliveries must publish in ONE fenced transaction: ownership is validated
# before anything becomes visible, rejected/stale executions leave no side
# effects, and overlapping occurrences can never invert baseline chronology.
# ---------------------------------------------------------------------------


def _outcome(members, as_of, status="complete"):
    ranked = sorted(members)
    results = [_member(key, index + 1, 10.0 - index) for index, key in enumerate(ranked)]
    return {
        "members": results,
        "coverage": {"expected": len(members), "evaluated": len(members),
                     "unavailable": 0, "unknown_conditions": 0,
                     "rank_value_missing": 0, "qualifying": len(members),
                     "complete": status == "complete"},
        "data_freshness": {"as_of": as_of.isoformat()},
        "status": status,
    }


class _StubPipeline:
    """Configurable pipeline outcome with an optional evaluation hook."""

    def __init__(self, status="complete", on_evaluate=None):
        self.status = status
        self.on_evaluate = on_evaluate

    def evaluate(self, document, members, *, as_of, context_loader=None, member_limit=None):
        if self.on_evaluate is not None:
            self.on_evaluate()
        return _outcome(members, as_of, self.status)


class _AlwaysOpenGate:
    def __call__(self, at):
        return True, "session"


class _IndexUniverse:
    def __init__(self, members):
        self.members = members

    def latest_revision(self, owner_id, name):
        return {"revision": 7, "members": list(self.members)}

    def preview_membership(self, *a):
        raise AssertionError

    def resolve_membership(self, *a):
        return {}


def _make_scheduler(factory, owner_id="pg-worker", pipeline=None):
    return ScreenerScheduler(
        session_factory=factory,
        workflow_repo=SqlAlchemyWorkflowRepository(factory),
        run_repo=ScreenerRunRepository(factory),
        pipeline=pipeline or _StubPipeline(),
        universe_service=_IndexUniverse(["NSE:A", "NSE:B", "NSE:C"]),
        session_gate=_AlwaysOpenGate(),
        owner_id=owner_id,
        channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
    )


def _entry_attachment():
    return [{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}]


def _only_run(factory, workflow):
    runs = ScreenerRunRepository(factory).list_runs("owner-1", workflow.id)
    assert len(runs) == 1
    return runs[0]


def _workflow_events(factory, workflow):
    with factory() as session:
        return list(
            session.execute(
                select(SignalEvent).where(SignalEvent.workflow_id == workflow.id)
            ).scalars().all()
        )


def _all_states(factory):
    with factory() as session:
        return {
            (state.attachment_id, state.instrument_key): state
            for state in session.execute(select(ScreenerAttachmentState)).scalars().all()
        }


def _assert_nothing_published(factory, workflow):
    """No finalized run, no members, no events, no deliveries, no baseline."""
    with factory() as session:
        runs = session.execute(
            select(ScreenerRun).where(ScreenerRun.workflow_id == workflow.id)
        ).scalars().all()
        assert len(runs) == 1 and runs[0].status == "running"
        assert runs[0].completed_at is None
        assert session.execute(select(ScreenerRunMember)).scalars().all() == []
        assert session.execute(select(SignalEvent)).scalars().all() == []
        deliveries = session.execute(text("SELECT count(*) FROM deliveries")).scalar()
        assert int(deliveries) == 0
    assert _all_states(factory) == {}


def test_failure_before_publication_leaves_zero_side_effects(clean_runs, monkeypatch):
    """Failure injected after attachment preparation but before publication:
    no result publication, no baseline advancement, no event, no delivery."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-pubfail", attachments=_entry_attachment()
    )
    scheduler = _make_scheduler(factory)
    repo = scheduler.run_repo

    def boom(*args, **kwargs):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(repo, "finalize_run", boom)
    assert scheduler.poll_once(now=T0 + timedelta(hours=20)) == 0
    _assert_nothing_published(factory, workflow)


def test_retry_after_failure_publishes_exactly_once(clean_runs, monkeypatch):
    """Same scenario, then the recovery pass (lease expiry -> takeover ->
    re-evaluate -> publish): exactly ONE logical publication, no duplicates."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-retry", attachments=_entry_attachment()
    )
    scheduler = _make_scheduler(factory)
    repo = scheduler.run_repo

    def boom(*args, **kwargs):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(repo, "finalize_run", boom)
    assert scheduler.poll_once(now=T0 + timedelta(hours=20)) == 0

    monkeypatch.undo()  # the interrupted worker is gone; a fresh pass recovers
    assert scheduler.poll_once(now=T0 + timedelta(hours=20) + timedelta(seconds=600)) == 1

    run = _only_run(factory, workflow)
    assert run.status == "complete"
    assert run.coverage["attachment_events_published"] == 3
    events = _workflow_events(factory, workflow)
    assert len(events) == 3  # one per instrument, exactly once
    assert len({event.occurrence_key for event in events}) == 3
    assert {event.evidence["run_id"] for event in events} == {str(run.id)}
    with factory() as session:
        deliveries = session.execute(
            text("SELECT count(*) FROM deliveries")
        ).scalar()
    assert int(deliveries) == 3
    states = _all_states(factory)
    assert {key for _, key in states} == {"NSE:A", "NSE:B", "NSE:C"}
    assert all(state.present for state in states.values())


def test_takeover_during_evaluation_fences_stale_worker(clean_runs):
    """A lease taken over while the original worker is still evaluating: the
    stale worker publishes NOTHING (no results, events, outbox, baseline);
    the new owner later produces the single logical publication."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-takeover-race", attachments=_entry_attachment()
    )
    repo = ScreenerRunRepository(factory)
    started = threading.Event()
    release = threading.Event()

    def block_until_released():
        started.set()
        assert release.wait(timeout=30), "test did not release the evaluator"

    stale_scheduler = _make_scheduler(
        factory, owner_id="worker-a",
        pipeline=_StubPipeline(on_evaluate=block_until_released),
    )
    stale_scheduler.lease_ttl_s = 60

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: stale_scheduler.poll_once(now=T0 + timedelta(hours=20))
        )
        assert started.wait(timeout=30)
        # the victim claimed its occurrence and is mid-evaluation: take the
        # run over with the SAME occurrence (expired lease -> CAS takeover)
        victim = _only_run(factory, workflow)
        assert victim.status == "running" and victim.lease_owner == "worker-a"
        # the evaluating worker's lease has lapsed: take the SAME occurrence
        takeover_now = victim.lease_expires_at + timedelta(seconds=1)
        taken = repo.claim_run(
            owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
            occurrence_key=victim.occurrence_key,
            scheduled_for=victim.scheduled_for,
            lease_owner="worker-b", lease_ttl_s=60,
            now=takeover_now,
        )
        assert taken is not None and taken.id == victim.id
        release.set()
        future.result(timeout=30)  # stale worker finishes its fenced publish

    _assert_nothing_published(factory, workflow)
    run = _only_run(factory, workflow)
    assert run.status == "running" and run.lease_owner == "worker-b"

    fresh_scheduler = _make_scheduler(factory, owner_id="worker-b")
    assert fresh_scheduler.poll_once(
        now=takeover_now + timedelta(seconds=600)
    ) == 1
    run = _only_run(factory, workflow)
    assert run.status == "complete"
    assert len(_workflow_events(factory, workflow)) == 3
    states = _all_states(factory)
    assert {key for _, key in states} == {"NSE:A", "NSE:B", "NSE:C"}


def _race_workflow(factory, name):
    """Workflow with a plain entry attachment (first run = silent baseline)."""
    attachments = [{"id": "en", "trigger": "entry", "channels": ["t"]}]
    workflow, revision = _seed_screener(factory, name=name, attachments=attachments)
    doc = parse_workflow_dict(
        SqlAlchemyWorkflowRepository(factory).get_active_revision(workflow.id).document
    )
    repo = ScreenerRunRepository(factory)

    def claim(bucket, owner):
        return repo.claim_run(
            owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
            occurrence_key=f"{workflow.id}:{int(bucket.timestamp())}",
            scheduled_for=bucket, lease_owner=owner, lease_ttl_s=3600, now=bucket,
        )

    def prepare(run, keys, bucket):
        members = [_member(key, index + 1, 10.0 - index) for index, key in enumerate(keys)]
        summary, transitions = prepare_attachments(
            workflow=workflow, revision=revision, document=doc, run=run,
            results=members, run_repo=repo,
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
            owner_id="owner-1", now=bucket,
        )
        payloads = [
            {"instrument_key": m.instrument_key, "passed": True, "rank": m.rank,
             "score": m.score, "values": {}}
            for m in members
        ]
        return summary, transitions, payloads

    def publish(run, transitions, payloads, bucket):
        return repo.finalize_run(
            run.id, run.lease_owner, status="complete", as_of=bucket,
            coverage={"expected": len(payloads)}, data_freshness={},
            members=payloads, attachments=transitions, now=bucket,
        )

    return workflow, repo, claim, prepare, publish


def test_newer_occurrence_publishing_first_suppresses_stale_older_run(clean_runs):
    """Two overlapping occurrences evaluate against the SAME baseline, the
    newer one publishes first: the older run's attachment publication is
    suppressed wholesale (no double notification, no baseline overwrite)
    while its run results still publish."""
    factory = clean_runs
    workflow, repo, claim, prepare, publish = _race_workflow(factory, "pg-race-newer-first")

    # baseline run R1: silent init with A present
    r1 = claim(T0, "w1")
    summary, transitions, payloads = prepare(r1, ["NSE:A"], T0)
    assert summary["events"] == 0
    assert publish(r1, transitions, payloads, T0) is True

    # overlapping evaluation: R2 and R3 both prepare against R1's baseline
    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_prep = prepare(r2, ["NSE:A", "NSE:B"], T0 + timedelta(days=1))
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_prep = prepare(r3, ["NSE:A", "NSE:C"], T0 + timedelta(days=2))

    # newer occurrence publishes first
    assert publish(r3, *r3_prep[1:], T0 + timedelta(days=2)) is True
    assert publish(r2, *r2_prep[1:], T0 + timedelta(days=1)) is True

    events = _workflow_events(factory, workflow)
    assert [(event.evidence["instrument_key"], event.evidence["action"]) for event in events] == [
        ("NSE:C", "entry")
    ]  # B's entry was computed from a superseded baseline: suppressed
    states = _all_states(factory)
    assert {((attach, key), state.present, state.last_complete_run_id) for (attach, key), state in states.items()} == {
        (("en", "NSE:A"), True, r3.id),
        (("en", "NSE:C"), True, r3.id),
    }  # older run wrote nothing
    run2 = repo.get_run(r2.id)
    assert run2.status == "complete"  # results publish regardless
    assert run2.coverage["attachment_events_stale_suppressed"] == 1
    assert run2.coverage["attachment_events_published"] == 0
    with factory() as session:
        members = session.execute(
            select(ScreenerRunMember).where(ScreenerRunMember.run_id == r2.id)
        ).scalars().all()
    assert {m.instrument_key for m in members} == {"NSE:A", "NSE:B"}


def test_older_occurrence_publishing_first_cannot_clobber_newer_state(clean_runs):
    """Same overlap, opposite publication order: the older run's event
    publishes (valid against the then-current baseline) and the newer run
    applies over it — the final baseline is the newer run's comparison
    state, identical to the newer-first interleaving."""
    factory = clean_runs
    workflow, repo, claim, prepare, publish = _race_workflow(factory, "pg-race-older-first")

    r1 = claim(T0, "w1")
    _summary, transitions, payloads = prepare(r1, ["NSE:A"], T0)
    assert publish(r1, transitions, payloads, T0) is True

    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_prep = prepare(r2, ["NSE:A", "NSE:B"], T0 + timedelta(days=1))
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_prep = prepare(r3, ["NSE:A", "NSE:C"], T0 + timedelta(days=2))

    # older occurrence publishes first, newer applies after it
    assert publish(r2, *r2_prep[1:], T0 + timedelta(days=1)) is True
    assert publish(r3, *r3_prep[1:], T0 + timedelta(days=2)) is True

    events = _workflow_events(factory, workflow)
    assert [(event.evidence["instrument_key"], event.fired_at) for event in events] == [
        ("NSE:B", T0 + timedelta(days=1)),
        ("NSE:C", T0 + timedelta(days=2)),
    ]  # chronological, each observed exactly once
    states = _all_states(factory)
    assert {((attach, key), state.present, state.last_complete_run_id) for (attach, key), state in states.items()} == {
        (("en", "NSE:A"), True, r3.id),
        (("en", "NSE:B"), True, r2.id),  # newer run never evaluated B: row persists
        (("en", "NSE:C"), True, r3.id),
    }
    run3 = repo.get_run(r3.id)
    assert run3.coverage["attachment_events_published"] == 1


def test_concurrent_occurrences_never_interleave_baseline_state(clean_runs):
    """Genuinely concurrent publication of two overlapping occurrences: the
    baseline lock serializes them, so the surviving state is exactly one of
    the two sequential outcomes — never a mixture, and the newest occurrence
    always owns the shared instrument's comparison state."""
    factory = clean_runs
    workflow, repo, claim, prepare, publish = _race_workflow(factory, "pg-race-concurrent")

    r1 = claim(T0, "w1")
    _summary, transitions, payloads = prepare(r1, ["NSE:A"], T0)
    assert publish(r1, transitions, payloads, T0) is True

    # both overlapping occurrences evaluate against R1's baseline before either
    # publishes, then race the publication transaction
    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_prep = prepare(r2, ["NSE:A", "NSE:B"], T0 + timedelta(days=1))
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_prep = prepare(r3, ["NSE:A", "NSE:C"], T0 + timedelta(days=2))

    barrier = threading.Barrier(2)

    def go(run, prep, bucket):
        barrier.wait(timeout=30)
        return publish(run, *prep[1:], bucket)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(go, r2, r2_prep, T0 + timedelta(days=1))
        newer = pool.submit(go, r3, r3_prep, T0 + timedelta(days=2))
        assert older.result(timeout=60) is True
        assert newer.result(timeout=60) is True

    events = sorted(
        (event.evidence["instrument_key"], event.fired_at)
        for event in _workflow_events(factory, workflow)
    )
    states = {
        (attachment, key): (state.present, state.last_complete_run_id)
        for (attachment, key), state in _all_states(factory).items()
    }
    older_first = (
        [("NSE:B", T0 + timedelta(days=1)), ("NSE:C", T0 + timedelta(days=2))],
        {
            ("en", "NSE:A"): (True, r3.id),
            ("en", "NSE:B"): (True, r2.id),
            ("en", "NSE:C"): (True, r3.id),
        },
    )
    newer_first = (
        [("NSE:C", T0 + timedelta(days=2))],
        {("en", "NSE:A"): (True, r3.id), ("en", "NSE:C"): (True, r3.id)},
    )
    assert (events, states) in (older_first, newer_first)
    # chronology invariant regardless of interleaving
    assert states[("en", "NSE:A")] == (True, r3.id)
    fired = [stamp for _, stamp in events]
    assert fired == sorted(fired) and len(fired) == len(set(fired))


def test_concurrent_first_runs_never_invert_empty_baseline(clean_runs):
    """Two overlapping FIRST occurrences: the baseline is still empty, so
    there is no baseline row to lock. The serialization must still hold —
    the newer occurrence always ends up owning the shared instrument's
    comparison state and the older one can never resurrect superseded rows."""
    factory = clean_runs
    workflow, repo, claim, prepare, publish = _race_workflow(factory, "pg-race-empty")

    r_old = claim(T0 + timedelta(days=1), "w-old")
    old_prep = prepare(r_old, ["NSE:A", "NSE:B"], T0 + timedelta(days=1))
    r_new = claim(T0 + timedelta(days=2), "w-new")
    new_prep = prepare(r_new, ["NSE:A", "NSE:C"], T0 + timedelta(days=2))
    assert old_prep[0]["events"] == 0 and new_prep[0]["events"] == 0  # silent init

    barrier = threading.Barrier(2)

    def go(run, prep, bucket):
        barrier.wait(timeout=30)
        return publish(run, *prep[1:], bucket)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(go, r_old, old_prep, T0 + timedelta(days=1))
        newer = pool.submit(go, r_new, new_prep, T0 + timedelta(days=2))
        assert older.result(timeout=60) is True
        assert newer.result(timeout=60) is True

    states = {
        (attachment, key): (state.present, state.last_rank, state.last_complete_run_id)
        for (attachment, key), state in _all_states(factory).items()
    }
    older_first = {
        ("en", "NSE:A"): (True, 1, r_new.id),
        ("en", "NSE:B"): (True, 2, r_old.id),
        ("en", "NSE:C"): (True, 2, r_new.id),
    }
    newer_first = {
        ("en", "NSE:A"): (True, 1, r_new.id),
        ("en", "NSE:C"): (True, 2, r_new.id),
    }
    assert states in (older_first, newer_first)
    assert states[("en", "NSE:A")][2] == r_new.id  # newest owns shared state


def test_expired_lease_without_takeover_still_publishes(clean_runs):
    """Fence semantics are OWNERSHIP, not wall time: a lease that expired
    but was never taken over does not fence the original owner out — the
    completed run (and its attachment publication) still lands."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-expired-lease", attachments=_entry_attachment()
    )
    repo = ScreenerRunRepository(factory)
    run = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=f"{workflow.id}:{int(T0.timestamp())}", scheduled_for=T0,
        lease_owner="solo-worker", lease_ttl_s=30, now=T0,
    )
    doc = parse_workflow_dict(
        SqlAlchemyWorkflowRepository(factory).get_active_revision(workflow.id).document
    )
    members = [_member("NSE:A", 1, 9.0)]
    _summary, transitions = prepare_attachments(
        workflow=workflow, revision=revision, document=doc, run=run,
        results=members, run_repo=repo,
        channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
        owner_id="owner-1", now=T0,
    )
    long_after_expiry = T0 + timedelta(seconds=120)
    published = repo.finalize_run(
        run.id, "solo-worker", status="complete", as_of=T0,
        coverage={}, data_freshness={},
        members=[{"instrument_key": "NSE:A", "passed": True, "rank": 1,
                  "score": 9.0, "values": {}}],
        attachments=transitions, now=long_after_expiry,
    )
    assert published is True
    assert repo.get_run(run.id).status == "complete"
    assert len(_workflow_events(factory, workflow)) == 1  # initial_match entrant


def test_partial_run_publishes_without_attachment_effects(clean_runs):
    """Partial runs publish their (partial) results but never evaluate
    attachments: no events, no deliveries, no baseline advancement (E-18)."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-partial", attachments=_entry_attachment()
    )
    scheduler = _make_scheduler(factory, pipeline=_StubPipeline(status="partial"))
    assert scheduler.poll_once(now=T0 + timedelta(hours=20)) == 1

    run = _only_run(factory, workflow)
    assert run.status == "partial"
    assert "attachment_events_published" not in (run.coverage or {})
    assert _workflow_events(factory, workflow) == []
    assert _all_states(factory) == {}
    with factory() as session:
        deliveries = session.execute(text("SELECT count(*) FROM deliveries")).scalar()
    assert int(deliveries) == 0


def test_mid_publication_failure_rolls_back_entire_transaction(clean_runs):
    """A failure halfway through the publication transaction (after the run
    fence, after earlier attachment events were inserted) leaves NOTHING
    behind; a retry of the same occurrence publishes exactly once."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-atomic", attachments=_entry_attachment()
    )
    repo = ScreenerRunRepository(factory)
    run = repo.claim_run(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        occurrence_key=f"{workflow.id}:{int(T0.timestamp())}", scheduled_for=T0,
        lease_owner="w", lease_ttl_s=300, now=T0,
    )
    doc = parse_workflow_dict(
        SqlAlchemyWorkflowRepository(factory).get_active_revision(workflow.id).document
    )
    members = [_member(key, index + 1, 10.0 - index) for index, key in enumerate(["NSE:A", "NSE:B", "NSE:C"])]
    _summary, transitions = prepare_attachments(
        workflow=workflow, revision=revision, document=doc, run=run,
        results=members, run_repo=repo,
        channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
        owner_id="owner-1", now=T0,
    )
    # poison the LAST prepared event's channel: the run CAS, the member rows,
    # the earlier events and their deliveries are already written when the
    # delivery FK violation aborts the transaction
    poisoned = []
    for transition in transitions:
        events = list(transition.events)
        last = events[-1]
        events[-1] = AttachmentEvent(
            attachment_id=last.attachment_id,
            occurrence_key=last.occurrence_key,
            fired_at=last.fired_at,
            evidence=last.evidence,
            channel_ids=("chan-does-not-exist",),
        )
        poisoned.append(AttachmentTransition(
            owner_id=transition.owner_id,
            workflow_id=transition.workflow_id,
            revision_id=transition.revision_id,
            attachment_id=transition.attachment_id,
            events=tuple(events),
            state_updates=transition.state_updates,
        ))

    with pytest.raises(Exception):
        repo.finalize_run(
            run.id, "w", status="complete", as_of=T0,
            coverage={}, data_freshness={},
            members=[
                {"instrument_key": m.instrument_key, "passed": True,
                 "rank": m.rank, "score": m.score, "values": {}}
                for m in members
            ],
            attachments=poisoned, now=T0,
        )
    _assert_nothing_published(factory, workflow)

    # unpoisoned retry publishes exactly once
    published = repo.finalize_run(
        run.id, "w", status="complete", as_of=T0,
        coverage={}, data_freshness={},
        members=[
            {"instrument_key": m.instrument_key, "passed": True,
             "rank": m.rank, "score": m.score, "values": {}}
            for m in members
        ],
        attachments=transitions, now=T0,
    )
    assert published is True
    assert len(_workflow_events(factory, workflow)) == 3
