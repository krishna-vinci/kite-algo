"""Options entry -> exit through the PRODUCTION HTTP route on PostgreSQL.

The whole point of this suite: the option-run edge is proved on the real route
(``POST /api/strategies/{id}/plans/{plan_id}/execute``) with the REAL durable
option store and the REAL paper repository, so "component tests are green" is not
standing in for the production path. It also covers partial-then-full exit
gating and the once-ever replay rule.

Disposable database on the local test server (port 15433) only.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

OWNER = "app:admin"
ACCOUNT = "kite:paper-opt-route"
G1 = "11111111-1111-1111-1111-111111111111"
PRICE = 1500.0


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_optroute_{uuid.uuid4().hex[:10]}"
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


def _leg(*, side, symbol, instrument_id, lot=75):
    return {
        "instrument_id": instrument_id,
        "exchange": "NFO",
        "tradingsymbol": symbol,
        "broker_exchange": "NFO",
        "broker_symbol": symbol,
        "broker_token": 900001,
        "product": "NRML",
        "instrument_type": "CE",
        "option_type": "CE",
        "strike": 25000.0,
        "expiry": "2026-10-29",
        "lot_size": lot,
        "ratio": 1,
        "side": side,
        "quantity": lot,
        "signed_quantity": lot if side == "BUY" else -lot,
        "reference_price": 100.0,
    }


class _Env:
    """One strategy with an entry plan; exit plans are added after the entry."""

    def __init__(self, factory):
        from sqlalchemy import text

        from backend.strategies.repository import SqlAlchemyStrategyRepository

        self.factory = factory
        # One hosted worker run per fixture: the module shares one database.
        self.run_id = f"run-entry-{uuid.uuid4().hex[:8]}"
        self.short_leg = _leg(side="SELL", symbol="NIFTY26OCT25000CE", instrument_id=str(uuid.uuid4()))
        self.hedge_leg = _leg(side="BUY", symbol="NIFTY26OCT30000CE", instrument_id=str(uuid.uuid4()))
        repo = SqlAlchemyStrategyRepository(factory)
        self.strategy_id = str(
            repo.create_strategy(
                owner_id=OWNER,
                name=f"optroute-{uuid.uuid4().hex[:6]}",
                description=None,
                execution_mode="paper",
                job_kind="finite",
                account_scope=ACCOUNT,
                max_duration_s=21600,
                progress_deadline_s=600,
                stale_exit_policy="exit_on_worker_stale",
            ).id
        )
        from backend.strategies.attribution import SqlAttributionStore

        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, template_id, "
                    " account_scope, execution_mode, status) "
                    "VALUES (:run, 'tok-entry', 'tpl-entry', :account, 'paper', 'open')"
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
        self.entry_plan_id = self._seed_plan(
            legs=[self.short_leg, self.hedge_leg],
            phase="entry",
            reference=None,
            run_id=self.run_id,
        )
        # The entry increases exposure, so the production path requires an
        # admission policy and an ACTIVE paper reservation for that exact plan.
        from datetime import timedelta

        from backend.strategies.admission import AdmissionService
        from backend.strategies.reservations import ClaimRequest, ReservationLedger

        AdmissionService(session_factory=factory).upsert_policy(
            strategy_id=self.strategy_id,
            account_id=ACCOUNT,
            updated_by=OWNER,
            allocation_inr=1_000_000.0,
        )
        ReservationLedger(session_factory=factory).claim(
            ClaimRequest(
                plan_id=self.entry_plan_id,
                strategy_id=self.strategy_id,
                account_id=ACCOUNT,
                evaluation_id=f"eval-{self.entry_plan_id}",
                execution_environment="paper",
                requirement_inr=30_000.0,
                valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
                allocation_inr=1_000_000.0,
                actor_id=OWNER,
            )
        )

    def _seed_plan(self, *, legs, phase, reference, run_id) -> str:
        from sqlalchemy import text

        plan_id = str(uuid.uuid4())
        proposal_id = str(uuid.uuid4())
        resolved = {
            "target_kind": "option_structure",
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
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
                    "run": run_id,
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

    def seed_exit_plan(self, *, legs, reference, run_id=None) -> str:
        """Insert the exit plan AFTER the entry ran (plans are insert-only)."""
        return self._seed_plan(
            legs=legs, phase="exit", reference=reference, run_id=run_id or self.run_id
        )

    def plan_view(self, plan_id: str) -> dict:
        """The plan as the production route passes it to the executor."""
        from sqlalchemy import text

        with self.factory() as session:
            row = (
                session.execute(
                    text(
                        "SELECT p.plan_id, p.proposal_id, p.strategy_id, p.account_id, p.plan_kind, "
                        " p.plan_hash, p.logical_plan, p.resolved_plan, p.pinned_catalog_generation, "
                        " p.pinned_universe_revision_id, p.pinned_member_hash, "
                        " prop.strategy_run_id AS strategy_run_id "
                        "FROM strategy_plans p JOIN strategy_proposals prop "
                        " ON prop.proposal_id = p.proposal_id WHERE p.plan_id = :p"
                    ),
                    {"p": plan_id},
                )
                .mappings()
                .first()
            )
        payload = dict(row)
        return {
            "plan_id": str(payload["plan_id"]),
            "proposal_id": str(payload["proposal_id"]),
            "strategy_id": str(payload["strategy_id"]),
            "account_id": str(payload["account_id"]),
            "plan_kind": str(payload["plan_kind"]),
            "plan_hash": str(payload["plan_hash"]),
            "logical_plan": _json(payload["logical_plan"]),
            "resolved_plan": _json(payload["resolved_plan"]),
            "pinned_catalog_generation": str(payload["pinned_catalog_generation"]),
            "pinned_universe_revision_id": payload["pinned_universe_revision_id"],
            "pinned_member_hash": payload["pinned_member_hash"],
        }

    def exit_legs(self):
        short_close = dict(self.short_leg)
        short_close.update({"side": "BUY", "signed_quantity": 0})
        hedge_close = dict(self.hedge_leg)
        hedge_close.update({"side": "SELL", "signed_quantity": 0})
        return [hedge_close, short_close]


def _app(factory, monkeypatch):
    from fastapi import FastAPI

    from backend.api.routers import strategies as strategies_module
    from backend.app import auth as auth_module
    from backend.app.auth import AppUser
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore
    from backend.strategies.execution import PaperPlanExecutor

    monkeypatch.setattr(
        auth_module, "get_optional_app_user", lambda _request: AppUser(username="admin", role="admin")
    )
    app = FastAPI()
    app.include_router(strategies_module.router, prefix="/api")
    app.dependency_overrides[strategies_module._strategies_db] = lambda: factory
    app.state.strategies_session_factory = factory
    app.state.attribution_store = SqlAttributionStore(session_factory=factory)
    app.state.paper_plan_executor = PaperPlanExecutor(
        session_factory=factory,
        paper_service=PaperTradingService(
            repository=SqlAlchemyPaperRepository(session_factory=factory),
            instruments_repository=_CatalogInstrument(),
            market_data_runtime=_TickRuntime(),
            default_starting_balance=Decimal("1000000"),
        ),
    )
    return app


def _client(factory, monkeypatch):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(factory, monkeypatch)),
        base_url="http://test",
    )


def _json(value):
    """JSONB comes back decoded on PostgreSQL and as text on SQLite."""
    return value if isinstance(value, (list, dict)) else json.loads(value)


def _rows(factory, sql, params=None):
    from sqlalchemy import text

    with factory() as session:
        return [dict(row) for row in session.execute(text(sql), params or {}).mappings().all()]


async def _execute(client, strategy_id: str, plan_id: str):
    return await client.post(f"/api/strategies/{strategy_id}/plans/{plan_id}/execute")


@pytest.mark.asyncio
async def test_options_entry_then_exit_through_the_production_route(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    client = _client(factory, monkeypatch)

    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        entry = await _execute(client, env.strategy_id, env.entry_plan_id)
        assert entry.status_code == 200, entry.text
        assert entry.json()["status"] == "filled"

        bindings = _rows(
            factory,
            "SELECT plan_id, option_run_id, phase FROM public.strategy_plan_option_runs "
            "WHERE strategy_id = :sid",
            {"sid": env.strategy_id},
        )
        assert len(bindings) == 1 and bindings[0]["phase"] == "entry"
        run_id = bindings[0]["option_run_id"]
        (run,) = _rows(
            factory,
            "SELECT status, orders, trades FROM public.option_run_states "
            "WHERE strategy_run_id = :r",
            {"r": run_id},
        )
        assert run["status"] == "entered"
        entry_orders = _json(run["orders"])
        assert len(entry_orders) == 2 and {o["status"] for o in entry_orders} == {"filled"}

        # The exit plan is a NEW plan (insert-only plans), referencing that run.
        exit_plan = env.seed_exit_plan(legs=env.exit_legs(), reference=run_id)
        exited = await _execute(client, env.strategy_id, exit_plan)
        assert exited.status_code == 200, exited.text
        assert exited.json()["status"] == "filled"

    (run,) = _rows(
        factory,
        "SELECT status, orders, trades FROM public.option_run_states WHERE strategy_run_id = :r",
        {"r": run_id},
    )
    assert run["status"] == "exited"
    orders = _json(run["orders"])
    trades = _json(run["trades"])
    # The run's own legs carry BOTH plans' fills: the exit did not create legs.
    assert {o["leg_id"] for o in orders} == {
        f"{env.entry_plan_id}:1",
        f"{env.entry_plan_id}:2",
    }
    assert sum(int(t["quantity"]) for t in trades) == 300  # 75 entry + 75 exit, x2 legs

    # The step trail's paper order ids are exactly the run's order ids.
    trail_ids = {
        row["paper_order_id"]
        for row in _rows(
            factory,
            "SELECT e.paper_order_id FROM strategy_plan_execution_events e "
            "JOIN strategy_plans p ON p.plan_id = e.plan_id WHERE p.strategy_id = :sid "
            "AND e.paper_order_id IS NOT NULL",
            {"sid": env.strategy_id},
        )
    }
    run_ids = {o["order_id"] for o in orders}
    assert trail_ids == run_ids

    # Replay: the same plan executes once, ever, and creates no second run.
    replay = await _execute(client, env.strategy_id, env.entry_plan_id)
    assert replay.status_code == 409
    assert replay.json()["detail"]["rejection_reason"] == "PLAN_ALREADY_EXECUTED"
    binds = _rows(
        factory,
        "SELECT COUNT(*) AS n FROM public.strategy_plan_option_runs WHERE strategy_id = :sid",
        {"sid": env.strategy_id},
    )
    assert int(binds[0]["n"]) == 2  # entry + exit, never a third


@pytest.mark.asyncio
async def test_a_partial_exit_stays_open_until_the_full_exit(pg, monkeypatch):
    factory = pg["factory"]
    env = _Env(factory)
    client = _client(factory, monkeypatch)

    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        entry = await _execute(client, env.strategy_id, env.entry_plan_id)
        assert entry.status_code == 200, entry.text
        (binding,) = _rows(
            factory,
            "SELECT option_run_id FROM public.strategy_plan_option_runs WHERE plan_id = :p",
            {"p": env.entry_plan_id},
        )
        run_id = binding["option_run_id"]

        # A PARTIAL exit closes only the short; the hedge stays open.
        short_close = dict(env.short_leg)
        short_close.update({"side": "BUY", "signed_quantity": 0})
        partial_plan = env.seed_exit_plan(legs=[short_close], reference=run_id)
        partial = await _execute(client, env.strategy_id, partial_plan)
        assert partial.status_code == 200, partial.text
        (run,) = _rows(
            factory,
            "SELECT status FROM public.option_run_states WHERE strategy_run_id = :r",
            {"r": run_id},
        )
        assert run["status"] == "partial_exit"

        # The full exit then closes the rest and reaches the terminal state.
        full_plan = env.seed_exit_plan(legs=env.exit_legs(), reference=run_id)
        full = await _execute(client, env.strategy_id, full_plan)
        assert full.status_code == 200, full.text

    (run,) = _rows(
        factory,
        "SELECT status, trades FROM public.option_run_states WHERE strategy_run_id = :r",
        {"r": run_id},
    )
    assert run["status"] == "exited"
    trades = _json(run["trades"])
    # Each run leg is exactly flat: 75 in, 75 out (never 150 of one).
    per_leg: dict[str, int] = {}
    for trade in trades:
        quantity = int(trade["quantity"])
        signed = quantity if str(trade["transaction_type"]).upper() == "BUY" else -quantity
        per_leg[trade["leg_id"]] = per_leg.get(trade["leg_id"], 0) + signed
    assert set(per_leg.values()) == {0}


class _BlockingPaperService:
    """A paper runtime that pauses inside the FIRST order (the race window)."""

    def __init__(self, inner, *, entered: "threading.Event", release: "threading.Event", block_on: int = 2):
        self.inner = inner
        self.entered = entered
        self.release = release
        self.block_on = block_on
        self.calls = 0

    async def place_order(self, *, account_scope, order_payload, attribution=None):
        import asyncio as _asyncio
        import threading as _threading

        self.calls += 1
        if self.calls == self.block_on:
            self.entered.set()
            # Wait off-loop so the test thread can drive the second plan.
            done = _threading.Event()

            def _wait():
                self.release.wait(timeout=10)
                done.set()

            _threading.Thread(target=_wait, daemon=True).start()
            await _asyncio.to_thread(done.wait, 10)
        return await self.inner.place_order(
            account_scope=account_scope, order_payload=order_payload, attribution=attribution
        )


def _execute_in_thread(executor, plan_view, *, out: dict, key: str):
    import asyncio as _asyncio
    import threading as _threading

    def _run():
        try:
            out[key] = _asyncio.run(executor.execute(plan_view, actor=OWNER))
        except Exception as exc:  # noqa: BLE001 - the assertion reads the refusal
            out[f"{key}_error"] = exc

    thread = _threading.Thread(target=_run)
    thread.start()
    return thread


def test_two_exit_plans_for_one_run_cannot_overclose(pg, monkeypatch):
    """Per-RUN ownership: the second exit of a run refuses instead of doubling.

    Two DIFFERENT exit plans (an operator retry / duplicate proposal) both name
    the same run. The run's transition is a compare-and-set, so exactly one plan
    owns it; the other refuses BEFORE submitting anything, and the run's own legs
    end exactly flat.
    """
    import asyncio
    import threading

    from sqlalchemy import text

    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.execution import PaperPlanExecutor

    factory = pg["factory"]
    env = _Env(factory)
    client = _client(factory, monkeypatch)

    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        entry = asyncio.run(_execute(client, env.strategy_id, env.entry_plan_id))
    assert entry.status_code == 200, entry.text

    with factory() as session:
        (run_id,) = (
            session.execute(
                text(
                    "SELECT option_run_id FROM public.strategy_plan_option_runs "
                    "WHERE plan_id = :p"
                ),
                {"p": env.entry_plan_id},
            )
            .scalars()
            .all()
        )

    exit_plan_one = env.seed_exit_plan(legs=env.exit_legs(), reference=run_id)
    exit_plan_two = env.seed_exit_plan(legs=env.exit_legs(), reference=run_id)

    entered = threading.Event()
    release = threading.Event()
    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        paper = _BlockingPaperService(
            PaperTradingService(
                repository=SqlAlchemyPaperRepository(session_factory=factory),
                instruments_repository=_CatalogInstrument(),
                market_data_runtime=_TickRuntime(),
                default_starting_balance=Decimal("1000000"),
            ),
            entered=entered,
            release=release,
        )
        executor = PaperPlanExecutor(session_factory=factory, paper_service=paper)

        out: dict = {}
        first = _execute_in_thread(
            executor, env.plan_view(exit_plan_one), out=out, key="one"
        )
        assert entered.wait(timeout=20), "the first exit never reached the runtime"
        second = _execute_in_thread(
            executor, env.plan_view(exit_plan_two), out=out, key="two"
        )
        second.join(timeout=30)
        release.set()
        first.join(timeout=30)

    # Exactly one owner; the other refused by name before submitting.
    assert "one" in out, out
    assert "one_error" not in out, out
    assert "two" not in out, out
    refusal = out.get("two_error")
    assert refusal is not None, out
    assert getattr(refusal, "reason_code", "") in {
        "OPTION_RUN_EXIT_IN_FLIGHT",
        "OPTION_RUN_STATE_CHANGED",
    }, refusal

    with factory() as session:
        (run,) = (
            session.execute(
                text(
                    "SELECT status, trades FROM public.option_run_states "
                    "WHERE strategy_run_id = :r"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
        losing_events = (
            session.execute(
                text(
                    "SELECT event FROM strategy_plan_execution_events WHERE plan_id = :p"
                ),
                {"p": exit_plan_two},
            )
            .scalars()
            .all()
        )
    assert run["status"] == "exited"
    trades = _json(run["trades"])
    # Entry (2) + exactly ONE exit (2): the losing plan added no fills at all.
    assert len(trades) == 4, trades
    per_leg: dict[str, int] = {}
    for trade in trades:
        quantity = int(trade["quantity"])
        signed = quantity if str(trade["transaction_type"]).upper() == "BUY" else -quantity
        per_leg[trade["leg_id"]] = per_leg.get(trade["leg_id"], 0) + signed
    # Each run leg closes to EXACTLY flat: never overclosed, never reversed.
    assert set(per_leg.values()) == {0}
    assert "submitted" not in set(losing_events), losing_events
