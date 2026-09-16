"""Hosted child authority at the worker HTTP boundary.

Pins: a hosted child token cannot claim/heartbeat/release its session; hosted
mutations are refused when the persisted attempt authority is fenced, expired or
mismatched; an external worker run keeps its established behavior.
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

from backend.api.routers import worker_auth as worker_auth_router  # noqa: E402
from backend.api.routers import worker_execution as worker_execution_router  # noqa: E402
from backend.options.api.worker_options_router import router as worker_options_router  # noqa: E402
from backend.options.execution.store import get_option_run_store  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import FakeWorkerRepository, StubJournalService  # noqa: E402

OWNER = "app:admin"
CHILD_RAW = "kwa_child_token"
CHILD_ID = "worker_child"
EXTERNAL_RAW = "kwa_external_token"
EXTERNAL_ID = "worker_external"


def _now():
    return datetime.now(timezone.utc)


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

    strategy = repo.create_strategy(
        owner_id=OWNER,
        name="hosted-child",
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
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )
    template = f"hosted:{strategy.id}"
    run_id = "run_hosted_1"

    worker.tokens[CHILD_ID] = {
        "token_id": CHILD_ID,
        "name": "hosted-child",
        "account_scope": "kite:paper",
        "allowed_modes": ["paper"],
        "allowed_actions": ["runs:read", "runs:log", "runs:progress", "intents:submit", "runs:exit", "notifications:publish"],
        "allowed_templates": [template],
        "status": "active",
        "expires_at": None,
        "metadata": {"source": "hosted_supervisor"},
    }
    worker.hashes[_hash_token(CHILD_RAW)] = CHILD_ID
    worker.tokens[EXTERNAL_ID] = {
        "token_id": EXTERNAL_ID,
        "name": "external",
        "account_scope": "kite:paper",
        "allowed_modes": ["paper"],
        "allowed_actions": ["runs:read", "heartbeat", "intents:submit", "notifications:publish"],
        "allowed_templates": [],
        "status": "active",
        "expires_at": None,
        "metadata": {},
    }
    worker.hashes[_hash_token(EXTERNAL_RAW)] = EXTERNAL_ID

    def _run(run_id, token_id, template_id):
        return {
            "strategy_run_id": run_id,
            "token_id": token_id,
            "template_id": template_id,
            "account_scope": "kite:paper",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": None,
            "worker_session_claimed_at": None,
            "last_heartbeat_at": None,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
            "closed_at": None,
        }

    worker.runs[run_id] = _run(run_id, CHILD_ID, template)
    worker.runs["run_external_1"] = _run("run_external_1", EXTERNAL_ID, "demo-template")

    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    claimed = repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=_now() + timedelta(hours=1),
    )
    # Bind the child token/run and move the attempt to running.
    repo.reserve_child_token(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1, token_id=CHILD_ID
    )
    repo.record_child_run(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=1,
        expected_attempt=1,
        token_id=CHILD_ID,
        run_id=run_id,
    )
    repo.mark_running_and_handoff(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1, run_id=run_id
    )

    app = FastAPI()
    app.include_router(worker_auth_router.router, prefix="/api")
    app.include_router(worker_execution_router.router, prefix="/api")
    app.include_router(worker_options_router)

    class _NoRunsOptionStore:
        def get_run(self, *_a, **_k):
            raise KeyError("no option run")

    app.dependency_overrides[get_option_run_store] = lambda: _NoRunsOptionStore()
    app.state.algo_worker_repository = worker
    app.state.strategies_session_factory = factory
    app.state.journal_service = StubJournalService()

    from backend.notifications.repository import SqlAlchemyNotificationRepository

    notification_repo = SqlAlchemyNotificationRepository(factory)
    notification_repo.upsert_channel(OWNER, "ops", "ntfy", {"url": "https://example.invalid"}, None, True)
    app.state.notification_repository = notification_repo

    yield repo, worker, app, job, run_id, template, factory
    engine.dispose()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _child_headers():
    return {"Authorization": f"Bearer {CHILD_RAW}"}


def _external_headers():
    return {"Authorization": f"Bearer {EXTERNAL_RAW}"}


BASE = "/api/algo-workers"


@pytest.mark.asyncio
async def test_hosted_child_cannot_claim_session(harness):
    _repo, _worker, app, _job, run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/claim-session", headers=_child_headers()
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_CHILD_LIFECYCLE_FORBIDDEN"


@pytest.mark.asyncio
async def test_hosted_child_cannot_release_session(harness):
    _repo, _worker, app, _job, run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.request(
            "DELETE",
            f"{BASE}/worker/runs/{run_id}/claim-session",
            headers={**_child_headers(), "X-Worker-Session-Nonce": "whatever"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_hosted_child_cannot_heartbeat(harness):
    _repo, _worker, app, _job, run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/heartbeat",
            headers={**_child_headers(), "X-Worker-Session-Nonce": "whatever"},
            json={"status": "healthy"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_external_worker_session_lifecycle_unchanged(harness):
    _repo, _worker, app, _job, _run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/run_external_1/claim-session", headers=_external_headers()
        )
        assert response.status_code == 200
        assert response.json()["worker_session_nonce"].startswith("wsn_")


@pytest.mark.asyncio
async def test_hosted_mutation_refused_when_fenced(harness):
    repo, _worker, app, job, run_id, _t, _f = harness
    asyncio_fence = repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    assert asyncio_fence is True
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/intents",
            headers=_child_headers(),
            json={"intent_type": "place_order", "payload": {}, "idempotency_key": "idem-12345"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_ATTEMPT_FENCED"


@pytest.mark.asyncio
async def test_hosted_mutation_refused_when_lease_expired(harness):
    repo, _worker, app, job, run_id, _t, factory = harness
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :id"),
            {"past": _now() - timedelta(minutes=1), "id": job.id},
        )
        session.commit()
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/intents",
            headers=_child_headers(),
            json={"intent_type": "place_order", "payload": {}, "idempotency_key": "idem-12345"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_LEASE_EXPIRED"


@pytest.mark.asyncio
async def test_hosted_token_cannot_mutate_options_without_a_bound_run(harness):
    _repo, _worker, app, _job, _run_id, _t, _f = harness
    async with _client(app) as client:
        # An options id with no corresponding worker run must fail closed.
        response = await client.post(
            f"{BASE}/worker/options/runs/run_does_not_exist/enter",
            headers=_child_headers(),
            json={},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_CHILD_RUN_REQUIRED"


@pytest.mark.asyncio
async def test_hosted_token_cannot_mutate_another_tokens_options_run(harness):
    _repo, _worker, app, _job, _run_id, _t, _f = harness
    async with _client(app) as client:
        # run_external_1 exists but is bound to the external token.
        response = await client.post(
            f"{BASE}/worker/options/runs/run_external_1/enter",
            headers=_child_headers(),
            json={},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_CHILD_RUN_REQUIRED"


@pytest.mark.asyncio
async def test_hosted_token_cannot_create_unbound_options_run(harness):
    _repo, _worker, app, _job, _run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/options/runs",
            headers=_child_headers(),
            json={"strategy_name": "x", "product": "MIS", "legs": []},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_CHILD_RUN_REQUIRED"


@pytest.mark.asyncio
async def test_hosted_mutation_refused_on_token_mismatch(harness):
    _repo, _worker, app, job, run_id, _t, factory = harness
    # The stored attempt is bound to a different child credential (e.g. after a
    # rotation), so this token is not the authorized one.
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET token_id = :tid WHERE id = :id"),
            {"tid": "worker_someone_else", "id": job.id},
        )
        session.commit()
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/intents",
            headers=_child_headers(),
            json={"intent_type": "place_order", "payload": {}, "idempotency_key": "idem-12345"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_ATTEMPT_TOKEN_MISMATCH"


@pytest.mark.asyncio
async def test_hosted_child_progress_is_child_authenticated_and_session_bound(harness):
    repo, worker, app, job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    async with _client(app) as client:
        # Session-bound: without the nonce the progress call is refused.
        missing = await client.post(
            f"{BASE}/worker/runs/{run_id}/progress", headers=_child_headers(), json={}
        )
        assert missing.status_code == 409
        assert missing.json()["detail"]["rejection_reason"] == "WORKER_SESSION_REQUIRED"

        assert repo.get_job(OWNER, job.id).last_progress_at is None
        ok = await client.post(
            f"{BASE}/worker/runs/{run_id}/progress",
            headers={**_child_headers(), "X-Worker-Session-Nonce": "wsn_live"},
            json={"note": "tick"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["recorded"] is True
        # Only accepted child progress writes last_progress_at.
        assert repo.get_job(OWNER, job.id).last_progress_at is not None


@pytest.mark.asyncio
async def test_hosted_child_progress_refused_when_fenced(harness):
    repo, worker, app, job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    repo.mark_recovery_required(job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1)
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/progress",
            headers={**_child_headers(), "X-Worker-Session-Nonce": "wsn_live"},
            json={},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_ATTEMPT_FENCED"


# ---------------------------------------------------------------------------
# options operation permissions (independent of identity)
# ---------------------------------------------------------------------------


def _options_entry_url(run_id: str, leaf: str) -> str:
    return f"{BASE}/worker/options/runs/{run_id}/{leaf}"


@pytest.mark.asyncio
async def test_hosted_data_only_token_cannot_enter_exit_or_change_protection(harness):
    _repo, worker, app, _job, run_id, _t, _f = harness
    # Same identity, but the token carries no trading/protection action.
    worker.tokens[CHILD_ID]["allowed_actions"] = ["runs:read", "runs:log", "runs:progress"]
    async with _client(app) as client:
        for leaf, method in (("enter", "post"), ("exit", "post"), ("protection", "put")):
            response = await getattr(client, method)(
                _options_entry_url(run_id, leaf), headers=_child_headers(), json={}
            )
            assert response.status_code == 403, (leaf, response.text)
            assert response.json()["detail"]["rejection_reason"] == "HOSTED_OPERATION_NOT_PERMITTED"


@pytest.mark.asyncio
async def test_hosted_dry_run_options_mutation_rejected_but_preview_allowed(harness):
    _repo, worker, app, job, run_id, _t, factory = harness
    worker.runs[run_id]["execution_mode"] = "dry_run"
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET execution_mode = 'dry_run' WHERE id = :id"),
            {"id": job.id},
        )
        session.commit()
    async with _client(app) as client:
        response = await client.post(_options_entry_url(run_id, "enter"), headers=_child_headers(), json={})
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_OPTIONS_MUTATION_PAPER_ONLY"

        # Preview does not mutate, so the mode gate does not apply to it.
        preview = await client.post(
            _options_entry_url(run_id, "preview-entry"), headers=_child_headers(), json={}
        )
        assert "HOSTED_OPTIONS_MUTATION_PAPER_ONLY" not in preview.text
        assert "HOSTED_OPERATION_NOT_PERMITTED" not in preview.text


@pytest.mark.asyncio
async def test_hosted_execution_injection_rejected(harness):
    _repo, _worker, app, _job, run_id, _t, _f = harness
    async with _client(app) as client:
        response = await client.post(
            _options_entry_url(run_id, "enter"),
            headers=_child_headers(),
            json={"order_results": [{"order_id": "fake"}]},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_EXECUTION_INJECTION_FORBIDDEN"


# ---------------------------------------------------------------------------
# run-scoped notifications
# ---------------------------------------------------------------------------


def _notify_headers():
    return {**_child_headers(), "X-Worker-Session-Nonce": "wsn_live"}


@pytest.mark.asyncio
async def test_hosted_child_notify_accepted_then_deduped(harness):
    _repo, worker, app, _job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    body = {"text": "target hit", "channels": ["ops"], "idempotency_key": "key-12345678"}
    async with _client(app) as client:
        first = await client.post(f"{BASE}/worker/runs/{run_id}/notify", headers=_notify_headers(), json=body)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "accepted" and first.json()["delivery_count"] == 1
        second = await client.post(f"{BASE}/worker/runs/{run_id}/notify", headers=_notify_headers(), json=body)
        assert second.json()["status"] == "deduped"
        assert second.json()["event_id"] == first.json()["event_id"]


@pytest.mark.asyncio
async def test_hosted_child_notify_unknown_channel_is_422(harness):
    _repo, worker, app, _job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/notify",
            headers=_notify_headers(),
            json={"text": "hi", "channels": ["nope"], "idempotency_key": "key-12345678"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["rejection_reason"] == "unknown_channel"


@pytest.mark.asyncio
async def test_hosted_child_notify_requires_session_nonce(harness):
    _repo, worker, app, _job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/notify",
            headers=_child_headers(),
            json={"text": "hi", "channels": ["ops"], "idempotency_key": "key-12345678"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["rejection_reason"] == "WORKER_SESSION_REQUIRED"


@pytest.mark.asyncio
async def test_hosted_child_notify_requires_publish_permission(harness):
    _repo, worker, app, _job, run_id, _t, _f = harness
    worker.runs[run_id]["worker_session_nonce"] = "wsn_live"
    worker.tokens[CHILD_ID]["allowed_actions"] = ["runs:read", "runs:log"]
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/{run_id}/notify",
            headers=_notify_headers(),
            json={"text": "hi", "channels": ["ops"], "idempotency_key": "key-12345678"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_external_run_notify_is_refused(harness):
    _repo, worker, app, _job, _run_id, _t, _f = harness
    worker.runs["run_external_1"]["worker_session_nonce"] = "wsn_ext"
    async with _client(app) as client:
        response = await client.post(
            f"{BASE}/worker/runs/run_external_1/notify",
            headers={**_external_headers(), "X-Worker-Session-Nonce": "wsn_ext"},
            json={"text": "hi", "channels": ["ops"], "idempotency_key": "key-12345678"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["rejection_reason"] == "HOSTED_NOTIFY_UNSUPPORTED"
