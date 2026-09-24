"""Operator schedule API: owner/origin scoping, next/last occurrence, disable.

The stored schedule is what the runtime drives, so these tests pin the operator
contract to the scheduler's own semantics: the returned next occurrence is in
the future, missed runs come from the durable occurrence table, and re-enabling
a schedule cannot silently resume a disabled strategy.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

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
from backend.strategies import models  # noqa: E402,F401 (table registration)
from backend.strategies.attribution_models import StrategyScheduleOccurrence  # noqa: E402
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


def _client(session_factory, monkeypatch, username="admin"):
    from backend.app import auth as auth_module

    user = AppUser(username=username, role="admin") if username else None
    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _strategy_with_version(client, name="scheduled-strategy"):
    created = await client.post(
        BASE,
        json={
            "name": name,
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": "kite:paper",
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "exit_on_worker_stale",
        },
    )
    assert created.status_code == 200, created.text
    strategy = created.json()
    version = await client.post(
        f"{BASE}/{strategy['strategy_id']}/versions",
        json={"source": "def main(ctx):\n    return 0\n"},
    )
    assert version.status_code == 200, version.text
    return strategy, version.json()


def _schedule_body(version_id: str, **overrides):
    body = {
        "version_id": version_id,
        "execution_mode": "paper",
        "job_kind": "finite",
        "params": {},
        "schedule_kind": "daily",
        "at_time": "09:30",
        "timezone": "Asia/Kolkata",
        "enabled": True,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_schedule_routes_require_a_session(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        assert (await client.get(f"{BASE}/s-1/schedule")).status_code == 401
        assert (
            await client.put(f"{BASE}/s-1/schedule", json=_schedule_body("v-1"))
        ).status_code == 401


@pytest.mark.asyncio
async def test_schedule_put_enforces_same_origin(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        response = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(version["version_id"]),
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert (await client.get(f"{BASE}/{sid}/schedule")).json() is None


@pytest.mark.asyncio
async def test_schedule_is_null_until_configured_then_round_trips(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        assert (await client.get(f"{BASE}/{sid}/schedule")).json() is None

        created = await client.put(
            f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"])
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["strategy_id"] == sid
        assert body["version_number"] == 1
        assert body["account_scope"] == "kite:paper"
        assert body["enabled"] is True
        assert body["schedule_kind"] == "daily"
        assert body["timezone"] == "Asia/Kolkata"
        # The runtime's own policy, reported rather than described.
        assert body["overlap_policy"] == "defer_until_resolved"
        assert body["misfire_grace_seconds"] > 0
        # No occurrence has been materialised yet.
        assert body["last_occurrence"] is None
        assert body["next_occurrence_at"] is not None
        assert body["next_occurrence_key"].startswith(body["schedule_id"])
        # Its own identity, not the strategy's.
        assert body["schedule_id"] != sid

        fetched = (await client.get(f"{BASE}/{sid}/schedule")).json()
        assert fetched["schedule_id"] == body["schedule_id"]


@pytest.mark.asyncio
async def test_schedule_edit_keeps_the_same_row_and_repins_the_version(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        first = (
            await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))
        ).json()

        second_version = await client.post(
            f"{BASE}/{sid}/versions", json={"source": "def main(ctx):\n    return 1\n"}
        )
        edited = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(
                second_version.json()["version_id"],
                schedule_kind="weekly",
                weekday=2,
                at_time="15:45",
            ),
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["schedule_id"] == first["schedule_id"]
        assert body["version_id"] == second_version.json()["version_id"]
        assert body["version_number"] == 2
        assert (body["schedule_kind"], body["weekday"], body["at_time"]) == ("weekly", 2, "15:45")
        assert body["next_occurrence_key"].startswith(first["schedule_id"])


@pytest.mark.asyncio
async def test_schedule_validation_refuses_kind_mismatches_and_foreign_versions(
    session_factory, monkeypatch
):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client, name="a")
        _other, other_version = await _strategy_with_version(client, name="b")
        sid = strategy["strategy_id"]

        weekly_without_weekday = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(version["version_id"], schedule_kind="weekly"),
        )
        assert weekly_without_weekday.status_code == 422

        bad_clock = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(version["version_id"], at_time="session_close"),
        )
        assert bad_clock.status_code == 422

        foreign_version = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(other_version["version_id"]),
        )
        assert foreign_version.status_code == 422
        # Nothing was stored by any refused attempt.
        assert (await client.get(f"{BASE}/{sid}/schedule")).json() is None


@pytest.mark.asyncio
async def test_schedule_disable_and_reenable_respect_the_strategy_state(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))

        disabled = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False

        await client.patch(f"{BASE}/{sid}", json={"status": "disabled"})
        refused = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": True})
        assert refused.status_code == 409
        assert refused.json()["detail"] == "STRATEGY_DISABLED"

        await client.patch(f"{BASE}/{sid}", json={"status": "active"})
        resumed = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": True})
        assert resumed.status_code == 200
        assert resumed.json()["enabled"] is True


@pytest.mark.asyncio
async def test_enabling_a_live_schedule_rechecks_the_deployment_flag(session_factory, monkeypatch):
    """A live schedule cannot be switched on after hosted live is turned off."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:paper,kite:live")
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    async with _client(session_factory, monkeypatch) as client:
        created = await client.post(
            BASE,
            json={
                "name": "live-scheduled",
                "execution_mode": "live",
                "job_kind": "finite",
                "account_scope": "kite:live",
                "max_duration_s": 21600,
                "progress_deadline_s": 600,
                "stale_exit_policy": "none",
            },
        )
        assert created.status_code == 200, created.text
        sid = created.json()["strategy_id"]
        version = await client.post(
            f"{BASE}/{sid}/versions", json={"source": "def main(ctx):\n    return 0\n"}
        )
        stored = await client.put(
            f"{BASE}/{sid}/schedule",
            json=_schedule_body(version.json()["version_id"], execution_mode="live"),
        )
        assert stored.status_code == 200, stored.text
        disabled = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": False})
        assert disabled.status_code == 200

    # The deployment stops offering live: enabling the stored live schedule must
    # refuse exactly like a launch, not quietly arm it.
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "false")
    async with _client(session_factory, monkeypatch) as client:
        refused = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": True})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["rejection_reason"] == "LIVE_DISABLED"
        assert (await client.get(f"{BASE}/{sid}/schedule")).json()["enabled"] is False


