"""Operator first-run readiness route and the runner profile in /options.

The route is cookie-authenticated like the rest of the hosted-strategy operator
surface, enforces the same-origin assertion for the POST, writes nothing, and
never imports or executes the submitted source.
"""

from __future__ import annotations

import os
from pathlib import Path

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
from backend.app.auth import AppUser  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/strategies"
READINESS = f"{BASE}/readiness"


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


def _client(session_factory, monkeypatch, username="admin"):
    from backend.app import auth as auth_module

    user = AppUser(username=username, role="admin") if username else None
    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_readiness_requires_a_session(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        response = await client.post(READINESS, json={"source": "def main(ctx):\n    return 1\n"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_ready_source_reports_profile_and_entrypoint(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(
            READINESS, json={"source": "import pandas as pd\n\n\ndef main(ctx):\n    return 1\n"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["schema_version"] == 1
        assert body["profile"]["id"] == "hosted-python-dataframe-indicators"
        assert body["profile"]["runtime_pip_install"] is False
        assert body["entrypoint"] == {
            "found": True,
            "compatible": True,
            "name": "main",
            "is_async": False,
            "detail": body["entrypoint"]["detail"],
            "remediation": None,
        }
        assert body["imports"]["available"] == ["pandas"]


@pytest.mark.asyncio
async def test_missing_dependency_is_blocked_with_an_actionable_message(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(
            READINESS, json={"source": "import scipy\n\n\ndef main(ctx):\n    return 1\n"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "blocked"
        assert body["imports"]["missing"] == ["scipy"]
        checks = {check["id"]: check for check in body["checks"]}
        assert checks["imports"]["status"] == "blocked"
        assert "scipy" in checks["imports"]["detail"]


@pytest.mark.asyncio
async def test_missing_entrypoint_is_blocked(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(READINESS, json={"source": "print('hello')\n"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "blocked"
        assert body["entrypoint"]["found"] is False


@pytest.mark.asyncio
async def test_source_is_never_imported_or_executed(session_factory, monkeypatch, tmp_path):
    marker = Path(tmp_path) / "should-not-exist.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "\n"
        "\n"
        "def main(ctx):\n"
        "    return 1\n"
    )
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(READINESS, json={"source": source})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cross_origin_post_is_refused(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(
            READINESS,
            json={"source": "def main(ctx):\n    return 1\n"},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_empty_or_invalid_body_is_422(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        empty = await client.post(READINESS, json={"source": ""})
        assert empty.status_code == 422
        unknown = await client.post(READINESS, json={"source": "x", "owner_id": "app:other"})
        assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_options_exposes_the_runner_profile(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        response = await client.get(f"{BASE}/options")
        assert response.status_code == 200, response.text
        profile = response.json()["runner_profile"]
        assert profile["id"] == "hosted-python-dataframe-indicators"
        import_names = {entry["import_name"] for entry in profile["packages"]}
        assert {"pandas", "numpy", "numba"} <= import_names
