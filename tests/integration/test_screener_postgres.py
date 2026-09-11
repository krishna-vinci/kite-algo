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

from backend.screeners.scheduler import ScreenerScheduler
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
    AttachmentPlan,
    AttachmentTask,
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
        published = repo.finalize_run(
            run.id, run.lease_owner,
            status="complete", as_of=T0,
            coverage={"expected": 2, "complete": True}, data_freshness={},
            members=[
                {"instrument_key": m.instrument_key, "passed": True,
                 "rank": m.rank, "score": m.score, "values": {}}
                for m in results
            ],
            attachment_plan=_attachment_plan(
                workflow, revision, doc, run, results,
                channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
            ),
            now=T0,
        )
        finished = repo.get_run(run.id)
        if not published:
            return 0, published
        return int((finished.coverage or {}).get("attachment_events_published", 0)), published

    first_events, first_published = evaluate()
    replay_events, replay_published = evaluate()  # crash-recovery replay of the same logical run
    assert first_events == 2 and first_published is True
    assert replay_events == 0 and replay_published is False

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


def _attachment_plan(workflow, revision, doc, run, members, channel_resolver):
    """The baseline-free plan the scheduler hands to ``finalize_run``."""
    return AttachmentPlan(
        owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
        run_id=run.id, scheduled_for=run.scheduled_for, screener_name=doc.name,
        tasks=tuple(
            AttachmentTask(attachment_id=attachment.id, spec=attachment, results=tuple(members))
            for attachment in doc.screener.attachments
        ),
        channel_resolver=channel_resolver,
    )


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


