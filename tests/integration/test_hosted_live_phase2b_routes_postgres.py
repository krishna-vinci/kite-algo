"""Hosted LIVE Phase 2B routes: futures/rolls and option structures, fake broker.

Every scenario is driven through the PUBLIC PRODUCTION ROUTES with a credential the
supervisor lifecycle actually minted, and reaches only the FAKE BROKER boundary.
Operator auth, the supervisor credential, the hosted-attempt authority, the
proposal/compiler, the readers, the parent protocol, the per-leg claims, the
reservation ledger, the roll state machine, the durable option run and the barrier
are all production code.

    RECONCILIATION_PG_ADMIN='postgresql://postgres:testonly@127.0.0.1:15433/postgres' \\
        .venv/bin/pytest tests/integration/test_hosted_live_phase2b_routes_postgres.py -q

Run this file in its own pytest process (a sibling suite installs a fake
``psycopg2`` at import, which breaks these PostgreSQL fixtures).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)

SUPERVISOR_CREDENTIAL = "phase2b-supervisor-credential"
APP_JWT_SECRET = "phase2b-jwt-secret"
APP_ADMIN_PASSWORD = "phase2b-operator-password"

FUT_OLD = "NIFTY26SEPFUT"
FUT_NEW = "NIFTY26OCTFUT"
FUT_OLD_TOKEN = 601
FUT_NEW_TOKEN = 602
OPT_SHORT = "NIFTY26OCT25000CE"
OPT_HEDGE = "NIFTY26OCT30000CE"
OPT_SHORT_TOKEN = 900001
OPT_HEDGE_TOKEN = 900002
LOT = 75


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live_phase2b_{uuid.uuid4().hex[:10]}"
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


@pytest.fixture
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    # A FRESH disposable database per test, so the pinned catalog must be seeded
    # again for each one (the identity keys are globally unique).
    _CATALOG.clear()
    name, dsn = _create_db()
    try:
        os.environ["DATABASE_URL"] = dsn
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", dsn)
        cfg.set_main_option("script_location", "backend/alembic")
        command.upgrade(cfg, "head")
        engine = create_engine(dsn, poolclass=NullPool)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield {"dsn": dsn, "factory": factory, "engine": engine}
        finally:
            engine.dispose()
    finally:
        _drop_db(name)


@pytest.fixture
def live_env(pg):
    broker_user_id = f"phase2b{uuid.uuid4().hex[:6]}"
    account_scope = f"kite:{broker_user_id}"
    saved = {}
    for key in (
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        saved[key] = os.environ.pop(key, None)
    os.environ.update(
        {
            "DATABASE_URL": pg["dsn"],
            "APP_ENV": "development",
            "APP_ALLOW_INSECURE_DEV_AUTH": "true",
            "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": APP_ADMIN_PASSWORD,
            "APP_JWT_SECRET": APP_JWT_SECRET,
            "JWT_SECRET": APP_JWT_SECRET,
            "HOSTED_SUPERVISOR_CREDENTIAL": SUPERVISOR_CREDENTIAL,
            "HOSTED_STRATEGY_ACCOUNT_SCOPES": account_scope,
            "HOSTED_LIVE_ENABLED": "true",
            "HOSTED_LIVE_LANES": "cnc,mis,futures,options",
        }
    )
    from sqlalchemy import text

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.kite_sessions "
                "(session_id, access_token, broker_user_id, created_at) "
                "VALUES ('system', 'phase2b-access-token', :uid, NOW())"
            ),
            {"uid": broker_user_id},
        )
        session.commit()
    try:
        yield {"account_scope": account_scope, "broker_user_id": broker_user_id}
    finally:
        for key in (
            "APP_ADMIN_PASSWORD",
            "APP_JWT_SECRET",
            "JWT_SECRET",
            "HOSTED_STRATEGY_ACCOUNT_SCOPES",
            "HOSTED_LIVE_ENABLED",
            "HOSTED_LIVE_LANES",
            "HOSTED_SUPERVISOR_CREDENTIAL",
        ):
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _funds_boundary(monkeypatch):
    """The broker margin/funds READ is the second fake; the route's own call."""
    from backend.api.routers import strategies as strategies_module

    monkeypatch.setattr(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 5_000_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )


_CATALOG: dict = {}


