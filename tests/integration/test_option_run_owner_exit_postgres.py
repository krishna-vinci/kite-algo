"""B2.6b S2: the owner exit of ONE option run, end to end on PostgreSQL.

Proves the parts SQLite cannot: the REAL owner route, the REAL durable option-run
store with its compare-and-set and stage claims, the REAL staged structure exit
and the REAL paper runtime, with the ordinary ``order_trade_fills`` ingestion the
proof is read from.

Two properties:

- the multi-stage flow: the first POST submits the SHORTS ONLY (the hedge is
  withheld), a working stage is WAITING rather than ambiguous, and the ingested
  confirmation of the short close is what admits the hedge release on the next
  POST - never before it;
- two concurrent exit POSTs for one run cannot both submit: the run's transition
  is the ownership token, so exactly one stage and one order exist afterwards.

Disposable database on the local test server (port 15433) only.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

OWNER = "app:admin"
ACCOUNT = "kite:paper-owner-exit"
G1 = "33333333-3333-3333-3333-333333333333"
PRICE = 1500.0
SHORT_SYMBOL = "NIFTY26OCT25000CE"
HEDGE_SYMBOL = "NIFTY26OCT30000CE"
WORKER_RUN = "worker-owner-exit"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_ownerexit_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    base = PG_ADMIN.rpartition("/")[0]
    return name, f"{base}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="module")
def pg():
    import psycopg2  # noqa: F401
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    os.environ["DATABASE_URL"] = dsn
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", "backend/alembic")
    command.upgrade(cfg, "head")

    engine = create_engine(dsn, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:id, 'published', NOW())"
            ),
            {"id": G1},
        )
        session.commit()
    try:
        yield {"factory": factory, "dsn": dsn}
    finally:
        engine.dispose()
        _drop_db(name)


@pytest.fixture(autouse=True)
def _authorized_account_scope(monkeypatch):
    """The hosted surface only serves configured account scopes (production rule)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


class _CatalogInstrument:
    def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
        return {
            "instrument_token": 900001,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "lot_size": 75,
            "instrument_type": "CE",
            "last_price": PRICE,
        }


class _TickRuntime:
    async def get_tick(self, token):
        return {"instrument_token": token, "last_price": PRICE}

    async def get_last_price(self, token):
        return PRICE


