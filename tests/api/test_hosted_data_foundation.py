"""Phase-1 hosted data foundation: capability lens, hosted read authority, routes.

Pins the concrete Phase-1 contract of
``documents/hosted-strategies-usable-platform-plan-2026-09-23.md`` against the
real code path, not a re-implementation:

- ``capability_actions`` composition: ``data`` grants ``market:read``,
  ``market:stream`` and the dedicated ``universes:read``/``universes:resolve``
  lens; ``data=false`` grants none of them; ``trade`` adds the paper order
  actions and ``funds:read``; ``heartbeat`` and ``workflows:*`` are never granted.
- The child credential is minted by the **real**
  ``hosted_lifecycle.prepare_launch`` path (the same call the supervisor makes)
  and the read routes are exercised over HTTP with that token, so the
  token/attempt binding is proved rather than assumed. Provider boundaries
  (market data, options chain, paper funds) are faked; auth is never faked.
- A hosted read is refused once the persisted attempt stops being live
  (fenced / stopped / expired lease / revoked token), while external tokens keep
  their established behaviour.
- Universe reads for a hosted child are scoped to the **strategy's application
  owner** and can never create a universe definition.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import worker_market as worker_market_router  # noqa: E402
from backend.api.routers import worker_universes as worker_universes_router  # noqa: E402
from backend.api.services import hosted_lifecycle  # noqa: E402
from backend.options.api.market_router import get_options_session_manager  # noqa: E402
from backend.options.api.worker_options_router import router as worker_options_router  # noqa: E402
from backend.strategies import models  # noqa: F401,E402
from backend.strategies import service as strategy_service  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from backend.workflows.universes import UniverseService  # noqa: E402
from tests.support.hosted_fakes import (  # noqa: E402
    FakeWorkerRepository,
    StubJournalService,
    make_request,
)

OWNER = "app:admin"
OTHER_OWNER = "app:other"
MARKET = "/algo-workers/worker/market"
INDICATORS = "/algo-workers/worker/indicators"
FUNDS = "/algo-workers/worker/funds"
OPTIONS = "/api/algo-workers/worker/options"
UNIVERSES = "/api/worker/universes"


def test_data_capability_grants_market_and_universe_reads():
    data_only = set(
        strategy_service.capability_actions({"data": True, "trade": False, "notify": False})
    )
    assert {"market:read", "market:stream", "universes:read", "universes:resolve"} <= data_only
    assert {"runs:read", "runs:log", "runs:progress"} <= data_only
    assert not data_only & {
        "heartbeat",
        "workflows:read",
        "workflows:write",
        "funds:read",
        "intents:submit",
    }


def test_data_false_grants_no_read_lens():
    actions = set(
        strategy_service.capability_actions({"data": False, "trade": False, "notify": False})
    )
    assert actions == {"runs:read", "runs:log", "runs:progress"}


def test_trade_adds_funds_and_order_actions_only():
    actions = set(
        strategy_service.capability_actions({"data": False, "trade": True, "notify": False})
    )
    assert {
        "funds:read",
        "intents:submit",
        "runs:exit",
        "risk:update",
        "proposals:submit",
    } <= actions
    assert "heartbeat" not in actions
    assert not actions & {"gtt:write", "workflows:write", "runs:create", "signals:admin"}


def test_notify_stays_separate_and_lifecycle_actions_are_always_refused():
    actions = set(
        strategy_service.capability_actions({"data": True, "trade": True, "notify": True})
    )
    assert "notifications:publish" in actions
    assert "heartbeat" not in actions
    with pytest.raises(strategy_service.StrategyValidationError):
        strategy_service.validate_child_token_actions(["heartbeat"])
    with pytest.raises(strategy_service.StrategyValidationError):
        strategy_service.validate_child_token_actions(["workflows:write"])
    with pytest.raises(strategy_service.StrategyValidationError):
        strategy_service.child_run_token_actions(extra=["broker:admin"])


class FakeMarketDataService:
    """Canned market reads; records what the route asked for."""

    def __init__(self) -> None:
        self.quotes: list = []
        self.candles: list = []

    async def resolve_ticker(self, symbol: str):
        return {"symbol": symbol}

    async def search_tickers(self, query: str, *, exchange=None, limit: int = 20):
        return {"results": []}

    async def resolve_many(self, *, symbols, instrument_tokens):
        return {"instruments": [], "missing": []}

    async def get_quotes(self, payload):
        self.quotes.append(list(payload.symbols))
        return {
            "quotes": [
                {"symbol": symbol, "ltp": 25000.0, "mode": str(payload.mode)}
                for symbol in payload.symbols
            ],
            "missing": [],
        }

    async def get_candles(
        self, *, symbol=None, instrument_token=None, interval="5minute", lookback=50
    ):
        self.candles.append(symbol)
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": [{"timestamp": "2026-09-23T09:15:00+05:30", "close": 25000.0}],
        }

    async def get_historical_candles(self, **_kwargs):
        return {"candles": [], "timeframe": "day"}

    async def get_market_snapshot(self, payload):
        return {"quotes": []}


class FakePaperRuntime:
    async def get_account_summary(self, account_scope: str):
        return {
            "available_funds": 100000.0,
            "blocked_funds": 0.0,
            "realized_pnl": 0.0,
            "starting_balance": 100000.0,
            "currency": "INR",
            "updated_at": "2026-09-23T09:15:00+05:30",
        }


class FakeOptionsManager:
    """Minimal stand-in for the options session manager (chain reads only)."""

    def __init__(self) -> None:
        self.instrument_repo = SimpleNamespace(
            normalize_underlying_symbol=lambda value: (value.strip().upper(), None)
        )
        self._snapshot = {
            "underlying": "NIFTY",
            "spot_ltp": 25000.0,
            "updated_at": "2026-09-23T09:15:00+05:30",
            "expiries": ["2026-09-29"],
            "per_expiry": {"2026-09-29": {"atm_strike": 25000, "rows": []}},
        }

    def normalize_underlying_symbol(self, value: str) -> str:
        return value.strip().upper()

    def get_snapshot(self, _underlying: str):
        return self._snapshot


class FakeCatalog:
    """Catalog identity stand-in: public key -> descriptor."""

    def __init__(self, public_keys=(), generation="generation-1"):
        self._keys = {str(key).upper() for key in public_keys}
        self.generation = generation

    def resolve_public_key(self, key):
        from backend.broker_api.instruments.catalog import InstrumentNotFoundError

        if str(key).upper() not in self._keys:
            raise InstrumentNotFoundError(f"instrument not found: {key}")
        return SimpleNamespace(
            public_key=str(key).upper(),
            lifecycle_status="active",
            catalog_generation=self.generation,
        )

    def health(self):
        return {"status": "published", "generation": self.generation}


class FakeExternalTokenRepo:
    """Auth-only repository for a non-hosted (external) worker token."""

    def __init__(self, token, *, raw_token: str) -> None:
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        from backend.shared.serialization import _hash_token

        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


class Harness:
    def __init__(self, *, factory, worker, app, repo, job, config, engine):
        self.factory = factory
        self.worker = worker
        self.app = app
        self.repo = repo
        self.job = job
        self.config = config
        self.engine = engine

    @property
    def child_token(self) -> str:
        return str(self.config["worker_token"])

    @property
    def run_id(self) -> str:
        return str(self.config["run_id"])

    @property
    def token_id(self) -> str:
        persisted = self.repo.get_job(self.job.owner_id, self.job.id)
        assert persisted is not None
        return str(persisted.token_id)

    def headers(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.child_token}"}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    def persisted_token(self) -> dict:
        return self.worker.tokens[self.token_id]

    def set_job(self, **columns) -> None:
        assignments = ", ".join(f"{key} = :{key}" for key in columns)
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE strategy_jobs SET {assignments} WHERE id = :job_id"),
                {**columns, "job_id": self.job.id},
            )


def _build_harness(*, data: bool = True, trade: bool = False, owner: str = OWNER) -> Harness:
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
        owner_id=owner,
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
        source_sha256="c" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={
            "schema_version": 2,
            "capabilities": {"trade": trade, "notify": False, "data": data},
        },
        created_by=owner,
    )
    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=owner,
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
    app.state.paper_runtime_service = FakePaperRuntime()
    app.state.alerts_session_factory = factory
    app.state.universe_service = UniverseService(
        factory, catalog=FakeCatalog(["NSE:RELIANCE", "NSE:TCS"])
    )
    app.include_router(worker_market_router.router)
    app.include_router(worker_options_router)
    app.include_router(worker_universes_router.router, prefix="/api")
    app.dependency_overrides[get_options_session_manager] = lambda: FakeOptionsManager()

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
        worker=worker,
        app=app,
        repo=repo,
        job=job,
        config=config,
        engine=engine,
    )


@pytest.fixture()
def harness():
    return _build_harness(data=True, trade=True)


def _seed_universe(harness: Harness, name: str, owner: str = OWNER) -> None:
    harness.app.state.universe_service.create_universe(
        owner, name, "explicit", {"members": ["NSE:RELIANCE"]}
    )


def _run(coro):
    return asyncio.run(coro)


def test_issued_child_token_carries_the_data_lens(harness):
    record = harness.persisted_token()
    assert record["metadata"]["source"] == "hosted_supervisor"
    actions = set(record["allowed_actions"])
    assert {"market:read", "market:stream", "universes:read", "universes:resolve"} <= actions
    assert "funds:read" in actions  # trade=True fixture
    assert "heartbeat" not in actions
    assert record["allowed_templates"] == [f"hosted:{harness.job.strategy_id}"]


@pytest.mark.asyncio
async def test_hosted_quote_and_index_ticker_reads(harness):
    async with harness.client() as client:
        response = await client.post(
            f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=harness.headers()
        )
        assert response.status_code == 200, response.text
        assert response.json()["quotes"][0]["symbol"] == "NSE:NIFTY 50"
        assert harness.app.state.worker_market_data_service.quotes == [["NSE:NIFTY 50"]]


@pytest.mark.asyncio
async def test_hosted_candle_read(harness):
    async with harness.client() as client:
        response = await client.get(
            f"{MARKET}/candles",
            params={"symbol": "NSE:NIFTY 50", "interval": "5minute", "lookback": 3},
            headers=harness.headers(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["symbol"] == "NSE:NIFTY 50"


@pytest.mark.asyncio
async def test_hosted_server_indicator_read(harness):
    bars = [
        {
            "timestamp": f"2026-09-23T09:{15 + index:02d}:00+05:30",
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 10.0 + index,
        }
        for index in range(5)
    ]
    async with harness.client() as client:
        response = await client.post(
            INDICATORS, json={"name": "sma", "period": 3, "bars": bars}, headers=harness.headers()
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "sma"


@pytest.mark.asyncio
async def test_hosted_options_chain_and_expiry_reads(harness):
    async with harness.client() as client:
        chain = await client.get(f"{OPTIONS}/underlyings/NIFTY/chain", headers=harness.headers())
        assert chain.status_code == 200, chain.text
        expiries = await client.get(
            f"{OPTIONS}/underlyings/NIFTY/expiries", headers=harness.headers()
        )
        assert expiries.status_code == 200, expiries.text
        greeks = await client.get(f"{OPTIONS}/underlyings/NIFTY/greeks", headers=harness.headers())
        assert greeks.status_code == 200, greeks.text
        assert "contracts" in greeks.json()


def test_data_false_hosted_child_cannot_read_option_chain_or_greeks():
    """The option reads are token-only for external callers, not for hosted ones.

    A lifecycle-issued ``data=false`` child holds no ``market:read``, so the
    chain/Greeks reads must be refused on the hosted path even though the
    external contract has no action on these routes.
    """
    case = _build_harness(data=False, trade=True)
    assert "market:read" not in set(case.persisted_token()["allowed_actions"])

    async def _check():
        async with case.client() as client:
            for path in (
                f"{OPTIONS}/underlyings/NIFTY/chain",
                f"{OPTIONS}/underlyings/NIFTY/greeks",
                f"{OPTIONS}/underlyings/NIFTY/expiries",
                f"{OPTIONS}/underlyings/NIFTY/mini-chain",
                f"{OPTIONS}/underlyings/NIFTY/analytics/pcr",
                f"{OPTIONS}/underlyings/NIFTY/analytics/max-pain",
            ):
                response = await client.get(path, headers=case.headers())
                assert response.status_code == 403, (path, response.text)
                detail = response.json()["detail"]
                assert detail["rejection_reason"] == "HOSTED_OPERATION_NOT_PERMITTED", path
                assert detail["required_action"] == "market:read", path

            selection = await client.post(
                f"{OPTIONS}/underlyings/NIFTY/selection/resolve",
                json={"expiry": "2026-09-29", "legs": [{"action": "buy", "option_type": "CE", "strike": 25000}]},
                headers=case.headers(),
            )
            assert selection.status_code == 403, selection.text

    _run(_check())


def test_data_true_hosted_child_keeps_the_option_reads():
    """The same reads are allowed once the child holds the data lens."""
    case = _build_harness(data=True, trade=True)
    assert "market:read" in set(case.persisted_token()["allowed_actions"])

    async def _check():
        async with case.client() as client:
            chain = await client.get(
                f"{OPTIONS}/underlyings/NIFTY/chain", headers=case.headers()
            )
            assert chain.status_code == 200, chain.text
            greeks = await client.get(
                f"{OPTIONS}/underlyings/NIFTY/greeks", headers=case.headers()
            )
            assert greeks.status_code == 200, greeks.text

    _run(_check())


@pytest.mark.asyncio
async def test_hosted_funds_read_for_own_trade_enabled_account(harness):
    async with harness.client() as client:
        response = await client.get(FUNDS, params={"mode": "paper"}, headers=harness.headers())
        assert response.status_code == 200, response.text
        assert response.json()["account_scope"] == "kite:paper"


@pytest.mark.asyncio
async def test_hosted_universe_list_read_and_resolve_own_universe(harness):
    _seed_universe(harness, "nifty-core")
    async with harness.client() as client:
        listed = await client.get(UNIVERSES, headers=harness.headers())
        assert listed.status_code == 200, listed.text
        assert [item["name"] for item in listed.json()["universes"]] == ["nifty-core"]

        detail = await client.get(f"{UNIVERSES}/nifty-core", headers=harness.headers())
        assert detail.status_code == 200, detail.text

        resolved = await client.post(f"{UNIVERSES}/nifty-core/resolve", headers=harness.headers())
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["revision"] == 1


def test_data_false_hosted_child_cannot_read_market_or_universes():
    case = _build_harness(data=False, trade=True)
    _seed_universe(case, "nifty-core")

    async def _check():
        async with case.client() as client:
            quote = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert quote.status_code == 403, quote.text
            assert "market:read" in quote.text
            listed = await client.get(UNIVERSES, headers=case.headers())
            assert listed.status_code == 403, listed.text

    _run(_check())


def test_trade_false_hosted_child_cannot_read_funds():
    case = _build_harness(data=True, trade=False)

    async def _check():
        async with case.client() as client:
            response = await client.get(FUNDS, params={"mode": "paper"}, headers=case.headers())
            assert response.status_code == 403, response.text
            assert "funds:read" in response.text

    _run(_check())


def _assert_read_refused(case: Harness, status_code: int, reason: str) -> None:
    async def _check():
        async with case.client() as client:
            response = await client.post(
                f"{MARKET}/quotes", json={"symbols": ["NSE:NIFTY 50"]}, headers=case.headers()
            )
            assert response.status_code == status_code, response.text
            assert reason in response.text

    _run(_check())


def test_fenced_attempt_cannot_read(harness):
    harness.set_job(status="recovery_required")
    _assert_read_refused(harness, 409, "HOSTED_ATTEMPT_FENCED")


def test_stopped_attempt_cannot_read(harness):
    harness.set_job(desired_state="stopped")
    _assert_read_refused(harness, 409, "HOSTED_ATTEMPT_STOPPED")


def test_expired_lease_cannot_read(harness):
    harness.set_job(lease_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    _assert_read_refused(harness, 409, "HOSTED_LEASE_EXPIRED")


def test_hosted_shaped_token_without_persisted_attempt_cannot_read(harness):
    """A token that *looks* hosted but has no attempt row fails closed."""
    from backend.shared.serialization import _hash_token

    harness.worker.tokens["worker_orphan"] = {
        "token_id": "worker_orphan",
        "name": "orphan",
        "account_scope": "kite:paper",
        "allowed_modes": ["paper"],
        "allowed_actions": ["runs:read", "market:read", "universes:read"],
        "allowed_templates": [f"hosted:{harness.job.strategy_id}"],
        "status": "active",
        "expires_at": None,
        "metadata": {"source": "hosted_supervisor"},
    }
    harness.worker.hashes[_hash_token("kwa_orphan")] = "worker_orphan"

    async def _check():
        async with harness.client() as client:
            response = await client.post(
                f"{MARKET}/quotes",
                json={"symbols": ["NSE:NIFTY 50"]},
                headers={"Authorization": "Bearer kwa_orphan"},
            )
            assert response.status_code == 403, response.text
            assert "HOSTED_ATTEMPT_UNKNOWN" in response.text

    _run(_check())


def test_revoked_child_token_cannot_read(harness):
    _run(harness.worker.revoke_token(harness.token_id))

    async def _check():
        async with harness.client() as client:
            response = await client.get(
                f"{MARKET}/candles",
                params={"symbol": "NSE:NIFTY 50"},
                headers=harness.headers(),
            )
            assert response.status_code == 401, response.text

    _run(_check())


def test_hosted_child_cannot_read_another_owners_universe(harness):
    _seed_universe(harness, "other-core", owner=OTHER_OWNER)

    async def _check():
        async with harness.client() as client:
            listed = await client.get(UNIVERSES, headers=harness.headers())
            assert listed.status_code == 200, listed.text
            assert listed.json()["universes"] == []

            detail = await client.get(f"{UNIVERSES}/other-core", headers=harness.headers())
            assert detail.status_code == 404, detail.text

            resolved = await client.post(
                f"{UNIVERSES}/other-core/resolve", headers=harness.headers()
            )
            assert resolved.status_code == 404, resolved.text

    _run(_check())


def test_hosted_child_cannot_create_a_universe_definition(harness):
    async def _check():
        async with harness.client() as client:
            response = await client.post(
                UNIVERSES,
                json={
                    "name": "new-core",
                    "kind": "explicit",
                    "source_config": {"members": ["NSE:TCS"]},
                },
                headers=harness.headers(),
            )
            assert response.status_code == 403, response.text
            assert "HOSTED_UNIVERSE_DEFINITION_FORBIDDEN" in response.text

    _run(_check())


def test_hosted_lens_never_carries_workflow_definition_authority(harness):
    actions = set(harness.persisted_token()["allowed_actions"])
    assert not {action for action in actions if action.startswith("workflows:")}


def test_hosted_owner_helper_derives_the_app_owner_not_the_account(harness):
    """Owner identity comes from the persisted strategy, never the account scope."""
    from backend.api.services.hosted_attempt import hosted_owner_for_token
    from backend.shared.serialization import _hash_token

    async def _check():
        token = await harness.worker.get_token_by_hash(_hash_token(harness.child_token))
        assert token is not None
        assert token.account_scope == "kite:paper"
        owner = await hosted_owner_for_token(make_request(harness.app), token)
        assert owner == OWNER

    _run(_check())


def test_external_universe_path_still_uses_workflows_actions():
    """An external token with workflows:read lists universes exactly as before."""
    from backend.api.repositories.algo_worker_repo import WorkerToken

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = UniverseService(factory, catalog=FakeCatalog(["NSE:RELIANCE"]))
    service.create_universe(OWNER, "nifty-core", "explicit", {"members": ["NSE:RELIANCE"]})

    token = WorkerToken(
        token_id="worker_external",
        name="external",
        account_scope=OWNER,
        allowed_modes=["paper"],
        allowed_actions=["workflows:read"],
        allowed_templates=[],
    )
    repo = FakeExternalTokenRepo(token, raw_token="kwa_external")

    app = FastAPI()
    app.state.algo_worker_repository = repo
    app.state.universe_service = service
    app.include_router(worker_universes_router.router, prefix="/api")
    app.dependency_overrides[worker_universes_router._universes_db] = lambda: factory

    async def _check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            listed = await client.get(UNIVERSES, headers={"Authorization": "Bearer kwa_external"})
            assert listed.status_code == 200, listed.text
            assert [item["name"] for item in listed.json()["universes"]] == ["nifty-core"]

            created = await client.post(
                UNIVERSES,
                json={
                    "name": "x",
                    "kind": "explicit",
                    "source_config": {"members": ["NSE:RELIANCE"]},
                },
                headers={"Authorization": "Bearer kwa_external"},
            )
            assert created.status_code == 403, created.text

    _run(_check())


# ---------------------------------------------------------------------------
# streaming authority: a stream must not outlive its hosted attempt
# ---------------------------------------------------------------------------


class FakeStreamPubSub:
    """Pub/sub double that never delivers a message (idle stream)."""

    def __init__(self, delay: float = 0.01) -> None:
        self._delay = delay
        self.unsubscribed = False

    async def subscribe(self, channel):
        return None

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(self._delay)
        return None

    async def unsubscribe(self, channel):
        self.unsubscribed = True

    async def aclose(self):
        return None


class FakeStreamRedis:
    def __init__(self, pubsub: FakeStreamPubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self):
        return self._pubsub


class FakeMarketRuntime:
    def __init__(self, redis) -> None:
        self.redis = redis
        self.owners: list = []

    async def set_owner_subscriptions(self, owner_id, subscriptions):
        self.owners.append(owner_id)

    async def delete_owner(self, owner_id):
        return None

    async def get_tick(self, instrument_token):
        return None


def _install_streaming_service(harness: Harness, *, candle_reader=None) -> FakeStreamPubSub:
    """Real WorkerMarketDataService with fake provider boundaries (not fake auth)."""
    from backend.api.services.market_data import WorkerMarketDataService

    pubsub = FakeStreamPubSub()
    redis = FakeStreamRedis(pubsub)
    service = WorkerMarketDataService(
        market_data_runtime=FakeMarketRuntime(redis),
        redis=redis,
        candle_reader=candle_reader,
    )
    instrument = {"symbol": "NSE:NIFTY 50", "instrument_token": 256265}

    async def fake_resolve_many(*, symbols, instrument_tokens):
        return {"instruments": [instrument], "missing": []}

    async def fake_resolve_ticker(symbol):
        return instrument

    async def fake_get_quotes(payload):
        return {"quotes": [{"symbol": instrument["symbol"], "ltp": 25000.0}], "missing": []}

    async def fake_get_candles(*, symbol=None, instrument_token=None, interval="5minute", lookback=50):
        return {"symbol": instrument["symbol"], "interval": interval, "candles": []}

    service.resolve_many = fake_resolve_many
    service.resolve_ticker = fake_resolve_ticker
    service.get_quotes = fake_get_quotes
    service.get_candles = fake_get_candles
    harness.app.state.worker_market_data_service = service
    return pubsub


async def _stream_until_closed(harness: Harness, url: str, params: dict, *, fence_after: float = 0.6):
    """Consume an SSE stream while the attempt is fenced mid-flight."""

    async def _consume() -> str:
        async with harness.client() as client:
            async with client.stream("GET", url, params=params, headers=harness.headers()) as response:
                assert response.status_code == 200, await response.aread()
                chunks = []
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")

    task = asyncio.create_task(_consume())
    await asyncio.sleep(fence_after)
    harness.set_job(status="recovery_required")
    return await asyncio.wait_for(task, timeout=30)


def test_hosted_tick_stream_stops_when_the_attempt_is_fenced(harness, monkeypatch):
    from backend.api.services import market_data as market_data_module

    monkeypatch.setattr(market_data_module, "HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS", 0.25)
    _install_streaming_service(harness)
    body = _run(
        _stream_until_closed(
            harness, f"{MARKET}/ticks/stream", {"symbols": "NSE:NIFTY 50", "mode": "quote"}
        )
    )
    assert "stream_closed" in body, body[-500:]
    assert "HOSTED_ATTEMPT_FENCED" in body, body[-500:]


def test_hosted_candle_stream_stops_when_the_attempt_is_fenced(harness, monkeypatch):
    from backend.api.services import market_data as market_data_module

    monkeypatch.setattr(market_data_module, "HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS", 0.25)
    _install_streaming_service(harness)
    body = _run(
        _stream_until_closed(
            harness,
            f"{MARKET}/candles/stream",
            {"symbol": "NSE:NIFTY 50", "interval": "5minute"},
        )
    )
    assert "stream_closed" in body, body[-500:]
    assert "HOSTED_ATTEMPT_FENCED" in body, body[-500:]


def test_stream_authority_is_inert_for_external_tokens(harness):
    """External tokens are never revalidated: their behaviour is unchanged."""
    from backend.api.repositories.algo_worker_repo import WorkerToken
    from backend.api.services.market_data import _HostedStreamAuthority

    external = WorkerToken(
        token_id="worker_external",
        name="external",
        account_scope="kite:paper",
        allowed_modes=["paper"],
        allowed_actions=["market:read", "market:stream"],
        allowed_templates=[],
    )
    authority = _HostedStreamAuthority(make_request(harness.app), external, interval=0.01)
    for _ in range(5):
        assert _run(authority.stop_reason()) is None


def test_hosted_websocket_market_stream_refused_when_attempt_is_fenced(harness):
    """The WS market routes refuse a dead hosted attempt before accepting.

    The tick/candle WebSocket routes share the SSE generators (so they inherit
    the periodic revalidation), and they now also check the hosted attempt at
    connection time, before the socket is accepted.
    """
    from backend.api.routers.worker_protection import worker_candles_ws, worker_ticks_ws

    accepted: list = []

    class FakeWebSocket:
        def __init__(self) -> None:
            self.app = harness.app
            self.query_params = {"token": harness.child_token}

        async def accept(self):
            accepted.append(True)

        async def close(self, code: int = 1000, reason=None):
            return None

        async def send_json(self, payload):
            return None

    harness.set_job(status="recovery_required")

    for route in (worker_ticks_ws, worker_candles_ws):
        with pytest.raises(Exception) as excinfo:
            _run(route(FakeWebSocket()))
        # The hosted guard raises the same HTTPException shape the worker action
        # check uses on these routes; Starlette turns it into a handshake refusal.
        assert getattr(excinfo.value, "status_code", None) == 409, route.__name__
        assert "HOSTED_ATTEMPT_FENCED" in str(excinfo.value.detail), route.__name__
    assert accepted == []


class FakeIdleCandleReader:
    """A candle reader that never produces a payload until it is closed."""

    def __init__(self, hold: float = 60.0) -> None:
        self.hold = hold
        self.started = False
        self.closed = False

    async def stream_candles(self, instrument_token, interval):
        self.started = True
        try:
            await asyncio.sleep(self.hold)
            yield {"instrument_token": instrument_token, "interval": interval}
        finally:
            self.closed = True


async def _stream_until_closed_with(harness: Harness, url: str, params: dict, mutate, *, after: float = 0.6):
    """Consume an SSE stream, applying ``mutate`` mid-flight (in-flight mutation)."""

    async def _consume() -> str:
        async with harness.client() as client:
            async with client.stream("GET", url, params=params, headers=harness.headers()) as response:
                assert response.status_code == 200, await response.aread()
                chunks = []
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")

    task = asyncio.create_task(_consume())
    await asyncio.sleep(after)
    outcome = mutate()
    if inspect.isawaitable(outcome):
        await outcome
    return await asyncio.wait_for(task, timeout=30)


def test_hosted_stream_stops_when_only_the_token_is_revoked(harness, monkeypatch):
    """Revoking the credential alone must end a live stream.

    ``revoke_token`` does not fence the job row, so the job keeps its live
    status, ``desired_state`` and a lease well inside the hour. The stream must
    still close, on the credential check rather than on the attempt check.
    """
    from backend.api.services import market_data as market_data_module

    monkeypatch.setattr(market_data_module, "HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS", 0.25)
    _install_streaming_service(harness)

    body = _run(
        _stream_until_closed_with(
            harness,
            f"{MARKET}/ticks/stream",
            {"symbols": "NSE:NIFTY 50", "mode": "quote"},
            lambda: harness.worker.revoke_token(harness.token_id),
        )
    )

    job = harness.repo.get_job(harness.job.owner_id, harness.job.id)
    assert job is not None
    assert job.desired_state == "started"
    assert str(job.status) in {"starting", "running"}, job.status
    assert job.lease_until is not None
    assert harness.worker.tokens[harness.token_id]["status"] == "revoked"

    assert "stream_closed" in body, body[-500:]
    assert "WORKER_TOKEN_REVOKED" in body, body[-500:]


def test_idle_hosted_candle_reader_stream_closes_on_revocation(harness, monkeypatch):
    """An idle reader cannot hold a revoked hosted stream open, and is cleaned up."""
    from backend.api.services import market_data as market_data_module

    monkeypatch.setattr(market_data_module, "HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS", 0.25)
    reader = FakeIdleCandleReader(hold=60.0)
    _install_streaming_service(harness, candle_reader=reader)

    async def _run_case():
        before = len(asyncio.all_tasks())
        body = await _stream_until_closed_with(
            harness,
            f"{MARKET}/candles/stream",
            {"symbol": "NSE:NIFTY 50", "interval": "5minute"},
            lambda: harness.worker.revoke_token(harness.token_id),
        )
        # Give any (wrongly) surviving task a chance to appear.
        await asyncio.sleep(0.2)
        return body, before, len(asyncio.all_tasks())

    body, before, after = _run(_run_case())

    assert reader.started is True
    assert "stream_closed" in body, body[-500:]
    assert "WORKER_TOKEN_REVOKED" in body, body[-500:]
    # The in-flight read was cancelled and the reader generator finalized:
    # nothing is left running.
    assert reader.closed is True
    assert after <= before, (before, after)


def test_idle_hosted_candle_reader_stream_closes_on_fenced_attempt(harness, monkeypatch):
    """The idle-reader bound also applies to a fenced attempt (not just tokens)."""
    from backend.api.services import market_data as market_data_module

    monkeypatch.setattr(market_data_module, "HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS", 0.25)
    reader = FakeIdleCandleReader(hold=60.0)
    _install_streaming_service(harness, candle_reader=reader)

    body = _run(
        _stream_until_closed_with(
            harness,
            f"{MARKET}/candles/stream",
            {"symbol": "NSE:NIFTY 50", "interval": "5minute"},
            lambda: harness.set_job(status="recovery_required"),
        )
    )
    assert "stream_closed" in body, body[-500:]
    assert "HOSTED_ATTEMPT_FENCED" in body, body[-500:]
    assert reader.closed is True


def test_external_candle_reader_stream_is_unchanged(harness):
    """An external token keeps the straight pass-through reader path."""
    from backend.api.repositories.algo_worker_repo import WorkerToken
    from backend.api.services.market_data import WorkerMarketDataService

    class TaggedRequest:
        def __init__(self, app):
            self.app = app

        async def is_disconnected(self):
            return False

    class ExternalCandleReader:
        def __init__(self):
            self.closed = False

        async def stream_candles(self, instrument_token, interval):
            try:
                for _ in range(2):
                    yield {"instrument_token": instrument_token, "close": 1.0}
            finally:
                self.closed = True

    external = WorkerToken(
        token_id="worker_external",
        name="external",
        account_scope="kite:paper",
        allowed_modes=["paper"],
        allowed_actions=["market:read", "market:stream"],
        allowed_templates=[],
    )
    reader = ExternalCandleReader()
    service = WorkerMarketDataService(candle_reader=reader)

    async def fake_resolve_ticker(symbol):
        return {"symbol": "NSE:NIFTY 50", "instrument_token": 256265}

    async def fake_get_candles(**kwargs):
        return {"symbol": "NSE:NIFTY 50", "candles": []}

    service.resolve_ticker = fake_resolve_ticker
    service.get_candles = fake_get_candles

    async def _collect():
        events = []
        async for event in service.stream_candles(
            TaggedRequest(harness.app), symbol="NSE:NIFTY 50", token=external
        ):
            events.append(event)
        return events

    events = _run(_collect())
    assert sum(1 for event in events if event.startswith("event: candle")) == 2
    assert reader.closed is True
    assert all("stream_closed" not in event for event in events)