def _seed_catalog(factory) -> dict:
    """Futures and option contracts, pinned in one published generation."""
    if _CATALOG:
        return _CATALOG
    from sqlalchemy import text

    generation = str(uuid.uuid4())
    instruments: dict = {}
    specs = [
        (FUT_OLD, FUT_OLD_TOKEN, "NFO", "NFO", "FUT", "2026-09-24", None, None, None),
        (FUT_NEW, FUT_NEW_TOKEN, "NFO", "NFO", "FUT", "2026-10-29", None, None, None),
        (OPT_SHORT, OPT_SHORT_TOKEN, "NFO", "NFO", "CE", "2026-10-29", 25000.0, "CE", "NIFTY"),
        (OPT_HEDGE, OPT_HEDGE_TOKEN, "NFO", "NFO", "CE", "2026-10-29", 30000.0, "CE", "NIFTY"),
    ]
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": generation},
        )
        for symbol, token, exchange, broker_exchange, kind, expiry, strike, option_type, underlying in specs:
            instrument_id = str(uuid.uuid4())
            instruments[symbol] = instrument_id
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                    " lifecycle_status, instrument_type, lot_size, tick_size, expiry, "
                    " strike, option_type, underlying, current_generation_id) "
                    "VALUES (:iid, :key, :key, :exchange, :symbol, 'active', :kind, "
                    " :lot, 0.05, :expiry, :strike, :option_type, :underlying, :gen)"
                ),
                {
                    "iid": instrument_id,
                    "key": f"{exchange}:{symbol}",
                    "exchange": exchange,
                    "symbol": symbol,
                    "kind": kind,
                    "lot": LOT,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "underlying": underlying,
                    "gen": generation,
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', :broker_exchange, :symbol, :token, :gen, TRUE)"
                ),
                {
                    "mid": str(uuid.uuid4()),
                    "iid": instrument_id,
                    "broker_exchange": broker_exchange,
                    "symbol": symbol,
                    "token": token,
                    "gen": generation,
                },
            )
        session.commit()
    _CATALOG.update({"instruments": instruments, "generation": generation})
    return _CATALOG


class _FakeBroker:
    """The ONLY broker boundary. It accepts (or raises) and never fills."""

    def __init__(self, *, order_ids=(), fail_on=None):
        self.calls = []
        self._order_ids = list(order_ids)
        self._fail_on = int(fail_on) if fail_on is not None else None

    async def handle(self, intent, *, context=None):
        self.calls.append((intent, dict(context or {})))
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise TimeoutError("socket closed mid-submit")
        index = min(len(self.calls) - 1, max(len(self._order_ids) - 1, 0))
        return {"result": {"order_id": self._order_ids[index] if self._order_ids else "OID-X"}}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _FakeOptionsManager:
    """The canonical session shape, frozen to the harness clock."""

    def __init__(self, clock):
        self.clock = clock
        soon = (clock() + timedelta(days=2)).date().isoformat()
        self.packets = {
            OPT_SHORT_TOKEN: {
                "token": OPT_SHORT_TOKEN, "tsym": OPT_SHORT, "ltp": 100.0,
                "iv": 0.14, "delta": 0.42, "updated_at": clock(),
            },
            OPT_HEDGE_TOKEN: {
                "token": OPT_HEDGE_TOKEN, "tsym": OPT_HEDGE, "ltp": 40.0,
                "iv": 0.16, "delta": 0.63, "updated_at": clock(),
            },
        }
        self.soon = soon

    def get_snapshot(self, _underlying):
        rows = [
            {"strike": 25000.0, "ce": self.packets[OPT_SHORT_TOKEN], "pe": None},
            {"strike": 30000.0, "ce": None, "pe": self.packets[OPT_HEDGE_TOKEN]},
        ]
        return {
            "underlying": "NIFTY",
            "expiries": ["2026-10-29", self.soon],
            "per_expiry": {
                "2026-10-29": {"rows": rows},
                self.soon: {"rows": rows},
            },
            "updated_at": self.clock(),
        }


def _build_app(factory, broker, clock):
    from fastapi import FastAPI

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import auth as auth_module
    from backend.api.routers import hosted_lifecycle, strategies, worker_auth, worker_execution
    from backend.api.routers import worker_proposals
    from backend.strategies.attribution import SqlAttributionStore
    from backend.strategies.execution import PaperPlanExecutor
    from backend.strategies.live_service import LivePlanExecutor
    from backend.strategies.settlement import ExecutionBarrier

    app = FastAPI(title="hosted live phase2b acceptance")
    for router in (
        auth_module.router,
        worker_auth.router,
        worker_execution.router,
        worker_proposals.router,
        strategies.router,
        hosted_lifecycle.router,
    ):
        app.include_router(router, prefix="/api")

    app.state.strategies_session_factory = factory
    app.state.options_session_manager = _FakeOptionsManager(clock)
    app.state.attribution_store = SqlAttributionStore(session_factory=factory)
    app.state.algo_worker_repository = SqlAlchemyAlgoWorkerRepository(factory)
    app.state.settlement_barrier = ExecutionBarrier(session_factory=factory)
    app.state.proposal_store = None
    app.state.paper_runtime_service = None
    app.state.paper_plan_executor = PaperPlanExecutor(session_factory=factory)
    executor = LivePlanExecutor(
        session_factory=factory,
        intent_handler=broker,
        clock=clock,
        quote_reader=lambda leg: {
            "instrument_id": str(leg.get("instrument_id") or ""),
            "ltp": 1500.0,
            "as_of": clock().isoformat(),
        },
        margin_reader=lambda account, plan: {
            "usable": 5_000_000.0,
            "required_margin_inr": 1_000_000.0,
            "as_of": clock().isoformat(),
        },
    )
    app.state.live_plan_executor = executor
    return app, executor


def _asgi_client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://phase2b"
    )


