"""Lifecycle preparation state machine (SQLite + fake worker repository).

The authoritative ledger (``strategy_jobs``) is a real
``SqlAlchemyStrategyRepository``; the worker-run/token/session surface is a fake.
This isolates the preparation state machine and its fail-closed behaviour, which
is the security-relevant part. Genuine concurrent preparation is pinned on
PostgreSQL in ``tests/integration/test_hosted_supervisor_lifecycle_postgres.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies import service  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
    make_request,
)

OWNER = "app:admin"


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _future(minutes=60):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


class Harness:
    def __init__(self, factory):
        self.factory = factory
        self.repo = SqlAlchemyStrategyRepository(factory)
        self.worker = FakeWorkerRepository()
        self.app = FastAPI()
        self.app.state.strategies_session_factory = factory
        self.app.state.algo_worker_repository = self.worker
        self.app.state.journal_service = StubJournalService()
        self.request = make_request(self.app)

    def job(self):
        strategy = self.repo.create_strategy(
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
        version = self.repo.create_version(
            strategy_id=strategy.id,
            source="print('hi')\n",
            source_sha256="a" * 64,
            parameters_schema={"type": "object", "properties": {"lots": {"type": "integer"}}, "required": ["lots"]},
            capabilities_snapshot={"schema_version": 1},
            created_by=OWNER,
        )
        job = self.repo.create_job(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={"lots": 1},
        )
        claimed = self.repo.claim_job(
            job.id,
            lease_owner="sup-A",
            expected_lease_epoch=0,
            expected_attempt=1,
            lease_until=_future(),
        )
        assert claimed is not None
        return strategy, job, 1, claimed.lease_epoch

    def prepare(self, job_id, *, owner="sup-A", epoch=1, attempt=1):
        return asyncio.run(
            hosted_lifecycle.prepare_launch(
                self.request,
                strategy_repo=self.repo,
                worker_repo=self.worker,
                job_id=job_id,
                lease_owner=owner,
                lease_epoch=epoch,
                attempt=attempt,
            )
        )


@pytest.fixture()
def harness(factory):
    return Harness(factory)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_prepare_hands_off_one_child_credential(harness):
    strategy, job, attempt, epoch = harness.job()
    config = harness.prepare(job.id, epoch=epoch, attempt=attempt)

    assert config["run_id"].startswith("run_")
    assert config["worker_token"].startswith("kwa_")
    assert config["session_nonce"].startswith("wsn_")
    assert config["template_id"] == f"hosted:{strategy.id}"
    assert config["execution_mode"] == "paper"
    assert config["params"] == {"lots": 1}

    # Exactly one token minted; it is a child token (no heartbeat).
    assert len(harness.worker.tokens) == 1
    token = next(iter(harness.worker.tokens.values()))
    assert "heartbeat" not in token["allowed_actions"]
    assert "intents:submit" in token["allowed_actions"]
    assert "runs:read" in token["allowed_actions"]
    assert token["allowed_templates"] == [f"hosted:{strategy.id}"]

    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "running"
    assert persisted.token_id == token["token_id"]
    assert persisted.run_id == config["run_id"]
    assert persisted.handoff_at is not None
    # The run is bound to the same token.
    assert harness.worker.runs[config["run_id"]]["token_id"] == token["token_id"]


def test_repeat_prepare_never_mints_second_credential(harness):
    _strategy, job, attempt, epoch = harness.job()
    config = harness.prepare(job.id, epoch=epoch, attempt=attempt)

    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.status_code == 409
    assert exc.value.detail["rejection_reason"] == "HOSTED_HANDOFF_ALREADY_COMPLETED"

    # Exactly one credential and one run: the launch is never replayed.
    assert len(harness.worker.tokens) == 1
    assert len(harness.worker.runs) == 1
    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "running"
    assert persisted.run_id == config["run_id"]
    assert harness.worker.tokens[persisted.token_id]["status"] == "active"


def test_partial_prepare_is_not_resumed(harness):
    _strategy, job, attempt, epoch = harness.job()
    # Simulate a crash after the token id was reserved but before handoff: the
    # lease is still live, only the marker exists.
    assert harness.repo.reserve_child_token(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=epoch,
        expected_attempt=attempt,
        token_id="worker_orphan",
    )

    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.detail["rejection_reason"] == "HOSTED_PREPARE_INCOMPLETE"
    # No second credential was minted, and the reservation is untouched.
    assert harness.worker.tokens == {}
    assert harness.repo.get_job(OWNER, job.id).token_id == "worker_orphan"


# ---------------------------------------------------------------------------
# authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owner,epoch,attempt",
    [("sup-B", 1, 1), ("sup-A", 0, 1), ("sup-A", 1, 2)],
)
def test_wrong_lease_authority_is_refused(harness, owner, epoch, attempt):
    _strategy, job, _a, claimed_epoch = harness.job()
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, owner=owner, epoch=epoch, attempt=attempt)
    assert exc.value.status_code == 403
    assert harness.repo.get_job(OWNER, job.id).status == "starting"


def test_expired_lease_is_refused(harness):
    _strategy, job, attempt, epoch = harness.job()
    with harness.factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": job.id},
        )
        session.commit()
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.detail["rejection_reason"] == "HOSTED_LEASE_EXPIRED"


def test_unknown_job_is_404(harness):
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare("hsj_missing", epoch=1, attempt=1)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# failure between steps
# ---------------------------------------------------------------------------


def test_token_mint_failure_fences_and_revokes(harness, monkeypatch):
    _strategy, job, attempt, epoch = harness.job()

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(harness.worker, "create_token", _boom)
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.detail["rejection_reason"] == "HOSTED_TOKEN_MINT_FAILED"
    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "recovery_required"
    # Reservation survived so the orphan id is discoverable/revocable.
    assert persisted.token_id is not None


def test_run_create_failure_fences_and_revokes(harness, monkeypatch):
    _strategy, job, attempt, epoch = harness.job()

    async def _boom(*a, **k):
        raise RuntimeError("run insert failed")

    monkeypatch.setattr(harness.worker, "create_run", _boom)
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.detail["rejection_reason"] == "HOSTED_RUN_CREATE_FAILED"
    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "recovery_required"
    assert harness.worker.tokens[persisted.token_id]["status"] == "revoked"


def test_session_claim_failure_fences_and_revokes(harness, monkeypatch):
    _strategy, job, attempt, epoch = harness.job()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(harness.worker, "claim_run_session", _none)
    with pytest.raises(hosted_lifecycle.HostedLifecycleError) as exc:
        harness.prepare(job.id, epoch=epoch, attempt=attempt)
    assert exc.value.detail["rejection_reason"] == "HOSTED_SESSION_CLAIM_FAILED"
    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "recovery_required"
    assert harness.worker.tokens[persisted.token_id]["status"] == "revoked"


# ---------------------------------------------------------------------------
# heartbeat / release / fence
# ---------------------------------------------------------------------------


def test_heartbeat_extends_lease_without_touching_progress(harness):
    _strategy, job, attempt, epoch = harness.job()
    harness.prepare(job.id, epoch=epoch, attempt=attempt)
    before = harness.repo.get_job(OWNER, job.id)
    assert before.last_progress_at is None

    result = asyncio.run(
        hosted_lifecycle.heartbeat(
            strategy_repo=harness.repo,
            worker_repo=harness.worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=epoch,
            attempt=attempt,
            lease_until=_future(minutes=120),
        )
    )
    assert result["status"] == "ok"
    after = harness.repo.get_job(OWNER, job.id)
    assert after.last_progress_at is None  # heartbeat is liveness, not progress
    assert after.lease_until > before.lease_until
    assert result["last_heartbeat_at"] is not None


def test_heartbeat_with_stale_epoch_is_refused(harness):
    _strategy, job, attempt, epoch = harness.job()
    harness.prepare(job.id, epoch=epoch, attempt=attempt)
    with pytest.raises(hosted_lifecycle.HostedLifecycleError):
        asyncio.run(
            hosted_lifecycle.heartbeat(
                strategy_repo=harness.repo,
                worker_repo=harness.worker,
                job_id=job.id,
                lease_owner="sup-A",
                lease_epoch=epoch + 5,
                attempt=attempt,
                lease_until=_future(),
            )
        )


def test_release_stops_and_revokes(harness):
    _strategy, job, attempt, epoch = harness.job()
    config = harness.prepare(job.id, epoch=epoch, attempt=attempt)
    result = asyncio.run(
        hosted_lifecycle.release(
            strategy_repo=harness.repo,
            worker_repo=harness.worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=epoch,
            attempt=attempt,
        )
    )
    assert result["status"] == "stopped"
    assert harness.repo.get_job(OWNER, job.id).status == "stopped"
    assert harness.worker.runs[config["run_id"]]["worker_session_nonce"] is None
    persisted = harness.repo.get_job(OWNER, job.id)
    assert harness.worker.tokens[persisted.token_id]["status"] == "revoked"


def test_fence_marks_recovery_required_and_revokes(harness):
    _strategy, job, attempt, epoch = harness.job()
    harness.prepare(job.id, epoch=epoch, attempt=attempt)
    asyncio.run(
        hosted_lifecycle.fence(
            strategy_repo=harness.repo,
            worker_repo=harness.worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=epoch,
            attempt=attempt,
        )
    )
    persisted = harness.repo.get_job(OWNER, job.id)
    assert persisted.status == "recovery_required"
    assert harness.worker.tokens[persisted.token_id]["status"] == "revoked"
