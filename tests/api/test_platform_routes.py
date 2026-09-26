"""Phase 2 UX platform routes: live settings, platform status, approvals inbox.

SQLite, no broker, no Redis, no PostgreSQL - the same bounded ASGI pattern the
hosted-strategy API suite uses (``httpx.ASGITransport`` + a minimal app built
from ``ALL_ROUTERS``, so the real route order is what is exercised).

Pinned properties:

- every route is app-session only, and the owner is server-derived;
- ``GET/PUT /api/platform/live-settings`` round-trips, writes the singleton row
  AND its audit row, reports which source answered, masks the account, and never
  lets a caller set the read-only ``live_enabled`` master switch;
- the live lane gate reads the DB row OVER the env allowlist (and still denies by
  default);
- ``GET /api/platform/status`` reports ``unknown``/``down`` where there is no
  evidence instead of guessing;
- ``GET /api/strategies/approvals/pending`` lists only ``awaiting_approval``
  requests belonging to the caller, and the literal path is registered before
  the hosted-strategy router so no ``/{strategy_id}`` pattern can shadow it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, delete, event, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import ALL_ROUTERS, platform as platform_router_module  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.broker_api.session.kite_session import KiteSession  # noqa: E402,F401
from backend.platform import settings as platform_settings  # noqa: E402
from backend.platform import status as platform_status  # noqa: E402
from backend.platform.models import (  # noqa: E402
    PlatformLiveSetting,
    PlatformLiveSettingAudit,
)
from backend.strategies.attribution_models import StrategyPlan  # noqa: E402
from backend.strategies.models import (  # noqa: E402
    HostedExecutionRequest,
    HostedStrategy,
    StrategyJob,
)
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/platform"
PENDING = "/api/strategies/approvals/pending"
OWNER = "app:admin"

NOW = datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        # The reconciled broker book the account day-P&L reader sums. It lives on
        # the ``public.``-qualified Core schema, so it is created explicitly.
        cursor.execute(
            "CREATE TABLE public.account_positions ("
            " account_id TEXT, instrument_token INTEGER, product TEXT, exchange TEXT,"
            " tradingsymbol TEXT, net_quantity INTEGER DEFAULT 0, realized_pnl REAL DEFAULT 0,"
            " last_price REAL, average_price REAL)"
        )
        dbapi_connection.commit()

    Base.metadata.create_all(engine)
    # The broker session store lives on a DIFFERENT declarative base
    # (``backend.app.database.Base``), so its table is created explicitly.
    KiteSession.__table__.create(engine, checkfirst=True)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def app(session_factory, monkeypatch):
    from backend.app import auth as auth_module

    monkeypatch.setattr(
        auth_module,
        "get_optional_app_user",
        lambda _request: AppUser(username="admin", role="admin"),
    )
    application = FastAPI()
    for router, prefix in ALL_ROUTERS:
        application.include_router(router, prefix=prefix)
    application.dependency_overrides[platform_router_module._platform_db] = lambda: (
        session_factory
    )
    return application


@pytest_asyncio.fixture()
async def client(app):
    """One open ASGI client per test (a closed httpx client cannot be reopened)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture(autouse=True)
def lane_env(monkeypatch):
    """A deployment whose env default opens MIS only, with live armed."""
    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
    monkeypatch.setenv("HOSTED_LIVE_LANES", "mis")
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", "kite:XJJ12345")


@pytest.fixture()
def gate_session(session_factory, monkeypatch):
    """Point the lane gate at the SQLite database (no real SessionLocal)."""
    monkeypatch.setattr(
        platform_settings, "_session_factory_override", lambda: session_factory
    )


def _session_row(session_factory):
    with session_factory() as session:
        return session.execute(
            select(PlatformLiveSetting).where(PlatformLiveSetting.settings_id == 1)
        ).scalar_one_or_none()


def _audit_rows(session_factory):
    with session_factory() as session:
        return list(
            session.execute(
                select(PlatformLiveSettingAudit).order_by(
                    PlatformLiveSettingAudit.audit_id
                )
            ).scalars()
        )