async def _operator_client(app):
    for stale in (
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        os.environ.pop(stale, None)
    os.environ["APP_ADMIN_USERNAME"] = "admin"
    os.environ["APP_ADMIN_PASSWORD"] = APP_ADMIN_PASSWORD
    os.environ["APP_JWT_SECRET"] = APP_JWT_SECRET
    os.environ.setdefault("JWT_SECRET", APP_JWT_SECRET)
    client = _asgi_client(app)
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": APP_ADMIN_PASSWORD}
    )
    assert response.status_code < 400, response.text
    return client


def _supervisor_headers():
    from backend.strategies import supervisor_auth

    return {supervisor_auth.HEADER_NAME: SUPERVISOR_CREDENTIAL}


async def _prepare_live_attempt(client, *, account_scope: str, lease_until: datetime):
    created = await client.post(
        "/api/strategies",
        json={
            "name": f"Phase2B {uuid.uuid4().hex[:8]}",
            "description": None,
            "execution_mode": "live",
            "job_kind": "finite",
            "account_scope": account_scope,
            "max_duration_s": 172_800,
            "progress_deadline_s": 900,
            "stale_exit_policy": "none",
        },
    )
    assert created.status_code < 400, created.text
    strategy_id = str(created.json()["strategy_id"])
    version = await client.post(
        f"/api/strategies/{strategy_id}/versions",
        json={
            "source": "print('phase2b')",
            "parameters_schema": {"type": "object", "properties": {}},
            "capabilities": {"trade": True, "data": True},
            "risk_policy": {"allowed_structure_families": ["vertical_spread"]},
        },
    )
    assert version.status_code < 400, version.text
    version_id = str(version.json().get("version_id") or "1")
    policy = await client.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": 50_000_000.0},
    )
    assert policy.status_code < 400, policy.text
    job = await client.post(
        f"/api/strategies/{strategy_id}/jobs",
        json={
            "version_id": version_id,
            "job_kind": "finite",
            "execution_mode": "live",
            "params": {},
            "idempotency_key": f"phase2b-{uuid.uuid4().hex[:8]}",
        },
    )
    assert job.status_code < 400, job.text
    job_body = job.json().get("job") or {}
    job_id = str(job_body.get("job_id") or job_body.get("id") or "")
    assert job_id, job.text
    claim = await client.post(
        f"/api/hosted-supervisor/jobs/{job_id}/claim",
        json={
            "lease_owner": "phase2b-supervisor",
            "expected_lease_epoch": 0,
            "expected_attempt": 1,
            "lease_until": lease_until.isoformat(),
        },
        headers=_supervisor_headers(),
    )
    assert claim.status_code < 400, claim.text
    prepared = await client.post(
        f"/api/hosted-supervisor/jobs/{job_id}/prepare",
        json={
            "lease_owner": "phase2b-supervisor",
            "lease_epoch": int(claim.json()["lease_epoch"]),
            "attempt": 1,
        },
        headers=_supervisor_headers(),
    )
    assert prepared.status_code < 400, prepared.text
    body = prepared.json()
    return {
        "strategy_id": strategy_id,
        "job_id": job_id,
        "run_id": str(body["run_id"]),
        "child_headers": {
            "Authorization": f"Bearer {body['worker_token']}",
            "X-Worker-Session-Nonce": str(body["session_nonce"]),
        },
    }


async def _submit_proposal(client, attempt, body, *, account_scope):
    return await client.post(
        "/api/algo-workers/worker/proposals",
        json={
            "evaluation_id": f"eval-{uuid.uuid4().hex[:8]}",
            "evaluation_kind": "run_now",
            "strategy_run_id": attempt["run_id"],
            "strategy_id": attempt["strategy_id"],
            "account_scope": account_scope,
            **body,
        },
        headers=attempt["child_headers"],
    )


async def _execute(client, strategy_id, plan_id):
    reserved = await client.post(f"/api/strategies/{strategy_id}/plans/{plan_id}/reserve")
    assert reserved.status_code < 400, reserved.text
    reservation = reserved.json()
    published = await client.post(
        f"/api/strategies/{strategy_id}/positions/rebuild?environment=live"
    )
    assert published.status_code < 400, published.text
    approved = await client.post(
        f"/api/strategies/{strategy_id}/plans/{plan_id}/approval",
        json={"reservation_id": reservation["reservation_id"], "validity_seconds": 3600},
    )
    assert approved.status_code < 400, approved.text
    executed = await client.post(f"/api/strategies/{strategy_id}/plans/{plan_id}/execute")
    return executed, reservation


def _ingest_fill(
    factory,
    *,
    account_id,
    run_id,
    order_id,
    trade_id,
    quantity,
    side,
    symbol,
    token,
    product="NRML",
    terminal=True,
):
    """One ordinary ingestion artifact: a trade fill plus the order's state."""
    from sqlalchemy import text

    status = "COMPLETE" if terminal else "OPEN"
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, "
                " strategy_run_id, strategy_family, strategy_name, entry_surface, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'options_strategy', :run, "
                " 'hosted_plan', :oid, 'live', 'placed')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": f"KA-{trade_id}",
                "account": account_id,
                "run": run_id,
                "oid": order_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, "
                " instrument_token, exchange, tradingsymbol, product, transaction_type, "
                " quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, :tid, :oid, :token, 'NFO', :symbol, :product, :side, "
                " :qty, 1500.0, NOW(), true)"
            ),
            {
                "account": account_id,
                "tid": trade_id,
                "oid": order_id,
                "token": token,
                "symbol": symbol,
                "product": str(product).upper(),
                "side": side,
                "qty": int(quantity),
            },
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, "
                " needs_reconcile, terminal, exchange, tradingsymbol, instrument_token, "
                " product, transaction_type, updated_at) "
                "VALUES (:account, :oid, :status, NOW(), :qty, false, false, :terminal, "
                " 'NFO', :symbol, :token, :product, :side, NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = :status, "
                " last_seen_filled_quantity = :qty, terminal = :terminal"
            ),
            {
                "account": account_id,
                "oid": order_id,
                "status": status,
                "terminal": bool(terminal),
                "qty": int(quantity),
                "symbol": symbol,
                "token": token,
                "product": str(product).upper(),
                "side": side,
            },
        )
        session.commit()


