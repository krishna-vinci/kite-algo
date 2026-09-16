# pyright: reportArgumentType=false
"""API tests for the worker notification-channel endpoints (Task 8).

Bootstrapping mirrors tests/api/test_algo_worker_api.py / test_worker_workflows.py:
dependency stubs first, in-memory SQLite with the shared alerts-platform
``Base.metadata.create_all``, auth via a stub worker-token repository on
``app.state``, alerts sessionmaker via ``app.dependency_overrides``.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Same pattern as test_worker_workflows.py: keep backend.app.database's
# module-level engine constructible under the dependency stubs.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers import worker_notifications as worker_notifications_router  # noqa: E402
from backend.api.routers import worker_workflows as worker_workflows_router  # noqa: E402
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.notifications.adapters import DeliveryOutcome, register_adapter  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

RAW_TOKEN = "worker-secret-token"
HEADERS = {"Authorization": f"Bearer {RAW_TOKEN}"}
CHANNELS = "/api/worker/notification-channels"
MISSING_SECRET_ENV = "ALERTS_TEST_SECRET_DEFINITELY_MISSING_42"
# Provider defaults must NEVER be used when the channel names its own secret.
TELEGRAM_DEFAULT_ENV = "TELEGRAM_BOT_TOKEN"
NTFY_DEFAULT_ENV = "NTFY_PRIMARY_URL"


class _StubWorkerTokenRepository:
    def __init__(self, token: WorkerToken, *, raw_token: str = RAW_TOKEN):
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


class _FakeAdapter:
    provider = "telegram"

    def __init__(self):
        self.calls = []

    async def send(self, destination, subject, body):
        self.calls.append({"destination": dict(destination), "subject": subject, "body": body})
        return DeliveryOutcome(status="accepted", provider_id="msg-1", detail="ok")


def _token(actions=None) -> WorkerToken:
    return WorkerToken(
        token_id="worker-1",
        name="test-worker",
        account_scope="kite:paper-a",
        allowed_modes=["paper", "dry_run"],
        allowed_actions=sorted(DEFAULT_WORKER_ACTIONS if actions is None else actions),
        allowed_templates=[],
    )


def _client(actions=None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(worker_notifications_router.router, prefix="/api")
    app.include_router(worker_workflows_router.router, prefix="/api")
    app.dependency_overrides[worker_workflows_router._alerts_db] = lambda: factory
    app.state.algo_worker_repository = _StubWorkerTokenRepository(_token(actions))
    return TestClient(app), factory


def _create_channel(client, **overrides):
    payload = {
        "name": "telegram_primary",
        "provider": "telegram",
        "destination": {"chat_id": "12345"},
        "secret_env": MISSING_SECRET_ENV,
        "enabled": True,
    }
    payload.update(overrides)
    response = client.post(CHANNELS, json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def fake_telegram():
    adapter = _FakeAdapter()
    previous = register_adapter("telegram", lambda: adapter)
    yield adapter
    register_adapter("telegram", previous)


def test_channel_create_and_list():
    client, _ = _client()
    created = _create_channel(client)
    assert created["channel_id"]
    assert created["name"] == "telegram_primary"
    assert created["provider"] == "telegram"
    assert created["destination"] == {"chat_id": "12345"}
    assert created["secret_env"] == MISSING_SECRET_ENV

    listing = client.get(CHANNELS, headers=HEADERS).json()["channels"]
    assert [channel["channel_id"] for channel in listing] == [created["channel_id"]]


def test_channel_upsert_same_name_updates_existing():
    client, _ = _client()
    first = _create_channel(client)
    second = _create_channel(client, enabled=False, secret_env=None)
    assert second["channel_id"] == first["channel_id"]
    assert second["enabled"] is False
    assert second["secret_env"] is None

    listing = client.get(CHANNELS, headers=HEADERS).json()["channels"]
    assert len(listing) == 1
    assert listing[0]["enabled"] is False


def test_channel_create_requires_workflows_write():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"workflows:write"})
    response = client.post(
        CHANNELS,
        json={"name": "x", "provider": "ntfy", "destination": {"topic": "alerts"}},
        headers=HEADERS,
    )
    assert response.status_code == 403, response.text


def test_channel_test_missing_env_secret_returns_400_naming_env_var():
    client, _ = _client()
    created = _create_channel(client)  # secret_env names a variable that is not set
    response = client.post(f"{CHANNELS}/{created['channel_id']}/test", json={}, headers=HEADERS)
    assert response.status_code == 400, response.text
    body = response.json()
    # the env var named is exactly the channel's secret_env, never a provider default
    assert body["detail"]["secret_env"] == MISSING_SECRET_ENV
    assert MISSING_SECRET_ENV in body["detail"]["message"]
    assert TELEGRAM_DEFAULT_ENV not in body["detail"]["message"]


def test_channel_test_with_env_set_and_fake_adapter_returns_outcome(fake_telegram, monkeypatch):
    monkeypatch.setenv("ALERTS_TEST_SECRET_OK", "token-value")
    client, _ = _client()
    created = _create_channel(client, secret_env="ALERTS_TEST_SECRET_OK")

    response = client.post(
        f"{CHANNELS}/{created['channel_id']}/test",
        json={"message": "ping"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "accepted", "provider_id": "msg-1", "detail": "ok"}

    assert len(fake_telegram.calls) == 1
    call = fake_telegram.calls[0]
    assert call["subject"].startswith("[Test]")
    assert call["body"] == "ping"
    # PINNED contract: the channel's secret_env overrides token_env in the
    # destination — the adapter resolves exactly this variable.
    assert call["destination"] == {"chat_id": "12345", "token_env": "ALERTS_TEST_SECRET_OK"}


def test_channel_test_ntfy_uses_url_env_override_and_names_it_when_missing(monkeypatch):
    """ntfy channels carry the secret through `url_env`; a missing env var is
    reported 400 naming the channel's secret_env, not the provider default."""
    client, _ = _client()
    created = _create_channel(
        client,
        name="ntfy_primary",
        provider="ntfy",
        destination={"topic": "https://ntfy.sh/alerts"},
        secret_env=MISSING_SECRET_ENV,
    )
    assert NTFY_DEFAULT_ENV != MISSING_SECRET_ENV
    monkeypatch.delenv(NTFY_DEFAULT_ENV, raising=False)

    response = client.post(f"{CHANNELS}/{created['channel_id']}/test", json={}, headers=HEADERS)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["detail"]["secret_env"] == MISSING_SECRET_ENV
    assert MISSING_SECRET_ENV in body["detail"]["message"]
    assert NTFY_DEFAULT_ENV not in body["detail"]["message"]


