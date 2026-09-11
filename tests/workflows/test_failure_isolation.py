"""Phase 6 6A.0 acceptance: bounded workflow failure isolation.

The failure this closes: a single malformed workflow — a pair stage whose
resolver raised — brought down the ENTIRE alerts worker. The raise happened in
`_dispatch`'s pre-call resolution, which sat outside the try/except, and that
first dispatch happens during STARTUP warmup, so `start()` raised, `run()`
propagated it, `supervise` cancelled every sibling task, and `main()` returned 0
— a crash-loop that looked like a clean shutdown while the container kept
reporting healthy from a stale health file.

Covered here:
- one bad subscription cannot abort startup or stop unrelated evaluation;
- the pre-call dispatch region is contained;
- repeated failures quarantine the subscription, then re-probe;
- supervised restarts tear down the failed instance (sources + owner) first;
- required-task liveness is visible in health and fails the healthcheck;
- a task that ended in failure is not reported as a clean shutdown.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.alerts.predicates import Observation
from backend.notifications.repository import Delivery  # noqa: F401
from backend.workflows import advanced_repository, worker_entry  # noqa: F401
from backend.workflows.compiler import compile_document
from backend.workflows.health_check import check_health
from backend.workflows.health_check import main as health_check_main
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import Base, SignalEvent, SqlAlchemyWorkflowRepository
from backend.workflows.runtime import EvaluationWorker
from backend.workflows.service import EvaluationService

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
GOOD = "NSE:GOOD"
BAD = "NSE:BAD"


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


def _document(name, instruments):
    return {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "instruments": list(instruments),
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
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition"}],
    }


def _activate(session_factory, name, instruments):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = _document(name, instruments)
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", name, compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)
    return repo, active


class _FakeSource:
    def __init__(self):
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True

    async def next_observation(self):
        return None


class _StubLoader:
    """Minimal external loader: resolves nothing, reports nothing."""

    def context_for(self, *args, **kwargs):
        return {}

    def health(self):
        return {}


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
        instrument_resolver=lambda keys: ({k: 1 for k in keys}, set()),
        renewal=None,
        owner_id="worker-1",
    )
    params.update(overrides)
    return EvaluationWorker(
        SqlAlchemyWorkflowRepository(session_factory), session_factory, **params
    )


def _tick(ltp=101.0, ts=T0, epoch="boot-1"):
    return Observation(ts=ts, epoch_id=epoch, ltp=ltp)


def _sub_for(worker, instrument_key):
    return next(s for s in worker._subscriptions if s.instrument_key == instrument_key)


def _events(session_factory):
    with session_factory() as session:
        return list(session.execute(select(SignalEvent)).scalars().all())


# ---------------------------------------------------------------------------
# 1. one bad subscription cannot take down startup or its neighbours
# ---------------------------------------------------------------------------


def _boom(self, sub):
    """Stands in for a resolver that raises, as the pair resolver did."""
    raise RuntimeError("injected: resolver failed")


def _candle_document(name, instruments):
    """Candle-clock workflow: this is the clock whose WARMUP dispatches at start."""
    return {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "instruments": list(instruments),
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "candle_close",
                "timeframe": "minute",
                "conditions": {
                    "all": [
                        {"left": {"field": "close"}, "op": "crosses_above",
                         "right": {"value": 100.0}}
                    ]
                },
            }
        ],
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition"}],
    }


def _activate_candle(session_factory, name, instruments):
    repo = SqlAlchemyWorkflowRepository(session_factory)
    doc = _candle_document(name, instruments)
    compiled = compile_document(parse_workflow_dict(doc))
    workflow, revision = repo.create_workflow(
        "owner-1", name, compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    EvaluationService(repo, session_factory).ensure_subscriptions(active)
    return repo, active


class _OneBarHistory:
    """One completed bar per instrument: enough for warmup to dispatch.

    Returns real Observations, as the ``CandleHistory`` protocol specifies and
    as ``PgCandleHistory`` does in production.
    """

    def __init__(self, close=99.0):
        self._close = close

    def recent_bars(self, instrument_key, timeframe, limit):
        return [
            Observation(
                ts=T0, epoch_id="candle", close=self._close, ltp=self._close,
                open=self._close, high=self._close, low=self._close,
                volume=1.0, final=True,
            )
        ]

    def previous_session_levels(self, instrument_key, at):
        return None


def test_a_raising_stage_does_not_abort_startup_or_stop_other_workflows(
    session_factory, monkeypatch
):
    """The reported crash, reproduced on the path that actually failed.

    The raise happened during STARTUP WARMUP — `start()` -> `_warm_candle_group`
    -> `_dispatch` -> the pair resolver — so `start()` itself raised and took
    down every unrelated alert. Here `NSE:BAD` warms first (alphabetically) and
    raises; `NSE:GOOD` must still warm, still evaluate, and still fire.
    """
    _activate_candle(session_factory, "good-wf", [GOOD])
    _activate_candle(session_factory, "bad-wf", [BAD])
    worker = _worker(session_factory, candle_history=_OneBarHistory())
    worker.external_loader = _StubLoader()

    monkeypatch.setattr(
        EvaluationWorker,
        "_pair_operands_for",
        lambda self, sub: [object()] if sub.instrument_key == BAD else [],
    )

    def _explode(self, sub, obs, operands):
        raise NameError("injected: pair resolver exploded")

    monkeypatch.setattr(EvaluationWorker, "_resolve_pairs", _explode)

    asyncio.run(worker.start())  # the call that used to crash-loop the worker

    # The failure is contained AND visible.
    assert worker.health["evaluation_errors"] >= 1
    snapshot = worker.health_snapshot()
    bad_id = _sub_for(worker, BAD).id
    assert bad_id in snapshot["subscription_failures"]
    assert "NameError" in snapshot["subscription_failures"][bad_id]["last_error"]

    # The healthy workflow still evaluates and can fire.
    good = _sub_for(worker, GOOD)
    worker._dispatch(
        good,
        Observation(
            ts=T0 + timedelta(minutes=1), epoch_id="candle", close=150.0,
            ltp=150.0, open=150.0, high=150.0, low=150.0, volume=1.0, final=True,
        ),
    )
    assert len(_events(session_factory)) == 1, "the healthy workflow must keep working"
    assert good.id not in snapshot["subscription_failures"]
    asyncio.run(worker.stop())


def test_a_failing_index_step_does_not_abort_startup(session_factory, monkeypatch):
    """Indexing one subscription must not stop the others being indexed.

    `_index_subscription` builds the layered plan and declares shared features;
    it runs in a bare loop at startup, so an exception there used to abort
    `start()` before ANY subscription was dispatched.
    """
    _activate(session_factory, "good-wf", [GOOD])
    _activate(session_factory, "bad-wf", [BAD])
    worker = _worker(session_factory)

    real = EvaluationWorker._index_subscription

    def _explode(self, sub):
        if sub.instrument_key == BAD:
            raise ValueError("injected: plan build failed")
        return real(self, sub)

    monkeypatch.setattr(EvaluationWorker, "_index_subscription", _explode)

    asyncio.run(worker.start())

    # The healthy subscription was indexed, got a source, and can evaluate.
    assert GOOD in worker._ltp_subs
    assert GOOD in worker._tick_sources
    good = _sub_for(worker, GOOD)
    worker._dispatch(good, _tick(ltp=99.0))
    worker._dispatch(good, _tick(ltp=150.0, ts=T0 + timedelta(minutes=1)))
    assert len(_events(session_factory)) == 1
    # The failing one is contained and visible.
    snapshot = worker.health_snapshot()
    assert _sub_for(worker, BAD).id in snapshot["subscription_failures"]
    asyncio.run(worker.stop())


def test_a_failing_source_factory_does_not_abort_startup(session_factory):
    """A feed source that cannot be opened must not prevent the others."""
    _activate(session_factory, "good-wf", [GOOD])
    _activate(session_factory, "bad-wf", [BAD])

    def _factory(instrument_key):
        if instrument_key == BAD:
            raise RuntimeError("injected: source unavailable")
        return _FakeSource()

    worker = _worker(session_factory, tick_source_factory=_factory)

    asyncio.run(worker.start())

    assert GOOD in worker._tick_sources
    assert BAD not in worker._tick_sources
    snapshot = worker.health_snapshot()
    bad_id = _sub_for(worker, BAD).id
    assert bad_id in snapshot["subscription_failures"]
    assert "source unavailable" in snapshot["subscription_failures"][bad_id]["last_error"]
    asyncio.run(worker.stop())


def test_evaluation_errors_are_contained_in_the_pre_call_region(session_factory, monkeypatch):
    """The pre-call resolvers must be inside the guard.

    `_external_references_for` / `_pair_operands_for` / `_resolve_pairs` /
    `_breadth_membership_for` all ran BEFORE the try/except, so a raise there
    escaped dispatch entirely.
    """
    _activate(session_factory, "good-wf", [GOOD])
    worker = _worker(session_factory)
    asyncio.run(worker.start())

    monkeypatch.setattr(EvaluationWorker, "_external_references_for", _boom)
    # Also give the worker an external loader so the branch is reached.
    worker.external_loader = _StubLoader()

    sub = _sub_for(worker, GOOD)
    worker._dispatch(sub, _tick())  # must NOT raise

    assert worker.health["evaluation_errors"] == 1
    snapshot = worker.health_snapshot()
    assert sub.id in snapshot["subscription_failures"]
    asyncio.run(worker.stop())


# ---------------------------------------------------------------------------
# 2. quarantine
# ---------------------------------------------------------------------------


def test_repeated_failures_quarantine_then_reprobe(session_factory, monkeypatch):
    """A permanently broken subscription stops consuming evaluation slots."""
    _activate(session_factory, "good-wf", [GOOD])
    worker = _worker(session_factory)
    worker.workflow_quarantine_after = 3
    worker.workflow_quarantine_cooldown_s = 0.05
    asyncio.run(worker.start())

    monkeypatch.setattr(EvaluationWorker, "_external_references_for", _boom)
    worker.external_loader = _StubLoader()

    sub = _sub_for(worker, GOOD)
    for _ in range(3):
        worker._dispatch(sub, _tick())

    snapshot = worker.health_snapshot()
    assert sub.id in snapshot["quarantined"], "must be parked after the threshold"
    assert worker.health["quarantined"] == 1

    # While quarantined the subscription is skipped, not re-evaluated.
    errors_before = worker.health["evaluation_errors"]
    worker._dispatch(sub, _tick())
    assert worker.health["evaluation_errors"] == errors_before
    assert worker.health["evaluations_skipped_quarantined"] == 1

    # After the cooldown it is re-probed once; a success releases it.
    monkeypatch.setattr(EvaluationWorker, "_external_references_for", lambda self, sub: [])
    worker._quarantined[sub.id] = datetime.now(timezone.utc) - timedelta(seconds=1)
    worker._dispatch(sub, _tick())
    assert sub.id not in worker.health_snapshot()["quarantined"]
    asyncio.run(worker.stop())


def test_quarantine_does_not_affect_other_subscriptions(session_factory, monkeypatch):
    _activate(session_factory, "good-wf", [GOOD])
    _activate(session_factory, "bad-wf", [BAD])
    worker = _worker(session_factory)
    worker.workflow_quarantine_after = 2
    asyncio.run(worker.start())

    real = EvaluationWorker._external_references_for

    def _explode(self, sub):
        if sub.instrument_key == BAD:
            raise RuntimeError("injected: bad only")
        return real(self, sub)

    monkeypatch.setattr(EvaluationWorker, "_external_references_for", _explode)
    worker.external_loader = _StubLoader()

    bad = _sub_for(worker, BAD)
    good = _sub_for(worker, GOOD)
    for _ in range(2):
        worker._dispatch(bad, _tick())
    assert bad.id in worker.health_snapshot()["quarantined"]

    # The healthy subscription keeps evaluating normally.
    worker._dispatch(good, _tick(ltp=99.0))
    worker._dispatch(good, _tick(ltp=150.0, ts=T0 + timedelta(minutes=1)))
    assert len(_events(session_factory)) == 1
    assert good.id not in worker.health_snapshot()["quarantined"]
    asyncio.run(worker.stop())


# ---------------------------------------------------------------------------
# 3. supervision: restart, teardown, liveness, honest exit
# ---------------------------------------------------------------------------


class _StubWorker:
    """Duck-typed worker for supervise() unit tests."""

    def __init__(self, outcomes, *, owner_release=None):
        self._outcomes = list(outcomes)
        self._task_state = {}
        self.stop_calls = 0

    async def run(self):
        outcome = self._outcomes.pop(0) if self._outcomes else "block"
        if outcome == "block":
            await asyncio.Event().wait()
        raise RuntimeError("injected: evaluation worker crashed")

    async def stop(self):
        self.stop_calls += 1


def _fake_stop(delay=0.05):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    loop.call_later(delay, fut.set_result, None)
    return fut


def test_crashed_task_is_restarted_with_backoff_and_teardown():
    """Restart must tear down the failed instance before the replacement.

    Without teardown the replacement would open a SECOND set of feed sources
    while the previous ones are still subscribed, and the old market-runtime
    owner would keep streaming the same tokens until its lease expired.
    """
    worker = _StubWorker(["crash"])
    releases = []

    async def _release():
        releases.append(1)

    async def scenario():
        stop = _fake_stop(0.25)
        return await worker_entry.supervise(
            worker, None, stop=stop, restart_backoff_s=0.01,
            max_consecutive_failures=3, release_owner=_release,
        )

    asyncio.run(scenario())

    assert worker.stop_calls >= 1, "the crashed instance must be stopped"
    assert releases, "the market-runtime owner must be released before a restart"


def test_liveness_is_published_and_cleared_on_stop():
    worker = _StubWorker([])

    async def scenario():
        stop = _fake_stop(0.05)
        await worker_entry.supervise(worker, None, stop=stop)

    asyncio.run(scenario())
    entry = worker._task_state["evaluation-worker"]
    assert entry["alive"] is False
    assert entry["state"] == "stopped"
    assert entry["restarts"] == 0


def test_repeated_crashes_give_up_and_report_failure():
    """A crash-looping task must stop being restarted and report failure."""
    worker = _StubWorker(["crash", "crash", "crash", "crash", "crash"])

    async def scenario():
        stop = _fake_stop(0.5)
        return await worker_entry.supervise(
            worker, None, stop=stop, restart_backoff_s=0.001,
            max_consecutive_failures=2,
        )

    results = asyncio.run(scenario())
    entry = worker._task_state["evaluation-worker"]
    assert entry["state"] == "failed"
    assert "RuntimeError" in entry["last_exit_reason"]
    assert worker_entry.task_failures(results) == ["evaluation-worker"]


def test_clean_stop_reports_no_task_failures():
    worker = _StubWorker([])

    async def scenario():
        stop = _fake_stop(0.05)
        return await worker_entry.supervise(worker, None, stop=stop)

    results = asyncio.run(scenario())
    assert worker_entry.task_failures(results) == []


# ---------------------------------------------------------------------------
# 4. healthcheck: freshness + required-task liveness
# ---------------------------------------------------------------------------


def _snapshot_file(tmp_path: Path, **overrides):
    snapshot = {
        "last_health_at": datetime.now(timezone.utc).isoformat(),
        "tasks": {
            "evaluation-worker": {"alive": True, "state": "running", "restarts": 0}
        },
    }
    snapshot.update(overrides)
    path = tmp_path / "alerts-health.json"
    path.write_text(json.dumps(snapshot))
    return path


def test_healthcheck_accepts_a_fresh_live_snapshot(tmp_path):
    assert check_health(str(_snapshot_file(tmp_path))) == []


def test_healthcheck_fails_on_a_stale_snapshot(tmp_path):
    """A stale file on a persistent filesystem must not read as healthy."""
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    problems = check_health(str(_snapshot_file(tmp_path, last_health_at=old)))
    assert problems and "stale" in problems[0]


def test_healthcheck_fails_when_a_required_task_is_not_alive(tmp_path):
    path = _snapshot_file(
        tmp_path,
        tasks={
            "evaluation-worker": {
                "alive": False,
                "state": "failed",
                "last_exit_reason": "RuntimeError: boom",
            }
        },
    )
    problems = check_health(str(path))
    assert problems
    assert "evaluation-worker" in problems[0]
    assert "RuntimeError" in problems[0]


def test_healthcheck_fails_when_a_required_task_is_backing_off(tmp_path):
    """Backing off is degraded, not healthy: the container is not evaluating."""
    path = _snapshot_file(
        tmp_path,
        tasks={"evaluation-worker": {"alive": False, "state": "backing_off"}},
    )
    assert check_health(str(path))


def test_healthcheck_fails_when_the_file_is_missing(tmp_path):
    assert check_health(str(tmp_path / "nope.json"))


def test_healthcheck_exits_nonzero_for_problems(tmp_path, capsys):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    path = _snapshot_file(tmp_path, last_health_at=old)
    assert health_check_main([str(path)]) == 1
    assert "UNHEALTHY" in capsys.readouterr().out


def test_health_snapshot_carries_last_health_at(session_factory, tmp_path):
    """The writer stamps the snapshot so a healthcheck can detect a stalled writer."""
    worker = _worker(session_factory, health_file=str(tmp_path / "h.json"))
    asyncio.run(worker.start())
    asyncio.run(worker.stop())
    worker._write_health()
    written = json.loads((tmp_path / "h.json").read_text())
    assert written.get("last_health_at")
    assert "ltp_freshness_enabled" in written
    assert "rejected_ticks" in written