def _claims(factory, plan_id):
    from sqlalchemy import text

    with factory() as session:
        rows = (
            session.execute(
                text(
                    "SELECT step_no, state, broker_order_ids, delta_snapshot, detail "
                    "FROM public.live_plan_submissions WHERE plan_id = :pid "
                    "ORDER BY step_no"
                ),
                {"pid": plan_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _execution(factory, plan_id):
    from sqlalchemy import text

    with factory() as session:
        return (
            session.execute(
                text(
                    "SELECT lane, state, step_spec, detail FROM public.live_plan_executions "
                    "WHERE plan_id = :pid"
                ),
                {"pid": plan_id},
            )
            .mappings()
            .first()
        )


class _Env:
    """The shared per-test environment: disposable DB, app, clock, consumer."""

    def __init__(self, pg, live_env, *, broker=None):
        self.factory = pg["factory"]
        self.account_scope = live_env["account_scope"]
        self.broker = broker or _FakeBroker(
            order_ids=("O-FUT-OLD", "O-FUT-NEW", "O-OPT-HEDGE", "O-OPT-SHORT")
        )
        self.clock = _Clock(datetime.now(timezone.utc))
        self.lease_until = self.clock() + timedelta(hours=12)
        self.app, self.executor = _build_app(self.factory, self.broker, self.clock)

    def consumer(self):
        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        return LiveOutcomeConsumer(
            session_factory=self.factory,
            clock=self.clock,
            sequence_releaser=self.executor.release_sequence,
        )


def _futures_payload(*, symbol: str, token: int, side: str, roll: dict | None = None) -> dict:
    payload = {
        "target_kind": "target_futures",
        "payload": {
            "instrument_token": token,
            "exchange": "NFO",
            "tradingsymbol": symbol,
            "lots": 1,
            "product": "NRML",
            "side": side,
            "reference_price": 20000.0,
        },
    }
    if roll is not None:
        payload["payload"]["roll"] = roll
    return payload


def _option_payload(
    *, phase: str, expiry: str = "2026-10-29", option_run_id: str | None = None
) -> dict:
    legs = [
        {
            "instrument_token": OPT_SHORT_TOKEN,
            "exchange": "NFO",
            "tradingsymbol": OPT_SHORT,
            "side": "SELL" if phase == "entry" else "BUY",
            "ratio": 1,
            "reference_price": 100.0,
        },
        {
            "instrument_token": OPT_HEDGE_TOKEN,
            "exchange": "NFO",
            "tradingsymbol": OPT_HEDGE,
            "side": "BUY" if phase == "entry" else "SELL",
            "ratio": 1,
            "reference_price": 40.0,
        },
    ]
    payload = {
        "legs": legs,
        "product": "NRML",
        "underlying": "NIFTY",
        "expiry": expiry,
        "expiry_policy": "exit_before_cutoff",
        "phase": phase,
    }
    if option_run_id:
        payload["option_run_id"] = option_run_id
    return {"target_kind": "option_structure", "payload": payload}


def test_futures_roll_releases_the_close_only_after_the_full_replacement(pg, live_env):
    """Two plans, one roll: acquisition FULL fill, then the old-contract close.

    The close is a separate frozen plan. It is materialized ``withheld`` - the roll
    has not released it - and stays that way through a partial replacement fill.
    Only the FULL required replacement quantity, PROVEN by the roll's own recorded
    replacement executions, releases it; the released order is an absolute flat of
    the strategy's own attributed old-contract quantity (never the frozen signed
    target doubled), and a repeated pass sends nothing.
    """
    env = _Env(pg, live_env)
    _seed_catalog(env.factory)

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]

            # -- open the OLD contract's position with an ordinary futures plan.
            open_response = await _submit_proposal(
                client,
                attempt,
                _futures_payload(symbol=FUT_OLD, token=FUT_OLD_TOKEN, side="BUY"),
                account_scope=env.account_scope,
            )
            assert open_response.status_code < 400, open_response.text
            open_plan = open_response.json()["plan"]
            executed, _res = await _execute(client, strategy_id, open_plan["plan_id"])
            assert executed.status_code < 400, executed.text
            open_order = executed.json()["broker_order_ids"][0]
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=open_order,
                trade_id="TR-FUT-OPEN",
                quantity=LOT,
                side="BUY",
                symbol=FUT_OLD,
                token=FUT_OLD_TOKEN,
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts

            # -- the replacement (acquisition) plan, then the roll that binds it.
            acquire_response = await _submit_proposal(
                client,
                attempt,
                _futures_payload(
                    symbol=FUT_NEW,
                    token=FUT_NEW_TOKEN,
                    side="BUY",
                    roll={"role": "open_new"},
                ),
                account_scope=env.account_scope,
            )
            assert acquire_response.status_code < 400, acquire_response.text
            acquire_plan = acquire_response.json()["plan"]
            instruments = _CATALOG["instruments"]
            roll = await client.post(
                f"/api/strategies/{strategy_id}/rolls",
                json={
                    "old_instrument_id": instruments[FUT_OLD],
                    "new_instrument_id": instruments[FUT_NEW],
                    "required_replacement_quantity": LOT,
                    "old_coordinate": {"product": "NRML", "exchange": "NFO"},
                    "new_coordinate": {"product": "NRML", "exchange": "NFO"},
                    "plan_id": acquire_plan["plan_id"],
                },
            )
            assert roll.status_code < 400, roll.text
            roll_id = roll.json()["roll_id"]

            # A plain futures plan on the OLD contract, with no roll binding, may
            # NOT close an open roll's old leg.
            bypass = await _submit_proposal(
                client,
                attempt,
                _futures_payload(symbol=FUT_OLD, token=FUT_OLD_TOKEN, side="SELL"),
                account_scope=env.account_scope,
            )
            assert bypass.status_code < 400, bypass.text
            bypass_plan = bypass.json()["plan"]
            refused, _reservation = await _execute(client, strategy_id, bypass_plan["plan_id"])
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["rejection_reason"] == "ROLL_CLOSE_REQUIRES_BINDING"

            executed, _res = await _execute(client, strategy_id, acquire_plan["plan_id"])
            assert executed.status_code < 400, executed.text
            acquire_order = executed.json()["broker_order_ids"][0]

            # -- the CLOSE plan: materialized withheld, nothing sent.
            close_response = await _submit_proposal(
                client,
                attempt,
                _futures_payload(
                    symbol=FUT_OLD,
                    token=FUT_OLD_TOKEN,
                    side="SELL",
                    roll={"role": "close_old", "roll_id": roll_id},
                ),
                account_scope=env.account_scope,
            )
            assert close_response.status_code < 400, close_response.text
            close_plan = close_response.json()["plan"]
            calls_before = len(env.broker.calls)
            executed, _res = await _execute(client, strategy_id, close_plan["plan_id"])
            assert executed.status_code < 400, executed.text
            assert executed.json()["broker_order_ids"] == [], executed.text
            assert len(env.broker.calls) == calls_before, "a locked close was sent"
            parent = _execution(env.factory, close_plan["plan_id"])
            assert parent["lane"] == "futures_roll", dict(parent)
            claim = _claims(env.factory, close_plan["plan_id"])[0]
            assert claim["state"] == "withheld", dict(claim)
            assert claim["detail"]["release_rule"] == "roll_close_released", claim["detail"]

            # -- a PARTIAL replacement fill proves nothing and releases nothing.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=acquire_order,
                trade_id="TR-FUT-NEW-1",
                quantity=37,
                side="BUY",
                symbol=FUT_NEW,
                token=FUT_NEW_TOKEN,
                terminal=False,
            )
            counts = await env.consumer().poll_once()
            assert counts["partial"] == 1, counts
            assert len(env.broker.calls) == calls_before, "a partial replacement released the close"
            assert _claims(env.factory, close_plan["plan_id"])[0]["state"] == "withheld"

            # -- the FULL replacement fill proves the roll and releases the close.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=acquire_order,
                trade_id="TR-FUT-NEW-2",
                quantity=38,
                side="BUY",
                symbol=FUT_NEW,
                token=FUT_NEW_TOKEN,
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts
            assert counts["sequence_released"] == 1, (
                counts,
                dict(_claims(env.factory, close_plan["plan_id"])[0]),
            )
            close_intent, _ctx = env.broker.calls[-1]
            order = close_intent.payload["order"]
            assert order["transaction_type"] == "SELL", order
            assert order["tradingsymbol"] == FUT_OLD, order
            # An ABSOLUTE FLAT of the attributed 75, not the doubled frozen target.
            assert int(order["quantity"]) == LOT, order
            released = _claims(env.factory, close_plan["plan_id"])[0]
            assert released["state"] == "pending", dict(released)
            assert released["detail"]["released_quantity"] == LOT, released["detail"]

            # -- a repeated / restarted pass sends nothing.
            calls_after = len(env.broker.calls)
            again = await env.executor.release_sequence()
            assert again["released"] == 0, again
            assert len(env.broker.calls) == calls_after
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_option_entry_gates_the_short_on_the_confirmed_hedge_fill(pg, live_env):
    """A structure's short is released only against a CONFIRMED hedge fill.

    The entry is two legs of ONE frozen structure on the existing durable option
    run. The long (hedge) is dispatched first; the short is materialized
    ``withheld`` and stays that way through a partial hedge fill - a submitted
    order is not a position, and releasing the short on one is how a bounded
    structure becomes unbounded. The FULL hedge fill releases it exactly once, and
    the run's own trades record both fills.
    """
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("O-OPT-HEDGE-FILL", "O-OPT-SHORT-FILL")),
    )
    _seed_catalog(env.factory)

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]
            proposed = await _submit_proposal(
                client,
                attempt,
                _option_payload(phase="entry"),
                account_scope=env.account_scope,
            )
            assert proposed.status_code < 400, proposed.text
            plan = proposed.json()["plan"]
            executed, _res = await _execute(client, strategy_id, plan["plan_id"])
            assert executed.status_code < 400, executed.text
            body = executed.json()
            assert len(body["broker_order_ids"]) == 1, body
            hedge_intent, _ctx = env.broker.calls[-1]
            assert hedge_intent.payload["order"]["transaction_type"] == "BUY", hedge_intent.payload
            assert hedge_intent.payload["order"]["tradingsymbol"] == OPT_HEDGE
            hedge_order = body["broker_order_ids"][0]

            parent = _execution(env.factory, plan["plan_id"])
            assert parent["lane"] == "option_structure", dict(parent)
            claims = {int(row["step_no"]): row for row in _claims(env.factory, plan["plan_id"])}
            assert len(claims) == 2, claims
            withheld = [row for row in claims.values() if row["state"] == "withheld"]
            assert len(withheld) == 1, claims
            short_step = int(withheld[0]["step_no"])
            assert withheld[0]["detail"]["release_rule"] == "hedge_fill_gate", withheld[0]["detail"]

            # -- a PARTIAL hedge fill releases nothing.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=hedge_order,
                trade_id="TR-HEDGE-1",
                quantity=40,
                side="BUY",
                symbol=OPT_HEDGE,
                token=OPT_HEDGE_TOKEN,
                terminal=False,
            )
            calls_before = len(env.broker.calls)
            counts = await env.consumer().poll_once()
            assert counts["partial"] == 1, counts
            assert len(env.broker.calls) == calls_before, "a partial hedge released the short"
            assert _claims(env.factory, plan["plan_id"])[short_step - 1]["state"] == "withheld"

            # -- the FULL hedge fill releases the short exactly once.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=hedge_order,
                trade_id="TR-HEDGE-2",
                quantity=35,
                side="BUY",
                symbol=OPT_HEDGE,
                token=OPT_HEDGE_TOKEN,
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts
            assert counts["sequence_released"] == 1, (
                counts,
                _claims(env.factory, plan["plan_id"]),
            )
            short_intent, _ctx = env.broker.calls[-1]
            short_order = short_intent.payload["order"]
            assert short_order["transaction_type"] == "SELL", short_order
            assert short_order["tradingsymbol"] == OPT_SHORT, short_order
            assert int(short_order["quantity"]) == LOT, short_order

            # -- a repeated / restarted pass sends nothing.
            calls_after = len(env.broker.calls)
            again = await env.executor.release_sequence()
            assert again["released"] == 0, again
            assert len(env.broker.calls) == calls_after
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_an_option_entry_inside_the_frozen_expiry_cutoff_is_refused(pg, live_env):
    """The frozen ``exit_before_cutoff`` policy is enforced at admission time.

    Opening a structure the platform would immediately have to escalate is not a
    trade it has a mandate for: the existing expiry adapter is consulted BEFORE
    anything is materialized, and the refusal names itself.
    """
    env = _Env(pg, live_env)
    _seed_catalog(env.factory)
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]
            proposed = await _submit_proposal(
                client,
                attempt,
                _option_payload(phase="entry", expiry=soon),
                account_scope=env.account_scope,
            )
            assert proposed.status_code < 400, proposed.text
            plan_id = proposed.json()["plan"]["plan_id"]
            calls_before = len(env.broker.calls)
            executed, _res = await _execute(client, strategy_id, plan_id)
            assert executed.status_code == 409, executed.text
            assert (
                executed.json()["detail"]["rejection_reason"] == "OPTION_EXPIRY_CUTOFF_PASSED"
            ), executed.text
            assert len(env.broker.calls) == calls_before
            assert _execution(env.factory, plan_id) is None, "a refused entry materialized work"
        finally:
            await client.aclose()

    asyncio.run(_run())


