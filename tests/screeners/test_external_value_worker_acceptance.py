"""Phase 4 F10 acceptance: external producer values through the REAL dispatch path.

These tests deliberately do NOT call ``lookup_value``,
``ExternalSignalLoader.context_for`` or ``evaluate_condition`` directly. They
register a producer and ingest values exactly as the signals API does, author a
workflow in the canonical document format, ACTIVATE it through the service, and
then drive completed candles through ``EvaluationWorker``'s dispatch entry
point — the same path a live candle completion takes.

What they pin:

- a value below the level changes nothing;
- a later valid value crosses on a COMPLETED candle and publishes exactly one
  event and one outbox row (not two, not zero);
- missing, expired and revoked values leave the condition UNKNOWN — never
  false, and never a signal — and an unknown evaluation must not poison the
  state for later evaluation.

The operator-facing rule the first case encodes is easy to get wrong when
authoring a workflow by hand: a LEVEL operator (``gt``/``gte``/``lt``/``lte``)
never fires on a value change. Level conditions report ``matched`` and nothing
else, while the engine's transition gate is driven by ``fired``, so a rule that
must notify when an external value changes has to be authored with an EDGE
operator (``crosses_above``/``crosses_below``). These tests use the edge
operator for exactly that reason.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.notifications.repository import Delivery  # noqa: F401 (registers tables)
from backend.workflows import advanced_repository  # noqa: F401 (registers tables)
from backend.workflows import external_signals as signals
from backend.workflows.compiler import compile_document
from backend.workflows.external_context import ExternalSignalLoader
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.runtime import EvaluationWorker

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
FIVEMIN = timedelta(minutes=5)
OWNER = "owner-1"
INSTRUMENT = "NSE:A"
PRODUCER = "ext-acceptance"
FIELD = "score"
LEVEL = 80.0

# The canonical identity the predicate layer derives for the authored rule.
COND_KEY = f"crosses_above:field:external.{PRODUCER}.{FIELD}:value:{LEVEL}"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _external_document():
    """An external-producer rule on the completed-candle clock."""
    return {
        "version": 1,
        "name": "external-acceptance",
        "session": "nse_equity",
        "instruments": [INSTRUMENT],
        "stages": [
            {
                "id": "ext",
                "type": "signal",
                "clock": "candle_close",
                "timeframe": "5minute",
                "conditions": {
                    "all": [
                        {
                            "left": {"field": f"external.{PRODUCER}.{FIELD}"},
                            "op": "crosses_above",
                            "right": {"value": LEVEL},
                        }
                    ]
                },
            }
        ],
        "alerts": [
            {"id": "ea", "source": "ext", "trigger": "on_transition",
             "channels": ["chan-1"]},
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


def _activate(session_factory, doc):
    """Author -> activate -> materialize subscriptions, the production order."""
    from backend.workflows.service import EvaluationService

    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        OWNER, doc["name"], compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)
    return repo, active


def _register_producer(session_factory):
    with session_factory() as session:
        signals.register_producer(
            session,
            owner_id=OWNER,
            name=PRODUCER,
            value_schema={"fields": {FIELD: "number"}},
            default_ttl_s=3600,
        )
        session.commit()


def _ingest(session_factory, value, event_time, *, expires_at=None, now=None):
    """Ingest one value exactly as the signals API endpoint does.

    ``now`` is the receipt instant the endpoint would observe. These cases
    drive a fixed event timeline, so it defaults to ``event_time``; passing it
    explicitly is what keeps a value in-timeline fresh (neither "late" past
    ``max_lateness_s`` nor rejected as future) instead of being judged against
    the wall clock the test happens to run at.
    """
    with session_factory() as session:
        producer = signals.get_producer(session, owner_id=OWNER, name=PRODUCER)
        row, _deduplicated = signals.ingest_value(
            session,
            producer=producer,
            payload={FIELD: value},
            event_time=event_time,
            instrument_key=INSTRUMENT,
            expires_at=expires_at,
            idempotency_key=None,
            max_future_skew_s=300,
            max_lateness_s=3600,
            now=now or event_time,
        )
        session.commit()
        return row.id


def _revoke_producer(session_factory):
    with session_factory() as session:
        signals.revoke_producer(session, owner_id=OWNER, name=PRODUCER)
        session.commit()


def _make_worker(session_factory):
    """A worker whose external loader is wired exactly as the entrypoint wires it."""
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
        external_loader=ExternalSignalLoader(session_factory),
        owner_id="worker-1",
    )


def _bar(ts, close):
    from backend.alerts.predicates import Observation

    return Observation(
        ts=ts, epoch_id="candle", ltp=close, open=close, high=close, low=close,
        close=close, volume=1000.0, final=True,
    )


def _dispatch_bar(worker, ts, close=100.0):
    """Send one completed bar through the worker's real dispatch path."""
    sub = next(
        s for s in worker._subscriptions if s.instrument_key == INSTRUMENT
    )
    worker._dispatch(sub, _bar(ts, close))


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


