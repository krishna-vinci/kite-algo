"""Persisted hosted read authority on PostgreSQL.

The SQLite suite (``tests/api/test_hosted_data_foundation.py``) proves the
permission composition and the route behaviour. This module proves the part
that only real PostgreSQL can: a child credential issued by the **real**
``hosted_lifecycle.prepare_launch`` path is authorised on read routes from the
persisted ``strategy_jobs``/token ledger (token -> job lookup by token id,
``desired_state``, attempt status, lease expiry), and is refused when that
ledger says the attempt is fenced or stopped.

It creates a DISPOSABLE, uniquely named database at the configured port, runs
``alembic upgrade head`` and drops it afterwards:

    HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_hosted_data_foundation_postgres.py -q

Run in its own pytest invocation (other suites stub ``psycopg2``). Skipped when
no URL is configured. Never points at the production database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg2  # real psycopg2 BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.api.routers import worker_market as worker_market_router  # noqa: E402
from backend.api.routers import worker_universes as worker_universes_router  # noqa: E402
from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.universes import UniverseService  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
    make_request,
)

PG_URL = os.environ.get("HOSTED_FOUNDATION_PG_URL") or os.environ.get(
    "ALERTS_TEST_DATABASE_URL", ""
)

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this PostgreSQL suite in its own "
        "pytest invocation",
        allow_module_level=True,
    )

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="no disposable PostgreSQL URL set; hosted read-authority PostgreSQL suite skipped",
)

OWNER = "app:admin"
OTHER_OWNER = "app:other"
MARKET = "/algo-workers/worker/market"
UNIVERSES = "/api/worker/universes"


def _parts():
    return urlsplit(PG_URL)


def _connect(dbname):
    parts = _parts()
    return psycopg2.connect(
        host=parts.hostname,
        port=parts.port or 5432,
        user=parts.username,
        password=parts.password,
        dbname=dbname,
    )


def _sqlalchemy_url(dbname):
    parts = _parts()
    return (
        f"postgresql+psycopg2://{parts.username}:{parts.password}"
        f"@{parts.hostname}:{parts.port or 5432}/{dbname}"
    )


def _admin_dbname():
    return _parts().path.lstrip("/") or "postgres"


@pytest.fixture(scope="module")
def temp_db():
    admin = _admin_dbname()
    name = f"hosted_read_{uuid.uuid4().hex[:10]}"
    conn = _connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()

    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _sqlalchemy_url(name)
    cfg = Config("backend/alembic.ini")
    try:
        command.upgrade(cfg, "head")
        yield _sqlalchemy_url(name), name
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
        conn = _connect(admin)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.close()


@pytest.fixture()
def env(temp_db):
    url, _name = temp_db
    engine = create_engine(url, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, engine
    engine.dispose()


class FakeMarketDataService:
    def __init__(self) -> None:
        self.quotes: list = []

    async def resolve_ticker(self, symbol: str):
        return {"symbol": symbol}

    async def search_tickers(self, query: str, *, exchange=None, limit: int = 20):
        return {"results": []}

    async def resolve_many(self, *, symbols, instrument_tokens):
        return {"instruments": [], "missing": []}

    async def get_quotes(self, payload):
        self.quotes.append(list(payload.symbols))
        return {
            "quotes": [{"symbol": symbol, "ltp": 25000.0} for symbol in payload.symbols],
            "missing": [],
        }

    async def get_candles(self, **kwargs):
        return {"symbol": kwargs.get("symbol"), "candles": []}

    async def get_historical_candles(self, **_kwargs):
        return {"candles": [], "timeframe": "day"}

    async def get_market_snapshot(self, payload):
        return {"quotes": []}


class FakeCatalog:
    #: The universe tables store ``source_generation`` as a UUID column, so the
    #: stand-in catalog generation must be a real UUID string.
    GENERATION = "11111111-2222-3333-4444-555555555555"

    def __init__(self, public_keys=()):
        self._keys = {str(key).upper() for key in public_keys}

    def resolve_public_key(self, key):
        from backend.broker_api.instruments.catalog import InstrumentNotFoundError

        if str(key).upper() not in self._keys:
            raise InstrumentNotFoundError(f"instrument not found: {key}")
        return SimpleNamespace(
            public_key=str(key).upper(),
            lifecycle_status="active",
            catalog_generation=self.GENERATION,
        )

    def health(self):
        return {"status": "published", "generation": self.GENERATION}


class Harness:
    def __init__(self, *, factory, engine, worker, app, repo, job, config):
        self.factory = factory
        self.engine = engine
        self.worker = worker
        self.app = app
        self.repo = repo
        self.job = job
        self.config = config

    @property
    def child_token(self) -> str:
        return str(self.config["worker_token"])

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.child_token}"}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    def set_job(self, **columns) -> None:
        assignments = ", ".join(f"{key} = :{key}" for key in columns)
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE strategy_jobs SET {assignments} WHERE id = :job_id"),
                {**columns, "job_id": self.job.id},
            )


def _build_harness(factory, engine, *, data: bool = True, trade: bool = True) -> Harness:
    repo = SqlAlchemyStrategyRepository(factory)
    worker = FakeWorkerRepository()

    strategy = repo.create_strategy(
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
    version = repo.create_version(
        strategy_id=strategy.id,
        source="def main(ctx):\n    return 0\n",
        source_sha256="d" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={
            "schema_version": 2,
            "capabilities": {"trade": trade, "notify": False, "data": data},
        },
        created_by=OWNER,
    )
    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={},
    )
    assert repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    app = FastAPI()
    app.state.strategies_session_factory = factory
    app.state.algo_worker_repository = worker
    app.state.journal_service = StubJournalService()
    app.state.worker_market_data_service = FakeMarketDataService()
    app.state.alerts_session_factory = factory
    app.state.universe_service = UniverseService(
        factory, catalog=FakeCatalog(["NSE:RELIANCE"])
    )
    app.include_router(worker_market_router.router)
    app.include_router(worker_universes_router.router, prefix="/api")

    config = asyncio.run(
        hosted_lifecycle.prepare_launch(
            make_request(app),
            strategy_repo=SqlAlchemyStrategyRepository(factory),
            worker_repo=worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=1,
            attempt=1,
        )
    )
    return Harness(
        factory=factory,
        engine=engine,
        worker=worker,
        app=app,
        repo=repo,
        job=job,
        config=config,
    )


def test_issued_child_token_reads_market_and_own_universe_pg(env):
    factory, engine = env
    case = _build_harness(factory, engine)
    case.app.state.universe_service.create_universe(
        OWNER, "nifty-core", "explicit", {"members": ["NSE:RELIANCE"]}
    )

    async def _run():
        async with case.client() as client:
            quote = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert quote.status_code == 200, quote.text
            assert quote.json()["quotes"][0]["symbol"] == "NSE:NIFTY 50"

            candles = await client.get(
                f"{MARKET}/candles",
                params={"symbol": "NSE:NIFTY 50", "interval": "5minute", "lookback": 2},
                headers=case.headers(),
            )
            assert candles.status_code == 200, candles.text

            listed = await client.get(UNIVERSES, headers=case.headers())
            assert listed.status_code == 200, listed.text
            assert [item["name"] for item in listed.json()["universes"]] == ["nifty-core"]

            resolved = await client.post(
                f"{UNIVERSES}/nifty-core/resolve", headers=case.headers()
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["revision"] == 1

    asyncio.run(_run())

    persisted = case.repo.get_job(OWNER, case.job.id)
    assert persisted is not None
    assert persisted.token_id in case.worker.tokens
    assert persisted.run_id == case.config["run_id"]


def test_fenced_attempt_reads_are_refused_pg(env):
    factory, engine = env
    case = _build_harness(factory, engine)
    assert case.repo.mark_recovery_required(
        case.job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )

    async def _run():
        async with case.client() as client:
            quote = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert quote.status_code == 409, quote.text
            assert "HOSTED_ATTEMPT_FENCED" in quote.text

            listed = await client.get(UNIVERSES, headers=case.headers())
            assert listed.status_code == 409, listed.text

    asyncio.run(_run())


def test_stopped_and_expired_attempts_refuse_reads_pg(env):
    factory, engine = env
    stopped = _build_harness(factory, engine)
    stopped.set_job(desired_state="stopped")

    expired = _build_harness(factory, engine)
    expired.set_job(lease_until=datetime.now(timezone.utc) - timedelta(minutes=2))

    async def _check(case, status_code, reason):
        async with case.client() as client:
            response = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert response.status_code == status_code, response.text
            assert reason in response.text

    async def _run():
        await _check(stopped, 409, "HOSTED_ATTEMPT_STOPPED")
        await _check(expired, 409, "HOSTED_LEASE_EXPIRED")

    asyncio.run(_run())


def test_data_false_child_is_refused_pg(env):
    factory, engine = env
    case = _build_harness(factory, engine, data=False, trade=True)

    async def _run():
        async with case.client() as client:
            quote = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert quote.status_code == 403, quote.text
            assert "market:read" in quote.text

            listed = await client.get(UNIVERSES, headers=case.headers())
            assert listed.status_code == 403, listed.text

    asyncio.run(_run())


def test_cross_owner_universe_is_invisible_pg(env):
    factory, engine = env
    case = _build_harness(factory, engine)
    case.app.state.universe_service.create_universe(
        OTHER_OWNER, "other-core", "explicit", {"members": ["NSE:RELIANCE"]}
    )

    async def _run():
        async with case.client() as client:
            detail = await client.get(f"{UNIVERSES}/other-core", headers=case.headers())
            assert detail.status_code == 404, detail.text

            created = await client.post(
                UNIVERSES,
                json={
                    "name": "new-core",
                    "kind": "explicit",
                    "source_config": {"members": ["NSE:RELIANCE"]},
                },
                headers=case.headers(),
            )
            assert created.status_code == 403, created.text
            assert "HOSTED_UNIVERSE_DEFINITION_FORBIDDEN" in created.text

    asyncio.run(_run())