async def _releasing_plan(client, env, attempt):
    """Materialize one live plan, then park its claim in the 'releasing' window.

    That state is what the release pass leaves behind when the process dies
    between committing the claim and persisting an order reference, so a test can
    only reach it by writing it - and everything that DECIDES about it afterwards
    reads the platform's own durable rows.
    """
    from sqlalchemy import text

    proposed = await _submit_proposal(
        client,
        attempt,
        _futures_payload(symbol=FUT_OLD, token=FUT_OLD_TOKEN, side="BUY"),
        account_scope=env.account_scope,
    )
    assert proposed.status_code < 400, proposed.text
    plan_id = proposed.json()["plan"]["plan_id"]
    # The plan carries a real LIVE reservation, exactly as a materialized plan
    # does: the disposition's per-leg capacity rule is what these scenarios are
    # about, so the reservation must exist for the assertion to mean anything.
    reserved = await client.post(
        f"/api/strategies/{attempt['strategy_id']}/plans/{plan_id}/reserve"
    )
    assert reserved.status_code < 400, reserved.text
    with env.factory() as session:
        session.execute(
            text(
                "INSERT INTO public.live_plan_executions "
                "(execution_id, plan_id, strategy_id, account_id, execution_environment, "
                " lane, state, step_spec, detail) "
                "SELECT :eid, plan_id, strategy_id, account_id, 'live', 'futures_roll', "
                " 'executing', CAST(:spec AS jsonb), '{}'::jsonb "
                "FROM public.strategy_plans WHERE plan_id = :pid"
            ),
            {
                "eid": f"live_exec_{uuid.uuid4().hex}",
                "pid": plan_id,
                "spec": "[]",
            },
        )
        session.execute(
            text(
                "INSERT INTO public.live_plan_submissions "
                "(submission_id, plan_id, step_no, step_ref, strategy_id, account_id, "
                " execution_environment, state, broker_order_ids, delta_snapshot, detail) "
                "SELECT :sid, plan_id, 1, :ref, strategy_id, account_id, 'live', "
                " 'releasing', '[]'::jsonb, CAST(:snap AS jsonb), '{}'::jsonb "
                "FROM public.strategy_plans WHERE plan_id = :pid"
            ),
            {
                "sid": f"live_sub_{uuid.uuid4().hex}",
                "pid": plan_id,
                "ref": f"live-plan:{plan_id}:step:1",
                "snap": '{"quantity": 75, "side": "BUY"}',
            },
        )
        session.commit()
    return plan_id