def test_channel_test_ntfy_destination_carries_url_env(monkeypatch):
    class _FakeNtfy:
        provider = "ntfy"

        def __init__(self):
            self.calls = []

        async def send(self, destination, subject, body):
            self.calls.append({"destination": dict(destination), "subject": subject, "body": body})
            return DeliveryOutcome(status="accepted", provider_id="ntfy-1", detail="ok")

    fake = _FakeNtfy()
    previous = register_adapter("ntfy", lambda: fake)
    try:
        monkeypatch.setenv("ALERTS_TEST_NTFY_URL", "https://ntfy.sh/alerts-test")
        client, _ = _client()
        created = _create_channel(
            client,
            name="ntfy_primary",
            provider="ntfy",
            destination={"topic": "https://ntfy.sh/alerts"},
            secret_env="ALERTS_TEST_NTFY_URL",
        )
        response = client.post(
            f"{CHANNELS}/{created['channel_id']}/test",
            json={"message": "ping"},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"
        # PINNED contract: ntfy destination gets {"url_env": channel.secret_env}
        assert fake.calls[0]["destination"] == {
            "topic": "https://ntfy.sh/alerts",
            "url_env": "ALERTS_TEST_NTFY_URL",
        }
    finally:
        register_adapter("ntfy", previous)


def test_channel_test_default_message_without_body(fake_telegram):
    client, _ = _client()
    created = _create_channel(client, secret_env=None)
    response = client.post(f"{CHANNELS}/{created['channel_id']}/test", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert fake_telegram.calls[0]["body"]


def test_channel_test_without_notifications_test_action_is_403():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"notifications:test"})
    created = _create_channel(client, secret_env=None)  # workflows:write is still allowed
    response = client.post(f"{CHANNELS}/{created['channel_id']}/test", json={}, headers=HEADERS)
    assert response.status_code == 403, response.text


def test_channel_test_unknown_channel_is_404():
    client, _ = _client()
    response = client.post(f"{CHANNELS}/no-such-channel/test", json={}, headers=HEADERS)
    assert response.status_code == 404


def test_channel_test_unknown_provider_is_400():
    client, _ = _client()
    created = _create_channel(client, provider="carrier-pigeon", secret_env=None)
    response = client.post(f"{CHANNELS}/{created['channel_id']}/test", json={}, headers=HEADERS)
    assert response.status_code == 400, response.text
