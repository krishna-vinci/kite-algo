"""The shipped starter's first read, through the real API and child bootstrap.

The stub-context test proves the example's logic; this proves the contract it
actually depends on. The exact source the composer pastes is written to a file
and run through ``kite_algo_worker.hosted.run_child`` - the same entry point the
supervisor launches - with:

* a real HTTP loopback server (uvicorn) mounting the real worker market router;
* a child credential minted by the real ``hosted_lifecycle.prepare_launch``;
* the real ``KiteAlgoWorkerClient`` inside the child;
* a faked market-data provider (one deterministic quote).

So "paste this, press run, read an index ticker" is exercised end to end minus
the broker, and the assertion is on what the *provider* was asked for.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import worker_execution as worker_execution_router  # noqa: E402
from backend.api.routers import worker_market as worker_market_router  # noqa: E402
from backend.api.routers import worker_auth as worker_auth_router  # noqa: E402
from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
    make_request,
)

STARTER_TS = "frontend-next/features/strategies/lib/starter.ts"
MARKER = "export const HOSTED_STARTER_SOURCE = `"
OWNER = "app:admin"


def starter_source() -> str:
    with open(STARTER_TS, encoding="utf-8") as handle:
        text = handle.read()
    return text[text.index(MARKER) + len(MARKER) : text.rindex("`;")]


class FakeMarketDataService:
    """The market boundary: one deterministic quote, recorded per request."""

    def __init__(self) -> None:
        self.requests: list = []

    async def resolve_many(self, *, symbols, instrument_tokens):
        return {"instruments": [], "missing": []}

    async def get_quotes(self, payload):
        self.requests.append(list(payload.symbols))
        return {
            "quotes": [
                {"symbol": symbol, "ltp": 24_850.25, "mode": str(payload.mode)}
                for symbol in payload.symbols
            ],
            "missing": [],
        }


def _build_app():
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
        name=f"starter-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="none",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source=starter_source(),
        source_sha256="d" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={
            "schema_version": 2,
            "capabilities": {"data": True, "trade": False, "notify": False},
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
    claimed = repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert claimed is not None

    app = FastAPI()
    app.state.strategies_session_factory = factory
    app.state.algo_worker_repository = worker
    app.state.journal_service = StubJournalService()
    app.state.worker_market_data_service = FakeMarketDataService()
    # The SDK's client prefix is `/api/algo-workers`, which is how the real app
    # mounts these routers.
    app.include_router(worker_market_router.router, prefix="/api")
    app.include_router(worker_auth_router.router, prefix="/api")
    app.include_router(worker_execution_router.router, prefix="/api")

    config = asyncio.run(
        hosted_lifecycle.prepare_launch(
            make_request(app),
            strategy_repo=repo,
            worker_repo=worker,
            job_id=job.id,
            lease_owner="sup-A",
            lease_epoch=1,
            attempt=1,
        )
    )
    return app, config


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LoopbackServer(threading.Thread):
    def __init__(self, app, port: int) -> None:
        super().__init__(daemon=True)
        import uvicorn

        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.port = port

    def run(self) -> None:  # pragma: no cover - thread body
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def test_shipped_starter_reads_an_index_ticker_through_the_real_api(tmp_path, monkeypatch):
    app, config = _build_app()
    port = _free_port()
    server = _LoopbackServer(app, port)
    server.start()
    deadline = time.time() + 30
    while time.time() < deadline and not server.server.started:
        time.sleep(0.1)
    assert server.server.started, "loopback API did not start"

    source_path = tmp_path / "starter_strategy.py"
    source_path.write_text(starter_source(), encoding="utf-8")

    # The child environment is exactly what the supervisor hands a child.
    monkeypatch.setenv("KITE_ALGO_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("KITE_ALGO_WORKER_TOKEN", str(config["worker_token"]))
    monkeypatch.setenv("KITE_ALGO_RUN_ID", str(config["run_id"]))
    monkeypatch.setenv("KITE_ALGO_SESSION_NONCE", str(config["session_nonce"]))
    monkeypatch.setenv("KITE_ALGO_TEMPLATE_ID", str(config["template_id"]))
    monkeypatch.setenv("KITE_ALGO_ACCOUNT_SCOPE", str(config["account_scope"]))
    monkeypatch.setenv("KITE_ALGO_MODE", str(config["execution_mode"]))
    monkeypatch.setenv("KITE_ALGO_PARAMS", "{}")
    monkeypatch.setenv("KITE_ALGO_SCRATCH", str(tmp_path))

    try:
        from kite_algo_worker.hosted import run_child

        exit_code = run_child(str(source_path))
    finally:
        server.stop()
        server.join(timeout=10)

    # The child's own exit code, then the provider's view of the read.
    assert exit_code == 0
    assert app.state.worker_market_data_service.requests == [["NSE:NIFTY 50"]]
