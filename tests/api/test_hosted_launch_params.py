"""What a launch actually sends: the operator's values, and nothing else.

The composer builds the first-run parameters from the pinned version's own
schema and sends exactly what the operator entered. Two contracts make that
work through the real API:

* a version whose schema is strict (``additionalProperties: false``) and has no
  properties at all launches with ``params={}`` - the platform does not stamp
  identity keys such as ``strategy_id`` into user parameters, and it does not
  require a hidden input to launch;
* the values a naive form would drop (``0``, ``false``, an enum member) are
  stored exactly as sent, and a stamped platform key is refused by the strict
  schema instead of being silently accepted.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.strategies import models  # noqa: E402,F401 (table registration)
from backend.strategies.models import StrategyJob  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

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


def _client(session_factory, monkeypatch):
    from backend.app import auth as auth_module

    user = AppUser(username="admin", role="admin")
    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _strategy_with_version(client, schema):
    created = await client.post(
        BASE,
        json={
            "name": f"launch-params-{uuid.uuid4().hex[:8]}",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": "kite:paper",
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "none",
        },
    )
    assert created.status_code == 200, created.text
    strategy = created.json()
    version = await client.post(
        f"{BASE}/{strategy['strategy_id']}/versions",
        json={
            "source": "def main(ctx):\n    return 0\n",
            "parameters_schema": schema,
        },
    )
    assert version.status_code == 200, version.text
    return strategy, version.json()


async def _launch(client, strategy_id, version_id, params, key):
    return await client.post(
        f"{BASE}/{strategy_id}/jobs",
        json={"version_id": version_id, "params": params, "idempotency_key": key},
    )


def _stored_params(session_factory, job_id):
    session = session_factory()
    try:
        row = session.execute(select(StrategyJob).where(StrategyJob.id == job_id)).scalar_one()
        return dict(row.params_snapshot or {})
    finally:
        session.close()


STRICT_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
STRICT_VALUES = {
    "type": "object",
    "properties": {
        "quantity": {"type": "integer", "minimum": 0},
        "enabled": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["intraday", "positional"]},
    },
    "required": ["quantity"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_parameterless_strict_version_launches_with_no_hidden_parameters(
    session_factory, monkeypatch
):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client, STRICT_EMPTY)
        response = await _launch(
            client,
            strategy["strategy_id"],
            version["version_id"],
            {},
            "launch-no-params",
        )
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        assert _stored_params(session_factory, job["job_id"]) == {}
        # The queued job is not a running process, and the response says so.
        assert job["status"] == "queued"


@pytest.mark.asyncio
async def test_false_zero_and_enum_values_reach_the_job_unchanged(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client, STRICT_VALUES)
        response = await _launch(
            client,
            strategy["strategy_id"],
            version["version_id"],
            {"quantity": 0, "enabled": False, "mode": "positional"},
            "launch-values",
        )
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        assert _stored_params(session_factory, job["job_id"]) == {
            "quantity": 0,
            "enabled": False,
            "mode": "positional",
        }


@pytest.mark.asyncio
async def test_a_platform_key_stamped_into_params_is_refused(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client, STRICT_EMPTY)
        response = await _launch(
            client,
            strategy["strategy_id"],
            version["version_id"],
            {"strategy_id": strategy["strategy_id"]},
            "launch-stamped",
        )
        # This is why the UI never injects identity into the operator's values.
        assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_required_value_with_no_default_must_be_supplied(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client, STRICT_VALUES)
        missing = await _launch(
            client, strategy["strategy_id"], version["version_id"], {}, "launch-required"
        )
        assert missing.status_code == 422, missing.text
        supplied = await _launch(
            client,
            strategy["strategy_id"],
            version["version_id"],
            {"quantity": 1},
            "launch-required-supplied",
        )
        assert supplied.status_code == 200, supplied.text
