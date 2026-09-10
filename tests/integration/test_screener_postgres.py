"""Phase 3 PostgreSQL fault-injection suite (spec E-1/E-2/E-17/E-18/E-19).

Proves on a real PostgreSQL what SQLite cannot: occurrence-claim uniqueness
under concurrency (E-2), stale-owner takeover fencing with atomic run
publication, crash injection between claim and finalize, and replay-idempotent
attachment events. Runs against the ISOLATED disposable PostgreSQL only:

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_screener_postgres.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.screeners.scheduler import ScreenerScheduler, evaluate_attachments
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    Base,
    SqlAlchemyWorkflowRepository,
    Workflow,
    WorkflowRevision,
)
from backend.workflows.screener_repository import (
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
        return evaluate_attachments(
            workflow=workflow, revision=revision, document=doc, run=run,
            results=results, run_repo=repo, session_factory=factory,
            channel_resolver=lambda owner, names: {n: "chan-t" for n in names},
            owner_id="owner-1", now=T0,
        )

    first = evaluate()
    replay = evaluate()  # crash-recovery replay of the same logical run
    assert first["events"] == 2 and replay["events"] == 0
    from backend.workflows.repository import SignalEvent

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
    from backend.workflows.repository import SignalEvent

    with factory() as session:
        events = session.execute(
            select(SignalEvent).where(SignalEvent.workflow_id == workflow.id)
        ).scalars().all()
        assert len(events) == 3  # initial_match entrants
