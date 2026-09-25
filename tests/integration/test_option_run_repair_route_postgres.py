"""The owner repair route for partial / cleanup option runs (B2.1b), on PostgreSQL.

Proves the production path end to end: the REAL owner route, the REAL durable
option store, the REAL staged structure exit and the REAL paper runtime, with
the same append-only audit the job reconciliation uses. A flat run is closed and
stops blocking a new equivalent entry; a residual run submits only the
risk-reducing close; an ambiguous run is refused by name and changes nothing;
another strategy's run id is never repaired.

Disposable database on the local test server (port 15433) only.
"""

from __future__ import annotations

import json
import os
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
ACCOUNT = "kite:paper-opt-repair"
G1 = "11111111-1111-1111-1111-111111111111"
PRICE = 1500.0
SHORT_SYMBOL = "NIFTY26OCT25000CE"
HEDGE_SYMBOL = "NIFTY26OCT30000CE"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_optrepair_{uuid.uuid4().hex[:10]}"
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
        yield {"factory": factory}
    finally:
        engine.dispose()
        _drop_db(name)


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


@pytest.fixture(autouse=True)
def _authorized_account_scope(monkeypatch):
    """The hosted surface only serves configured account scopes (production rule)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


def _leg(*, side, symbol, strike) -> dict:
    return {
        "instrument_id": str(uuid.uuid4()),
        "exchange": "NFO",
        "tradingsymbol": symbol,
        "broker_exchange": "NFO",
        "broker_symbol": symbol,
        "broker_token": 900001,
        "product": "NRML",
        "instrument_type": "CE",
        "option_type": "CE",
        "strike": strike,
        "expiry": "2026-10-29",
        "lot_size": 75,
        "ratio": 1,
        "side": side,
        "quantity": 75,
        "signed_quantity": 75 if side == "BUY" else -75,
        "reference_price": 100.0,
    }


class _Env:
    """One strategy + hosted job + entry plan, and the option runs seeded onto it."""

    def __init__(self, factory) -> None:
        from sqlalchemy import text

        from backend.strategies.attribution import SqlAttributionStore
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        self.factory = factory
        self.run_id = f"run-repair-{uuid.uuid4().hex[:8]}"
        self.short_leg = _leg(side="SELL", symbol=SHORT_SYMBOL, strike=25000.0)
        self.hedge_leg = _leg(side="BUY", symbol=HEDGE_SYMBOL, strike=30000.0)
        repo = SqlAlchemyStrategyRepository(factory)
        strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"optrepair-{uuid.uuid4().hex[:6]}",
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
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, template_id, "
                    " account_scope, execution_mode, status) "
                    "VALUES (:run, 'tok-repair', 'tpl-repair', :account, 'paper', 'open')"
                ),
                {"run": self.run_id, "account": ACCOUNT},
            )
            session.commit()
        SqlAttributionStore(session_factory=factory).bind_run(
            strategy_run_id=self.run_id,
            strategy_id=self.strategy_id,
            owner_id=OWNER,
            account_id=ACCOUNT,
            execution_environment="paper",
            bound_by="test",
            binding_source="hosted_job",
        )
        version = repo.create_version(
            strategy_id=self.strategy_id,
            source="# option repair route test\n",
            source_sha256=f"sha-{uuid.uuid4().hex[:12]}",
            parameters_schema={},
            capabilities_snapshot={},
            created_by=OWNER,
        )
        job = repo.create_job(
            strategy_id=self.strategy_id,
            version_id=str(version.id),
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            attempt=1,
            desired_state="started",
        )
        self.job_id = str(job.id)
        with factory() as session:
            session.execute(
                text("UPDATE public.strategy_jobs SET run_id = :run WHERE id = :job"),
                {"run": self.run_id, "job": self.job_id},
            )
            session.commit()
        self.entry_plan_id = self._seed_plan(
            legs=[self.short_leg, self.hedge_leg], phase="entry", reference=None
        )

    def _seed_plan(self, *, legs, phase, reference) -> str:
        from sqlalchemy import text

        plan_id = str(uuid.uuid4())
        proposal_id = str(uuid.uuid4())
        resolved = {
            "target_kind": "option_structure",
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
            "structure_digest": "digest-old-structure",
            "legs": legs,
            "option_run": {"phase": phase, "option_run_id": reference},
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
                    "run": self.run_id,
                    "sha": f"sha-{plan_id}",
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                    " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation) "
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
        self, *, status: str, trades: list, orders: list | None = None, environment: str = "paper"
    ) -> str:
        """Seed one durable option run bound to this env's entry plan."""
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.models import OptionRunCreateRequest
        from backend.options.execution.plan_binding import PlanOptionRunBindingStore

        store = DurableOptionRunStore(session_factory=self.factory)
        option_run_id = f"opt_run_{uuid.uuid4().hex[:10]}"
        request = OptionRunCreateRequest(
            strategy_run_id=option_run_id,
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
            protection={"structure_digest": "digest-old-structure"},
            metadata={
                "strategy_id": self.strategy_id,
                "account_id": ACCOUNT,
                "execution_environment": environment,
                "worker_run_id": self.run_id,
                "plan_id": self.entry_plan_id,
                "source": "hosted_plan_execution",
            },
        )
        store.create_run(request)
        run = store.get_run(option_run_id)
        run.status = status
        run.trades = list(trades)
        run.orders = list(orders or [])
        store.save_run(run)
        PlanOptionRunBindingStore(session_factory=self.factory).bind(
            plan_id=self.entry_plan_id,
            option_run_id=option_run_id,
            strategy_id=self.strategy_id,
            account_id=ACCOUNT,
            execution_environment=environment,
            phase="entry",
            worker_run_id=self.run_id,
        )
        return option_run_id