# ---------------------------------------------------------------------------
# live settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_settings_default_to_env_and_never_expose_the_account(
    client, session_factory, gate_session
):
    response = await client.get(f"{BASE}/live-settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["live_enabled"] is True
    assert body["lanes_source"] == "env"
    assert body["lanes"] == {
        "cnc": False,
        "mis": True,
        "futures": False,
        "options": False,
    }
    assert body["updated_at"] is None
    assert body["updated_by"] is None
    # No broker session is recorded, so the single allowlisted scope is the
    # platform account - and it is masked, never returned verbatim.
    assert body["account"] == {"scope": "kite:XJJ***", "allowed": True}
    assert "XJJ12345" not in response.text
    assert _session_row(session_factory) is None


@pytest.mark.asyncio
async def test_put_persists_the_row_audits_it_and_overrides_the_env(
    client, session_factory, gate_session
):
    from backend.strategies.live_service import enabled_live_lanes, live_lane_enabled

    # Before: the env answer is what gates new exposure.
    assert enabled_live_lanes() == ["mis"]

    response = await client.put(
        f"{BASE}/live-settings",
        json={
            "lanes": {"cnc": True, "mis": False, "futures": False, "options": True},
            "reason": "owner opened cnc + options for the C2 rollout",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lanes"] == {
        "cnc": True,
        "mis": False,
        "futures": False,
        "options": True,
    }
    assert body["lanes_source"] == "db"
    assert body["updated_by"] == OWNER
    assert body["updated_at"] is not None
    # The master switch is read-only and stays where the deployment put it.
    assert body["live_enabled"] is True

    # The lane gate now reads the DB row OVER the env allowlist: MIS is closed
    # even though HOSTED_LIVE_LANES names it.
    assert enabled_live_lanes() == ["cnc", "options"]
    assert live_lane_enabled("cnc") is True
    assert live_lane_enabled("mis") is False

    # A second write replaces the row and appends its own audit row.
    again = await client.put(
        f"{BASE}/live-settings",
        json={
            "lanes": {"cnc": False, "mis": False, "futures": False, "options": False}
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["lanes_source"] == "db"

    row = _session_row(session_factory)
    assert row is not None and row.updated_by == OWNER
    assert row.lanes == {"cnc": False, "mis": False, "futures": False, "options": False}

    audits = _audit_rows(session_factory)
    assert len(audits) == 2
    assert audits[0].actor_id == OWNER
    assert audits[0].reason == "owner opened cnc + options for the C2 rollout"
    assert audits[0].previous_lanes == {}
    assert audits[0].lanes == {
        "cnc": True,
        "mis": False,
        "futures": False,
        "options": True,
    }
    # The second write recorded the previous map, so the trail reads as a change.
    assert audits[1].actor_id == OWNER
    assert audits[1].previous_lanes == audits[0].lanes
    assert audits[1].lanes == row.lanes

    # Default deny survives every write: a closed row opens nothing.
    assert enabled_live_lanes() == []
    assert live_lane_enabled("cnc") is False


@pytest.mark.asyncio
async def test_the_account_daily_loss_cap_round_trips_and_is_audited(
    client, session_factory, gate_session
):
    lanes = {"cnc": False, "mis": True, "futures": False, "options": False}
    opened = await client.put(
        f"{BASE}/live-settings",
        json={
            "lanes": lanes,
            "account_daily_loss_cap_inr": 5000.0,
            "reason": "owner set a 5k account-wide cap",
        },
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["account_daily_loss_cap_inr"] == 5000.0

    # GET reads the cap back, and the write is legible with its author.
    assert (await client.get(f"{BASE}/live-settings")).json()[
        "account_daily_loss_cap_inr"
    ] == 5000.0
    row = _session_row(session_factory)
    assert float(row.account_daily_loss_cap_inr) == 5000.0
    first = _audit_rows(session_factory)[0]
    assert first.previous_account_daily_loss_cap_inr is None
    assert float(first.account_daily_loss_cap_inr) == 5000.0

    # A lanes-only PUT must not silently clear a configured cap...
    preserved = await client.put(
        f"{BASE}/live-settings",
        json={"lanes": {"cnc": True, "mis": False, "futures": False, "options": False}},
    )
    assert preserved.json()["account_daily_loss_cap_inr"] == 5000.0
    second = _audit_rows(session_factory)[1]
    assert float(second.previous_account_daily_loss_cap_inr) == 5000.0
    assert float(second.account_daily_loss_cap_inr) == 5000.0

    # ...while an explicit null clears it.
    cleared = await client.put(
        f"{BASE}/live-settings",
        json={
            "lanes": {"cnc": True, "mis": False, "futures": False, "options": False},
            "account_daily_loss_cap_inr": None,
        },
    )
    assert cleared.json()["account_daily_loss_cap_inr"] is None
    third = _audit_rows(session_factory)[2]
    assert float(third.previous_account_daily_loss_cap_inr) == 5000.0
    assert third.account_daily_loss_cap_inr is None

    # A negative cap is refused before it can be stored.
    negative = await client.put(
        f"{BASE}/live-settings",
        json={"lanes": lanes, "account_daily_loss_cap_inr": -1.0},
    )
    assert negative.status_code == 422, negative.text
    assert len(_audit_rows(session_factory)) == 3


@pytest.mark.asyncio
async def test_a_lane_outside_the_contract_and_a_cross_origin_put_are_refused(
    client, session_factory, gate_session
):
    unknown_lane = await client.put(
        f"{BASE}/live-settings",
        json={"lanes": {"cnc": True, "equities_only": True}, "reason": "typo"},
    )
    cross_origin = await client.put(
        f"{BASE}/live-settings",
        json={"lanes": {"cnc": True, "mis": False, "futures": False, "options": False}},
        headers={"Origin": "https://evil.example"},
    )
    # The master switch is read-only: a body cannot carry it at all.
    switches_the_master = await client.put(
        f"{BASE}/live-settings",
        json={
            "lanes": {"cnc": True, "mis": False, "futures": False, "options": False},
            "live_enabled": False,
        },
    )

    assert unknown_lane.status_code == 422, unknown_lane.text
    assert cross_origin.status_code == 403, cross_origin.text
    assert switches_the_master.status_code == 422, switches_the_master.text
    # Neither refusal wrote anything.
    assert _session_row(session_factory) is None
    assert _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_the_platform_routes_require_an_app_session(session_factory, monkeypatch):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: None)
    application = FastAPI()
    application.include_router(platform_router_module.router, prefix="/api")
    application.dependency_overrides[platform_router_module._platform_db] = lambda: (
        session_factory
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.get(f"{BASE}/live-settings")).status_code == 401
        assert (await anonymous.get(f"{BASE}/status")).status_code == 401
        assert (await anonymous.get(PENDING)).status_code == 401
        assert (
            await anonymous.put(
                f"{BASE}/live-settings", json={"lanes": {}, "reason": "x"}
            )
        ).status_code == 401


# ---------------------------------------------------------------------------
# platform status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_says_unknown_rather_than_guessing(
    client, monkeypatch, gate_session
):
    """No broker record, no published runtime status, no leased job: say so."""
    monkeypatch.delenv("HOSTED_LIVE_LANES", raising=False)

    async def _no_runtime_status():
        return None

    monkeypatch.setattr(platform_status, "redis_market_status", _no_runtime_status)

    # The broker session store and the job table are unreadable, so neither
    # component can be observed at all.
    def _unreadable(session_factory=None):
        raise RuntimeError("no store here")

    monkeypatch.setattr(platform_status, "platform_session_factory", _unreadable)

    response = await client.get(f"{BASE}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "paper"
    assert body["broker"] == {
        "state": "unknown",
        "detail": "broker session store unavailable",
    }
    assert body["market_data"] == {"state": "down", "last_tick_age_s": None}
    assert body["strategy_runner"] == {"state": "unknown", "last_seen_age_s": None}
    assert body["live"] == {"enabled": True, "lanes_open": []}
    # No cap is configured (the settings store is unreadable), so the risk axis
    # is inert rather than invented.
    assert body["risk"] == {"day_pnl_inr": None, "cap_inr": None, "cap_reached": False}


def _leased_job(session, *, lease_until, updated_at) -> None:
    session.add(
        StrategyJob(
            id="job-1",
            strategy_id="strat-1",
            version_id="ver-1",
            owner_id=OWNER,
            account_scope="kite:XJJ12345",
            job_kind="continuous",
            execution_mode="live",
            status="running",
            max_duration_s=21600,
            progress_deadline_s=600,
            lease_owner="supervisor-1",
            lease_epoch=1,
            lease_until=lease_until,
            updated_at=updated_at,
        )
    )


@pytest.mark.asyncio
async def test_status_reports_the_evidence_it_actually_has(
    client, session_factory, monkeypatch, gate_session
):
    from backend.broker_api.session.kite_session import KiteSession

    moment = datetime.now(timezone.utc)
    with session_factory() as session:
        session.add(
            KiteSession(
                session_id="system",
                access_token="system-token",
                broker_user_id="XJJ12345",
                created_at=moment,
            )
        )
        _leased_job(
            session,
            lease_until=moment + timedelta(seconds=120),
            updated_at=moment - timedelta(seconds=12),
        )
        session.commit()

    async def _runtime_status():
        return {
            "status": "healthy",
            "last_tick_at": (moment - timedelta(seconds=1.2)).isoformat(),
        }

    monkeypatch.setattr(platform_status, "redis_market_status", _runtime_status)

    response = await client.get(f"{BASE}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "live"
    assert body["broker"]["state"] == "connected"
    assert "XJJ12345" not in response.text
    assert body["market_data"]["state"] == "ok"
    assert 1.0 <= body["market_data"]["last_tick_age_s"] <= 5.0
    assert body["strategy_runner"]["state"] == "ok"
    assert 10 <= body["strategy_runner"]["last_seen_age_s"] <= 20
    assert body["live"] == {"enabled": True, "lanes_open": ["mis"]}
    assert body["risk"] == {"day_pnl_inr": None, "cap_inr": None, "cap_reached": False}

    # With the session gone the broker is EXPIRED (a fresh login is required),
    # which is a different answer from "unknown".
    with session_factory() as session:
        session.execute(delete(KiteSession))
        session.commit()
    expired = (await client.get(f"{BASE}/status")).json()
    assert expired["broker"]["state"] == "expired"
    assert expired["broker"]["detail"] is not None


@pytest.mark.asyncio
async def test_status_reports_the_account_day_pnl_and_the_cap_it_is_tested_against(
    client, session_factory, gate_session
):
    platform_settings.update_live_settings(
        {"cnc": False, "mis": True, "futures": False, "options": False},
        actor_id=OWNER,
        account_daily_loss_cap_inr=5000.0,
        session_factory=session_factory,
    )
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.account_positions "
                "(account_id, instrument_token, product, exchange, tradingsymbol, "
                " net_quantity, realized_pnl, last_price, average_price) "
                "VALUES ('kite:XJJ12345', 100, 'CNC', 'NSE', 'RELIANCE', 0, -4500.0, 0, 0)"
            )
        )
        session.commit()

    body = (await client.get(f"{BASE}/status")).json()
    assert body["risk"] == {
        "day_pnl_inr": -4500.0,
        "cap_inr": 5000.0,
        "cap_reached": False,
    }

    # The day's loss reaching the cap flips it, without any flatten.
    with session_factory() as session:
        session.execute(
            text("UPDATE public.account_positions SET realized_pnl = -5000.0")
        )
        session.commit()
    reached = (await client.get(f"{BASE}/status")).json()
    assert reached["risk"]["cap_reached"] is True
    assert reached["risk"]["day_pnl_inr"] == -5000.0


@pytest.mark.asyncio
async def test_status_reports_stale_and_unhealthy_components(
    client, session_factory, monkeypatch, gate_session
):
    moment = datetime.now(timezone.utc)
    with session_factory() as session:
        # The lease lapsed: the supervisor stopped renewing it.
        _leased_job(
            session,
            lease_until=moment - timedelta(seconds=30),
            updated_at=moment - timedelta(seconds=300),
        )
        session.commit()

    async def _runtime_status():
        return {"status": "degraded", "last_tick_at": moment.isoformat()}

    monkeypatch.setattr(platform_status, "redis_market_status", _runtime_status)

    response = await client.get(f"{BASE}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    # The runtime's own word is not translated into an "ok" of ours.
    assert body["market_data"]["state"] == "down"
    assert body["strategy_runner"] == {"state": "stale", "last_seen_age_s": 300}


@pytest.mark.asyncio
async def test_status_survives_an_unreachable_runtime(
    client, monkeypatch, gate_session
):
    async def _explodes():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(platform_status, "redis_market_status", _explodes)

    response = await client.get(f"{BASE}/status")

    assert response.status_code == 200, response.text
    assert response.json()["market_data"] == {"state": "down", "last_tick_age_s": None}


# ---------------------------------------------------------------------------
# approvals inbox
# ---------------------------------------------------------------------------


def _strategy(session, *, strategy_id: str, owner: str, name: str) -> None:
    session.add(
        HostedStrategy(
            id=strategy_id,
            owner_id=owner,
            name=name,
            template_id=f"hosted:{strategy_id}",
            default_execution_mode="live",
            default_job_kind="finite",
            default_account_scope="kite:XJJ12345",
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
    )


def _plan(session, *, plan_id: str, strategy_id: str) -> None:
    session.add(
        StrategyPlan(
            plan_id=plan_id,
            proposal_id=f"proposal-{plan_id}",
            strategy_id=strategy_id,
            account_id="kite:XJJ12345",
            plan_kind="single_instrument",
            plan_hash=f"hash-{plan_id}",
            logical_plan={"legs": [{"instrument_id": "NSE:INFY"}]},
            resolved_plan={"legs": [{"instrument_id": "NSE:INFY"}]},
            pinned_catalog_generation="00000000-0000-0000-0000-000000000001",
        )
    )


def _request(
    session,
    *,
    request_id: str,
    owner: str,
    strategy_id: str,
    plan_id: str,
    created_at: datetime,
    status: str = "awaiting_approval",
) -> None:
    session.add(
        HostedExecutionRequest(
            request_id=request_id,
            owner_id=owner,
            strategy_id=strategy_id,
            canonical_strategy_id=f"canonical-{strategy_id}",
            account_id="kite:XJJ12345",
            execution_environment="live",
            strategy_run_id=f"run-{request_id}",
            version_id="ver-1",
            source_sha256="source-hash",
            policy_hash="policy-hash",
            plan_id=plan_id,
            plan_hash=f"hash-{plan_id}",
            authorization_mode="approval_based",
            status=status,
            idempotency_key=f"key-{request_id}",
            request_hash=f"request-hash-{request_id}",
            created_at=created_at,
        )
    )


@pytest.mark.asyncio
async def test_pending_approvals_are_owner_scoped_and_only_awaiting(
    client, session_factory
):
    with session_factory() as session:
        _strategy(session, strategy_id="strat-1", owner=OWNER, name="Intraday CNC")
        _strategy(session, strategy_id="strat-2", owner="app:other", name="Other owner")
        _plan(session, plan_id="plan-old", strategy_id="strat-1")
        _plan(session, plan_id="plan-new", strategy_id="strat-1")
        _plan(session, plan_id="plan-decided", strategy_id="strat-1")
        _plan(session, plan_id="plan-foreign", strategy_id="strat-2")
        _request(
            session,
            request_id="req-old",
            owner=OWNER,
            strategy_id="strat-1",
            plan_id="plan-old",
            created_at=NOW - timedelta(minutes=10),
        )
        _request(
            session,
            request_id="req-new",
            owner=OWNER,
            strategy_id="strat-1",
            plan_id="plan-new",
            created_at=NOW,
        )
        # Already decided: not pending, whatever its outcome.
        _request(
            session,
            request_id="req-decided",
            owner=OWNER,
            strategy_id="strat-1",
            plan_id="plan-decided",
            status="queued",
            created_at=NOW - timedelta(minutes=5),
        )
        # Another owner's pending request is not this owner's inbox.
        _request(
            session,
            request_id="req-foreign",
            owner="app:other",
            strategy_id="strat-2",
            plan_id="plan-foreign",
            created_at=NOW - timedelta(minutes=1),
        )
        session.commit()

    response = await client.get(PENDING)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert [item["request_id"] for item in body["items"]] == ["req-new", "req-old"]
    newest = body["items"][0]
    assert newest["strategy_id"] == "strat-1"
    assert newest["strategy_name"] == "Intraday CNC"
    assert newest["plan_id"] == "plan-new"
    assert newest["environment"] == "live"
    assert newest["summary"] == "single_instrument (1 leg)"
    assert newest["created_at"] is not None
    # No reservation exists yet, so the expiry is unknown rather than invented.
    assert newest["expires_at"] is None


def test_the_pending_route_is_registered_before_the_strategy_id_pattern():
    """The literal path must win over ``/strategies/{strategy_id}`` routes."""
    from backend.api.routers import strategies as strategies_module

    routers = [router for router, _prefix in ALL_ROUTERS]
    assert routers.index(platform_router_module.router) < routers.index(
        strategies_module.router
    )
    # The router is mounted under ``/api``, so its own declared path is relative.
    relative = PENDING.removeprefix("/api")
    pending = [
        route.path
        for route in platform_router_module.router.routes
        if getattr(route, "path", "") == relative
    ]
    assert pending == [relative]