class _Env:
    """One strategy, its attribution binding and the entry plan the run names."""

    def __init__(self, factory) -> None:
        from sqlalchemy import text

        from backend.strategies.attribution import SqlAttributionStore
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        self.factory = factory
        self.worker_run = f"worker-owner-exit-{uuid.uuid4().hex[:8]}"
        repo = SqlAlchemyStrategyRepository(factory)
        strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"ownerexit-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=ACCOUNT,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        self.strategy_id = str(strategy.id)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, "
                    " template_id, account_scope, execution_mode, status) "
                    "VALUES (:run, 'tok-owner-exit', 'tpl-owner-exit', :account, "
                    " 'paper', 'open')"
                ),
                {"run": self.worker_run, "account": ACCOUNT},
            )
            session.commit()
        SqlAttributionStore(session_factory=factory).bind_run(
            strategy_run_id=self.worker_run,
            strategy_id=self.strategy_id,
            owner_id=OWNER,
            account_id=ACCOUNT,
            execution_environment="paper",
            bound_by="test",
            binding_source="hosted_job",
        )
        self.entry_plan_id = self._seed_plan()

    def _seed_plan(self) -> str:
        from sqlalchemy import text

        plan_id = str(uuid.uuid4())
        proposal_id = str(uuid.uuid4())
        resolved = {
            "target_kind": "option_structure",
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
            "structure_digest": "digest-owner-exit",
            "protection_policy": {},
            "legs": [
                {"side": "SELL", "broker_symbol": SHORT_SYMBOL, "quantity": 75},
                {"side": "BUY", "broker_symbol": HEDGE_SYMBOL, "quantity": 75},
            ],
            "option_run": {"phase": "entry", "option_run_id": None},
        }
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                    " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                    " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                    " :run, 'option_structure', '{}', :sha, 'validated')"
                ),
                {
                    "pid": proposal_id,
                    "sid": self.strategy_id,
                    "account": ACCOUNT,
                    "eval": f"eval-{plan_id}",
                    "run": self.worker_run,
                    "sha": f"sha-{plan_id}",
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                    " plan_kind, plan_hash, logical_plan, resolved_plan, "
                    " pinned_catalog_generation) "
                    "VALUES (:pid, :prop, :sid, :account, 'option_structure', :hash, '{}', "
                    " :resolved, :gen)"
                ),
                {
                    "pid": plan_id,
                    "prop": proposal_id,
                    "sid": self.strategy_id,
                    "account": ACCOUNT,
                    "hash": f"hash-{plan_id}",
                    "resolved": json.dumps(resolved),
                    "gen": G1,
                },
            )
            session.commit()
        return plan_id

    def seed_run(
        self,
        *,
        status: str,
        trades: list,
        option_run_id: str | None = None,
    ) -> str:
        """The real durable run with its OWN confirmed fills, plus its edge."""
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.models import OptionRunCreateRequest
        from backend.options.execution.plan_binding import PlanOptionRunBindingStore

        store = DurableOptionRunStore(session_factory=self.factory)
        run_id = str(option_run_id or f"opt_run_{uuid.uuid4().hex[:10]}")
        store.create_run(
            OptionRunCreateRequest(
                strategy_run_id=run_id,
                strategy_name=self.strategy_id,
                product="NRML",
                legs=[
                    {
                        "leg_id": "leg_short",
                        "tradingsymbol": SHORT_SYMBOL,
                        "transaction_type": "SELL",
                        "quantity": 75,
                        "exchange": "NFO",
                        "product": "NRML",
                    },
                    {
                        "leg_id": "leg_hedge",
                        "tradingsymbol": HEDGE_SYMBOL,
                        "transaction_type": "BUY",
                        "quantity": 75,
                        "exchange": "NFO",
                        "product": "NRML",
                    },
                ],
                protection={"structure_digest": "digest-owner-exit"},
                metadata={
                    "strategy_id": self.strategy_id,
                    "account_id": ACCOUNT,
                    "execution_environment": "paper",
                    "worker_run_id": self.worker_run,
                    "plan_id": self.entry_plan_id,
                    "source": "hosted_plan_execution",
                },
            )
        )
        run = store.get_run(run_id)
        run.status = status
        run.trades = list(trades)
        store.save_run(run)
        PlanOptionRunBindingStore(session_factory=self.factory).bind(
            plan_id=self.entry_plan_id,
            option_run_id=run_id,
            strategy_id=self.strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase="entry",
            worker_run_id=self.worker_run,
        )
        return run_id


class _BlockingPaperService:
    """A paper runtime that pauses inside the FIRST order (the race window)."""

    def __init__(self, inner, *, entered: threading.Event, release: threading.Event):
        self.inner = inner
        self.entered = entered
        self.release = release
        self.calls = 0

    async def place_order(self, *, account_scope, order_payload, attribution=None):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            done = threading.Event()

            def _wait():
                self.release.wait(timeout=20)
                done.set()

            threading.Thread(target=_wait, daemon=True).start()
            await asyncio.to_thread(done.wait, 20)
        return await self.inner.place_order(
            account_scope=account_scope,
            order_payload=order_payload,
            attribution=attribution,
        )


def _paper_runtime(factory):
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService

    return PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory=factory),
        instruments_repository=_CatalogInstrument(),
        market_data_runtime=_TickRuntime(),
        default_starting_balance=Decimal("1000000"),
    )


def _app(factory, monkeypatch, paper=None):
    from fastapi import FastAPI

    from backend.api.routers import strategies as strategies_module
    from backend.api.routers import strategy_owner_actions as owner_actions_module
    from backend.app import auth as auth_module
    from backend.app.auth import AppUser

    monkeypatch.setattr(
        auth_module,
        "get_optional_app_user",
        lambda _request: AppUser(username="admin", role="admin"),
    )
    app = FastAPI()
    app.include_router(strategies_module.router, prefix="/api")
    app.include_router(owner_actions_module.router, prefix="/api")
    app.dependency_overrides[strategies_module._strategies_db] = lambda: factory
    app.dependency_overrides[owner_actions_module._owner_actions_db] = lambda: factory
    app.state.strategies_session_factory = factory
    app.state.paper_runtime_service = paper or _paper_runtime(factory)
    return app