def _app(factory, monkeypatch):
    from fastapi import FastAPI

    from backend.api.routers import strategies as strategies_module
    from backend.app import auth as auth_module
    from backend.app.auth import AppUser
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore

    monkeypatch.setattr(
        auth_module, "get_optional_app_user", lambda _request: AppUser(username="admin", role="admin")
    )
    app = FastAPI()
    app.include_router(strategies_module.router, prefix="/api")
    app.dependency_overrides[strategies_module._strategies_db] = lambda: factory
    app.state.strategies_session_factory = factory
    app.state.attribution_store = SqlAttributionStore(session_factory=factory)
    # The residual close goes through the SAME paper runtime the plan path uses.
    app.state.paper_runtime_service = PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory=factory),
        instruments_repository=_CatalogInstrument(),
        market_data_runtime=_TickRuntime(),
        default_starting_balance=Decimal("1000000"),
    )
    return app


def _client(factory, monkeypatch):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(factory, monkeypatch)),
        base_url="http://test",
    )


def _rows(factory, sql, params=None):
    from sqlalchemy import text

    with factory() as session:
        return [dict(row) for row in session.execute(text(sql), params or {}).mappings().all()]


def _run_row(factory, option_run_id: str) -> dict:
    (row,) = _rows(
        factory,
        "SELECT status, orders, trades, pending_legs FROM public.option_run_states "
        "WHERE strategy_run_id = :r",
        {"r": option_run_id},
    )
    return row


def _json(value):
    """JSONB comes back decoded on PostgreSQL and as text on SQLite."""
    return value if isinstance(value, (list, dict)) else json.loads(value)


def _open_trade(leg_id: str, side: str, quantity: int = 75) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT_SYMBOL if leg_id == "leg_short" else HEDGE_SYMBOL,
    }


def _new_entry_plan() -> dict:
    """A DIFFERENT structure this strategy would open: only the B2.1a gate sees it."""
    return {
        "plan_id": str(uuid.uuid4()),
        "resolved_plan": {
            "target_kind": "option_structure",
            "structure_digest": "digest-new-structure",
            "option_run": {"phase": "entry", "option_run_id": None},
            "legs": [{"instrument_id": str(uuid.uuid4()), "side": "SELL", "broker_symbol": SHORT_SYMBOL}],
        },
    }


def _entry_gate(factory, strategy_id: str):
    from backend.options.execution.plan_binding import assess_option_entry_admissibility

    plan = _new_entry_plan()
    with factory() as session:
        assess_option_entry_admissibility(
            plan,
            strategy_id=strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            session=session,
        )


def _repair_url(strategy_id: str, option_run_id: str) -> str:
    return f"/api/strategies/{strategy_id}/option-runs/{option_run_id}/repair"


@pytest.mark.asyncio
async def test_a_flat_partial_run_repair_closes_it_and_unblocks_an_equivalent_entry(pg, monkeypatch):
    from backend.options.execution.plan_binding import PlanBindingRefusal

    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="partial_entry",
        trades=[
            _open_trade("leg_short", "SELL"),
            _open_trade("leg_short", "BUY"),
            _open_trade("leg_hedge", "BUY"),
            _open_trade("leg_hedge", "SELL"),
        ],
    )

    # B2.1a blocks a new entry while this run is unfinished.
    with pytest.raises(PlanBindingRefusal) as refusal:
        _entry_gate(factory, env.strategy_id)
    assert refusal.value.reason_code == "OPTION_STRUCTURE_UNRESOLVED"

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(env.strategy_id, option_run_id))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["state"] == "flat"
        assert body["close_plan"] == []

        repair = await client.post(
            _repair_url(env.strategy_id, option_run_id),
            json={"action": "close_flat", "evidence_digest": body["evidence_digest"]},
        )
        assert repair.status_code == 200, repair.text
        assert repair.json()["run_status"] == "exited"
        assert repair.json()["audit_id"]

    assert _run_row(factory, option_run_id)["status"] == "exited"
    audit = _rows(
        factory,
        "SELECT reason_code, outcome, actor_id FROM public.strategy_job_reconciliations "
        "WHERE job_id = :job",
        {"job": env.job_id},
    )
    assert [row["reason_code"] for row in audit] == ["OPTION_RUN_REPAIR_CLOSE_FLAT"]
    # A repair is NOT a reconciliation: it never clears the job's block, so the
    # job's reconciliation history must not read as if it had.
    assert audit[0]["outcome"] == "option_run_repair" and audit[0]["actor_id"] == OWNER

    # The gate no longer refuses: the repaired run is provably finished.
    _entry_gate(factory, env.strategy_id)