def _insert_fence_row(env, *, plan_id: str, broker_order_id: str | None = None) -> str:
    from sqlalchemy import text

    with env.factory() as session:
        plan = (
            session.execute(
                text(
                    "SELECT strategy_id, account_id FROM public.strategy_plans "
                    "WHERE plan_id = :pid"
                ),
                {"pid": plan_id},
            )
            .mappings()
            .first()
        )
        client_order_ref = f"KAF{uuid.uuid4().hex[:6].upper()}"
        session.execute(
            text(
                "INSERT INTO public.live_order_intents "
                "(intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, idempotency_key, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, 'run-fence', 'options_strategy', "
                " 'phase2b', 'hosted_plan', :key, :oid, 'live', 'pending')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": client_order_ref,
                "account": str(plan["account_id"]),
                "key": f"live-plan:{plan_id}:step:1",
                "oid": broker_order_id,
            },
        )
        session.commit()
    return client_order_ref


def _gone_authority_reader(*_args, **_kwargs):
    from backend.strategies.live_authority import LiveAuthorityRefusal

    raise LiveAuthorityRefusal("TOKEN_NOT_ACTIVE", {"token_status": "revoked"})


def _claim_state(env, plan_id: str) -> dict:
    return dict(_claims(env.factory, plan_id)[0])


