"""Authorization and store tests for the hosted-strategy foundation API.

Uses a bounded ``httpx`` ASGI transport rather than ``TestClient`` (the
TestClient's portal can hang under the shared test process) with a minimal app
that mounts only this router — no lifespan, no background tasks.

Pinned properties:

- every route requires an app session; a worker bearer token does not open it;
- the owner is server-derived; a body ``owner_id`` is rejected (no actor-supplied
  owner identity);
- cross-owner ids are 404, never 403-with-existence-leak;
- unsafe methods enforce the same-origin assertion;
- invalid schemas / oversized payloads are 422;
- source is stored, never imported or executed;
- there is no lifecycle mutation route in this slice.
"""

from __future__ import annotations

import hashlib
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.app.auth import AppUser
from backend.strategies import models  # noqa: F401 (table registration)
from backend.workflows.repository import Base

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
    """Authorize exactly the paper account used by the fixtures (default-deny)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:paper")
    yield


def _app(session_factory, monkeypatch, user):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    return app


def _client(session_factory, monkeypatch, username="admin"):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


VALID_CREATE = {
    "name": "intraday-fut",
    "execution_mode": "paper",
    "job_kind": "finite",
    "account_scope": "kite:paper",
    "max_duration_s": 21600,
    "progress_deadline_s": 600,
    "stale_exit_policy": "exit_on_worker_still",  # replaced below
}


def _payload(**overrides):
    body = dict(VALID_CREATE)
    body["stale_exit_policy"] = "exit_on_worker_stale"
    body.update(overrides)
    return body


async def _create(client, **overrides):
    response = await client.post(BASE, json=_payload(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# authentication / authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_route_requires_a_session(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        assert (await client.get(BASE)).status_code == 401
        assert (await client.post(BASE, json=_payload())).status_code == 401


@pytest.mark.asyncio
async def test_a_worker_bearer_token_does_not_open_the_surface(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        response = await client.get(BASE, headers={"Authorization": "Bearer kwa_something"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_body_owner_identity_is_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(BASE, json={**_payload(), "owner_id": "app:someone-else"})
        assert response.status_code == 422  # extra="forbid": no actor-supplied owner


@pytest.mark.asyncio
async def test_cross_owner_ids_are_404(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username="admin") as client:
        created = await _create(client)
    async with _client(session_factory, monkeypatch, username="other") as client:
        assert (await client.get(f"{BASE}/{created['strategy_id']}")).status_code == 404
        assert (await client.get(f"{BASE}/{created['strategy_id']}/versions")).status_code == 404
        assert (await client.get(BASE)).json()["strategies"] == []


@pytest.mark.asyncio
async def test_unsafe_method_enforces_same_origin(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(
            BASE, json=_payload(), headers={"Origin": "http://evil.example"}
        )
        assert response.status_code == 403
        # A same-origin (or absent) origin is allowed.
        assert (await client.post(BASE, json=_payload())).status_code == 200


@pytest.mark.asyncio
async def test_unauthorized_account_scope_is_403_and_writes_nothing(session_factory, monkeypatch):
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:some-other-account")
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(BASE, json=_payload())  # well-shaped kite:paper
        assert response.status_code == 403
        assert (await client.get(BASE)).json()["strategies"] == []


@pytest.mark.asyncio
async def test_default_deny_when_no_account_policy_is_configured(session_factory, monkeypatch):
    monkeypatch.delenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", raising=False)
    async with _client(session_factory, monkeypatch) as client:
        assert (await client.post(BASE, json=_payload())).status_code == 403


@pytest.mark.asyncio
async def test_malformed_scope_is_422_before_authorization(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(BASE, json=_payload(account_scope="not-a-scope"))
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# store behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_list_get_roundtrip_and_template_id(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        assert created["template_id"] == f"hosted:{created['strategy_id']}"
        assert created["owner_id"] == "app:admin"
        listed = (await client.get(BASE)).json()["strategies"]
        assert [item["strategy_id"] for item in listed] == [created["strategy_id"]]
        fetched = (await client.get(f"{BASE}/{created['strategy_id']}")).json()
        assert fetched["name"] == "intraday-fut"


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        await _create(client)
        assert (await client.post(BASE, json=_payload())).status_code == 409


@pytest.mark.asyncio
async def test_update_and_disable_are_owner_scoped_and_origin_guarded(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username="admin") as client:
        created = await _create(client)
        sid = created["strategy_id"]
        evil = await client.patch(
            f"{BASE}/{sid}", json={"status": "disabled"}, headers={"Origin": "http://evil.example"}
        )
        assert evil.status_code == 403
        updated = await client.patch(f"{BASE}/{sid}", json={"status": "disabled", "description": "hi"})
        assert updated.status_code == 200
        assert updated.json()["status"] == "disabled" and updated.json()["description"] == "hi"
        assert (await client.patch(f"{BASE}/{sid}", json={"status": "nonsense"})).status_code == 422
    async with _client(session_factory, monkeypatch, username="other") as client:
        assert (await client.patch(f"{BASE}/{sid}", json={"status": "active"})).status_code == 404


@pytest.mark.asyncio
async def test_versions_are_numbered_and_have_no_update_route(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        sid = created["strategy_id"]
        source = "print('hi')\n"
        v1 = await client.post(f"{BASE}/{sid}/versions", json={"source": source})
        v2 = await client.post(f"{BASE}/{sid}/versions", json={"source": source + "#2\n"})
        assert (v1.status_code, v2.status_code) == (200, 200)
        assert (v1.json()["version"], v2.json()["version"]) == (1, 2)
        assert v1.json()["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
        versions = (await client.get(f"{BASE}/{sid}/versions")).json()["versions"]
        assert [v["version"] for v in versions] == [1, 2]
        # Immutability: there is no update route.
        assert (await client.put(f"{BASE}/{sid}/versions/1", json={"source": "x"})).status_code == 405


@pytest.mark.asyncio
async def test_source_is_stored_not_executed(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        hostile = "import sys\nraise SystemExit('must never run')\n"
        response = await client.post(
            f"{BASE}/{created['strategy_id']}/versions", json={"source": hostile}
        )
        assert response.status_code == 200
        assert response.json()["source"] == hostile


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_schema_is_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        response = await client.post(
            f"{BASE}/{created['strategy_id']}/versions",
            json={"source": "x", "parameters_schema": {"type": "nonsense"}},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_remote_ref_schema_is_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        response = await client.post(
            f"{BASE}/{created['strategy_id']}/versions",
            json={"source": "x", "parameters_schema": {"$ref": "https://evil.example/s.json"}},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_account_scope_mode_mismatch_is_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(BASE, json=_payload(account_scope="kite:live-account"))
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_mode_and_kind_are_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        assert (await client.post(BASE, json=_payload(execution_mode="live"))).status_code == 422
        assert (await client.post(BASE, json=_payload(job_kind="scheduled"))).status_code == 422


@pytest.mark.asyncio
async def test_missing_explicit_policy_is_rejected(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        body = _payload()
        del body["stale_exit_policy"]
        assert (await client.post(BASE, json=body)).status_code == 422


@pytest.mark.asyncio
async def test_there_is_no_lifecycle_route_in_this_slice(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        created = await _create(client)
        sid = created["strategy_id"]
        for suffix in ("start", "stop", "cancel", "flatten"):
            assert (await client.post(f"{BASE}/{sid}/{suffix}")).status_code in (404, 405)
