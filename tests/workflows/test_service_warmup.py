"""Service-level pinned contracts (faults 2-5): warmup (``allow_emit``),
stale-bar replay boundary, session identity + gating, context-resolved
prev-day levels, and INFO suppression logging.

Uses a real ``SqlAlchemyWorkflowRepository`` over in-memory SQLite with the
seeding pattern from ``tests/workflows/test_repository.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.repository import Delivery  # noqa: F401  (registers tables)
from backend.workflows.compiler import canonical_json
from backend.workflows.models import (
    AlertSpec,
    Condition,
    InstrumentRef,
    Operand,
    Stage,
    WorkflowDocument,
)
from backend.workflows.repository import (
    ActiveSubscription,
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    WorkflowRevision,
)
from backend.workflows.service import EvaluationService

T0 = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
LEVEL_KEY = "crosses_above:field:close:value:100.0"
LTP_KEY = "crosses_above:field:ltp:value:100.0"
PDH_KEY = "breaks_prev_high:field:ltp:field:prev_day_high"


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
def repo(session_factory):
    return SqlAlchemyWorkflowRepository(session_factory)


def _candle_doc(trigger: str = "once", level: float = 100.0) -> WorkflowDocument:
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
        alerts=(
            AlertSpec(id="cross", source="bar", trigger=trigger, channels=("c1",)),
        ),
    )


def _prev_day_doc() -> WorkflowDocument:
    """A prev-day break rule.

    NOTE: the Phase 1 compiler does not yet whitelist context-resolved
    fields, so this document is hashed from its dict form directly; the
    parser (which the service uses) accepts it.
    """
    return WorkflowDocument(
        version=1,
        name="prevday-break",
        instruments=(InstrumentRef(symbol="RELIANCE", exchange="NSE"),),
        stages=(
            Stage(
                id="px",
                type="signal",
                clock="ltp",
                timeframe=None,
                conditions=(
                    Condition(
                        Operand(kind="field", name="ltp"),
                        "breaks_prev_high",
                        Operand(kind="field", name="prev_day_high"),
                    ),
                ),
            ),
        ),
        alerts=(
            AlertSpec(id="pdh", source="px", trigger="on_transition", channels=("c1",)),
        ),
    )


def _ltp_doc(
    trigger: str = "once_per_session",
    *,
    exchange: str = "NSE",
    session: str = "nse_equity",
) -> WorkflowDocument:
    return WorkflowDocument(
        version=1,
        name="ltp-cross",
        instruments=(InstrumentRef(symbol="RELIANCE", exchange=exchange),),
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
                        Operand(kind="value", value=100.0),
                    ),
                ),
            ),
        ),
        alerts=(
            AlertSpec(id="cross", source="px", trigger=trigger, channels=("c1",)),
        ),
        session=session,
    )


def _activate(repo, doc: WorkflowDocument, *, validated: bool = True):
    if validated:
        from backend.workflows.compiler import compile_document

        compiled = compile_document(doc)
        document_dict, canonical_hash = (
            compiled.document.to_document_dict(),
            compiled.canonical_hash,
        )
    else:
        document_dict = doc.to_document_dict()
        canonical_hash = canonical_json(doc)
    workflow, revision = repo.create_workflow(
        "owner-1", doc.name, document_dict, canonical_hash
    )
    repo.activate_revision(workflow.id, revision.id)
    return workflow, revision


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


def _sub_state(session_factory):
    from backend.workflows.repository import AlertSubscription

    with session_factory() as session:
        rows = list(session.execute(select(AlertSubscription)).scalars().all())
    assert len(rows) == 1
    return rows[0].state


def _bar(minutes: float, close: float) -> Observation:
    ts = T0 + timedelta(minutes=minutes)
    return Observation(
        ts=ts,
        epoch_id="candle",
        ltp=close,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
        final=True,
    )


def _tick(minutes: float, ltp: float, epoch: str = "boot-1") -> Observation:
    return Observation(ts=T0 + timedelta(minutes=minutes), epoch_id=epoch, ltp=ltp)


def _candle_service(repo, session_factory, **kwargs) -> EvaluationService:
    return EvaluationService(repo, session_factory, **kwargs)


# ---------------------------------------------------------------------------
# fault 4: warmup (allow_emit=False) — engine runs, checkpoint saved, no rows
# ---------------------------------------------------------------------------


def test_warmup_saves_checkpoint_without_event_rows(repo, session_factory):
    _workflow, revision = _activate(repo, _candle_doc(trigger="once"))
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    warm1 = service.handle_observation(sub, _bar(0, 95.0), allow_emit=False)
    assert (warm1.fired, warm1.emitted, warm1.suppression_reason) == (False, False, None)
    warm2 = service.handle_observation(sub, _bar(1, 99.0), allow_emit=False)
    assert (warm2.fired, warm2.emitted, warm2.suppression_reason) == (False, False, None)

    # no signal rows from warmup
    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []
    assert _sub_state(session_factory) == "active"

    # but the checkpoint state WAS saved (continuity for the first live bar)
    state, owner_epoch = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    assert owner_epoch == 2
    assert state["conds"][LEVEL_KEY]["prev"] == 99.0
    assert state["last_bar_ts"] == _bar(1, 99.0).ts.isoformat()
    assert state["initialized"] is True

    # the first live bar can fire on a REAL crossing (warmup established prev)
    live = service.handle_observation(sub, _bar(2, 101.0))
    assert live.fired is True
    assert live.emitted is True
    assert live.suppression_reason is None
    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].evidence["epoch_id"] == "candle"
    assert len(_deliveries(session_factory)) == 1
    # trigger once + allow_emit=True completed the rule
    assert _sub_state(session_factory) == "completed"


def test_warmup_would_emit_reports_warmup_and_never_completes(repo, session_factory):
    _workflow, revision = _activate(repo, _candle_doc(trigger="once"))
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # the third warmup bar crosses the level: delivery suppressed as "warmup"
    service.handle_observation(sub, _bar(0, 95.0), allow_emit=False)
    service.handle_observation(sub, _bar(1, 99.0), allow_emit=False)
    warm = service.handle_observation(sub, _bar(2, 101.0), allow_emit=False)
    assert warm.emitted is False
    assert warm.suppression_reason == "warmup"
    assert warm.rule_completed is False

    assert _events(session_factory) == []
    assert _deliveries(session_factory) == []
    assert _sub_state(session_factory) == "active"  # warmup never completes a rule

    # warmup establishes predicate continuity but strips trigger-lifecycle
    # bookkeeping: a historical crossing must not spend the alert's once-only
    # live emission (see test_warmup_crossing_does_not_consume_once_trigger).
    state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    assert "fired_once" not in state
    assert state["last_bar_ts"] == _bar(2, 101.0).ts.isoformat()


def test_warmup_suppression_logged_at_info(repo, session_factory, caplog):
    _workflow, revision = _activate(repo, _candle_doc(trigger="once"))
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    service.handle_observation(sub, _bar(0, 95.0), allow_emit=False)
    with caplog.at_level(logging.INFO, logger="backend.workflows.service"):
        warm = service.handle_observation(sub, _bar(1, 99.0), allow_emit=False)
        service.handle_observation(sub, _bar(2, 101.0), allow_emit=False)
    assert warm.emitted is False
    assert "warmup" in caplog.text
    assert "cross" in caplog.text  # alert id
    assert sub.instrument_key in caplog.text


# ---------------------------------------------------------------------------
# fault 5: stale bars after newer state are ignored, not re-processed
# ---------------------------------------------------------------------------


def test_stale_bar_after_newer_state_is_ignored(repo, session_factory, caplog):
    _workflow, revision = _activate(repo, _candle_doc(trigger="on_transition"))
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    service.handle_observation(sub, _bar(0, 95.0))
    service.handle_observation(sub, _bar(1, 99.0))
    fired = service.handle_observation(sub, _bar(2, 101.0))
    assert fired.emitted is True
    assert len(_events(session_factory)) == 1

    with caplog.at_level(logging.INFO, logger="backend.workflows.service"):
        # an OLD bar (ts before last_bar_ts) arriving late: must be ignored
        stale = service.handle_observation(sub, _bar(1, 200.0))
        assert (stale.fired, stale.emitted) == (False, False)
        assert stale.suppression_reason == "stale_bar"
        # a re-delivered duplicate of the newest bar is also skipped
        dup = service.handle_observation(sub, _bar(2, 101.0))
        assert (dup.fired, dup.emitted) == (False, False)
        assert dup.suppression_reason == "stale_bar"

    # neither re-processed against newer state: no extra events, state intact
    assert len(_events(session_factory)) == 1
    assert len(_deliveries(session_factory)) == 1
    state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    assert state["conds"][LEVEL_KEY]["prev"] == 101.0
    assert state["last_bar_ts"] == _bar(2, 101.0).ts.isoformat()

    # strictly newer bars still process normally after the skips
    after = service.handle_observation(sub, _bar(3, 90.0))
    assert after.emitted is False
    state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "candle")
    assert state["conds"][LEVEL_KEY]["prev"] == 90.0
    assert "stale_bar" in caplog.text
    assert sub.instrument_key in caplog.text


# ---------------------------------------------------------------------------
# fault 4: session identity + gating via session_provider
# ---------------------------------------------------------------------------


def test_once_per_session_re_fires_on_session_change(repo, session_factory, caplog):
    _workflow, revision = _activate(repo, _ltp_doc(trigger="once_per_session"))
    sessions = {}
    for minute, sid in [(0, "s1"), (1, "s1"), (2, "s1"), (3, "s1"), (4, "s2"), (5, "s2")]:
        sessions[T0 + timedelta(minutes=minute)] = sid

    def provider(ts):
        return True, sessions[ts]

    service = _candle_service(repo, session_factory, session_provider=provider)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    outcomes = []
    with caplog.at_level(logging.INFO, logger="backend.workflows.service"):
        for minute, ltp in [(0, 99.0), (1, 101.0), (2, 99.0), (3, 101.0), (4, 99.0), (5, 101.0)]:
            outcomes.append(service.handle_observation(sub, _tick(minute, ltp)))

    # 10:01 fires and emits (session s1); 10:03 re-fire is session-suppressed;
    # session s2 re-arms: 10:05 fires and emits again.
    assert [(r.fired, r.emitted) for r in outcomes] == [
        (False, False),
        (True, True),
        (False, False),
        (True, False),
        (False, False),
        (True, True),
    ]
    assert outcomes[3].suppression_reason == "session_fired"
    assert len(_events(session_factory)) == 2
    assert "session_fired" in caplog.text


def test_session_provider_quiet_session_suppresses_but_keeps_predicate_state(
    repo, session_factory
):
    _workflow, revision = _activate(repo, _ltp_doc(trigger="on_transition"))
    flags = {
        T0: (False, "monday"),
        T0 + timedelta(minutes=1): (True, "monday"),
        T0 + timedelta(minutes=2): (True, "monday"),
    }

    service = _candle_service(
        repo, session_factory, session_provider=lambda ts: flags[ts]
    )
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    quiet = service.handle_observation(sub, _tick(0, 99.0))
    assert quiet.emitted is False
    assert quiet.suppression_reason == "quiet_session"
    assert _events(session_factory) == []
    # quiet session suppresses engine state advance, not predicate continuity
    state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "boot-1")
    assert state["conds"][LTP_KEY]["prev"] == 99.0

    # session opens: first active observation initializes (below the level)…
    init = service.handle_observation(sub, _tick(1, 95.0))
    assert (init.fired, init.emitted, init.suppression_reason) == (False, False, None)
    # …then a genuine crossing emits
    live = service.handle_observation(sub, _tick(2, 101.0))
    assert live.emitted is True
    assert len(_events(session_factory)) == 1


def test_session_fallback_without_provider_is_obs_date_in_ist(repo, session_factory):
    _workflow, revision = _activate(repo, _ltp_doc(trigger="once_per_session"))
    service = _candle_service(repo, session_factory)  # no session_provider
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # same UTC afternoon, same IST session: second fire is suppressed
    r1 = service.handle_observation(sub, _tick(0, 99.0))
    r2 = service.handle_observation(sub, _tick(1, 101.0))  # IST date 2026-09-08
    r3 = service.handle_observation(sub, _tick(2, 99.0))
    r4 = service.handle_observation(sub, _tick(3, 101.0))  # still IST 2026-09-08
    assert r2.emitted is True
    assert r4.emitted is False
    assert r4.suppression_reason == "session_fired"
    state, _ = repo.load_checkpoint(sub.id, sub.instrument_key, "boot-1")
    assert state["last_session"] == "2026-09-08"

    # past 00:00 IST the observation date rolls over: the alert re-arms
    late_night = Observation(
        ts=datetime(2026, 9, 8, 19, 31, tzinfo=timezone.utc),  # IST 2026-09-09 01:01
        epoch_id="boot-1",
        ltp=99.0,
    )
    service.handle_observation(sub, late_night)
    refire = Observation(
        ts=datetime(2026, 9, 8, 19, 32, tzinfo=timezone.utc),  # IST 2026-09-09
        epoch_id="boot-1",
        ltp=101.0,
    )
    r5 = service.handle_observation(sub, refire)
    assert r5.emitted is True
    assert len(_events(session_factory)) == 2


def test_service_passes_workflow_session_and_instrument_to_provider(repo, session_factory):
    _workflow, revision = _activate(
        repo,
        _ltp_doc(
            trigger="once",
            exchange="MCX",
            session="mcx_commodity",
        ),
    )
    calls = []

    def provider(session_name, instrument_key, timestamp):
        calls.append((session_name, instrument_key, timestamp))
        return True, "MCX:2026-09-08"

    service = _candle_service(
        repo,
        session_factory,
        session_provider=provider,
    )
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    result = service.handle_observation(sub, _tick(0, 95.0))

    assert result.emitted is False
    assert calls == [("mcx_commodity", "MCX:RELIANCE", T0)]


# ---------------------------------------------------------------------------
# fault 2: context-resolved previous-day levels through the service
# ---------------------------------------------------------------------------


def test_prev_day_break_via_context(repo, session_factory):
    _workflow, revision = _activate(repo, _prev_day_doc(), validated=False)
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)
    context = {"prev_day_high": 100.0}

    r1 = service.handle_observation(sub, _tick(0, 95.0), context=context)
    assert (r1.fired, r1.emitted) == (False, False)
    r2 = service.handle_observation(sub, _tick(1, 105.0), context=context)
    assert (r2.fired, r2.emitted) == (True, True)
    r3 = service.handle_observation(sub, _tick(2, 106.0), context=context)
    assert (r3.fired, r3.emitted) == (False, False)  # guard prevents refire

    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].evidence[PDH_KEY] == {"ltp": 105.0, "level": 100.0}

    # back below resets the guard: breaking again fires again
    service.handle_observation(sub, _tick(3, 95.0), context=context)
    r5 = service.handle_observation(sub, _tick(4, 101.0), context=context)
    assert (r5.fired, r5.emitted) == (True, True)
    assert len(_events(session_factory)) == 2


def test_prev_day_break_without_context_is_unknown(repo, session_factory):
    _workflow, revision = _activate(repo, _prev_day_doc(), validated=False)
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # no context argument at all: the level is unknown, never fires
    r1 = service.handle_observation(sub, _tick(0, 105.0))
    assert (r1.fired, r1.emitted, r1.suppression_reason) == (False, False, None)
    # context present but missing the key: same
    r2 = service.handle_observation(sub, _tick(1, 105.0), context={"prev_day_low": 1.0})
    assert (r2.fired, r2.emitted, r2.suppression_reason) == (False, False, None)
    assert _events(session_factory) == []
    # supplying the context afterwards: this is the first observation the
    # break condition ever RESOLVES (unknown obs left the state untouched),
    # so it only arms the seen/guard state without firing.
    r3 = service.handle_observation(sub, _tick(2, 105.0), context={"prev_day_high": 100.0})
    assert r3.fired is False
    r4 = service.handle_observation(sub, _tick(3, 106.0), context={"prev_day_high": 100.0})
    assert r4.fired is False  # still holding: guard active since 10:02 armed it
    service.handle_observation(sub, _tick(4, 95.0), context={"prev_day_high": 100.0})
    r6 = service.handle_observation(sub, _tick(5, 101.0), context={"prev_day_high": 100.0})
    assert (r6.fired, r6.emitted) == (True, True)


# ---------------------------------------------------------------------------
# warmup must not consume live trigger lifecycle
# ---------------------------------------------------------------------------


def test_warmup_crossing_does_not_consume_once_trigger(repo, session_factory):
    """A crossing during warmup is history: it establishes predicate
    continuity but must not spend the alert's once-only emission."""
    _workflow, revision = _activate(repo, _ltp_doc(trigger="once"))
    service = _candle_service(repo, session_factory)
    service.ensure_subscriptions(revision)
    sub = _single_sub(repo)

    # warmup: crossing at 10:01 happened before activation
    service.handle_observation(sub, _tick(0, 99.0), allow_emit=False)
    warm = service.handle_observation(sub, _tick(1, 101.0), allow_emit=False)
    assert warm.fired is True and warm.emitted is False

    # live: price returns below, then crosses again -> must emit
    service.handle_observation(sub, _tick(2, 99.0))
    live = service.handle_observation(sub, _tick(3, 101.0))
    assert (live.fired, live.emitted) == (True, True)
    assert live.suppression_reason is None
    assert len(_events(session_factory)) == 1