def test_the_releasing_window_recovers_only_on_proof(pg, live_env):
    """A 'releasing' claim is decided by the platform's durable pre-send fence.

    "No broker order reference on the claim" is what a LOST RESPONSE looks like, so
    it is not proof of anything. The four scenarios below are the whole contract:

    * no pre-send record  -> nothing was ever sent, and the bounded disposition may
      proceed (with the fence evidence recorded);
    * a pre-send record with no order id and no authoritative read -> UNKNOWN: the
      step stays in flight, keeps its capacity and never produces a quiet proof;
    * a pre-send record whose immutable client correlation the AUTHORITATIVE broker
      read finds -> the discovered order is ADOPTED onto the claim and ingestion
      owns it - a repair, never a retransmission;
    * a pre-send record whose client correlation a COMPLETE authoritative read does
      not find -> non-submission is proven, and the disposition may proceed.
    """
    from backend.strategies.live_repair import (
        LiveRepairRefusal,
        LiveRepairService,
    )
    from backend.strategies.settlement import enumerate_inflight_work

    env = _Env(pg, live_env)
    _seed_catalog(env.factory)

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]
            service = LiveRepairService(
                session_factory=env.factory, authority_reader=_gone_authority_reader
            )

            # -- 1. no pre-send record: proven non-submission, disposition allowed.
            plan_a = await _releasing_plan(client, env, attempt)
            result = service.abandon_residual(plan_id=plan_a, actor="operator")
            assert result["state"] == "residual_abandoned", result
            claim = _claim_state(env, plan_a)
            assert claim["state"] == "residual_abandoned", claim
            record = dict(claim["detail"]["disposition_record"])
            assert record["prior_state"] == "releasing", record
            assert record["send_outcome"] == "unknown_never_retransmitted", record
            assert record["dispatch_fence"]["state"] == "not_attempted", record
            with env.factory() as session:
                kinds = {
                    item.kind
                    for item in enumerate_inflight_work(
                        account_id=env.account_scope,
                        strategy_id=strategy_id,
                        execution_environment="live",
                        db=session,
                    )
                }
            assert "live_submission_releasing" not in kinds, kinds

            # -- 2. pre-send record, no order id, no authoritative read: UNKNOWN.
            plan_b = await _releasing_plan(client, env, attempt)
            _insert_fence_row(env, plan_id=plan_b)
            with pytest.raises(LiveRepairRefusal) as ctx:
                service.abandon_residual(plan_id=plan_b, actor="operator")
            assert ctx.value.reason_code == "LIVE_REPAIR_RELEASING_OUTCOME_UNKNOWN", ctx.value.detail
            claim = _claim_state(env, plan_b)
            assert claim["state"] == "releasing", claim
            reservation = env.executor.ledger.for_plan(plan_b)
            assert str(reservation["status"]) in ("active", "renewed"), reservation
            with env.factory() as session:
                kinds = {
                    item.kind
                    for item in enumerate_inflight_work(
                        account_id=env.account_scope,
                        strategy_id=strategy_id,
                        execution_environment="live",
                        db=session,
                    )
                }
            assert "live_submission_releasing" in kinds, kinds

            # -- 3. the authoritative broker read FINDS the accepted order: adopt.
            plan_c = await _releasing_plan(client, env, attempt)
            _insert_fence_row(env, plan_id=plan_c)

            def _lookup(*, account_id, client_order_ref, idempotency_key):
                _ = (account_id, client_order_ref, idempotency_key)
                return {"state": "present", "order_id": "OID-RECOVERED-1"}

            from backend.strategies.live_dispatch_fence import LiveDispatchFence

            recovering = LiveRepairService(
                session_factory=env.factory,
                authority_reader=_gone_authority_reader,
                dispatch_fence=LiveDispatchFence(
                    session_factory=env.factory, broker_lookup=_lookup
                ),
            )
            adopted = recovering.abandon_residual(plan_id=plan_c, actor="operator")
            assert adopted["state"] == "pending", adopted
            assert adopted["recovered_order_ids"] == ["OID-RECOVERED-1"], adopted
            claim = _claim_state(env, plan_c)
            assert claim["state"] == "pending", claim
            assert list(claim["broker_order_ids"]) == ["OID-RECOVERED-1"], claim
            reservation = env.executor.ledger.for_plan(plan_c)
            assert str(reservation["status"]) in ("active", "renewed"), reservation

            # -- 4. a COMPLETE read that does not find it proves non-submission.
            plan_d = await _releasing_plan(client, env, attempt)
            _insert_fence_row(env, plan_id=plan_d)

            def _absent(*, account_id, client_order_ref, idempotency_key):
                _ = (account_id, client_order_ref, idempotency_key)
                return {"state": "absent"}

            complete = LiveRepairService(
                session_factory=env.factory,
                authority_reader=_gone_authority_reader,
                dispatch_fence=LiveDispatchFence(
                    session_factory=env.factory, broker_lookup=_absent
                ),
            )
            disposed = complete.abandon_residual(plan_id=plan_d, actor="operator")
            assert disposed["state"] == "residual_abandoned", disposed
            claim = _claim_state(env, plan_d)
            record = dict(claim["detail"]["disposition_record"])
            assert record["dispatch_fence"]["state"] == "not_attempted", record
            assert record["dispatch_fence"]["reason"] == "BROKER_HAS_NO_SUCH_ORDER", record

            # -- 5. a KNOWN subset can never prove the REST: with two pre-send
            #    records, one provably absent and one unreadable, non-submission is
            #    NOT proven and the step stays in flight.
            plan_e = await _releasing_plan(client, env, attempt)
            ref_ok = _insert_fence_row(env, plan_id=plan_e)
            _insert_fence_row(env, plan_id=plan_e)

            def _inconclusive(*, account_id, client_order_ref, idempotency_key):
                _ = (account_id, idempotency_key)
                if client_order_ref == ref_ok:
                    return {"state": "absent"}
                raise RuntimeError("broker read timed out")

            partial_lookup = LiveRepairService(
                session_factory=env.factory,
                authority_reader=_gone_authority_reader,
                dispatch_fence=LiveDispatchFence(
                    session_factory=env.factory, broker_lookup=_inconclusive
                ),
            )
            with pytest.raises(LiveRepairRefusal) as ctx:
                partial_lookup.abandon_residual(plan_id=plan_e, actor="operator")
            assert ctx.value.reason_code == "LIVE_REPAIR_RELEASING_OUTCOME_UNKNOWN", ctx.value.detail
            assert ctx.value.detail["dispatch_fence"]["reason"] == "BROKER_READ_FAILED", (
                ctx.value.detail
            )
            assert _claim_state(env, plan_e)["state"] == "releasing"

            # -- 6. every row proven absent IS complete evidence.
            plan_f = await _releasing_plan(client, env, attempt)
            _insert_fence_row(env, plan_id=plan_f)
            _insert_fence_row(env, plan_id=plan_f)
            both_absent = LiveRepairService(
                session_factory=env.factory,
                authority_reader=_gone_authority_reader,
                dispatch_fence=LiveDispatchFence(
                    session_factory=env.factory,
                    broker_lookup=lambda **kwargs: {"state": "absent"},
                ),
            )
            disposed_all = both_absent.abandon_residual(plan_id=plan_f, actor="operator")
            assert disposed_all["state"] == "residual_abandoned", disposed_all
            claim = _claim_state(env, plan_f)
            assert claim["detail"]["disposition_record"]["dispatch_fence"]["state"] == "not_attempted"
        finally:
            await client.aclose()

    asyncio.run(_run())