@pytest.mark.asyncio
async def test_a_residual_repair_submits_only_the_risk_reducing_close(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="partial_entry",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(env.strategy_id, option_run_id))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["state"] == "residual"
        assert [(row["tradingsymbol"], row["transaction_type"], row["quantity"]) for row in body["close_plan"]] == [
            (SHORT_SYMBOL, "BUY", 75)
        ]

        with patch("backend.paper_runtime.service.publish_event", autospec=True):
            repair = await client.post(
                _repair_url(env.strategy_id, option_run_id),
                json={"action": "close_residual", "evidence_digest": body["evidence_digest"]},
            )
        assert repair.status_code == 200, repair.text
        payload = repair.json()
        assert payload["run_status"] == "exiting"
        assert payload["state"] == "residual"
        assert payload["submission"]["submitted"] is True
        submitted = payload["submission"]["orders"]
        assert submitted and {order["transaction_type"] for order in submitted} == {"BUY"}
        assert {order["tradingsymbol"] for order in submitted} == {SHORT_SYMBOL}
        assert payload["submission"]["order_ids"]

    row = _run_row(factory, option_run_id)
    assert row["status"] == "exiting"
    assert list(row["pending_legs"]) == ["leg_short"]
    stages = [order for order in _json(row["orders"]) if order.get("stage_digest")]
    assert stages, "the residual close is a durable stage on the run itself"
    assert all(order["transaction_type"] == "BUY" for order in stages[-1]["legs"])


@pytest.mark.asyncio
async def test_an_ambiguous_run_is_refused_by_name_and_changes_nothing(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    unresolved = [
        {"stage_digest": "abcdef1234567890", "attempt": 1, "state": "sending", "legs": []}
    ]
    option_run_id = env.seed_run(
        status="partial_exit", trades=[_open_trade("leg_short", "SELL")], orders=unresolved
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(env.strategy_id, option_run_id))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["state"] == "ambiguous"
        assert "protective_stage_unresolved" in body["reasons"]

        repair = await client.post(
            _repair_url(env.strategy_id, option_run_id),
            json={"action": "close_residual", "evidence_digest": body["evidence_digest"]},
        )
        assert repair.status_code == 409, repair.text
        assert repair.json()["detail"]["rejection_reason"] == "OPTION_RUN_REPAIR_AMBIGUOUS"

    row = _run_row(factory, option_run_id)
    assert row["status"] == "partial_exit"
    assert _json(row["orders"]) == unresolved


@pytest.mark.asyncio
async def test_a_stale_evidence_digest_is_refused(pg, monkeypatch):
    from backend.options.execution.durable_store import DurableOptionRunStore

    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="partial_entry",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(env.strategy_id, option_run_id))
        stale = inspection.json()["evidence_digest"]

        # A fill lands between the inspection and the action.
        store = DurableOptionRunStore(session_factory=factory)
        run = store.get_run(option_run_id)
        run.trades.append(_open_trade("leg_short", "BUY"))
        store.save_run(run)

        repair = await client.post(
            _repair_url(env.strategy_id, option_run_id),
            json={"action": "close_residual", "evidence_digest": stale},
        )
        assert repair.status_code == 409, repair.text
        assert repair.json()["detail"]["rejection_reason"] == "OPTION_RUN_REPAIR_EVIDENCE_CHANGED"

    assert _run_row(factory, option_run_id)["status"] == "partial_entry"


@pytest.mark.asyncio
async def test_another_strategys_run_id_is_never_repaired(pg, monkeypatch):
    factory = pg["factory"]
    owner = _Env(factory)
    stranger = _Env(factory)
    option_run_id = owner.seed_run(
        status="partial_entry",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(stranger.strategy_id, option_run_id))
        assert inspection.status_code == 404
        repair = await client.post(
            _repair_url(stranger.strategy_id, option_run_id),
            json={"action": "close_residual", "evidence_digest": "0" * 32},
        )
        assert repair.status_code == 404

    assert _run_row(factory, option_run_id)["status"] == "partial_entry"


@pytest.mark.asyncio
async def test_a_live_residual_close_is_refused_rather_than_invented(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        status="partial_entry",
        trades=[_open_trade("leg_short", "SELL"), _open_trade("leg_hedge", "BUY")],
        environment="live",
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_repair_url(env.strategy_id, option_run_id))
        assert inspection.status_code == 200, inspection.text
        assert inspection.json()["state"] == "residual"

        repair = await client.post(
            _repair_url(env.strategy_id, option_run_id),
            json={
                "action": "close_residual",
                "evidence_digest": inspection.json()["evidence_digest"],
            },
        )
        assert repair.status_code == 409, repair.text
        assert repair.json()["detail"]["rejection_reason"] == "OPTION_RUN_REPAIR_LIVE_UNSUPPORTED"

    assert _run_row(factory, option_run_id)["status"] == "partial_entry"