def _deliveries(session_factory):
    with session_factory() as session:
        return list(session.execute(select(Delivery)).scalars().all())


def _condition_state(session_factory):
    """The condition's own checkpoint substate, or None when it was unknown.

    Unknown evaluation leaves the state COMPLETELY unchanged (that is the
    documented contract), so an absent substate is what proves "unknown" rather
    than "evaluated to false".
    """
    with session_factory() as session:
        rows = session.execute(text(
            "select state from evaluation_checkpoints"
        )).scalars().all()
    for raw in rows:
        # A JSON column read through raw SQL is text on SQLite (PostgreSQL
        # decodes jsonb for us), so a plain isinstance check would silently
        # read every state as empty.
        if isinstance(raw, str):
            try:
                state = json.loads(raw) if raw.strip() else {}
            except (TypeError, ValueError):
                state = {}
        elif isinstance(raw, dict):
            state = raw
        else:
            state = {}
        conds = state.get("conds") or {}
        if COND_KEY in conds:
            return conds[COND_KEY]
    return None


# ---------------------------------------------------------------------------
# 1. below -> above on a completed candle publishes exactly once
# ---------------------------------------------------------------------------


def test_external_value_crossing_publishes_exactly_once(session_factory):
    """A value below the level is inert; a later value crossing it notifies once."""
    _register_producer(session_factory)
    _activate(session_factory, _external_document())
    worker = _make_worker(session_factory)
    asyncio.run(worker.start())

    # 1. A value BELOW the level: the condition is false, nothing is published.
    low_id = _ingest(session_factory, 50.0, T0 + timedelta(minutes=1))
    _dispatch_bar(worker, T0 + FIVEMIN)
    assert _events(session_factory) == [], "a below-level value must not notify"
    # It DID evaluate (a level of `prev` is recorded) — so the absence of an
    # event is the rule holding, not the input being unavailable.
    state = _condition_state(session_factory)
    assert state is not None, "the condition must have been evaluated, not unknown"
    assert state.get("prev") == 50.0
    assert _deliveries(session_factory) == []

    # 2. A later valid value ABOVE the level, evaluated on a completed candle.
    high_id = _ingest(session_factory, 90.0, T0 + timedelta(minutes=6))
    _dispatch_bar(worker, T0 + 2 * FIVEMIN)

    events = _events(session_factory)
    assert len(events) == 1, "exactly one event for one crossing"
    event = events[0]
    assert event.subscription_id is not None
    # SQLite hands datetimes back naive; the instant is what matters.
    assert event.fired_at.replace(tzinfo=timezone.utc) == T0 + 2 * FIVEMIN

    # The evidence records the value that was actually used, the level, and the
    # previous value, so the crossing is auditable after the fact.
    detail = event.evidence[COND_KEY]
    assert detail[f"external.{PRODUCER}.{FIELD}"] == 90.0
    assert detail["level"] == LEVEL
    assert detail["prev_ltp"] == 50.0

    deliveries = _deliveries(session_factory)
    assert len(deliveries) == 1, "one delivery per channel, not per evaluation"
    assert deliveries[0].channel_id == "chan-1"
    assert deliveries[0].event_id == event.id
    assert low_id != high_id