def _race_workflow(factory, name, attachments=None):
    """Screener workflow + claim/plan/publish helpers for interleaving tests.

    ``plan`` carries NO baseline state, so it can be built at any moment —
    including while the baseline is still empty — without freezing a stale
    comparison state into the run. Every baseline-dependent decision is made
    inside ``publish``, under the attachment lock.
    """
    attachments = attachments or [
        {"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}
    ]
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

    def plan(run, keys, ranks=None):
        ranks = ranks or {}
        members = [
            _member(key, ranks.get(key, index + 1), 10.0 - index)
            for index, key in enumerate(keys)
        ]
        payloads = [
            {"instrument_key": m.instrument_key, "passed": True, "rank": m.rank,
             "score": m.score, "values": {}}
            for m in members
        ]
        attachment_plan = AttachmentPlan(
            owner_id="owner-1", workflow_id=workflow.id, revision_id=revision.id,
            run_id=run.id, scheduled_for=run.scheduled_for, screener_name=doc.name,
            tasks=tuple(
                AttachmentTask(attachment_id=a.id, spec=a, results=tuple(members))
                for a in doc.screener.attachments
            ),
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
        )
        return attachment_plan, payloads

    def publish(run, attachment_plan, payloads):
        return repo.finalize_run(
            run.id, run.lease_owner, status="complete", as_of=run.scheduled_for,
            coverage={"expected": len(payloads)}, data_freshness={},
            members=payloads, attachment_plan=attachment_plan, now=run.scheduled_for,
        )

    return workflow, repo, claim, plan, publish


def _event_pairs(factory, workflow):
    return [
        (event.evidence["instrument_key"], event.evidence["action"])
        for event in _workflow_events(factory, workflow)
    ]


def test_newer_occurrence_publishing_first_suppresses_stale_older_run(clean_runs):
    """Two overlapping occurrences, the newer one publishes first: the older
    run's attachment publication is suppressed wholesale (no event derived
    from a superseded baseline, no baseline overwrite) while its run results
    still publish."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(factory, "pg-race-newer-first")

    # baseline run R1: silent init with A present
    r1 = claim(T0, "w1")
    p1, m1 = plan(r1, ["NSE:A"])
    assert publish(r1, p1, m1) is True
    assert _event_pairs(factory, workflow) == [("NSE:A", "entry")]  # initial_match

    # overlapping evaluation: R2 and R3 both plan while R1 owns the baseline
    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_plan, r2_members = plan(r2, ["NSE:A", "NSE:B"])
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_plan, r3_members = plan(r3, ["NSE:A", "NSE:C"])

    # newer occurrence publishes first
    assert publish(r3, r3_plan, r3_members) is True
    assert publish(r2, r2_plan, r2_members) is True

    # B's entry would be derived from a superseded baseline: suppressed whole
    assert _event_pairs(factory, workflow) == [("NSE:A", "entry"), ("NSE:C", "entry")]
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


def test_older_occurrence_publishing_first_applies_over_locked_baseline(clean_runs):
    """Same overlap, opposite publication order: the older run publishes first
    and the newer run derives its transitions from the baseline the older run
    just wrote — including an exit for an instrument the newer run dropped —
    so the final baseline is the newer run's comparison state, never a
    resurrected superseded row."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(factory, "pg-race-older-first")

    r1 = claim(T0, "w1")
    p1, m1 = plan(r1, ["NSE:A"])
    assert publish(r1, p1, m1) is True

    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_plan, r2_members = plan(r2, ["NSE:A", "NSE:B"])
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_plan, r3_members = plan(r3, ["NSE:A", "NSE:C"])

    # older occurrence publishes first, newer applies after it
    assert publish(r2, r2_plan, r2_members) is True
    assert publish(r3, r3_plan, r3_members) is True

    events = _workflow_events(factory, workflow)
    assert [(event.evidence["instrument_key"], event.evidence["action"]) for event in events] == [
        ("NSE:A", "entry"),  # R1's initial_match entrant
        ("NSE:B", "entry"),
        ("NSE:C", "entry"),
        ("NSE:B", "exit"),  # B left the universe between R2 and R3
    ]
    assert [event.fired_at for event in events[:3]] == [T0, T0 + timedelta(days=1), T0 + timedelta(days=2)]
    assert events[3].fired_at == T0 + timedelta(days=2)
    states = _all_states(factory)
    assert {((attach, key), state.present, state.last_complete_run_id) for (attach, key), state in states.items()} == {
        (("en", "NSE:A"), True, r3.id),
        (("en", "NSE:B"), False, r3.id),  # exited, not left behind as present
        (("en", "NSE:C"), True, r3.id),
    }
    assert repo.get_run(r3.id).coverage["attachment_events_published"] == 2


def test_concurrent_occurrences_never_interleave_baseline_state(clean_runs):
    """Genuinely concurrent publication of two overlapping occurrences: the
    baseline lock serializes them, so the surviving state is exactly one of
    the two sequential outcomes — never a mixture, and the newest occurrence
    always owns the shared instrument's comparison state."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(factory, "pg-race-concurrent")

    r1 = claim(T0, "w1")
    p1, m1 = plan(r1, ["NSE:A"])
    assert publish(r1, p1, m1) is True

    # both overlapping occurrences plan before either publishes, then race
    # the publication transaction
    r2 = claim(T0 + timedelta(days=1), "w2")
    r2_plan, r2_members = plan(r2, ["NSE:A", "NSE:B"])
    r3 = claim(T0 + timedelta(days=2), "w3")
    r3_plan, r3_members = plan(r3, ["NSE:A", "NSE:C"])

    barrier = threading.Barrier(2)

    def go(run, attachment_plan, payloads):
        barrier.wait(timeout=30)
        return publish(run, attachment_plan, payloads)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(go, r2, r2_plan, r2_members)
        newer = pool.submit(go, r3, r3_plan, r3_members)
        assert older.result(timeout=60) is True
        assert newer.result(timeout=60) is True

    pairs = sorted(_event_pairs(factory, workflow))
    states = {
        (attachment, key): (state.present, state.last_complete_run_id)
        for (attachment, key), state in _all_states(factory).items()
    }
    older_first = (
        # R2 publishes first: R3 then sees A present, C entering and B gone
        [("NSE:A", "entry"), ("NSE:B", "entry"), ("NSE:B", "exit"), ("NSE:C", "entry")],
        {
            ("en", "NSE:A"): (True, r3.id),
            ("en", "NSE:B"): (False, r3.id),
            ("en", "NSE:C"): (True, r3.id),
        },
    )
    newer_first = (
        # R3 publishes first: R2 is superseded and publishes nothing
        [("NSE:A", "entry"), ("NSE:C", "entry")],
        {("en", "NSE:A"): (True, r3.id), ("en", "NSE:C"): (True, r3.id)},
    )
    assert (pairs, states) in (older_first, newer_first)
    # invariants regardless of interleaving
    assert states[("en", "NSE:A")] == (True, r3.id)
    assert pairs.count(("NSE:C", "entry")) == 1
    assert pairs.count(("NSE:A", "entry")) == 1
    assert len(pairs) == len(set(pairs))  # no instrument+action notified twice
    fired = [event.fired_at for event in _workflow_events(factory, workflow)]
    assert fired == sorted(fired)  # publications land in schedule order


def test_concurrent_first_runs_never_invert_empty_baseline(clean_runs):
    """Two overlapping FIRST occurrences: the baseline is still empty, so
    there is no baseline row to lock. The serialization must still hold —
    the newer occurrence always ends up owning the shared instrument's
    comparison state and the older one can never resurrect superseded rows."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(
        factory, "pg-race-empty",
        attachments=[{"id": "en", "trigger": "entry", "channels": ["t"]}],
    )

    r_old = claim(T0 + timedelta(days=1), "w-old")
    old_plan, old_members = plan(r_old, ["NSE:A", "NSE:B"])
    r_new = claim(T0 + timedelta(days=2), "w-new")
    new_plan, new_members = plan(r_new, ["NSE:A", "NSE:C"])

    barrier = threading.Barrier(2)

    def go(run, attachment_plan, payloads):
        barrier.wait(timeout=30)
        return publish(run, attachment_plan, payloads)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(go, r_old, old_plan, old_members)
        newer = pool.submit(go, r_new, new_plan, new_members)
        assert older.result(timeout=60) is True
        assert newer.result(timeout=60) is True

    states = {
        (attachment, key): (state.present, state.last_rank, state.last_complete_run_id)
        for (attachment, key), state in _all_states(factory).items()
    }
    older_first = {
        # R_old initializes silently; R_new then compares against it: C enters,
        # B (dropped from the universe) exits
        ("en", "NSE:A"): (True, 1, r_new.id),
        ("en", "NSE:B"): (False, None, r_new.id),
        ("en", "NSE:C"): (True, 2, r_new.id),
    }
    newer_first = {
        # R_new initializes silently; R_old is superseded and writes nothing
        ("en", "NSE:A"): (True, 1, r_new.id),
        ("en", "NSE:C"): (True, 2, r_new.id),
    }
    assert states in (older_first, newer_first)
    assert states[("en", "NSE:A")][2] == r_new.id  # newest owns shared state
    assert repo.get_run(r_old.id).status == "complete"
    assert repo.get_run(r_new.id).status == "complete"


