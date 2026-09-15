"""HTTP-boundary tests for the supervisor lifecycle API.

A minimal FastAPI app mounts only the lifecycle router, with the authoritative
``strategy_jobs`` ledger on SQLite and a fake worker repo/token/session surface.
Pins: the narrow credential is required before any job detail; authority is the
persisted lease; unrelated/absent authority is refused; preparation hands off
once and then fails closed.
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
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import hosted_lifecycle as lifecycle_router  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.strategies.supervisor_auth import HEADER_NAME  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
)

BASE = "/api/hosted-supervisor"
OWNER = "app:admin"
CRED = "sup-test-credential"


@pytest.fixture(autouse=True)
def _credential(monkeypatch):
    monkeypatch.setenv("HOSTED_SUPERVISOR_CREDENTIAL", CRED)


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repo = SqlAlchemyStrategyRepository(factory)
    worker = FakeWorkerRepository()

    app = FastAPI()
    app.include_router(lifecycle_router.router, prefix="/api")
    app.state.strategies_session_factory = factory
    app.state.algo_worker_repository = worker
    app.state.journal_service = StubJournalService()

    yield repo, worker, app
    engine.dispose()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _headers(cred=CRED):
    return {HEADER_NAME: cred} if cred else {}


def _queued_job(repo):
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"s-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={
            "schema_version": 2,
            "capabilities": {"trade": True, "notify": False, "data": True},
        },
        created_by=OWNER,
    )
    return repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )


def _authority(lease_owner="sup-A", epoch=0, attempt=1):
    return {"lease_owner": lease_owner, "lease_epoch": epoch, "attempt": attempt}


def _claim_body(lease_owner="sup-A", expected_lease_epoch=0, expected_attempt=1):
    return {
        "lease_owner": lease_owner,
        "expected_lease_epoch": expected_lease_epoch,
        "expected_attempt": expected_attempt,
        "lease_until": _lease(),
    }


def _lease(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credential_is_401_before_any_job_detail(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=0)
        )
        assert response.status_code == 401
        assert "rejection_reason" not in response.text  # no job state leaked


@pytest.mark.asyncio
async def test_wrong_credential_and_child_token_are_rejected(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        assert (
            await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(), headers=_headers("nope"))
        ).status_code == 401
        # A worker bearer token is not a supervisor credential.
        assert (
            await client.post(
                f"{BASE}/jobs/{job.id}/prepare",
                json=_authority(),
                headers={"Authorization": "Bearer kwa_child"},
            )
        ).status_code == 401


# ---------------------------------------------------------------------------
# claim / state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_then_state(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        claimed = await client.post(
            f"{BASE}/jobs/{job.id}/claim",
            json=_claim_body(),
            headers=_headers(),
        )
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        assert body["status"] == "starting" and body["lease_epoch"] == 1

        state = await client.get(
            f"{BASE}/jobs/{job.id}",
            params={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert state.status_code == 200 and state.json()["status"] == "starting"


@pytest.mark.asyncio
async def test_claim_with_stale_epoch_is_409(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await client.post(
            f"{BASE}/jobs/{job.id}/claim",
            json=_claim_body(),
            headers=_headers(),
        )
        stale = await client.post(
            f"{BASE}/jobs/{job.id}/claim",
            json=_claim_body(),
            headers=_headers(),
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_state_with_unrelated_authority_is_403(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await client.post(
            f"{BASE}/jobs/{job.id}/claim",
            json=_claim_body(),
            headers=_headers(),
        )
        response = await client.get(
            f"{BASE}/jobs/{job.id}",
            params={"lease_owner": "someone-else", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_prepare_on_unclaimed_job_is_refused(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=0), headers=_headers()
        )
        assert response.status_code == 403  # no lease owner recorded


@pytest.mark.asyncio
async def test_unknown_job_is_404(harness):
    _repo, _worker, app = harness
    async with _client(app) as client:
        response = await client.get(
            f"{BASE}/jobs/hsj_absent",
            params={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# prepare / handoff / fail-closed
# ---------------------------------------------------------------------------


async def _claim(client, job):
    return await client.post(
        f"{BASE}/jobs/{job.id}/claim",
        json=_claim_body(),
        headers=_headers(),
    )


@pytest.mark.asyncio
async def test_prepare_returns_one_time_child_config(harness):
    repo, worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        assert (await _claim(client, job)).status_code == 200
        response = await client.post(
            f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers()
        )
        assert response.status_code == 200, response.text
        config = response.json()
        assert config["worker_token"].startswith("kwa_")
        assert config["session_nonce"].startswith("wsn_")
        assert config["run_id"] in worker.runs
        # The supervisor credential is never echoed.
        assert CRED not in response.text


@pytest.mark.asyncio
async def test_repeat_prepare_fails_closed(harness):
    repo, worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        assert (
            await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers())
        ).status_code == 200
        second = await client.post(
            f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers()
        )
        assert second.status_code == 409
        assert second.json()["detail"]["rejection_reason"] == "HOSTED_HANDOFF_ALREADY_COMPLETED"
        # Never a second credential: the attempt was not replayed.
        assert len(worker.tokens) == 1
        assert repo.get_job(OWNER, job.id).status == "running"


@pytest.mark.asyncio
async def test_heartbeat_serializes_session_heartbeat_timestamp(harness):
    """A production-shaped worker repo returns a datetime, not an ISO string.

    The response model declares ``last_heartbeat_at: Optional[str]``; returning
    the raw datetime made every post-session heartbeat fail response validation
    (HTTP 500) while the string-returning fake hid it.
    """
    repo, worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        prepared = await client.post(
            f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers()
        )
        assert prepared.status_code == 200, prepared.text
        run_id = prepared.json()["run_id"]

        async def datetime_heartbeat(strategy_run_id, *, expected_nonce):
            run = await FakeWorkerRepository.record_run_heartbeat(
                worker, strategy_run_id, expected_nonce=expected_nonce
            )
            if run is None:
                return None
            run["last_heartbeat_at"] = datetime.now(timezone.utc)
            return run

        worker.record_run_heartbeat = datetime_heartbeat  # type: ignore[assignment]
        response = await client.post(
            f"{BASE}/jobs/{job.id}/heartbeat",
            json={**_authority(epoch=1), "lease_until": _lease()},
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body["last_heartbeat_at"], str)
        assert run_id in worker.runs


@pytest.mark.asyncio
async def test_release_and_fence_at_boundary(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers())
        fenced = await client.post(
            f"{BASE}/jobs/{job.id}/fence", json=_authority(epoch=1), headers=_headers()
        )
        assert fenced.status_code == 200 and fenced.json()["status"] == "recovery_required"


@pytest.mark.asyncio
async def test_launched_release_blocks_replacement_and_state_stays_readable(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers())
        released = await client.post(
            f"{BASE}/jobs/{job.id}/release", json=_authority(epoch=1), headers=_headers()
        )
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "recovery_required"
        assert released.json()["replacement_blocked"] is True

        # Terminal state remains readable with the original authority.
        state = await client.get(
            f"{BASE}/jobs/{job.id}",
            params={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert state.status_code == 200
        assert state.json()["status"] == "recovery_required"


@pytest.mark.asyncio
async def test_released_unlaunched_state_read_and_authority_withdrawal(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        assert (await _claim(client, job)).status_code == 200
        released = await client.post(
            f"{BASE}/jobs/{job.id}/release", json=_authority(epoch=1), headers=_headers()
        )
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "stopped"
        assert released.json()["replacement_blocked"] is False

        # Terminal state read works and reports desired_state=stopped.
        state = await client.get(
            f"{BASE}/jobs/{job.id}",
            params={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert state.status_code == 200, state.text
        assert state.json()["status"] == "stopped"
        assert state.json()["desired_state"] == "stopped"

        # Reads did not restore heartbeat or prepare authority.
        assert (
            await client.post(
                f"{BASE}/jobs/{job.id}/heartbeat",
                json={**_authority(epoch=1), "lease_until": _lease()},
                headers=_headers(),
            )
        ).status_code in (409, 403)
        assert (
            await client.post(
                f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers()
            )
        ).status_code in (409, 403)


@pytest.mark.asyncio
async def test_recover_expired_lease_at_boundary(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers())
        # Lapse the lease.
        with repo.session_factory() as session:
            from sqlalchemy import text as _text

            session.execute(
                _text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :id"),
                {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": job.id},
            )
            session.commit()
        recovered = await client.post(
            f"{BASE}/jobs/{job.id}/recover", json=_authority(epoch=1), headers=_headers()
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "recovery_required"
        assert recovered.json()["replacement_blocked"] is True

        # A live-lease job refuses recovery and must be fenced instead.
        other = _queued_job(repo)
        await _claim(client, other)
        await client.post(f"{BASE}/jobs/{other.id}/prepare", json=_authority(epoch=1), headers=_headers())
        refused = await client.post(
            f"{BASE}/jobs/{other.id}/recover", json=_authority(epoch=1), headers=_headers()
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["rejection_reason"] == "HOSTED_LEASE_STILL_LIVE"


@pytest.mark.asyncio
async def test_process_cleanup_evidence_is_attempt_bound(harness):
    repo, _worker, app = harness
    job = _queued_job(repo)
    async with _client(app) as client:
        await _claim(client, job)
        await client.post(f"{BASE}/jobs/{job.id}/prepare", json=_authority(epoch=1), headers=_headers())

        body = {**_authority(epoch=1), "state": "confirmed"}
        recorded = await client.post(
            f"{BASE}/jobs/{job.id}/process-cleanup", json=body, headers=_headers()
        )
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["process_cleanup_state"] == "confirmed"

        state = await client.get(
            f"{BASE}/jobs/{job.id}",
            params={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 1},
            headers=_headers(),
        )
        assert state.json()["process_cleanup_state"] == "confirmed"

        # A stale attempt cannot attach cleanup evidence.
        stale = await client.post(
            f"{BASE}/jobs/{job.id}/process-cleanup",
            json={"lease_owner": "sup-A", "lease_epoch": 1, "attempt": 2, "state": "confirmed"},
            headers=_headers(),
        )
        assert stale.status_code == 403

        # A child worker token cannot forge supervisor cleanup evidence.
        forged = await client.post(
            f"{BASE}/jobs/{job.id}/process-cleanup",
            json=body,
            headers={"Authorization": "Bearer kwa_child"},
        )
        assert forged.status_code == 401