def _client(factory, monkeypatch, paper=None):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(factory, monkeypatch, paper)),
        base_url="http://test",
    )


def _rows(factory, sql, params=None):
    from sqlalchemy import text

    with factory() as session:
        return [
            dict(row)
            for row in session.execute(text(sql), params or {}).mappings().all()
        ]


def _run_row(factory, option_run_id: str) -> dict:
    (row,) = _rows(
        factory,
        "SELECT status, orders, trades, pending_legs FROM public.option_run_states "
        "WHERE strategy_run_id = :r",
        {"r": option_run_id},
    )
    return row


def _json(value):
    """JSONB comes back decoded on PostgreSQL."""
    return value if isinstance(value, (list, dict)) else json.loads(value)


def _open_trade(leg_id: str, side: str, quantity: int = 75) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT_SYMBOL if leg_id == "leg_short" else HEDGE_SYMBOL,
    }


def _seed_fill(
    factory, *, order_id: str, symbol: str, side: str, quantity: int = 75
) -> None:
    """The ordinary ingestion row for one confirmed broker fill."""
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.order_trade_fills (account_id, trade_id, order_id, "
                " instrument_token, exchange, tradingsymbol, product, transaction_type, "
                " quantity, price, fill_timestamp) VALUES (:account, :trade, :order, "
                " 900001, 'NFO', :symbol, 'NRML', :side, :quantity, 100, NOW())"
            ),
            {
                "account": ACCOUNT,
                "trade": f"trade-{order_id}-{uuid.uuid4().hex[:6]}",
                "order": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
            },
        )
        session.commit()


def _exit_url(strategy_id: str, option_run_id: str) -> str:
    return f"/api/strategies/{strategy_id}/option-runs/{option_run_id}/exit"


def _distinct_stages(row: dict) -> dict:
    """One entry per ``(stage_digest, attempt)``: a stage is claim + resolution."""
    latest: dict = {}
    for stage in _json(row["orders"]):
        if not stage.get("stage_digest"):
            continue
        latest[(str(stage["stage_digest"]), int(stage.get("attempt") or 1))] = stage
    return latest


@pytest.mark.asyncio
async def test_a_multi_stage_owner_exit_releases_the_hedge_only_after_proof(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="entered",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_exit_url(env.strategy_id, option_run_id))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["state"] == "residual"
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in body["close_plan"]
        ] == [(SHORT_SYMBOL, "BUY", 75)]

        with patch("backend.paper_runtime.service.publish_event", autospec=True):
            first = await client.post(
                _exit_url(env.strategy_id, option_run_id),
                json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
            )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["status"] == "accepted", first_body
        assert first_body["run_status"] == "exiting"
        assert {row["tradingsymbol"] for row in first_body["items"]} == {SHORT_SYMBOL}
        assert {row["transaction_type"] for row in first_body["items"]} == {"BUY"}
        short_order_id = first_body["items"][0]["order_id"]
        assert short_order_id

        # The short close is working: WAITING, not ambiguous.
        waiting = await client.get(_exit_url(env.strategy_id, option_run_id))
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["state"] == "residual", waiting.json()
        assert waiting.json()["close_plan"] == []
        assert waiting.json()["waiting_reason"] == "orders_outstanding"

        # A POST while waiting adds nothing releasable and submits no order.
        with patch("backend.paper_runtime.service.publish_event", autospec=True):
            repeat = await client.post(
                _exit_url(env.strategy_id, option_run_id),
                json={
                    "evidence_digest": waiting.json()["evidence_digest"],
                    "reason": "owner_exit",
                },
            )
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["status"] == "accepted"
        assert repeat.json()["items"] == [], repeat.json()["submission"]

        # Ordinary ingestion confirms the short closed. The proven closure is
        # what admits the hedge release - and only that.
        _seed_fill(factory, order_id=short_order_id, symbol=SHORT_SYMBOL, side="BUY")
        ingested = await client.get(_exit_url(env.strategy_id, option_run_id))
        assert ingested.status_code == 200, ingested.text
        assert ingested.json()["state"] == "residual"
        assert ingested.json()["shorts_proven_closed"] is True
        assert ingested.json()["withheld_hedges"] == []
        assert ingested.json()["waiting_reason"] is None
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in ingested.json()["close_plan"]
        ] == [(HEDGE_SYMBOL, "SELL", 75)]

        # The next POST reconciles the ingested fill onto the run and releases
        # exactly the hedge.
        with patch("backend.paper_runtime.service.publish_event", autospec=True):
            released = await client.post(
                _exit_url(env.strategy_id, option_run_id),
                json={
                    "evidence_digest": ingested.json()["evidence_digest"],
                    "reason": "owner_exit",
                },
            )
        assert released.status_code == 200, released.text
        released_body = released.json()
        assert released_body["status"] == "accepted", released_body
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in released_body["items"]
        ] == [(HEDGE_SYMBOL, "SELL", 75)]

        after = await client.get(_exit_url(env.strategy_id, option_run_id))
        assert after.status_code == 200, after.text
        assert after.json()["shorts_proven_closed"] is True
        assert after.json()["withheld_hedges"] == []
        assert after.json()["state"] == "residual"
        # The hedge release is now the working stage, so nothing else is
        # releasable and the exit waits on it.
        assert after.json()["close_plan"] == []
        assert after.json()["waiting_reason"] == "orders_outstanding"

    row = _run_row(factory, option_run_id)
    assert row["status"] == "exiting"
    # Every order the exit sent closes a leg the run's own evidence holds, shorts
    # first, and the ingested confirmation is now one of the run's own trades.
    stages = _distinct_stages(row)
    assert len(stages) == 2, stages
    sent = sorted(
        (leg["tradingsymbol"], leg["transaction_type"])
        for stage in stages.values()
        for leg in stage["legs"]
    )
    assert sent == sorted([(HEDGE_SYMBOL, "SELL"), (SHORT_SYMBOL, "BUY")]), sent
    assert any(
        str(trade.get("order_id") or "") == short_order_id
        and str(trade.get("leg_id") or "") == "leg_short"
        and str(trade.get("transaction_type") or "").upper() == "BUY"
        for trade in _json(row["trades"])
    ), _json(row["trades"])


