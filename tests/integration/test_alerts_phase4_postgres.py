"""Phase 4 F10 PostgreSQL integration suite.

Proves on a real database what SQLite cannot: atomic rollback of advanced
state together with the event/outbox, fencing under a real advisory lock,
workflow-level breadth state across concurrent instruments with out-of-order
arrival, and external-signal ingestion durability plus revocation.

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_alerts_phase4_postgres.py -q
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

from backend.workflows import advanced_repository as repo
from backend.workflows import breadth as breadth_engine
from backend.workflows import external_signals as signals
from backend.workflows.models import BreadthSpec, ConditionGroup
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base  # noqa: F401  (table registration)

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; Phase 4 PostgreSQL suite skipped",
)

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


@pytest.fixture(scope="module")
def factory():
    engine = create_engine(PG_URL, poolclass=NullPool)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def clean(factory):
    """Truncate the Phase 4 tables this module owns."""
    with factory() as session:
        session.execute(text("TRUNCATE alert_breadth_state, alert_breadth_triggers"))
        session.execute(text("TRUNCATE alert_session_counters, alert_suppression_counters"))
        session.execute(
            text("TRUNCATE external_signal_values, external_signal_producer_credentials, "
                 "external_signal_producers CASCADE")
        )
        session.commit()
    return factory


def _breadth_spec(threshold=3, window_s=1800):
    return BreadthSpec(
        condition=(ConditionGroup(
            kind="all",
            conditions=tuple(
                parse_workflow_dict({
                    "version": 1, "name": "spec", "session": "nse_equity",
                    "instruments": ["NSE:A"],
                    "stages": [{"id": "s", "type": "signal", "clock": "candle_close",
                                "timeframe": "5minute",
                                "conditions": {"all": [{"field": "close", "op": "gt",
                                                        "value": 50}]}}],
                    "alerts": [],
                }).stages[0].conditions
            ),
        ),),
        distinct_instruments=threshold,
        window_s=window_s,
    )


_UNRESOLVED = object()


def _evaluate(session, *, stage_id="b", spec=None, members, triggering=None,
              observed_at=T0, resolved_at=_UNRESOLVED, max_instruments=1000,
              membership_max_age_s=900):
    return breadth_engine.evaluate_breadth(
        session,
        owner_id=OWNER,
        workflow_id="11111111-1111-1111-1111-111111111111",
        revision_id="22222222-2222-2222-2222-222222222222",
        stage_id=stage_id,
        spec=spec or _breadth_spec(),
        member_keys=members,
        member_universe_revision=1,
        membership_resolved_at=(
            observed_at if resolved_at is _UNRESOLVED else resolved_at
        ),
        triggering=triggering,
        observed_at=observed_at,
        max_instruments=max_instruments,
        membership_max_age_s=membership_max_age_s,
    )


def _state(session):
    return repo.read_breadth_state(
        session,
        owner_id=OWNER,
        workflow_id="11111111-1111-1111-1111-111111111111",
        revision_id="22222222-2222-2222-2222-222222222222",
        stage_id="b",
    )


# ---------------------------------------------------------------------------
# breadth: ordering, crossings, membership
# ---------------------------------------------------------------------------


def test_first_crossing_notifies_because_satisfied_starts_false(clean):
    session = clean()
    try:
        # A brand-new state row must NOT be pre-satisfied.
        outcome = _evaluate(
            session, members=["NSE:A", "NSE:B", "NSE:C"], triggering="NSE:A"
        )
        assert outcome.matched is False
        assert outcome.fired is False
        session.commit()
        outcome = _evaluate(
            session, members=["NSE:A", "NSE:B", "NSE:C"], triggering="NSE:B"
        )
        session.commit()
        outcome = _evaluate(
            session, members=["NSE:A", "NSE:B", "NSE:C"], triggering="NSE:C"
        )
        session.commit()
        assert outcome.fired is True
        assert outcome.crossing_seq == 1
        assert outcome.count == 3
        assert (_state(session).satisfied, _state(session).last_count) == (True, 3)
    finally:
        session.close()


def test_reversed_arrival_order_converges_to_the_same_crossing(clean):
    """Order independence: the same contributions yield the same crossing."""
    factory = clean
    spec = _breadth_spec()
    members = ["NSE:A", "NSE:B", "NSE:C"]

    def sequence(order):
        with factory() as truncator:
            truncator.execute(text(
                "TRUNCATE alert_breadth_state, alert_breadth_triggers"
            ))
            truncator.commit()
        session = factory()
        try:
            fired_at = []
            for index, key in enumerate(order):
                outcome = _evaluate(
                    session, spec=spec, members=members, triggering=key,
                    observed_at=T0 + timedelta(seconds=index),
                )
                session.commit()
                if outcome.fired:
                    fired_at.append(outcome.crossing_seq)
            return fired_at
        finally:
            session.close()

    forward = sequence(["NSE:A", "NSE:B", "NSE:C"])
    backward = sequence(["NSE:C", "NSE:B", "NSE:A"])
    assert forward == [1]
    assert backward == [1]


def test_late_observation_is_recorded_without_minting_a_crossing(clean):
    """E-13: late data is recorded, never retroactively notified."""
    session = clean()
    try:
        members = ["NSE:A", "NSE:B", "NSE:C"]
        for index, key in enumerate(members):
            _evaluate(session, members=members, triggering=key,
                      observed_at=T0 + timedelta(seconds=10 * index))
        session.commit()
        assert _state(session).satisfied is True

        # A much older observation arrives: it must not move the watermark
        # backwards, must not rewrite a newer contribution, and must not mint
        # a crossing.
        before = _state(session).aggregation_watermark
        outcome = _evaluate(
            session, members=members, triggering="NSE:A",
            observed_at=T0 - timedelta(seconds=3600),
        )
        session.commit()
        assert outcome.fired is False
        assert outcome.reason == "breadth_stale_observation"
        assert _state(session).crossing_seq == 1
        assert _state(session).aggregation_watermark == before
    finally:
        session.close()


def test_older_observation_never_rewrites_a_newer_contribution(clean):
    session = clean()
    try:
        _evaluate(session, members=["NSE:A"], triggering="NSE:A",
                  observed_at=T0 + timedelta(minutes=5))
        session.commit()
        newer = session.execute(
            select(repo.AlertBreadthTrigger.last_trigger_ts)
        ).scalar_one()
        _evaluate(session, members=["NSE:A"], triggering="NSE:A",
                  observed_at=T0)
        session.commit()
        still = session.execute(
            select(repo.AlertBreadthTrigger.last_trigger_ts)
        ).scalar_one()
        assert still == newer
    finally:
        session.close()


def test_same_timestamp_crossings_get_distinct_identities(clean):
    """The identity is the crossing sequence, never a timestamp."""
    session = clean()
    try:
        members = ["NSE:A", "NSE:B"]
        spec = _breadth_spec(threshold=2, window_s=60)
        for key in members:
            _evaluate(session, spec=spec, members=members, triggering=key,
                      observed_at=T0)
        session.commit()
        assert _state(session).crossing_seq == 1

        # Rearm (both contributions age out) and re-cross at the SAME timestamp
        # value: the sequence must advance, so the identity differs.
        _evaluate(session, spec=spec, members=members, triggering=None,
                  observed_at=T0 + timedelta(seconds=120))
        session.commit()
        for key in members:
            _evaluate(session, spec=spec, members=members, triggering=key,
                      observed_at=T0 + timedelta(seconds=120))
        session.commit()
        assert _state(session).crossing_seq == 2
    finally:
        session.close()


def test_membership_change_midwindow_filters_without_clearing(clean):
    session = clean()
    try:
        # Threshold 3 so the first two contributions do not yet cross.
        spec = _breadth_spec(threshold=3, window_s=1800)
        _evaluate(session, spec=spec, members=["NSE:A", "NSE:B", "NSE:C"],
                  triggering="NSE:A")
        _evaluate(session, spec=spec, members=["NSE:A", "NSE:B", "NSE:C"],
                  triggering="NSE:B")
        session.commit()
        # B leaves the universe: its retained row must stop counting, so the
        # third member's contribution is still genuinely needed.
        outcome = _evaluate(session, spec=spec, members=["NSE:A", "NSE:C"],
                            triggering="NSE:C")
        session.commit()
        assert outcome.count == 2  # A and C — B's retained row is excluded
        assert outcome.matched is False
        assert outcome.fired is False
        # The departed row is retained as history, not deleted.
        keys = set(session.execute(
            select(repo.AlertBreadthTrigger.instrument_key)
        ).scalars().all())
        assert keys == {"NSE:A", "NSE:B", "NSE:C"}
    finally:
        session.close()


def test_readmitted_instrument_does_not_regain_its_contribution(clean):
    session = clean()
    try:
        spec = _breadth_spec(threshold=2, window_s=1800)
        _evaluate(session, spec=spec, members=["NSE:A", "NSE:B"], triggering="NSE:A")
        _evaluate(session, spec=spec, members=["NSE:A", "NSE:B"], triggering="NSE:B")
        session.commit()
        assert _state(session).satisfied is True

        # B departs, then rejoins inside the same window: its contribution must
        # be cleared on re-admission so the rule stays "contributes AFTER
        # admission".
        repo.evict_breadth_contribution(
            session, owner_id=OWNER,
            workflow_id="11111111-1111-1111-1111-111111111111",
            revision_id="22222222-2222-2222-2222-222222222222",
            stage_id="b", instrument_key="NSE:B",
        )
        session.commit()
        assert "NSE:B" not in set(session.execute(
            select(repo.AlertBreadthTrigger.instrument_key)
        ).scalars().all())
        outcome = _evaluate(session, spec=spec, members=["NSE:A", "NSE:B"],
                            triggering=None)
        session.commit()
        assert outcome.count == 1
        assert outcome.fired is False
    finally:
        session.close()


def test_stale_membership_and_capacity_report_unknown(clean):
    session = clean()
    try:
        stale = _evaluate(session, members=["NSE:A"], triggering="NSE:A",
                          resolved_at=T0 - timedelta(hours=2))
        assert stale.matched is None
        assert stale.reason == "membership_stale"
        over = _evaluate(session, members=["NSE:A", "NSE:B"], triggering="NSE:A",
                         max_instruments=1)
        assert over.matched is None
        assert over.reason == "breadth_capacity_exceeded"
        missing = _evaluate(session, members=["NSE:A"], triggering="NSE:A",
                            resolved_at=None)  # nothing resolvable at all
        assert missing.reason == "membership_unavailable"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# concurrency and rollback
# ---------------------------------------------------------------------------


def test_concurrent_stages_cannot_interleave_the_windows(clean):
    """Overlapping publications of one stage serialize on the advisory lock."""
    factory = clean
    spec = _breadth_spec(threshold=5, window_s=1800)
    members = [f"NSE:{chr(65 + i)}" for i in range(5)]
    barrier = threading.Barrier(5)
    errors = []

    def contribute(key, index):
        session = factory()
        try:
            barrier.wait(timeout=30)
            breadth_engine.evaluate_breadth(
                session,
                owner_id=OWNER,
                workflow_id="11111111-1111-1111-1111-111111111111",
                revision_id="22222222-2222-2222-2222-222222222222",
                stage_id="b",
                spec=spec,
                member_keys=members,
                member_universe_revision=1,
                membership_resolved_at=T0,
                triggering=key,
                observed_at=T0 + timedelta(seconds=index),
                max_instruments=1000,
                membership_max_age_s=900,
            )
            session.commit()
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda pair: contribute(pair[1], pair[0]), enumerate(members)))

    assert errors == []
    session = factory()
    try:
        state = repo.read_breadth_state(
            session, owner_id=OWNER,
            workflow_id="11111111-1111-1111-1111-111111111111",
            revision_id="22222222-2222-2222-2222-222222222222", stage_id="b",
        )
        # Every contribution landed and the crossing was minted exactly once.
        assert state.last_count == 5
        assert state.crossing_seq == 1
        assert state.satisfied is True
        assert session.execute(
            select(repo.AlertBreadthTrigger)
        ).scalars().all().__len__() == 5
    finally:
        session.close()


def test_failure_rolls_back_the_contribution_and_the_state(clean):
    """State, contribution and counter commit or roll back together."""
    session = clean()
    try:
        _evaluate(session, members=["NSE:A"], triggering="NSE:A")
        session.commit()
        before = _state(session).last_count

        # Inject a failure after the state write but before commit.
        _evaluate(session, members=["NSE:A", "NSE:B"], triggering="NSE:B")
        session.rollback()

        after = _state(session)
        assert after.last_count == before
        keys = set(session.execute(
            select(repo.AlertBreadthTrigger.instrument_key)
        ).scalars().all())
        assert keys == {"NSE:A"}  # B's contribution was rolled back too
    finally:
        session.close()


# ---------------------------------------------------------------------------
# session caps
# ---------------------------------------------------------------------------


def test_session_cap_is_shared_atomically_across_instruments(clean):
    factory = clean
    slot_args = dict(
        owner_id=OWNER,
        workflow_id="11111111-1111-1111-1111-111111111111",
        revision_id="22222222-2222-2222-2222-222222222222",
        alert_id="a1",
        session_id="NSE:CM:2026-09-11",
        maximum=3,
    )
    barrier = threading.Barrier(12)
    granted = []

    def claim(_index):
        session = factory()
        try:
            barrier.wait(timeout=30)
            ok = repo.reserve_session_slot(session, **slot_args)
            session.commit()
            granted.append(ok)
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(claim, range(12)))

    # Exactly the cap is granted; the rest are refused, with no lost update.
    assert sum(1 for ok in granted if ok) == 3
    session = factory()
    try:
        assert repo.session_count(session, **{k: v for k, v in slot_args.items()
                                             if k != "maximum"}) == 3
    finally:
        session.close()


def test_suppression_counters_accumulate_durably(clean):
    session = clean()
    try:
        for index in range(3):
            repo.record_suppression(
                session, owner_id=OWNER,
                workflow_id="11111111-1111-1111-1111-111111111111",
                revision_id="22222222-2222-2222-2222-222222222222",
                alert_id="a1", session_id="s1", reason="session_cap",
                instrument_key=f"NSE:{chr(65 + index)}", stage_id="px",
            )
        session.commit()
        summary = repo.suppression_summary(
            session, workflow_id="11111111-1111-1111-1111-111111111111"
        )
        assert summary == {"session_cap": 3}
        row = session.execute(
            select(repo.AlertSuppressionCounter)
        ).scalar_one()
        assert row.last_instrument_key == "NSE:C"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# external signals
# ---------------------------------------------------------------------------

SCHEMA = {"fields": {"score": "number"}}


def _producer(session, name="catalyst"):
    return signals.register_producer(
        session, owner_id=OWNER, name=name, value_schema=SCHEMA,
        default_ttl_s=3600,
    )


def test_external_ingestion_survives_a_restart_before_evaluation(clean):
    """Durable acceptance: a value is readable by a later, fresh session."""
    factory = clean
    session = factory()
    try:
        producer = _producer(session)
        signals.ingest_value(
            session, producer=producer, payload={"score": 7.5},
            event_time=T0, instrument_key="NSE:A", expires_at=None,
            idempotency_key="restart-1", max_future_skew_s=300,
            max_lateness_s=3600, now=T0,
        )
        session.commit()
    finally:
        session.close()

    # A completely new session (the worker restarting) sees the value.
    session = factory()
    try:
        lookup = signals.lookup_value(
            session, owner_id=OWNER, producer_name="catalyst", field="score",
            cutoff=T0,
        )
        assert lookup.value == 7.5
        assert lookup.usable is True
    finally:
        session.close()


def test_concurrent_duplicate_ingestion_produces_one_row(clean):
    factory = clean
    session = factory()
    try:
        producer = _producer(session)
        session.commit()
        producer_id = producer.id
    finally:
        session.close()

    results = []
    barrier = threading.Barrier(6)

    def submit(_index):
        session = factory()
        try:
            producer = signals.get_producer_by_id(session, producer_id)
            barrier.wait(timeout=30)
            row, deduplicated = signals.ingest_value(
                session, producer=producer, payload={"score": 1.0},
                event_time=T0, instrument_key=None, expires_at=None,
                idempotency_key="race-1", max_future_skew_s=300,
                max_lateness_s=3600, now=T0,
            )
            session.commit()
            results.append((row.id, deduplicated))
        except signals.ExternalSignalIdempotencyConflict:
            session.rollback()
            results.append((None, True))
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(submit, range(6)))

    session = factory()
    try:
        rows = session.execute(select(signals.ExternalSignalValue)).scalars().all()
        assert len(rows) == 1
        # Exactly one caller performed the insert; every other caller either
        # deduplicated or lost the race and converged on the winner's row.
        assert len(results) == 6
        assert sum(1 for _id, deduplicated in results if not deduplicated) == 1
    finally:
        session.close()


def test_expired_and_revoked_values_are_unusable(clean):
    session = clean()
    try:
        producer = _producer(session)
        signals.ingest_value(
            session, producer=producer, payload={"score": 9.0},
            event_time=T0 - timedelta(minutes=30),
            expires_at=T0 + timedelta(minutes=10),
            instrument_key=None, idempotency_key=None,
            max_future_skew_s=300, max_lateness_s=3600,
            now=T0 - timedelta(minutes=30),
        )
        session.commit()
        # Within validity at the cutoff.
        inside = signals.lookup_value(
            session, owner_id=OWNER, producer_name="catalyst", field="score",
            cutoff=T0,
        )
        assert inside.value == 9.0
        # Past expiry at a later cutoff: unknown, no fallback.
        after = signals.lookup_value(
            session, owner_id=OWNER, producer_name="catalyst", field="score",
            cutoff=T0 + timedelta(minutes=30),
        )
        assert after.value is None
        assert after.reason == "external_expired"

        signals.revoke_producer(session, owner_id=OWNER, name="catalyst")
        session.commit()
        revoked = signals.lookup_value(
            session, owner_id=OWNER, producer_name="catalyst", field="score",
            cutoff=T0,
        )
        assert revoked.value is None
        assert revoked.reason == "external_revoked"
    finally:
        session.close()


def test_credential_hash_round_trip_on_postgres(clean):
    session = clean()
    try:
        producer = _producer(session)
        credential, secret = signals.issue_credential(session, producer=producer)
        session.commit()
        assert credential.token_hash != secret
        resolved = signals.resolve_producer_credential(session, raw_token=secret)
        assert resolved is not None and resolved.id == producer.id
        signals.revoke_producer(session, owner_id=OWNER, name="catalyst")
        session.commit()
        assert signals.resolve_producer_credential(session, raw_token=secret) is None
        # The stored material is a hash, never the secret itself.
        assert session.execute(
            select(signals.ExternalSignalProducerCredential.token_hash)
        ).scalar_one() != secret
    finally:
        session.close()