def test_repeat_and_restart_do_not_duplicate_the_crossing(session_factory):
    """The same value does not re-fire, and a restart does not re-mint it."""
    _register_producer(session_factory)
    _activate(session_factory, _external_document())
    worker = _make_worker(session_factory)
    asyncio.run(worker.start())

    _ingest(session_factory, 50.0, T0 + timedelta(minutes=1))
    _dispatch_bar(worker, T0 + FIVEMIN)
    _ingest(session_factory, 90.0, T0 + timedelta(minutes=6))
    _dispatch_bar(worker, T0 + 2 * FIVEMIN)
    assert len(_events(session_factory)) == 1

    # Replaying the same completed bar must not re-mint the crossing.
    _dispatch_bar(worker, T0 + 2 * FIVEMIN)
    assert len(_events(session_factory)) == 1

    # Neither must a restart, which rebuilds dispatch state from the durable
    # checkpoint.
    restarted = _make_worker(session_factory)
    asyncio.run(restarted.start())
    _dispatch_bar(restarted, T0 + 3 * FIVEMIN)
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1


# ---------------------------------------------------------------------------
# 2. missing / expired / revoked are UNKNOWN, never false and never a signal
# ---------------------------------------------------------------------------


def test_missing_expired_and_revoked_values_are_unknown(session_factory):
    """Each unusable input leaves the condition unknown and emits nothing.

    Unknown is not false: the condition's state is left completely untouched, so
    an unavailable input can never masquerade as "the level was not crossed" —
    and, just as importantly, it cannot poison the state for a later valid
    value.
    """
    _register_producer(session_factory)
    _activate(session_factory, _external_document())
    worker = _make_worker(session_factory)
    asyncio.run(worker.start())

    # --- MISSING: no value has ever been ingested. -------------------------
    _dispatch_bar(worker, T0 + FIVEMIN)
    assert _events(session_factory) == []
    assert _condition_state(session_factory) is None, (
        "a missing value must leave the condition UNKNOWN (state untouched)"
    )

    # --- EXPIRED: the newest candidate has lapsed at the evaluation cutoff. --
    # An older, still-valid value exists and must NOT be substituted: the
    # documented contract is that a lapsed input is unknown, because falling
    # back would attribute a stale observation to a bar whose intended input
    # had already expired.
    _ingest(session_factory, 95.0, T0 + timedelta(minutes=7),
            expires_at=T0 + timedelta(minutes=7, seconds=30))
    _dispatch_bar(worker, T0 + 2 * FIVEMIN)
    assert _events(session_factory) == []
    assert _condition_state(session_factory) is None, (
        "an expired value must leave the condition UNKNOWN, not false"
    )

    # --- REVOKED: the producer is disabled out from under a valid value. ----
    _ingest(session_factory, 95.0, T0 + timedelta(minutes=11))
    _revoke_producer(session_factory)
    _dispatch_bar(worker, T0 + 3 * FIVEMIN)
    assert _events(session_factory) == []
    assert _condition_state(session_factory) is None, (
        "a revoked producer must leave the condition UNKNOWN"
    )
    assert _deliveries(session_factory) == []

    # --- RECOVERY: unknown left no residue, so a valid value still works. ---
    # Re-registering clears the revocation (the documented re-enable path).
    _register_producer(session_factory)
    _ingest(session_factory, 40.0, T0 + timedelta(minutes=16))
    _dispatch_bar(worker, T0 + 4 * FIVEMIN)
    assert _events(session_factory) == [], "initialization does not fire"
    _ingest(session_factory, 90.0, T0 + timedelta(minutes=21))
    _dispatch_bar(worker, T0 + 5 * FIVEMIN)
    assert len(_events(session_factory)) == 1, (
        "a valid value after unknown/invalid inputs must still cross"
    )