def test_two_runs_planned_on_empty_baseline_publish_one_entry_each(clean_runs):
    """The race that motivated computing transitions under the lock: two
    successive occurrences BOTH plan while the baseline is empty, and the
    older one publishes first. The newer run must derive its transitions from
    the baseline the older run just wrote — instrument A, present in both
    runs, gets exactly ONE entry notification (a precomputed transition would
    re-fire it), and the final baseline is the newer run's comparison state."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(
        factory, "pg-absent-baseline",
        attachments=[{"id": "en", "trigger": "entry", "channels": ["t"], "initial_match": True}],
    )

    r_old = claim(T0 + timedelta(days=1), "w-old")
    r_new = claim(T0 + timedelta(days=2), "w-new")
    old_plan, old_members = plan(r_old, ["NSE:A", "NSE:B"])
    new_plan, new_members = plan(r_new, ["NSE:A", "NSE:C"])
    assert _all_states(factory) == {}  # both plans were built on nothing

    assert publish(r_old, old_plan, old_members) is True
    assert publish(r_new, new_plan, new_members) is True

    pairs = _event_pairs(factory, workflow)
    entries = [key for key, action in pairs if action == "entry"]
    assert sorted(entries) == ["NSE:A", "NSE:B", "NSE:C"]
    assert entries.count("NSE:A") == 1  # present in both runs, notified once
    assert len(entries) == len(set(entries))  # no instrument ever enters twice
    assert repo.get_run(r_old.id).coverage["attachment_events_published"] == 2
    assert repo.get_run(r_new.id).coverage["attachment_events_published"] == 2

    states = {
        key: (state.present, state.last_rank, state.consecutive_absent, state.last_complete_run_id)
        for (_, key), state in _all_states(factory).items()
    }
    assert states == {
        "NSE:A": (True, 1, 0, r_new.id),   # entry from R1 not repeated by R2
        "NSE:B": (False, None, 1, r_new.id),  # dropped by R2's universe
        "NSE:C": (True, 2, 0, r_new.id),
    }


def test_exit_after_counter_advances_from_locked_baseline(clean_runs):
    """``exit_after=2`` under the same interleaving: the absence counter must
    advance from the baseline the older run published, never from the empty
    snapshot both runs planned against. The first absence increments the
    counter without notifying; the second crosses the threshold exactly once."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(
        factory, "pg-exit-after",
        attachments=[{"id": "ex", "trigger": "exit", "channels": ["t"], "exit_after": 2}],
    )

    r1 = claim(T0, "w1")
    r2 = claim(T0 + timedelta(days=1), "w2")
    r3 = claim(T0 + timedelta(days=2), "w3")
    # every run plans against the still-empty baseline
    p1, m1 = plan(r1, ["NSE:A"])
    p2, m2 = plan(r2, [])
    p3, m3 = plan(r3, [])

    assert publish(r1, p1, m1) is True  # silent baseline init: A present
    assert publish(r2, p2, m2) is True  # first absence: counter only
    assert _event_pairs(factory, workflow) == []
    states = _all_states(factory)
    assert (states[("ex", "NSE:A")].present, states[("ex", "NSE:A")].consecutive_absent,
            states[("ex", "NSE:A")].last_complete_run_id) == (False, 1, r2.id)

    assert publish(r3, p3, m3) is True  # second absence: exit fires once
    events = _workflow_events(factory, workflow)
    assert _event_pairs(factory, workflow) == [("NSE:A", "exit")]
    assert events[0].fired_at == T0 + timedelta(days=2)
    states = _all_states(factory)
    assert (states[("ex", "NSE:A")].present, states[("ex", "NSE:A")].consecutive_absent,
            states[("ex", "NSE:A")].last_complete_run_id) == (False, 2, r3.id)


