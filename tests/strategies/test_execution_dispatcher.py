"""The bounded governed-execution dispatcher (Phase 2).

The dispatcher is what turns a durable decision into work, so its own contract is
worth pinning: an explicit falsy setting is the only way to switch it off, one
bounded pass claims and runs what is queued, an unexpected failure leaves the
request ``dispatch_unresolved`` rather than silently retrying it, and the health
snapshot says what happened.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.strategies.execution_dispatcher import (  # noqa: E402
    DISPATCH_ENABLED_ENV,
    HostedExecutionDispatcher,
    hosted_execution_dispatch_enabled,
)
from backend.strategies.execution_requests import (  # noqa: E402
    ExecutionRequestStateError,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class StubService:
    def __init__(self, *, claimed, outcomes=None, recovery=None, stale=None) -> None:
        self.claimed = list(claimed)
        self.outcomes = dict(outcomes or {})
        self.recovery = dict(recovery or {})
        self.stale = set(stale or ())
        self.calls: list = []
        self.finished: list = []

    def recover_abandoned_claims(self, *, timeout_seconds, now=None):
        self.calls.append(("recover", timeout_seconds))
        return dict(self.recovery)

    def claim_next(self, *, limit, now=None):
        self.calls.append(("claim", limit))
        batch, self.claimed = self.claimed[:limit], self.claimed[limit:]
        return batch

    async def dispatch(self, request_id, *, claim_id=None, now=None):
        self.calls.append(("dispatch", request_id, claim_id))
        if request_id in self.stale:
            raise ExecutionRequestStateError(
                {
                    "request_id": request_id,
                    "status": "dispatch_unresolved",
                    "dispatch_claim_id": claim_id,
                    "message": "this finish is stale",
                }
            )
        outcome = self.outcomes.get(request_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {"request_id": request_id, "status": "executed"}

    def finish(
        self,
        request_id,
        *,
        status,
        refusal_code,
        detail,
        claim_id=None,
        expected_status=None,
    ):
        self.finished.append(
            {
                "request_id": request_id,
                "status": status,
                "refusal_code": refusal_code,
                "detail": dict(detail),
                "claim_id": claim_id,
                "expected_status": expected_status,
            }
        )
        return {"request_id": request_id, "status": status}


def _run(dispatcher):
    return asyncio.get_event_loop().run_until_complete(dispatcher.poll_once())


def test_only_an_explicit_falsy_setting_disables_dispatch():
    assert hosted_execution_dispatch_enabled({}) is True
    assert hosted_execution_dispatch_enabled({DISPATCH_ENABLED_ENV: ""}) is True
    assert hosted_execution_dispatch_enabled({DISPATCH_ENABLED_ENV: "flase"}) is True
    for falsy in ("0", "false", "no", "off", "disabled", " FALSE "):
        assert hosted_execution_dispatch_enabled({DISPATCH_ENABLED_ENV: falsy}) is False


def test_disabled_dispatcher_does_no_work_and_reports_it():
    service = StubService(claimed=[])
    dispatcher = HostedExecutionDispatcher(service=service, enabled=lambda: False)
    counts = _run(dispatcher)
    assert counts["disabled"] is True
    assert service.calls == []
    health = dispatcher.health()
    assert health["state"] == "disabled"
    assert health["enabled"] is False


def test_one_bounded_pass_claims_runs_and_records_its_outcomes():
    service = StubService(
        claimed=[{"request_id": "r1"}, {"request_id": "r2"}, {"request_id": "r3"}],
        outcomes={
            "r1": {"request_id": "r1", "status": "executed"},
            "r2": {"request_id": "r2", "status": "refused", "refusal_code": "ADMISSION_REFUSED"},
            "r3": RuntimeError("broker boundary exploded"),
        },
        recovery={"scanned": 2, "proved_submitted": 1, "unresolved": 1},
    )
    dispatcher = HostedExecutionDispatcher(service=service, limit=3, enabled=lambda: True)
    counts = _run(dispatcher)
    assert counts["recovered_submitted"] == 1
    assert counts["recovered_unresolved"] == 1
    assert counts["claimed"] == 3
    assert counts["executed"] == 1
    assert counts["refused"] == 1
    assert counts["errors"] == 1
    # An unexpected failure is NOT retried: it is recorded as an unresolved
    # outcome so an operator sees it instead of a second physical submission.
    assert service.finished == [
        {
            "request_id": "r3",
            "status": "dispatch_unresolved",
            "refusal_code": "EXECUTION_OUTCOME_UNKNOWN",
            "detail": {"message": "broker boundary exploded", "stage": "dispatch"},
            # The unresolved finish is CAS-fenced on the claim that raised it, so
            # a stale worker cannot overwrite a recovery decision later.
            "claim_id": None,
            "expected_status": "dispatching",
        }
    ]
    health = dispatcher.health()
    assert health["state"] == "degraded"
    assert health["last_pass_at"] is not None
    assert health["last_counts"]["claimed"] == 3


def test_a_broken_pass_is_reported_and_not_fatal():
    class Broken(StubService):
        def claim_next(self, *, limit, now=None):
            raise RuntimeError("database unavailable")

    dispatcher = HostedExecutionDispatcher(service=Broken(claimed=[]), enabled=lambda: True)
    counts = _run(dispatcher)
    assert counts["errors"] == 1
    assert "database unavailable" in counts["claim_error"]
    assert dispatcher.health()["state"] == "degraded"


def test_run_forever_stops_on_cancellation():
    service = StubService(claimed=[])
    dispatcher = HostedExecutionDispatcher(
        service=service, interval_seconds=0.25, enabled=lambda: True
    )

    async def _main():
        task = asyncio.ensure_future(dispatcher.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return dispatcher.health()

    health = asyncio.get_event_loop().run_until_complete(_main())
    assert health["state"] == "stopped"


# ---------------------------------------------------------------------------
# application-lifecycle wiring
# ---------------------------------------------------------------------------


def _app_state_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.workflows.repository import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_the_dispatcher_is_started_and_stopped_by_the_app_lifecycle(monkeypatch):
    from fastapi import FastAPI

    from backend.app.bootstrap import (
        ensure_governed_execution_state,
        start_hosted_execution_dispatcher,
        stop_hosted_execution_dispatcher,
    )

    engine, factory = _app_state_factory()
    app = FastAPI()
    app.state.strategies_session_factory = factory
    monkeypatch.delenv(DISPATCH_ENABLED_ENV, raising=False)
    try:
        ensure_governed_execution_state(app)
        assert app.state.plan_execution_pipeline is not None

        async def _run():
            task = await start_hosted_execution_dispatcher(app)
            assert task is not None
            await asyncio.sleep(0.05)
            health = app.state.hosted_execution_dispatcher.health()
            await stop_hosted_execution_dispatcher(app)
            return health, task

        health, task = asyncio.get_event_loop().run_until_complete(_run())
        assert app.state.hosted_execution_dispatcher_task is None
        assert task.done()
        assert health["enabled"] is True
        assert health["last_counts"] == {} or health["last_counts"]["claimed"] == 0
    finally:
        engine.dispose()


def test_the_dispatch_switch_is_honoured_at_startup(monkeypatch):
    from fastapi import FastAPI

    from backend.app.bootstrap import (
        ensure_governed_execution_state,
        start_hosted_execution_dispatcher,
    )

    engine, factory = _app_state_factory()
    app = FastAPI()
    app.state.strategies_session_factory = factory
    monkeypatch.setenv(DISPATCH_ENABLED_ENV, "false")
    try:
        ensure_governed_execution_state(app)
        task = asyncio.get_event_loop().run_until_complete(
            start_hosted_execution_dispatcher(app)
        )
        assert task is None
        assert app.state.hosted_execution_dispatcher_task is None
        assert app.state.hosted_execution_dispatcher.health()["enabled"] is False
    finally:
        engine.dispose()
