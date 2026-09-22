"""Operator reconciliation HTTP API tests (SQLite, isolated app).

Covers allowed cases, blocked cases, stale/cross-owner/cross-account refusal,
durable audit, and that reconciliation unblocks a new attempt.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies import service as strategy_service  # noqa: E402
from backend.strategies.reconciliation import ReconciliationEvidence  # noqa: E402
from backend.strategies.repository import (  # noqa: E402
    SqlAlchemyStrategyRepository,
    StrategyFenceError,
)
from backend.workflows.repository import Base  # noqa: E402

OWNER = "app:admin"


def _evidence(**overrides) -> ReconciliationEvidence:
    base = dict(
        job_id="",
        strategy_id="",
        attempt=1,
        replacement_blocked=True,
        job_status="recovery_required",
        launched=True,
        trade_capable=True,
        process_cleanup_state="confirmed",
        authority_state="revoked",
        work_state="settled",
        exposure_state="flat",
        protection_state="settled",
    )
    base.update(overrides)
    return ReconciliationEvidence(**base)


class StubCollector:
    """Deterministic evidence, or a sequence for TOCTOU tests."""

    def __init__(self, template, *, second=None) -> None:
        self.template = template
        self.second = second
        self.calls = 0

    async def collect(self, job):
        self.calls += 1
        source = self.template
        if self.second is not None and self.calls > 1:
            source = self.second
        return replace(
            source,
            job_id=str(job.id),
            strategy_id=str(job.strategy_id),
            attempt=int(job.attempt),
            run_id=str(job.run_id),
        )


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, connection_record):
        """The raw platform tables the barrier enumeration reads.

        The unblock transaction now validates the durable settlement proof, so
        this fixture exposes the same evidence sources the real schema has - an
        unreadable source is NOT an empty one.
        """
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.execute(
            """
            CREATE TABLE public.worker_live_execution_links (
                link_id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_run_id TEXT NOT NULL,
                account_id TEXT NOT NULL, broker_order_id TEXT NOT NULL, trade_id TEXT,
                client_order_ref TEXT, basket_execution_id TEXT, basket_leg_index INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.live_order_intents (
                intent_id TEXT PRIMARY KEY, client_order_ref TEXT NOT NULL,
                account_id TEXT NOT NULL, strategy_run_id TEXT NOT NULL, broker_order_id TEXT,
                basket_execution_id TEXT, basket_leg_index INTEGER, bracket_intent_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        dbapi_connection.commit()

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:paper")
    yield


def _make_job(repo, *, owner=OWNER, account="kite:paper", trade=True, status="recovery_required", cleanup="confirmed"):
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
        parameters_schema={"type": "object"},
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=trade),
        created_by=owner,
    )
    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=owner,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    with repo.session_factory() as session:
        session.execute(
            text(
                "UPDATE strategy_jobs SET status = :status, handoff_at = :now, run_id = 'run_1', "
                "token_id = 'worker_1', lease_owner = 'sup-1', lease_epoch = 1, "
                "process_cleanup_state = :cleanup, recovery_required_at = :now WHERE id = :id"
            ),
            {"status": status, "now": datetime.now(timezone.utc), "cleanup": cleanup, "id": job.id},
        )
        session.commit()
    return strategy, job


def _app(session_factory, monkeypatch, user, collector):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.state.reconciliation_collector = collector
    return app


def _client(session_factory, monkeypatch, collector, username="admin"):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user, collector)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _repo(session_factory):
    return SqlAlchemyStrategyRepository(session_factory)


BASE = "/api/strategies"


@pytest.mark.asyncio
async def test_an_injected_quiescence_claim_does_not_unblock_without_a_durable_proof(
    session_factory, monkeypatch
):
    """A collector that CLAIMS quiescence is not a durable proof.

    The unblock transaction validates the execution-settlement barrier itself, so
    with no current proof for the job's own book the attempt stays blocked even
    though the injected evidence says "verified". The allowed path is covered by
    the real-barrier PostgreSQL suite
    (``tests/integration/test_reconciliation_barrier_toctou_postgres.py``) and by
    the end-to-end example, not by injecting the callback being certified.
    """
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(_evidence(quiescence_state="verified"))
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation",
            json={"attempt": 1, "lease_epoch": 1},
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["rejection_reason"] in {
        "RECONCILE_RACE_LOST",
        "EXECUTION_WORK_OUTSTANDING",
    }
    refreshed = repo.get_job(OWNER, job.id)
    assert refreshed.status == "recovery_required" and refreshed.reconciled_at is None