def test_rank_delta_compares_against_locked_baseline(clean_runs):
    """``rank_delta`` under the same interleaving: the delta must be measured
    against the rank the older run recorded, not against an empty baseline
    (which would silently drop the notification)."""
    factory = clean_runs
    workflow, repo, claim, plan, publish = _race_workflow(
        factory, "pg-rank-delta",
        attachments=[{"id": "rd", "trigger": "rank_delta", "channels": ["t"], "rank_delta": 3}],
    )

    r1 = claim(T0, "w1")
    r2 = claim(T0 + timedelta(days=1), "w2")
    p1, m1 = plan(r1, ["NSE:A"], ranks={"NSE:A": 1})
    p2, m2 = plan(r2, ["NSE:A"], ranks={"NSE:A": 5})

    assert publish(r1, p1, m1) is True  # silent baseline: A at rank 1
    assert _event_pairs(factory, workflow) == []
    assert publish(r2, p2, m2) is True

    events = _workflow_events(factory, workflow)
    assert len(events) == 1
    evidence = events[0].evidence
    assert (evidence["instrument_key"], evidence["action"]) == ("NSE:A", "rank_change")
    assert (evidence["prev_rank"], evidence["rank"], evidence["rank_delta"], evidence["direction"]) == (1, 5, 4, "down")
    assert events[0].fired_at == T0 + timedelta(days=1)
    states = _all_states(factory)
    assert (states[("rd", "NSE:A")].last_rank, states[("rd", "NSE:A")].last_complete_run_id) == (5, r2.id)


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
    long_after_expiry = T0 + timedelta(seconds=120)
    published = repo.finalize_run(
        run.id, "solo-worker", status="complete", as_of=T0,
        coverage={}, data_freshness={},
        members=[{"instrument_key": "NSE:A", "passed": True, "rank": 1,
                  "score": 9.0, "values": {}}],
        attachment_plan=_attachment_plan(
            workflow, revision, doc, run, members,
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
        ),
        now=long_after_expiry,
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
    """A failure halfway through the publication transaction — after the run
    fence, after the FIRST attachment's events, deliveries and baseline rows
    were already written — leaves NOTHING behind; a retry of the same
    occurrence publishes exactly once."""
    factory = clean_runs
    workflow, revision = _seed_screener(
        factory, name="pg-atomic",
        attachments=[
            {"id": "a1", "trigger": "entry", "channels": ["t"], "initial_match": True},
            {"id": "a2", "trigger": "entry", "channels": ["bad"], "initial_match": True},
        ],
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
    payloads = [
        {"instrument_key": m.instrument_key, "passed": True,
         "rank": m.rank, "score": m.score, "values": {}}
        for m in members
    ]

    def poisoned_resolver(owner, names):
        # a2 resolves to a channel row that does not exist, so its delivery
        # insert violates the FK — by then a1's events, deliveries and
        # baseline rows are already written in the same transaction
        return {name: ("chan-t" if name == "t" else "chan-does-not-exist") for name in names}

    with pytest.raises(Exception):
        repo.finalize_run(
            run.id, "w", status="complete", as_of=T0,
            coverage={}, data_freshness={}, members=payloads,
            attachment_plan=_attachment_plan(
                workflow, revision, doc, run, members,
                channel_resolver=poisoned_resolver,
            ),
            now=T0,
        )
    _assert_nothing_published(factory, workflow)

    # unpoisoned retry publishes exactly once
    published = repo.finalize_run(
        run.id, "w", status="complete", as_of=T0,
        coverage={}, data_freshness={}, members=payloads,
        attachment_plan=_attachment_plan(
            workflow, revision, doc, run, members,
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
        ),
        now=T0,
    )
    assert published is True
    assert len(_workflow_events(factory, workflow)) == 6  # 2 attachments x 3 entrants
    with factory() as session:
        deliveries = session.execute(text("SELECT count(*) FROM deliveries")).scalar()
    assert int(deliveries) == 6
    assert len(_all_states(factory)) == 6
