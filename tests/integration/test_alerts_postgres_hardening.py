"""Phase 1.5 PostgreSQL fault-injection suite (spec E-1..E-3 semantics).

Exercises the multi-process behaviors that SQLite tests cannot prove:
``FOR UPDATE SKIP LOCKED`` delivery claims, durable evaluation-ownership
leases with takeover fencing, occurrence deduplication under concurrency,
and event+checkpoint+outbox atomicity under injected commit failures.

Runs against an ISOLATED disposable PostgreSQL only (never live data). Set
``ALERTS_TEST_DATABASE_URL`` to enable; the module skips cleanly when it is
absent. Migrations must be applied first:

    docker run -d --name kite-test-postgres -e POSTGRES_PASSWORD=testonly \\
      -e POSTGRES_DB=kite_test -p 15433:5432 postgres:16-alpine
    DATABASE_URL='postgresql+psycopg2://postgres:testonly@127.0.0.1:15433/kite_test' \\
      alembic -c backend/alembic.ini upgrade head
    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
      pytest tests/integration/test_alerts_postgres_hardening.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

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
from backend.workflows.repository import (
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.service import EvaluationService

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; PostgreSQL fault-injection suite skipped",
)

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(PG_URL, poolclass=NullPool)
    with engine.begin() as conn:
        for table in (
            "delivery_attempts",
            "deliveries",
            "signal_events",
            "evaluation_checkpoints",
            "evaluation_ownership",
            "alert_subscriptions",
            "workflow_revisions",
            "workflows",
            "channel_references",
        ):
            conn.execute(text(f"DELETE FROM public.{table}"))
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


def _ltp_doc(level=100.0, trigger="once", name="pg-hardening"):
    return WorkflowDocument(
        version=1,
        name=name,
        instruments=(InstrumentRef(symbol="RELIANCE", exchange="NSE"),),
        session="nse_equity",
        stages=(
            Stage(
                id="px",
                type="signal",
                clock="ltp",
                timeframe=None,
                conditions=(
                    Condition(
                        Operand(kind="field", name="ltp"),
                        "crosses_above",
                        Operand(kind="value", value=level),
                    ),
                ),
            ),
        ),
        alerts=(AlertSpec(id="a1", source="px", trigger=trigger, channels=("c1",)),),
    )


def _activate(repo, doc, session_factory):
    compiled = compile_document(doc)
    workflow, revision = repo.create_workflow(
        "owner-pg", doc.name, compiled.document.to_document_dict(), compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)
    return active


def _subs_for(repo, revision):
    """Subscriptions of THIS revision only (the module DB accumulates)."""
    return [s for s in repo.list_active_subscriptions() if s.revision_id == revision.id]


# ---------------------------------------------------------------------------
# E-3: evaluation ownership — acquisition, renewal, expiry, takeover, fencing
# ---------------------------------------------------------------------------


def test_ownership_takeover_and_stale_owner_fencing(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    revision = _activate(repo, _ltp_doc(name="pg-own"), session_factory)
    sub = _subs_for(repo, revision)[0]
    key = sub.instrument_key

    epoch_a = repo.claim_evaluation(sub.id, key, "worker-a", lease_seconds=60, now=T0)
    assert epoch_a == 1

    # worker-b cannot take a live lease
    assert repo.claim_evaluation(
        sub.id, key, "worker-b", lease_seconds=60, now=T0 + timedelta(seconds=1),
    ) is None
    # worker-a renews and stays on the same epoch
    assert repo.claim_evaluation(
        sub.id, key, "worker-a", lease_seconds=60, now=T0 + timedelta(seconds=2),
    ) == 1

    # after the lease expires worker-b takes over and the epoch increments
    epoch_b = repo.claim_evaluation(
        sub.id, key, "worker-b", lease_seconds=60, now=T0 + timedelta(seconds=120),
    )
    assert epoch_b == 2

    # the stale worker-a epoch is fenced at the transaction boundary
    from backend.workflows.repository import LeaseConflict

    with pytest.raises(LeaseConflict):
        repo.assert_evaluation_owner(
            sub.id, key, "worker-a", epoch_a, now=T0 + timedelta(seconds=121),
        )
    # the new owner passes
    repo.assert_evaluation_owner(
        sub.id, key, "worker-b", epoch_b, now=T0 + timedelta(seconds=121),
    )


# ---------------------------------------------------------------------------
# E-2: two workers racing the same occurrence -> exactly one event
# ---------------------------------------------------------------------------


def test_concurrent_occurrence_dedup_yields_one_event(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    revision = _activate(repo, _ltp_doc(trigger="on_transition", name="pg-race"), session_factory)
    sub = _subs_for(repo, revision)[0]
    occurrence = (
        f"{sub.workflow_id}:{sub.revision_id}:{sub.id}:a1:"
        f"{sub.instrument_key}:{T0.isoformat()}"
    )

    def _record(owner):
        session = session_factory()
        try:
            try:
                repo.record_signal(
                    sub.id, occurrence, fired_at=T0, evidence={"worker": owner},
                    channel_ids=[], now=T0, db=session,
                )
                session.commit()
                return "committed"
            except Exception:
                session.rollback()
                return "skipped"
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_record, "a"), pool.submit(_record, "b")]
        outcomes = [f.result() for f in futures]

    assert outcomes.count("committed") == 1
    session = session_factory()
    try:
        events = session.execute(select(SignalEvent)).scalars().all()
        assert len(events) == 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# E-1: event + outbox + checkpoint atomicity under an injected commit failure
# ---------------------------------------------------------------------------


def _commit_failing_factory(inner_factory, fail_on_commit=1):
    """sessionmaker wrapper whose Nth commit raises (simulated crash)."""
    state = {"n": 0}

    def factory():
        session = inner_factory()
        real_commit = session.commit

        def commit():
            state["n"] += 1
            if state["n"] == fail_on_commit:
                session.rollback()
                raise RuntimeError("injected crash between writes and commit")
            return real_commit()

        session.commit = commit
        return session

    return factory


def test_signal_deliveries_checkpoint_rollback_together(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    revision = _activate(repo, _ltp_doc(trigger="on_transition", name="pg-atomic"), session_factory)
    sub = _subs_for(repo, revision)[0]

    def _resolver(owner, names):
        return {name: "channel-1" for name in names}

    crashing = EvaluationService(
        repo, _commit_failing_factory(session_factory, fail_on_commit=1),
        channel_resolver=_resolver,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        crashing.handle_observation(sub, Observation(ts=T0, epoch_id="boot-1", ltp=99.0))

    session = session_factory()
    try:
        # nothing from the crashed evaluation persisted — the trio rolls back
        surviving = session.execute(
            select(SignalEvent).where(SignalEvent.subscription_id == sub.id)
        ).scalars().all()
        assert surviving == []
        assert int(session.execute(
            text("SELECT COUNT(*) FROM public.deliveries d JOIN public.signal_events e "
                 "ON e.id = d.event_id WHERE e.subscription_id = :sub"),
            {"sub": sub.id},
        ).scalar()) == 0
        assert int(session.execute(
            text("SELECT COUNT(*) FROM public.evaluation_checkpoints "
                 "WHERE subscription_id = :sub"),
            {"sub": sub.id},
        ).scalar()) == 0
        assert int(session.execute(
            text("SELECT COUNT(*) FROM public.evaluation_ownership "
                 "WHERE subscription_id = :sub"),
            {"sub": sub.id},
        ).scalar()) == 0
    finally:
        session.close()

    # the same observation evaluates cleanly afterwards (no partial state)
    service = EvaluationService(repo, session_factory, channel_resolver=_resolver)
    result = service.handle_observation(sub, Observation(ts=T0, epoch_id="boot-1", ltp=99.0))
    assert result.emitted is False  # first observation initializes only


# ---------------------------------------------------------------------------
# F4/E-20: delivery claim/reclaim under SKIP LOCKED; lease expiry reclaim
# ---------------------------------------------------------------------------


def _seed_deliveries(session_factory, repo, sub_id, count=4):
    """Create ``count`` signal events with one pending delivery each.

    Deliveries are unique per (event, channel), so distinct events are the
    way to seed a claimable pool for one destination.
    """
    notif = SqlAlchemyNotificationRepository(session_factory)
    session = session_factory()
    try:
        channel = notif.upsert_channel(
            "owner-pg", "c1", "ntfy", {}, "NTFY_URL", True, db=session,
        )
        prefix = str(sub_id)[:8]
        ids = []
        for i in range(count):
            event = repo.record_signal(
                sub_id,
                f"occ-{sub_id}-{i}",
                fired_at=T0,
                evidence={},
                channel_ids=[],
                now=T0,
                db=session,
            )
            delivery_id = f"{prefix}-d{i}"
            session.add(Delivery(
                id=delivery_id, event_id=event.id, channel_id=channel.id,
                status="pending", created_at=T0 + timedelta(seconds=i), updated_at=T0,
            ))
            ids.append((channel.id, event.id, delivery_id))
        session.commit()
        return ids
    finally:
        session.close()


def test_delivery_claims_are_disjoint_and_expired_leases_reclaim(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    revision = _activate(repo, _ltp_doc(trigger="on_transition", name="pg-claims"), session_factory)
    sub = _subs_for(repo, revision)[0]

    notif = SqlAlchemyNotificationRepository(session_factory)
    _seed_deliveries(session_factory, repo, sub.id, count=4)
    expected_ids = {f"{str(sub.id)[:8]}-d{i}" for i in range(4)}

    def _claim(_worker):
        return {d.id for d in notif.claim_deliveries(now=T0, limit=4, lease_seconds=30)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_claim, "a"), pool.submit(_claim, "b")]
        claimed_a, claimed_b = futures[0].result(), futures[1].result()

    # two claims never return the same delivery (SKIP LOCKED on PostgreSQL)
    assert not (claimed_a & claimed_b)
    assert claimed_a | claimed_b == expected_ids

    # while the lease is live nothing new is claimable
    assert notif.claim_deliveries(now=T0 + timedelta(seconds=5), limit=4) == []

    # after the lease expires, the crashed worker's rows are reclaimable
    again = {d.id for d in notif.claim_deliveries(now=T0 + timedelta(seconds=31), limit=4)}
    assert again == expected_ids


def test_pre_send_lease_recheck_prevents_stale_completion(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    revision = _activate(repo, _ltp_doc(trigger="on_transition", name="pg-stale"), session_factory)
    sub = _subs_for(repo, revision)[0]
    _ids = _seed_deliveries(session_factory, repo, sub.id, count=2)
    target_id = _ids[0][2]

    notif = SqlAlchemyNotificationRepository(session_factory)
    claimed = notif.claim_deliveries(now=T0, limit=1, lease_seconds=30)
    assert [d.id for d in claimed] == [target_id]
    lease_until = claimed[0].lease_until

    # another worker steals the row right at lease expiry...
    stolen = notif.claim_deliveries(now=lease_until, limit=5, lease_seconds=30)
    assert stolen and stolen[0].id == target_id

    # ...the first worker's late completion must NOT overwrite the new owner
    from backend.workflows.repository import LeaseConflict

    with pytest.raises(LeaseConflict):
        notif.record_attempt(
            target_id, 1, "accepted", "late completion from the crashed worker",
            "delivered", delivered_at=T0, lease_until=lease_until, now=T0,
        )

    # a legitimate completion from the CURRENT owner applies
    stolen_row = notif.claim_deliveries(now=T0 + timedelta(seconds=60), limit=5, lease_seconds=30)
    assert stolen_row and stolen_row[0].id == target_id
    notif.record_attempt(
        target_id, 2, "accepted", "current owner delivered", "delivered",
        delivered_at=T0 + timedelta(seconds=60),
        lease_until=stolen_row[0].lease_until, now=T0 + timedelta(seconds=60),
    )
    session = session_factory()
    try:
        row = session.get(Delivery, target_id)
        assert row.status == "delivered"
        assert row.attempts == 2
    finally:
        session.close()


# ---------------------------------------------------------------------------
# concurrent workflow create/activate -> idempotency key holds
# ---------------------------------------------------------------------------


def test_concurrent_workflow_creation_is_idempotent_by_idempotency_key(session_factory):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(_ltp_doc(name="pg-idem"))

    def _create():
        session = session_factory()
        try:
            result = repo.create_workflow(
                "owner-pg", "pg-idem", compiled.document.to_document_dict(),
                compiled.canonical_hash, idempotency_key="pg-idem-key", db=session,
            )
            session.commit()
            return result
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_create), pool.submit(_create)]
        results = [f.result() for f in futures]

    workflow_ids = {str(r[0].id if isinstance(r, tuple) else r.id) for r in results}
    assert len(workflow_ids) == 1