@pytest.mark.asyncio
async def test_blocked_by_open_exposure_stays_blocked_and_audited(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(_evidence(exposure_state="open"))
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "OPEN_EXPOSURE"

        history = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation")).json()["history"]
        assert any(row["outcome"] == "blocked" for row in history)

    assert repo.get_job(OWNER, job.id).status == "recovery_required"
    with pytest.raises(StrategyFenceError):
        repo.create_job(
            strategy_id=strategy.id,
            version_id=job.version_id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={},
            attempt=2,
        )


@pytest.mark.asyncio
async def test_stale_attempt_is_refused(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 99}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "STALE_ATTEMPT"


@pytest.mark.asyncio
async def test_cross_owner_is_404(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo, owner=OWNER)
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector, username="other") as client:
        assert (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}")).status_code == 404
        assert (
            await client.post(
                f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_cross_account_detail_is_403_and_list_omits(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo, account="kite:other-paper")
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector) as client:
        assert (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}")).status_code == 403
        listed = (await client.get(f"{BASE}/{strategy.id}/jobs")).json()["jobs"]
        assert listed == []


@pytest.mark.asyncio
async def test_data_only_completion_does_not_invent_flatness(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo, trade=False)
    collector = StubCollector(_evidence(trade_capable=False, exposure_state="not_applicable"))
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["case"] == "data_only_completed"
        # Evidence records "not_applicable" rather than a fabricated flatness proof.
        assert payload["evidence"]["exposure_state"] == "not_applicable"


@pytest.mark.asyncio
async def test_unavailable_evidence_stays_blocked(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(
        _evidence(exposure_state="unknown", evidence_complete=False, unavailable=["paper_runtime"])
    )
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "EVIDENCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_owner_list_and_detail_are_account_authorized(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector) as client:
        listed = (await client.get(f"{BASE}/{strategy.id}/jobs")).json()["jobs"]
        assert [row["job_id"] for row in listed] == [job.id]
        detail = (await client.get(f"{BASE}/{strategy.id}/jobs/{job.id}")).json()
        assert detail["status"] == "recovery_required"
        assert detail["replacement_blocked"] is True
        assert detail["process_cleanup_state"] == "confirmed"


@pytest.mark.asyncio
async def test_authentication_required(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector, username=None) as client:
        assert (await client.get(f"{BASE}/{strategy.id}/jobs")).status_code == 401


@pytest.mark.asyncio
async def test_trading_capable_blocked_on_unverified_quiescence(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    # Fully clean trading evidence but no execution-settlement barrier.
    collector = StubCollector(_evidence())
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "EXECUTION_QUIESCENCE_UNVERIFIED"
    assert repo.get_job(OWNER, job.id).status == "recovery_required"


@pytest.mark.asyncio
async def test_evidence_changed_between_assessment_and_commit_is_refused(session_factory, monkeypatch):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    # First collection allows; the pre-commit re-check sees open exposure.
    collector = StubCollector(_evidence(quiescence_state="verified"), second=_evidence(exposure_state="open"))
    async with _client(session_factory, monkeypatch, collector) as client:
        response = await client.post(
            f"{BASE}/{strategy.id}/jobs/{job.id}/reconciliation", json={"attempt": 1}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "EVIDENCE_CHANGED"
    assert repo.get_job(OWNER, job.id).status == "recovery_required"
    history = repo.list_reconciliations(job.id)
    assert history and history[0].outcome == "blocked"


def test_audit_write_failure_rolls_back_unblocking(session_factory):
    repo = _repo(session_factory)
    strategy, job = _make_job(repo)
    # A non-JSON-serializable evidence value makes the audit insert fail inside
    # the same transaction: the block must NOT be cleared.
    with pytest.raises(Exception):
        repo.reconcile_with_audit(
            job.id,
            owner_id=OWNER,
            expected_lease_epoch=1,
            expected_attempt=1,
            expected_process_cleanup_state="confirmed",
            expected_run_id="run_1",
            reason_code="TRADING_SETTLED_FLAT",
            evidence={"bad": object()},
            actor_id=OWNER,
        )
    assert repo.get_job(OWNER, job.id).status == "recovery_required"
    assert repo.get_job(OWNER, job.id).reconciled_at is None
    assert repo.list_reconciliations(job.id) == []
