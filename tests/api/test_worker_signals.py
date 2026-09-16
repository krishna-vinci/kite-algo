"""External-producer authorization and API contract (Phase 4 F10).

Proves the least-privilege model: worker tokens administer producers and
producer credentials submit values, and neither can do the other's job.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers import worker_signals as worker_signals_router  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

ADMIN_TOKEN = "worker-admin-token"
READ_ONLY_TOKEN = "worker-read-token"
SIGNALS = "/api/worker/signals"
# Ingestion compares against the real clock (a producer cannot submit an
# observation from the future), so the fixture times are relative to now.
def _now():
    return datetime.now(timezone.utc)


T0 = _now()
SCHEMA = {"fields": {"score": "number"}}


@pytest.fixture()
def app_and_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI()

    async def _fake_require_worker_token(request):
        header = request.headers.get("Authorization", "")
        raw = header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else ""
        if raw == ADMIN_TOKEN:
            return WorkerToken(
                token_id="worker_admin", name="admin", account_scope="owner-1",
                allowed_modes=["paper"], allowed_actions=["signals:read", "signals:admin"],
                allowed_templates=[], status="active", expires_at=None,
            )
        if raw == READ_ONLY_TOKEN:
            return WorkerToken(
                token_id="worker_read", name="read", account_scope="owner-1",
                allowed_modes=["paper"], allowed_actions=["signals:read"],
                allowed_templates=[], status="active", expires_at=None,
            )
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Invalid worker token")

    app.state.alerts_session_factory = factory
    app.state._fake_require_worker_token = _fake_require_worker_token
    app.include_router(worker_signals_router.router, prefix="/api")

    import backend.api.routers.worker_shared as shared
    import backend.api.routers.worker_signals as signals_module

    original = shared.require_worker_token
    shared.require_worker_token = _fake_require_worker_token
    signals_module.require_worker_token = _fake_require_worker_token
    try:
        yield app, factory, TestClient(app)
    finally:
        shared.require_worker_token = original
        signals_module.require_worker_token = original
        engine.dispose()


ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
READ_ONLY = {"Authorization": f"Bearer {READ_ONLY_TOKEN}"}


def _register(client, name="quant"):
    response = client.post(
        f"{SIGNALS}/producers",
        json={"name": name, "value_schema": SCHEMA, "default_ttl_s": 3600},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    return response.json()["producer"]


def _credential(client, name="quant"):
    response = client.post(f"{SIGNALS}/producers/{name}/credentials", headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()["secret"]


def _submit(client, secret, payload, **overrides):
    body = {
        "value": payload,
        "event_time": (_now() - timedelta(minutes=5)).isoformat(),
        **overrides,
    }
    return client.post(
        f"{SIGNALS}/values",
        json=body,
        headers={"Authorization": f"Bearer {secret}"},
    )


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------


def test_producer_administration_requires_signals_admin(app_and_factory):
    _app, _factory, client = app_and_factory
    # A read-only worker token may list but not administer.
    assert client.get(f"{SIGNALS}/producers", headers=READ_ONLY).status_code == 200
    denied = client.post(
        f"{SIGNALS}/producers", json={"name": "x", "default_ttl_s": 60}, headers=READ_ONLY
    )
    assert denied.status_code == 403
    assert "signals:admin" in denied.json()["detail"]


def test_a_worker_token_cannot_submit_values_as_a_producer(app_and_factory):
    """Submission needs a producer credential, never worker administration."""
    _app, _factory, client = app_and_factory
    _register(client)
    denied = client.post(
        f"{SIGNALS}/values",
        json={"value": {"score": 1.0}, "event_time": _now().isoformat()},
        headers=ADMIN,
    )
    assert denied.status_code == 401


def test_a_producer_credential_cannot_administer(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    response = client.post(
        f"{SIGNALS}/producers", json={"name": "other", "default_ttl_s": 60},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401


def test_signals_actions_are_grantable_but_not_default(app_and_factory):
    """No existing token silently gains producer administration."""
    from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS
    from backend.api.schemas.worker import _DEFAULT_WORKER_ACTIONS

    assert "signals:read" in DEFAULT_WORKER_ACTIONS
    assert "signals:admin" in DEFAULT_WORKER_ACTIONS
    assert "signals:read" not in _DEFAULT_WORKER_ACTIONS
    assert "signals:admin" not in _DEFAULT_WORKER_ACTIONS


def test_cross_owner_producer_access_is_404(app_and_factory):
    _app, factory, client = app_and_factory
    _register(client, "quant")
    from backend.workflows import external_signals as signals

    session = factory()
    try:
        signals.register_producer(
            session, owner_id="owner-2", name="elsewhere",
            value_schema=SCHEMA, default_ttl_s=60,
        )
        session.commit()
    finally:
        session.close()
    # owner-2's producer is invisible to an owner-1 token.
    assert (
        client.get(f"{SIGNALS}/producers/elsewhere", headers=ADMIN).status_code == 404
    )


# ---------------------------------------------------------------------------
# credential lifecycle through the API
# ---------------------------------------------------------------------------


def test_secret_is_returned_once_and_never_again(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    issued = client.post(f"{SIGNALS}/producers/quant/credentials", headers=ADMIN)
    secret = issued.json()["secret"]
    assert issued.json()["note"] == "store this now; it cannot be retrieved"

    # No subsequent read returns the secret or a hash.
    for response in (
        client.get(f"{SIGNALS}/producers", headers=ADMIN),
        client.get(f"{SIGNALS}/producers/quant", headers=ADMIN),
        client.get(f"{SIGNALS}/values?producer=quant", headers=ADMIN),
        client.get(f"{SIGNALS}/health", headers=ADMIN),
    ):
        assert secret not in response.text
        assert "token_hash" not in response.text


def test_revoked_credential_cannot_submit(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    issued = client.post(f"{SIGNALS}/producers/quant/credentials", headers=ADMIN).json()
    secret, token_id = issued["secret"], issued["token_id"]
    assert _submit(client, secret, {"score": 1.0}).status_code == 201
    revoke = client.post(
        f"{SIGNALS}/producers/quant/credentials/{token_id}/revoke", headers=ADMIN
    )
    assert revoke.status_code == 200
    assert _submit(client, secret, {"score": 2.0}).status_code == 401


def test_revoking_the_producer_stops_all_submissions(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    assert _submit(client, secret, {"score": 1.0}).status_code == 201
    assert client.post(f"{SIGNALS}/producers/quant/revoke", headers=ADMIN).status_code == 200
    assert _submit(client, secret, {"score": 2.0}).status_code == 401


# ---------------------------------------------------------------------------
# ingestion contract
# ---------------------------------------------------------------------------


def test_submit_returns_after_durable_storage(app_and_factory):
    _app, factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    response = _submit(client, secret, {"score": 42.0}, idempotency_key="api-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["deduplicated"] is False
    # Committed before the response: a fresh session sees it.
    from backend.workflows import external_signals as signals

    session = factory()
    try:
        stored = session.get(signals.ExternalSignalValue, body["value_id"])
        assert stored is not None
    finally:
        session.close()


def test_idempotent_replay_and_conflict_are_distinguished(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    first = _submit(client, secret, {"score": 1.0}, idempotency_key="dup")
    assert first.json()["deduplicated"] is False
    replay = _submit(client, secret, {"score": 1.0}, idempotency_key="dup")
    assert replay.status_code == 201
    assert replay.json()["deduplicated"] is True
    assert replay.json()["value_id"] == first.json()["value_id"]

    conflict = _submit(client, secret, {"score": 2.0}, idempotency_key="dup")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["rejection_reason"] == "IDEMPOTENCY_CONFLICT"


def test_future_event_time_is_rejected(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    response = _submit(
        client, secret, {"score": 1.0},
        event_time=(_now() + timedelta(seconds=400)).isoformat(),
    )
    assert response.status_code == 422
    assert "future" in response.json()["detail"]


def test_payload_must_match_the_declared_schema(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    missing = _submit(client, secret, {})
    assert missing.status_code == 422
    wrong_type = _submit(client, secret, {"score": "high"})
    assert wrong_type.status_code == 422


def test_unknown_producer_credential_is_rejected(app_and_factory):
    _app, _factory, client = app_and_factory
    assert _submit(client, "kas_not-a-real-secret", {"score": 1.0}).status_code == 401


def test_health_reports_state_without_secrets(app_and_factory):
    _app, _factory, client = app_and_factory
    _register(client)
    secret = _credential(client)
    _submit(client, secret, {"score": 1.0})
    response = client.get(f"{SIGNALS}/health", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["limits"]["max_future_skew_s"] == 300
    assert "SAMPLED" in body["note"]
    entry = body["producers"][0]
    assert entry["accepted"] == 1
    assert "secret" not in entry and "token_hash" not in entry
    assert secret not in response.text


def test_unauthenticated_requests_always_get_401_not_a_validation_error(app_and_factory):
    """The auth boundary must not depend on the request's shape.

    Authorization is a FastAPI dependency, which is solved BEFORE the
    endpoint's own query/body validation. Without that, a caller sending an
    incomplete request would get a 422 describing the request shape instead of
    a 401 — leaking the expected parameters and making the auth boundary
    inconsistent across routes.
    """
    _app, _factory, client = app_and_factory
    cases = [
        ("GET", f"{SIGNALS}/producers", None),
        ("POST", f"{SIGNALS}/producers", {"name": "x"}),      # admin-only
        ("GET", f"{SIGNALS}/values", None),                    # required query param
        ("POST", f"{SIGNALS}/values", {"value": {}}),          # incomplete body
        ("GET", f"{SIGNALS}/health", None),
    ]
    for method, url, body in cases:
        response = client.request(method, url, json=body)
        assert response.status_code == 401, (
            f"{method} {url} without a credential returned "
            f"{response.status_code}, expected 401"
        )


def test_an_authenticated_but_incomplete_request_still_validates(app_and_factory):
    """Once authorized, ordinary validation applies (401 is not masking it)."""
    _app, _factory, client = app_and_factory
    response = client.get(f"{SIGNALS}/values", headers=ADMIN)
    assert response.status_code == 422
    assert "producer" in response.text