@pytest.mark.asyncio
async def test_enabling_a_schedule_rechecks_account_authorization(session_factory, monkeypatch):
    """An account removed from the allowlist cannot be re-armed by a schedule."""
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))
        disabled = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": False})
        assert disabled.status_code == 200

    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:something-else")
    async with _client(session_factory, monkeypatch) as client:
        refused = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": True})
        assert refused.status_code in (403, 422), refused.text
        assert (await client.get(f"{BASE}/{sid}/schedule")).json()["enabled"] is False


@pytest.mark.asyncio
async def test_disable_stays_possible_when_the_schedule_is_no_longer_authorized(
    session_factory, monkeypatch
):
    """Turning work OFF must never be blocked by an authorization drift."""
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))

    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:something-else")
    async with _client(session_factory, monkeypatch) as client:
        disabled = await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False


@pytest.mark.asyncio
async def test_schedule_is_owner_scoped_and_occurrences_404_without_one(
    session_factory, monkeypatch
):
    async with _client(session_factory, monkeypatch, username="admin") as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))
        assert (await client.get(f"{BASE}/{sid}/schedule/occurrences")).status_code == 200

    async with _client(session_factory, monkeypatch, username="other") as client:
        assert (await client.get(f"{BASE}/{sid}/schedule")).status_code == 404
        assert (await client.get(f"{BASE}/{sid}/schedule/occurrences")).status_code == 404
        assert (
            await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))
        ).status_code == 404
        assert (
            await client.post(f"{BASE}/{sid}/schedule/enabled", json={"enabled": False})
        ).status_code == 404


@pytest.mark.asyncio
async def test_last_occurrence_reports_the_schedulers_own_row(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch) as client:
        strategy, version = await _strategy_with_version(client)
        sid = strategy["strategy_id"]
        stored = (
            await client.put(f"{BASE}/{sid}/schedule", json=_schedule_body(version["version_id"]))
        ).json()

        with session_factory() as session:
            session.add(
                StrategyScheduleOccurrence(
                    id="occ-missed-1",
                    schedule_id=stored["schedule_id"],
                    strategy_id=sid,
                    occurrence_key=f"{stored['schedule_id']}:2026-09-20",
                    due_at=datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc),
                    status="skipped",
                    skip_reason="MISFIRE_GRACE_EXCEEDED",
                    detail={"reason_code": "MISFIRE_GRACE_EXCEEDED"},
                )
            )
            session.commit()

        body = (await client.get(f"{BASE}/{sid}/schedule")).json()
        assert body["last_occurrence"]["status"] == "skipped"
        assert body["last_occurrence"]["skip_reason"] == "MISFIRE_GRACE_EXCEEDED"

        rows = (await client.get(f"{BASE}/{sid}/schedule/occurrences")).json()
        assert [row["status"] for row in rows] == ["skipped"]
