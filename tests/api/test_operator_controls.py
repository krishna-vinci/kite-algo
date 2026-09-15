"""Operator controls API tests (SQLite, isolated app).

Run now (idempotency, blocks, authorization), Stop (queued/active, states,
supervisor cleanup still possible after a stop request), bounded/redacted logs,
and run-notification history ownership.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.notifications.repository import SqlAlchemyNotificationRepository  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies import service as strategy_service  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import FakeWorkerRepository  # noqa: E402

OWNER = "app:admin"
BASE = "/api/strategies"


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


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:paper")
    yield


def _repo(session_factory):
    return SqlAlchemyStrategyRepository(session_factory)


def _strategy(repo, *, owner=OWNER, account="kite:paper", trade=True, status="active"):
    strategy = repo.create_strategy(
        owner_id=owner,
        name=f"s-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=account,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object", "properties": {"lots": {"type": "integer", "minimum": 1}}, "required": ["lots"]},
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=trade),
        created_by=owner,
    )
    if status != "active":
        repo.update_strategy(owner, strategy.id, status=status)
    return strategy, version


def _app(session_factory, monkeypatch, user, notification_repo=None):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    if notification_repo is not None:
        app.state.notification_repository = notification_repo
    return app


def _client(session_factory, monkeypatch, username="admin", notification_repo=None):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user, notification_repo)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _run_body(version_id, **overrides):
    body = {"version_id": version_id, "params": {"lots": 1}, "idempotency_key": "run-key-0001"}
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_run_now_creates_queued_job_and_is_idempotent(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    async with _client(session_factory, monkeypatch) as client:
        first = await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id))
        assert first.status_code == 200, first.text
        assert first.json()["idempotent"] is False
        job = first.json()["job"]
        assert job["status"] == "queued" and job["attempt"] == 1
        assert job["replacement_blocked"] is True

        retry = await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id))
        assert retry.status_code == 200
        assert retry.json()["idempotent"] is True
        assert retry.json()["job"]["job_id"] == job["job_id"]
    # Snapshots persisted by the store.
    persisted = repo.get_job(OWNER, job["job_id"])
    assert persisted.params_snapshot == {"lots": 1}
    assert persisted.capabilities_snapshot["capabilities"]["trade"] is True
    assert persisted.policy_snapshot["max_duration_s"] == 21600


@pytest.mark.asyncio
async def test_run_now_blocked_by_active_job(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    async with _client(session_factory, monkeypatch) as client:
        assert (await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id))).status_code == 200
        second = await client.post(
            f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id, idempotency_key="run-key-0002")
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "STRATEGY_BLOCKED"


@pytest.mark.asyncio
async def test_run_now_refused_when_disabled_or_recovery_blocked(session_factory, monkeypatch):
    repo = _repo(session_factory)
    disabled, dversion = _strategy(repo, status="disabled")
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(f"{BASE}/{disabled.id}/jobs", json=_run_body(dversion.id))
        assert response.status_code == 409 and response.json()["detail"] == "STRATEGY_DISABLED"

    blocked, bversion = _strategy(repo)
    job = repo.create_job(
        strategy_id=blocked.id, version_id=bversion.id, owner_id=OWNER, job_kind="finite",
        execution_mode="paper", params={"lots": 1},
    )
    repo.claim_job(job.id, lease_owner="sup-A", expected_lease_epoch=0, expected_attempt=1, lease_until=datetime.now(timezone.utc) + timedelta(hours=1))
    repo.mark_recovery_required(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1)
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(f"{BASE}/{blocked.id}/jobs", json=_run_body(bversion.id, idempotency_key="run-key-0003"))
        assert response.status_code == 409 and response.json()["detail"] == "STRATEGY_BLOCKED"


@pytest.mark.asyncio
async def test_run_now_authorization_and_validation(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    other, other_version = _strategy(repo, owner="app:other")

    async with _client(session_factory, monkeypatch) as client:
        # Cross-owner and cross-account do not leak existence.
        assert (await client.post(f"{BASE}/{other.id}/jobs", json=_run_body(other_version.id))).status_code == 404
        # Unknown version for this strategy is 422.
        assert (
            await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body("hsv_missing"))
        ).status_code == 422
        # Invalid params fail against the pinned schema.
        assert (
            await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id, params={}))
        ).status_code == 422
        # Origin check on the unsafe method.
        assert (
            await client.post(
                f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id), headers={"Origin": "http://evil.example"}
            )
        ).status_code == 403

    unauthorized, uversion = _strategy(repo, account="kite:other-paper")
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(f"{BASE}/{unauthorized.id}/jobs", json=_run_body(uversion.id))
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_stop_queued_job_does_not_launch(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    async with _client(session_factory, monkeypatch) as client:
        created = (await client.post(f"{BASE}/{strategy.id}/jobs", json=_run_body(version.id))).json()["job"]
        stopped = await client.post(
            f"{BASE}/{strategy.id}/jobs/{created['job_id']}/stop", json={"attempt": 1}
        )
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert body["stop"]["state"] == "confirmed"
        assert body["stop"]["replacement_blocked"] is False
    persisted = repo.get_job(OWNER, created["job_id"])
    assert persisted.status == "stopped" and persisted.desired_state == "stopped"


def _to_running(repo, strategy, version, *, launched=True):
    job = repo.create_job(
        strategy_id=strategy.id, version_id=version.id, owner_id=OWNER, job_kind="finite",
        execution_mode="paper", params={"lots": 1},
    )
    repo.claim_job(job.id, lease_owner="sup-A", expected_lease_epoch=0, expected_attempt=1, lease_until=datetime.now(timezone.utc) + timedelta(hours=1))
    if launched:
        repo.reserve_child_token(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1, token_id="worker_1")
        repo.record_child_run(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1, token_id="worker_1", run_id="run_1")
        repo.mark_running_and_handoff(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1, run_id="run_1")
    return job


@pytest.mark.asyncio
async def test_stop_active_preserves_supervisor_cleanup_and_block(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    job = _to_running(repo, strategy, version)
    async with _client(session_factory, monkeypatch) as client:
        stopped = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/stop", json={"attempt": 1}
        )
        assert stopped.status_code == 200, stopped.text
        state = stopped.json()["stop"]
        assert state["state"] == "stopping" and state["requested"] is True
        assert state["replacement_blocked"] is True

    persisted = repo.get_job(OWNER, job.id)
    # Stop request does not revoke authority: status/lease intact for cleanup.
    assert persisted.desired_state == "stopped" and persisted.status == "running"
    assert persisted.lease_owner == "sup-A"

    # The supervisor can still report cleanup and complete its terminal transition.
    worker = FakeWorkerRepository()
    await hosted_lifecycle.report_process_cleanup(
        strategy_repo=repo, job_id=job.id, lease_owner="sup-A", lease_epoch=1, attempt=1, state="confirmed"
    )
    released = await hosted_lifecycle.release(
        strategy_repo=repo, worker_repo=worker, job_id=job.id, lease_owner="sup-A", lease_epoch=1, attempt=1
    )
    assert released["status"] == "recovery_required" and released["replacement_blocked"] is True

    async with _client(session_factory, monkeypatch) as client:
        detail = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}")).json()
        assert detail["status"] == "recovery_required"
        assert detail["stop"]["state"] == "confirmed"
        assert detail["stop"]["replacement_blocked"] is True


@pytest.mark.asyncio
async def test_stop_active_reports_cleanup_unresolved_without_evidence(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    job = _to_running(repo, strategy, version)
    repo.request_stop_active(job.id, owner_id=OWNER, expected_attempt=1, actor=OWNER)
    # Supervisor stops the child but does not (yet) report cleanup: fenced.
    repo.mark_recovery_required(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1)
    async with _client(session_factory, monkeypatch) as client:
        detail = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}")).json()
        assert detail["status"] == "recovery_required"
        assert detail["stop"]["state"] == "cleanup_unresolved"


@pytest.mark.asyncio
async def test_logs_are_bounded_redacted_and_unavailable_is_explicit(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    job = _to_running(repo, strategy, version)

    async with _client(session_factory, monkeypatch) as client:
        # Before any collection: explicitly unavailable.
        empty = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/logs")).json()
        assert empty["available"] is False and empty["notice"]

    await hosted_lifecycle.report_job_logs(
        strategy_repo=repo,
        job_id=job.id,
        lease_owner="sup-A",
        lease_epoch=1,
        attempt=1,
        chunks=["starting\n", "token kwa_abcdefghijklmnop\nBearer supersecrettoken123\n"],
    )
    async with _client(session_factory, monkeypatch) as client:
        body = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/logs")).json()
        assert body["available"] is True
        text = "".join(entry["content"] for entry in body["entries"])
        assert "kwa_abcdefghijklmnop" not in text
        assert "supersecrettoken123" not in text
        assert "[redacted]" in text


@pytest.mark.asyncio
async def test_logs_truncate_at_cap(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    job = _to_running(repo, strategy, version)
    big = "x" * (16 * 1024)
    for _ in range(20):
        await hosted_lifecycle.report_job_logs(
            strategy_repo=repo, job_id=job.id, lease_owner="sup-A", lease_epoch=1, attempt=1, chunks=[big]
        )
    assert repo.job_log_byte_count(job.id) <= 256 * 1024
    async with _client(session_factory, monkeypatch) as client:
        body = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/logs")).json()
        assert body["truncated"] is True


@pytest.mark.asyncio
async def test_notification_history_is_owner_scoped(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, version = _strategy(repo)
    job = _to_running(repo, strategy, version)
    notifications = SqlAlchemyNotificationRepository(session_factory)
    notifications.upsert_channel(OWNER, "ops", "ntfy", {"url": "https://example.invalid"}, None, True)
    notifications.enqueue_run_notification(
        owner_id=OWNER, run_id="run_1", channel_names=["ops"], text="hello", idempotency_key="k-12345678",
        occurred_at=datetime.now(timezone.utc),
    )

    async with _client(session_factory, monkeypatch, notification_repo=notifications) as client:
        body = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/notifications")).json()
        assert len(body["events"]) == 1
        assert body["events"][0]["text"] == "hello"
        assert body["events"][0]["deliveries"][0]["status"] == "pending"

    async with _client(session_factory, monkeypatch, username="other", notification_repo=notifications) as client:
        assert (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/notifications")).status_code == 404