@pytest.mark.asyncio
async def test_two_exit_posts_for_one_run_cannot_both_submit(pg, monkeypatch):
    """Two owners press Exit at once: ONE stage, ONE order, one named refusal."""
    import httpx

    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="entered",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
    )
    entered = threading.Event()
    release = threading.Event()
    paper = _BlockingPaperService(
        _paper_runtime(factory), entered=entered, release=release
    )
    app = _app(factory, monkeypatch, paper=paper)
    url = _exit_url(env.strategy_id, option_run_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        inspection = await client.get(url)
        assert inspection.status_code == 200, inspection.text
        digest = inspection.json()["evidence_digest"]

    out: dict = {}

    def _post(key: str) -> None:
        async def _run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post(
                    url, json={"evidence_digest": digest, "reason": "owner_exit"}
                )

        try:
            out[key] = asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 - the assertion reads the failure
            out[f"{key}_error"] = exc

    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        first = threading.Thread(target=_post, args=("one",), daemon=True)
        first.start()
        assert entered.wait(timeout=20), "the first exit never reached the boundary"
        second = threading.Thread(target=_post, args=("two",), daemon=True)
        second.start()
        second.join(timeout=30)
        release.set()
        first.join(timeout=30)

    assert "one_error" not in out, out
    assert "two_error" not in out, out
    responses = {name: out[name] for name in ("one", "two")}
    assert sorted(response.status_code for response in responses.values()) == [200, 409], {
        name: response.text for name, response in responses.items()
    }
    accepted = next(r for r in responses.values() if r.status_code == 200)
    refused = next(r for r in responses.values() if r.status_code == 409)
    assert accepted.json()["status"] == "accepted"
    # The loser either observed the winner's live stage claim or lost the run's
    # own transition: both mean the run moved under it.
    assert refused.json()["detail"]["rejection_reason"] in {
        "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
        "OPTION_RUN_STATE_CHANGED",
    }, refused.text

    row = _run_row(factory, option_run_id)
    assert row["status"] == "exiting"
    stages = _distinct_stages(row)
    assert len(stages) == 1, stages
    assert len([stage for stage in stages.values() if stage.get("state") == "submitted"]) == 1
    assert paper.calls == 1, paper.calls
