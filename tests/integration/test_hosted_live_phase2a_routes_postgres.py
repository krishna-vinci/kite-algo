"""Hosted LIVE Phase 2A acceptance: durable multi-step engine, CNC and MIS.

This is the Phase 2A evidence the Phase 1 suites could not give: every scenario
below is driven through the PUBLIC PRODUCTION ROUTES with a credential the
supervisor lifecycle actually minted, and reaches only the FAKE BROKER boundary.

Nothing here hand-inserts the credential, the job, the run binding, the proposal
or the plan. The order is always:

1. the operator logs in through ``/api/auth/login`` and creates the strategy,
   immutable version, admission policy and job through the owner routes;
2. ``POST /api/hosted-supervisor/jobs/{id}/claim`` then ``/prepare`` mints the
   child credential and creates the worker run + run binding;
3. the child submits a ``target_weights`` (CNC) or ``single_instrument`` (MIS)
   proposal over ``/api/algo-workers/worker/proposals`` with that token;
4. the operator reserves, approves and EXECUTES the frozen plan, which
   materializes the durable parent + every step claim atomically and dispatches
   only the steps with no unmet prerequisite;
5. the platform's ordinary ingestion artifacts drive the real
   ``LiveOutcomeConsumer``, whose shared sequence release pass is the production
   ``LivePlanExecutor.release_sequence``.

Only the broker intent handler, the market quote and the margin/funds reading are
faked. Operator auth, the supervisor credential, the hosted-attempt authority,
the proposal/compiler, the readers, the parent protocol, the per-leg claims, the
reservation ledger and the barrier are all production code.
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

SUPERVISOR_CREDENTIAL = "phase2a-supervisor-credential"
APP_JWT_SECRET = "phase2a-jwt-secret"
APP_ADMIN_PASSWORD = "phase2a-operator-password"
RELIANCE = "RELIANCE"
INFY = "INFY"
RELIANCE_TOKEN = 738561
INFY_TOKEN = 408065
IST = timezone(timedelta(hours=5, minutes=30))


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live_phase2a_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, f"{PG_ADMIN.rpartition('/')[0]}/{name}"


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

    # A FRESH disposable database per test. The live outcome consumer and the
    # sequence release pass are DELIBERATELY global (they scan every unresolved
    # live step), so sharing one database across scenarios would let one
    # scenario's withheld leg be released by another scenario's pass - and the
    # evidence would stop being about the plan under test.
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
    broker_user_id = f"phase2a{uuid.uuid4().hex[:6]}"
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
        }
    )
    # The live session reader resolves the account's broker session from the
    # platform's own store; this is that row, not a bypass of the reader.
    from sqlalchemy import text

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.kite_sessions "
                "(session_id, access_token, broker_user_id, created_at) "
                "VALUES ('system', 'phase2a-access-token', :uid, NOW())"
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
            "HOSTED_SUPERVISOR_CREDENTIAL",
        ):
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _funds_boundary(monkeypatch):
    """The second fake: the broker margin/funds READ, exactly as Phase 1 does.

    The route's own margin reader would try to load a live broker session; the
    deployment under test has no broker, so the stated amount stands in for it.
    Every other route reader (session, authority, attribution, ledger) stays real.
    """
    from backend.api.routers import strategies as strategies_module

    monkeypatch.setattr(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 5_000_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )


#: The pinned catalog is seeded ONCE per disposable database: the identity keys
#: are globally unique, so a second insert would (correctly) violate the schema.
_CATALOG: dict = {}


def _seed_catalog(factory) -> dict:
    """A two-member pinned catalog: the portfolio scope this suite trades."""
    if _CATALOG:
        return _CATALOG
    from sqlalchemy import text

    generation = str(uuid.uuid4())
    universe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    instruments = {}
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": generation},
        )
        for symbol, token in ((RELIANCE, RELIANCE_TOKEN), (INFY, INFY_TOKEN)):
            instrument_id = str(uuid.uuid4())
            instruments[symbol] = instrument_id
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                    " lifecycle_status, instrument_type, lot_size, tick_size, "
                    " current_generation_id) "
                    "VALUES (:iid, :key, :key, 'NSE', :symbol, 'active', 'EQ', 1, 0.05, :gen)"
                ),
                {
                    "iid": instrument_id,
                    "key": f"NSE:{symbol}",
                    "symbol": symbol,
                    "gen": generation,
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', 'NSE', :symbol, :token, :gen, TRUE)"
                ),
                {
                    "mid": str(uuid.uuid4()),
                    "iid": instrument_id,
                    "symbol": symbol,
                    "token": token,
                    "gen": generation,
                },
            )
        session.execute(
            text(
                "INSERT INTO public.universes (id, owner_id, name, kind) "
                "VALUES (:uid, 'app:admin', :name, 'explicit')"
            ),
            {"uid": universe_id, "name": f"phase2a-{uuid.uuid4().hex[:6]}"},
        )
        session.execute(
            text(
                "INSERT INTO public.universe_revisions "
                "(id, universe_id, revision, members, member_count, source_generation) "
                "VALUES (:rid, :uid, 1, :members, :count, :gen)"
            ),
            {
                "rid": revision_id,
                "uid": universe_id,
                "members": [RELIANCE, INFY],
                "count": 2,
                "gen": generation,
            },
        )
        session.commit()
    _CATALOG.update(
        {
            "universe_revision_id": revision_id,
            "instruments": instruments,
            "generation": generation,
        }
    )
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
    """A movable clock: authority, quotes and the MIS square-off all read it."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _build_app(factory, broker, clock, *, mis_clock=None, fail_margin=False):
    from fastapi import FastAPI

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import auth as auth_module
    from backend.api.routers import hosted_lifecycle, strategies, worker_auth, worker_execution
    from backend.api.routers import worker_proposals
    from backend.strategies.attribution import SqlAttributionStore
    from backend.strategies.execution import PaperPlanExecutor
    from backend.strategies.live_service import LivePlanExecutor
    from backend.strategies.settlement import ExecutionBarrier

    app = FastAPI(title="hosted live phase2a acceptance")
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
        mis_clock=mis_clock or clock,
        quote_reader=lambda leg: {
            "instrument_id": str(leg.get("instrument_id") or ""),
            "ltp": 1500.0,
            "as_of": clock().isoformat(),
        },
        margin_reader=lambda account, plan: (
            None
            if fail_margin
            else {"usable": 5_000_000.0, "as_of": clock().isoformat()}
        ),
    )
    app.state.live_plan_executor = executor
    return app, executor


def _asgi_client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://phase2a"
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
    """A live strategy + version + policy + job + claim + REAL child credential."""
    created = await client.post(
        "/api/strategies",
        json={
            "name": f"Phase2A {uuid.uuid4().hex[:8]}",
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
            "source": "print('phase2a')",
            "parameters_schema": {"type": "object", "properties": {}},
            "capabilities": {"trade": True, "data": True},
        },
    )
    assert version.status_code < 400, version.text
    version_id = str(version.json().get("version_id") or "1")

    policy = await client.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": 5_000_000.0},
    )
    assert policy.status_code < 400, policy.text

    job = await client.post(
        f"/api/strategies/{strategy_id}/jobs",
        json={
            "version_id": version_id,
            "job_kind": "finite",
            "execution_mode": "live",
            "params": {},
            "idempotency_key": f"phase2a-{uuid.uuid4().hex[:8]}",
        },
    )
    assert job.status_code < 400, job.text
    job_body = job.json().get("job") or {}
    job_id = str(job_body.get("job_id") or job_body.get("id") or "")
    assert job_id, job.text

    claim = await client.post(
        f"/api/hosted-supervisor/jobs/{job_id}/claim",
        json={
            "lease_owner": "phase2a-supervisor",
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
            "lease_owner": "phase2a-supervisor",
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
    response = await client.post(
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
    return response


async def _execute(client, strategy_id, plan_id, *, approve=True):
    reserved = await client.post(f"/api/strategies/{strategy_id}/plans/{plan_id}/reserve")
    assert reserved.status_code < 400, reserved.text
    reservation = reserved.json()
    published = await client.post(
        f"/api/strategies/{strategy_id}/positions/rebuild?environment=live"
    )
    assert published.status_code < 400, published.text
    if approve:
        approved = await client.post(
            f"/api/strategies/{strategy_id}/plans/{plan_id}/approval",
            json={
                "reservation_id": reservation["reservation_id"],
                "validity_seconds": 3600,
            },
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
    product="CNC",
    terminal=True,
):
    """One ordinary ingestion artifact: a trade fill plus the order's state.

    ``terminal=False`` is a PARTIAL fill: the trade is recorded and the order
    projection stays non-terminal, which is exactly what the broker reports while
    the rest of the order can still work.
    """
    from sqlalchemy import text

    status = "COMPLETE" if terminal else "OPEN"
    product = str(product).upper()
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, "
                " strategy_run_id, strategy_family, strategy_name, entry_surface, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'target_weights', :run, 'hosted_plan', "
                " :oid, 'live', 'placed')"
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
                "VALUES (:account, :tid, :oid, :token, 'NSE', :symbol, :product, :side, "
                " :qty, 1500.0, NOW(), true)"
            ),
            {
                "account": account_id,
                "tid": trade_id,
                "oid": order_id,
                "token": token,
                "symbol": symbol,
                "product": product,
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
                " 'NSE', :symbol, :token, :product, :side, NOW()) "
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
                "product": product,
                "side": side,
            },
        )
        session.commit()


def _claim(factory, plan_id, step_no):
    from sqlalchemy import text

    with factory() as session:
        return (
            session.execute(
                text(
                    "SELECT state, broker_order_ids, delta_snapshot, detail "
                    "FROM public.live_plan_submissions "
                    "WHERE plan_id = :pid AND step_no = :step"
                ),
                {"pid": plan_id, "step": int(step_no)},
            )
            .mappings()
            .first()
        )


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


def _attributed(factory, strategy_id, account_id):
    from sqlalchemy import text

    with factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT COALESCE(SUM(net_quantity), 0) FROM strategy_position_projection "
                    "WHERE strategy_id = :sid AND account_id = :account "
                    "AND execution_environment = 'live'"
                ),
                {"sid": strategy_id, "account": account_id},
            ).scalar()
            or 0
        )


def _clock_base() -> datetime:
    """A fixed instant AFTER the NSE MIS square-off, so leases stay valid."""
    real_now = datetime.now(timezone.utc)
    today_ist = real_now.astimezone(IST).date()
    squareoff = datetime(today_ist.year, today_ist.month, today_ist.day, 15, 25, tzinfo=IST)
    return max(real_now, squareoff)


class _Env:
    """The shared per-test environment: disposable DB, app, clock, consumer."""

    def __init__(self, pg, live_env, *, broker=None, fail_margin=False):
        self.factory = pg["factory"]
        self.account_scope = live_env["account_scope"]
        self.broker = broker or _FakeBroker(order_ids=("O-A", "O-B", "O-C", "O-D"))
        # The REQUEST clock (authority, quote freshness, reservation windows) is
        # the real instant: the reservation the routes create is valid for 900s of
        # wall time, and this suite must not depend on the time of day it runs.
        self.clock = _Clock(datetime.now(timezone.utc))
        # The PLATFORM SESSION clock decides the MIS square-off. It is pinned to an
        # instant AFTER the schedule so the square-off is provably due; a test that
        # wants the "not due" branch moves ``env.session.now`` earlier.
        self.session = _Clock(_clock_base())
        self.lease_until = max(datetime.now(timezone.utc), self.clock()) + timedelta(hours=12)
        self.app, self.executor = _build_app(
            self.factory,
            self.broker,
            self.clock,
            mis_clock=self.session,
            fail_margin=fail_margin,
        )

    def consumer(self):
        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        return LiveOutcomeConsumer(
            session_factory=self.factory,
            clock=self.clock,
            sequence_releaser=self.executor.release_sequence,
        )


# ---------------------------------------------------------------- CNC basket


def test_cnc_full_snapshot_sequences_reductions_before_dependent_increases(pg, live_env):
    """The multi-step engine, end to end, over the public production routes.

    A full-snapshot portfolio target is an ORDERED set: the reducing leg is
    materialized ready and the increasing leg ``withheld`` behind it. A partial
    sell keeps the buy withheld and holds the parent's reservation. S2 releases
    only after the confirmed full sell, fresh quote/funds evidence and one
    keyed authorization event. A restart cannot send the buy again, and the
    parent reservation remains held until every leg is terminal.
    """
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")))
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]
            # -- open a two-member book: INFY held, RELIANCE explicitly zero.
            entry = await _submit_proposal(
                client,
                attempt,
                {
                    "target_kind": "target_weights",
                    "payload": {
                        "universe_revision_id": revision_id,
                        "target_weights": {RELIANCE: 0.0, INFY: 0.02},
                        "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
                    },
                },
                account_scope=env.account_scope,
            )
            assert entry.status_code < 400, entry.text
            entry_plan = entry.json()["plan"]
            entry_resolved = entry_plan["resolved_plan"]
            # FULL SNAPSHOT: the omitted member is an EXPLICIT zero, not an absence.
            zeros = {
                leg["tradingsymbol"]: leg["explicit_zero"] for leg in entry_resolved["legs"]
            }
            assert zeros == {RELIANCE: True, INFY: False}, zeros

            executed, _reservation = await _execute(client, strategy_id, entry_plan["plan_id"])
            assert executed.status_code < 400, executed.text
            assert executed.json()["broker_order_ids"] == ["OID-INFY-ENTRY"], executed.text
            entry_execution = _execution(env.factory, entry_plan["plan_id"])
            assert entry_execution is not None, "no durable parent for the one-leg plan"
            assert entry_execution["lane"] == "target_weights"
            assert entry_execution["state"] == "executing", dict(entry_execution)

            entry_order = executed.json()["broker_order_ids"][0]
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=entry_order,
                trade_id="TR-INFY-1",
                quantity=66,
                side="BUY",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            consumer = env.consumer()
            counts = await consumer.poll_once()
            assert counts["filled"] == 1, counts
            assert _attributed(env.factory, strategy_id, env.account_scope) == 66

            # -- the rebalancing plan: SELL INFY to zero, BUY RELIANCE.
            rebalance = await _submit_proposal(
                client,
                attempt,
                {
                    "target_kind": "target_weights",
                    "payload": {
                        "universe_revision_id": revision_id,
                        "target_weights": {RELIANCE: 0.04, INFY: 0.0},
                        "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
                    },
                },
                account_scope=env.account_scope,
            )
            assert rebalance.status_code < 400, rebalance.text
            plan = rebalance.json()["plan"]
            plan_id = plan["plan_id"]
            executed, reservation = await _execute(client, strategy_id, plan_id)
            assert executed.status_code < 400, executed.text
            body = executed.json()
            # ONLY the reduction was sent: the increase has an unmet prerequisite.
            assert body["broker_order_ids"] == ["OID-INFY-EXIT"], body
            assert len(env.broker.calls) == 2, [c[0].payload for c in env.broker.calls]
            sell_intent, _ = env.broker.calls[-1]
            assert sell_intent.payload["order"]["transaction_type"] == "SELL"
            assert sell_intent.payload["order"]["tradingsymbol"] == INFY

            parent = _execution(env.factory, plan_id)
            assert parent["lane"] == "target_weights"
            spec_by_ref = {row["step_ref"]: row for row in parent["step_spec"]}
            assert len(parent["step_spec"]) == 2, parent["step_spec"]
            sell_spec = next(
                row for row in parent["step_spec"] if row["tradingsymbol"] == INFY
            )
            buy_spec = next(
                row for row in parent["step_spec"] if row["tradingsymbol"] == RELIANCE
            )
            assert sell_spec["depends_on"] == [], sell_spec
            assert buy_spec["depends_on"] == [sell_spec["step_no"]], buy_spec
            assert buy_spec["release_rule"] == "staged_funding_gate"
            assert set(spec_by_ref) == {sell_spec["step_ref"], buy_spec["step_ref"]}

            buy_claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert buy_claim["state"] == "withheld", dict(buy_claim)
            assert list(buy_claim["broker_order_ids"]) == []
            # The withheld leg is IN-FLIGHT work for the settlement barrier.
            from backend.strategies.settlement import enumerate_inflight_work

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
            assert "live_submission_withheld" in kinds, kinds
            assert "live_plan_execution_executing" in kinds, kinds

            # -- a PARTIAL sell keeps the buy withheld and the capacity held.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-INFY-2",
                quantity=30,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            counts = await consumer.poll_once()
            assert counts["partial"] == 1, counts
            assert counts["sequence_released"] == 0, counts
            assert len(env.broker.calls) == 2, "a partial fill released a dependent leg"
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"
            parent = _execution(env.factory, plan_id)
            assert parent["state"] == "executing", dict(parent)
            held = env.executor.ledger.for_plan(plan_id)
            assert str(held["status"]) in ("active", "renewed"), held

            # -- the CONFIRMED full sell fill gives the gate its first executable
            # fact: only now may it read funds and authorize the exact buy.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-INFY-3",
                quantity=36,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await consumer.poll_once()
            assert counts["filled"] == 1, counts
            assert counts["sequence_released"] == 1, counts
            assert len(env.broker.calls) == 3, [c[0].payload for c in env.broker.calls]
            buy_intent, _ = env.broker.calls[-1]
            assert buy_intent.payload["order"]["transaction_type"] == "BUY"
            assert buy_intent.payload["order"]["tradingsymbol"] == RELIANCE
            buy_claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert buy_claim["state"] == "pending", dict(buy_claim)
            assert list(buy_claim["broker_order_ids"]) == ["OID-REL-BUY"]

            with env.factory() as session:
                from sqlalchemy import text

                reservations = session.execute(
                    text(
                        "SELECT reservation_id FROM public.strategy_reservations "
                        "WHERE plan_id = :plan_id"
                    ),
                    {"plan_id": plan_id},
                ).scalars().all()
                authorizations = session.execute(
                    text(
                        "SELECT detail FROM public.strategy_reservation_events "
                        "WHERE reservation_id = :rid "
                        "AND event = 'staged_increase_authorized'"
                    ),
                    {"rid": str(reservations[0])},
                ).scalars().all()
            assert len(authorizations) == 1
            assert authorizations[0]["authorization_key"] == f"{plan_id}:{buy_spec['step_no']}"
            assert authorizations[0]["quote"]["ltp"] == 1500.0
            assert authorizations[0]["funds_evidence_sha256"]

            # -- restart / duplicate consumer: the durable claim is no longer
            # withheld, so neither path reauthorizes or retransmits the buy.
            calls_before = len(env.broker.calls)
            again = await env.executor.release_sequence()
            assert again["released"] == 0, again
            restarted = LiveOutcomeConsumer(
                session_factory=env.factory,
                clock=env.clock,
                sequence_releaser=_fresh_executor(env).release_sequence,
            )
            counts = await restarted.poll_once()
            assert counts["sequence_released"] == 0, counts
            assert len(env.broker.calls) == calls_before
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "pending"
            assert reservation["execution_environment"] == "live"

            # -- the buy's terminal proof is the LAST funding leg. Until it arrives,
            # the parent (and therefore the reservation) remains in flight.
            buy_order = list(buy_claim["broker_order_ids"])[0]
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=buy_order,
                trade_id="TR-REL-1",
                quantity=int(buy_spec["quantity"]),
                side="BUY",
                symbol=RELIANCE,
                token=RELIANCE_TOKEN,
            )
            counts = await consumer.poll_once()
            assert counts["filled"] == 1, counts
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "filled"
            return {"plan_id": plan_id, "buy_step": buy_spec["step_no"], "strategy_id": strategy_id}
        finally:
            await client.aclose()

    result = asyncio.run(_run())
    # Only the terminal BUY let the parent settle; a real fill consumes capacity
    # rather than releasing it, even though a keyed authorization already exists.
    parent = _execution(env.factory, result["plan_id"])
    assert parent["state"] == "settled", dict(parent)
    reservation = env.executor.ledger.for_plan(result["plan_id"])
    assert reservation["status"] == "consumed", dict(reservation)


def _fresh_executor(env):
    """A SECOND executor instance: another process reading the same durable rows."""
    _app, executor = _build_app(env.factory, env.broker, env.clock, mis_clock=env.session)
    return executor


async def _open_position(
    client, env, attempt, revision_id, *, order_id="OID-INFY-ENTRY", product="CNC"
):
    """Open a real attributed INFY book so a later plan must REDUCE it."""
    entry = await _submit_proposal(
        client,
        attempt,
        {
            "target_kind": "target_weights",
            "payload": {
                "universe_revision_id": revision_id,
                "product": product,
                "target_weights": {RELIANCE: 0.0, INFY: 0.02},
                "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
            },
        },
        account_scope=env.account_scope,
    )
    assert entry.status_code < 400, entry.text
    plan_id = entry.json()["plan"]["plan_id"]
    executed, _reservation = await _execute(client, attempt["strategy_id"], plan_id)
    assert executed.status_code < 400, executed.text
    assert executed.json()["broker_order_ids"] == [order_id], executed.text
    _ingest_fill(
        env.factory,
        account_id=env.account_scope,
        run_id=attempt["run_id"],
        order_id=order_id,
        trade_id=f"TR-OPEN-{uuid.uuid4().hex[:6]}",
        quantity=66,
        side="BUY",
        symbol=INFY,
        token=INFY_TOKEN,
        product=product,
    )
    counts = await env.consumer().poll_once()
    assert counts["filled"] == 1, counts
    assert _attributed(env.factory, attempt["strategy_id"], env.account_scope) == 66


async def _rebalance_plan(client, env, attempt, revision_id, *, product="CNC"):
    """Materialize a two-leg plan: SELL INFY to zero, BUY RELIANCE."""
    response = await _submit_proposal(
        client,
        attempt,
        {
            "target_kind": "target_weights",
            "payload": {
                "universe_revision_id": revision_id,
                "product": product,
                "target_weights": {RELIANCE: 0.04, INFY: 0.0},
                "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
            },
        },
        account_scope=env.account_scope,
    )
    assert response.status_code < 400, response.text
    plan_id = response.json()["plan"]["plan_id"]
    executed, _reservation = await _execute(client, attempt["strategy_id"], plan_id)
    assert executed.status_code < 400, executed.text
    parent = _execution(env.factory, plan_id)
    sell_spec = next(row for row in parent["step_spec"] if row["tradingsymbol"] == INFY)
    buy_spec = next(
        row for row in parent["step_spec"] if row["tradingsymbol"] == RELIANCE
    )
    return plan_id, sell_spec, buy_spec, executed.json()


def test_cnc_release_refuses_when_the_attempt_authority_is_gone(pg, live_env):
    """An expired/revoked attempt refuses the dependent leg AND names the reason.

    The sell fills, so the prerequisite IS met - and the release still places
    nothing, because the persisted authority no longer re-derives. That is the
    difference between "sequence the legs" and "keep trading".
    """
    from sqlalchemy import text

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(client, env, attempt, revision_id)
            plan_id, _sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert body["broker_order_ids"] == ["OID-INFY-EXIT"], body
            calls_before = len(env.broker.calls)

            # The attempt's credential is revoked, so the plan's authority is GONE
            # before the sell's fill is confirmed.
            with env.factory() as session:
                session.execute(
                    text(
                        "UPDATE public.algo_worker_tokens SET status = 'revoked' "
                        "WHERE token_id = (SELECT token_id FROM public.algo_worker_runs "
                        "WHERE strategy_run_id = :run)"
                    ),
                    {"run": attempt["run_id"]},
                )
                session.commit()

            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-EXIT-GONE",
                quantity=66,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await env.consumer().poll_once()
            # The reduction still resolved (it was already sent); the dependent
            # increase did NOT, and the refusal is named on the claim.
            assert counts["filled"] == 1, counts
            assert counts["sequence_released"] == 0, counts
            assert counts["sequence_blocked"] >= 1, counts
            assert len(env.broker.calls) == calls_before, "an expired attempt placed an order"
            buy_claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert buy_claim["state"] == "withheld", dict(buy_claim)
            assert buy_claim["detail"]["release_blocked"] == "TOKEN_NOT_ACTIVE", dict(
                buy_claim["detail"]
            )
            assert list(buy_claim["broker_order_ids"]) == []
            return plan_id
        finally:
            await client.aclose()

    plan_id = asyncio.run(_run())
    # The plan is NOT settled and NOT quietly resolved: the withheld leg keeps it
    # in flight until an operator disposes of it.
    parent = _execution(env.factory, plan_id)
    assert parent["state"] in ("executing", "planned", "blocked"), dict(parent)


def test_non_staged_uncertain_release_is_never_retransmitted(pg, live_env):
    """A transport-uncertain release keeps its work and is NEVER repeated.

    The basket is NON-CNC (MIS product), so it never enters the C1.1 staged
    funding lane: its dependent buy keeps the generic ``all_prerequisites_filled``
    rule and still releases once the reduction fills - no behaviour change. (A
    STAGED CNC basket's dependent buy is now gated; S1 pins that in the sibling
    test above.)
    """
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(
            order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY"), fail_on=3
        ),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(client, env, attempt, revision_id, product="MIS")
            plan_id, _sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id, product="MIS"
            )
            assert body["broker_order_ids"] == ["OID-INFY-EXIT"], body
            assert buy_spec["release_rule"] == "all_prerequisites_filled", buy_spec
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-EXIT-UNC",
                quantity=66,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                product="MIS",
            )
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 1, counts
            claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert claim["state"] == "uncertain", dict(claim)
            calls_after_uncertain = len(env.broker.calls)
            assert calls_after_uncertain == 3, calls_after_uncertain

            # Two more passes - including from a SECOND executor instance - must
            # not place the order again: an unconfirmed outcome is recovery work.
            await env.executor.release_sequence()
            await _fresh_executor(env).release_sequence()
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 0, counts
            assert len(env.broker.calls) == calls_after_uncertain, [
                call[0].payload for call in env.broker.calls
            ]
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "uncertain"
        finally:
            await client.aclose()

    asyncio.run(_run())


# ---------------------------------------------------------------------- MIS


def test_mis_squareoff_is_released_by_the_platform_clock_only(pg, live_env):
    """MIS: the platform's own square-off clock releases the exit, never a guess.

    A risk-REDUCING MIS step is materialized ``withheld``. Before the exchange's
    scheduled square-off it stays withheld with a NAMED blocker and nothing is
    sent; once the schedule is due, the step is released, sized to the strategy's
    ATTRIBUTED quantity, and the platform records its own square-off evidence.
    """
    from sqlalchemy import text

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-MIS-ENTRY", "OID-MIS-EXIT")))
    _seed_catalog(env.factory)

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]

            entry = await _submit_proposal(
                client,
                attempt,
                {
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": INFY_TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": INFY,
                        "product": "MIS",
                        "target_quantity": 5,
                        "reference_price": 1500.0,
                    },
                },
                account_scope=env.account_scope,
            )
            assert entry.status_code < 400, entry.text
            entry_plan = entry.json()["plan"]
            assert entry_plan["plan_kind"] == "single_instrument"
            assert entry_plan["resolved_plan"]["legs"][0]["product"] == "MIS"
            executed, _reservation = await _execute(client, strategy_id, entry_plan["plan_id"])
            assert executed.status_code < 400, executed.text
            assert executed.json()["broker_order_ids"] == ["OID-MIS-ENTRY"], executed.text
            intent, _ = env.broker.calls[-1]
            assert intent.payload["order"]["product"] == "MIS"
            assert intent.payload["order"]["transaction_type"] == "BUY"

            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-MIS-ENTRY",
                trade_id="TR-MIS-1",
                quantity=5,
                side="BUY",
                symbol=INFY,
                token=INFY_TOKEN,
                product="MIS",
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts
            assert _attributed(env.factory, strategy_id, env.account_scope) == 5

            # -- the intraday exit: materialized withheld, nothing sent.
            exit_proposal = await _submit_proposal(
                client,
                attempt,
                {
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": INFY_TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": INFY,
                        "product": "MIS",
                        "target_quantity": 0,
                        "reference_price": 1500.0,
                    },
                },
                account_scope=env.account_scope,
            )
            assert exit_proposal.status_code < 400, exit_proposal.text
            exit_plan_id = exit_proposal.json()["plan"]["plan_id"]
            # Attributed +5 is not enough on its own: the exit needs no capacity.
            exit_executed, _res = await _execute(client, strategy_id, exit_plan_id)
            assert exit_executed.status_code < 400, exit_executed.text
            assert exit_executed.json()["broker_order_ids"] == [], exit_executed.text
            calls_after_freeze = len(env.broker.calls)
            assert calls_after_freeze == 1, calls_after_freeze
            parent = _execution(env.factory, exit_plan_id)
            assert parent["lane"] == "mis", dict(parent)
            spec = parent["step_spec"][0]
            assert spec["release_rule"] == "mis_squareoff", spec
            claim = _claim(env.factory, exit_plan_id, spec["step_no"])
            assert claim["state"] == "withheld", dict(claim)

            # -- BEFORE the schedule: refused by NAME, and NOTHING is placed.
            today_ist = env.clock().astimezone(IST).date()
            before = datetime(
                today_ist.year, today_ist.month, today_ist.day, 11, 0, tzinfo=IST
            )
            env.session.now = before
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 0, counts
            assert counts["sequence_blocked"] >= 1, {
                key: counts.get(key)
                for key in ("sequence_released", "sequence_blocked", "sequence_error")
            }
            assert len(env.broker.calls) == calls_after_freeze, "a guessed close was placed"
            claim = _claim(env.factory, exit_plan_id, spec["step_no"])
            assert claim["state"] == "withheld", dict(claim)
            assert claim["detail"]["release_blocked"] == "MIS_SQUAREOFF_NOT_DUE", dict(
                claim["detail"]
            )
            with env.factory() as session:
                evidence = session.execute(
                    text(
                        "SELECT COUNT(*) FROM public.strategy_squareoff_evidence "
                        "WHERE strategy_run_id = :run"
                    ),
                    {"run": attempt["run_id"]},
                ).scalar()
            assert int(evidence or 0) == 0, "square-off evidence without a square-off"

            # -- AT the schedule: the platform's own clock releases the exit.
            env.session.now = datetime(
                today_ist.year, today_ist.month, today_ist.day, 15, 25, tzinfo=IST
            )
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 1, counts
            assert len(env.broker.calls) == calls_after_freeze + 1
            exit_intent, _ = env.broker.calls[-1]
            assert exit_intent.payload["order"]["transaction_type"] == "SELL"
            assert exit_intent.payload["order"]["quantity"] == 5
            assert exit_intent.payload["order"]["product"] == "MIS"

            with env.factory() as session:
                row = (
                    session.execute(
                        text(
                            "SELECT outcome, product, exchange, exit_claim_id, detail "
                            "FROM public.strategy_squareoff_evidence "
                            "WHERE strategy_run_id = :run"
                        ),
                        {"run": attempt["run_id"]},
                    )
                    .mappings()
                    .first()
                )
            assert row is not None, "no platform square-off evidence"
            assert str(row["outcome"]) == "squared_off", dict(row)
            assert str(row["product"]) == "MIS"
            assert str(row["exchange"]) == "NSE"
            assert str(row["exit_claim_id"]) == spec["step_ref"]
            detail = row["detail"] if isinstance(row["detail"], dict) else {}
            assert detail.get("release_authority") == "squareoff_clock", detail
            assert detail.get("released_quantity") == 5, detail

            # -- the exit fill closes the book and settles the parent.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-MIS-EXIT",
                trade_id="TR-MIS-2",
                quantity=5,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                product="MIS",
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts
            assert _attributed(env.factory, strategy_id, env.account_scope) == 0
            settled = _execution(env.factory, exit_plan_id)
            assert settled["state"] == "settled", dict(settled)
            return exit_plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())
# --------------------------------------------------- residual disposition


async def _drive_step_to_repair_required(env, *, order_id, filled, cancelled_residual):
    """A real live step whose broker order was terminally CANCELLED with a residual."""
    from sqlalchemy import text

    with env.factory() as session:
        session.execute(
            text(
                "UPDATE public.order_state_projection SET latest_status = 'CANCELLED', "
                " last_seen_filled_quantity = :filled, terminal = true "
                "WHERE account_id = :account AND order_id = :oid"
            ),
            {"account": env.account_scope, "oid": order_id, "filled": int(filled)},
        )
        session.commit()
    _ = cancelled_residual
    counts = await env.consumer().poll_once()
    assert counts["repair_required"] == 1, counts


async def _materialized_parent_with_withheld_leg(client, env, attempt, revision_id):
    """A parent whose SELL leg filled (66) and whose BUY leg is still withheld."""
    await _open_position(client, env, attempt, revision_id)
    plan_id, _sell_spec, buy_spec, body = await _rebalance_plan(
        client, env, attempt, revision_id
    )
    assert body["broker_order_ids"] == ["OID-INFY-EXIT"], body
    return plan_id, buy_spec


def test_residual_disposition_retains_the_capacity_of_other_legs(pg, live_env):
    """Abandoning ONE residual never releases another leg's allocation.

    Leg 1 fills and leg 2 is still ``withheld``, so the parent's reservation must
    stay exactly where it is: releasing it would un-fund the leg that has not been
    submitted yet. The disposition is bounded, idempotent and audited once.
    """
    from sqlalchemy import text

    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")))
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, buy_spec = await _materialized_parent_with_withheld_leg(
                client, env, attempt, revision_id
            )
            # The reduction partially fills and is then terminally cancelled: the
            # step is a residual an operator must decide about.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-RESIDUAL-1",
                quantity=40,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            await _drive_step_to_repair_required(
                env, order_id="OID-INFY-EXIT", filled=40, cancelled_residual=26
            )

            # While the attempt is live the disposition is refused outright.
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )
            with pytest.raises(LiveRepairRefusal) as ctx:
                service.abandon_residual(
                    plan_id=plan_id, actor="operator", reason="operator decision"
                )
            assert ctx.value.reason_code == "LIVE_AUTHORITY_STILL_ACTIVE", ctx.value.detail

            # The operator stops the attempt: the authority is provably gone.
            _stop_attempt(env, attempt)
            result = service.abandon_residual(
                plan_id=plan_id, actor="operator", reason="residual will not be worked"
            )
            assert result["state"] == "residual_abandoned", result
            capacity = result["disposition"]
            # The OTHER leg is still withheld, so nothing may be released.
            assert capacity["capacity_state"] == "retained", capacity
            assert capacity["capacity_released"] is False, capacity
            assert capacity["capacity_retained"] is True, capacity
            assert buy_spec["step_no"] in capacity["outstanding_legs"], capacity
            reservation = service.ledger.for_plan(plan_id)
            assert str(reservation["status"]) in ("active", "renewed"), reservation
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"

            # Idempotent: the second call reports the SAME decision and writes
            # neither a second audit row nor a second barrier event.
            barrier = _barrier_event_count(env, plan_id)
            again = service.abandon_residual(plan_id=plan_id, actor="operator")
            assert again["idempotent"] is True, again
            assert _audit_rows(env, plan_id) == 1
            assert _barrier_event_count(env, plan_id) == barrier
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_residual_disposition_consumes_capacity_a_real_fill_backs(pg, live_env):
    """A partial fill means the reservation backs exposure: it is consumed, not freed."""
    from backend.strategies.live_repair import LiveRepairService

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")))
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            # A ONE-leg plan, so abandoning its residual leaves every leg terminal:
            # that is the only shape in which the settlement rule may act.
            entry = await _submit_proposal(
                client,
                attempt,
                {
                    "target_kind": "target_weights",
                    "payload": {
                        "universe_revision_id": revision_id,
                        "target_weights": {RELIANCE: 0.0, INFY: 0.02},
                        "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
                    },
                },
                account_scope=env.account_scope,
            )
            assert entry.status_code < 400, entry.text
            plan_id = entry.json()["plan"]["plan_id"]
            executed, _reservation = await _execute(client, attempt["strategy_id"], plan_id)
            assert executed.status_code < 400, executed.text
            assert executed.json()["broker_order_ids"] == ["OID-INFY-ENTRY"], executed.text
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-ENTRY",
                trade_id="TR-RESIDUAL-2",
                quantity=40,
                side="BUY",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            await _drive_step_to_repair_required(
                env, order_id="OID-INFY-ENTRY", filled=40, cancelled_residual=26
            )
            _stop_attempt(env, attempt)
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )
            result = service.abandon_residual(plan_id=plan_id, actor="operator")
            capacity = result["disposition"]
            # 40 of the order FILLED, so the reservation now backs real exposure:
            # it is CONSUMED (the ledger cannot release part of it) rather than
            # handed back to the owner as if nothing had happened.
            assert capacity["capacity_state"] == "settled", capacity
            assert capacity["capacity_consumed"] is True, capacity
            assert capacity["capacity_released"] is False, capacity
            reservation = service.ledger.for_plan(plan_id)
            assert str(reservation["status"]) == "consumed", reservation
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_residual_disposition_rolls_back_when_the_audit_write_fails(pg, live_env):
    """An audit failure must leave the step untouched - no partial disposition."""
    from backend.strategies.live_repair import LIVE_ENVIRONMENT, LiveRepairService

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")))
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, _buy_spec = await _materialized_parent_with_withheld_leg(
                client, env, attempt, revision_id
            )
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-RESIDUAL-3",
                quantity=40,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            await _drive_step_to_repair_required(
                env, order_id="OID-INFY-EXIT", filled=40, cancelled_residual=26
            )
            _stop_attempt(env, attempt)
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )
            barrier_before = _barrier_event_count(env, plan_id)

            def _boom(*_args, **_kwargs):
                raise RuntimeError("audit store unavailable")

            service._record_trail = _boom  # type: ignore[assignment]
            with pytest.raises(Exception) as ctx:
                service.abandon_residual(plan_id=plan_id, actor="operator")
            assert getattr(ctx.value, "reason_code", "") == "LIVE_REPAIR_DISPOSITION_FAILED"
            assert "audit store unavailable" in str(
                (getattr(ctx.value, "detail", {}) or {}).get("error") or ""
            ), (ctx.value, getattr(ctx.value, "detail", None))

            # NOTHING stuck: the step is still repairable, no barrier event landed
            # and no release happened.
            states = {row["step_no"]: row["state"] for row in _claims(env.factory, plan_id)}
            assert "repair_required" in states.values(), states
            assert _barrier_event_count(env, plan_id) == barrier_before
            assert _audit_rows(env, plan_id) == 0
            reservation = service.ledger.for_plan(plan_id)
            assert str(reservation["status"]) in ("active", "renewed"), reservation
            _ = LIVE_ENVIRONMENT
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_residual_disposition_refuses_unknown_authority_evidence(pg, live_env):
    """UNREADABLE authority is not EVIDENCE OF ABSENCE: the disposition is refused."""
    from sqlalchemy import text

    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")))
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, _buy_spec = await _materialized_parent_with_withheld_leg(
                client, env, attempt, revision_id
            )
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-RESIDUAL-4",
                quantity=40,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            await _drive_step_to_repair_required(
                env, order_id="OID-INFY-EXIT", filled=40, cancelled_residual=26
            )

            # (a) an unreadable reader refuses as UNKNOWN, not as "gone".
            def _unreadable(*_args, **_kwargs):
                raise RuntimeError("authority source offline")

            service = LiveRepairService(
                session_factory=env.factory,
                clock=env.clock,
                authority_reader=_unreadable,
            )
            with pytest.raises(LiveRepairRefusal) as ctx:
                service.abandon_residual(plan_id=plan_id, actor="operator")
            assert ctx.value.reason_code == "LIVE_REPAIR_AUTHORITY_UNKNOWN", ctx.value.detail

            # (b) a reader that raises the platform's OWN authority refusal for a
            # reason that is not proof of absence is ALSO unknown.
            from backend.strategies.live_authority import LiveAuthorityRefusal

            def _inconclusive(*_args, **_kwargs):
                raise LiveAuthorityRefusal("HOSTED_LEASE_MISSING", {"job_id": "j"})

            service = LiveRepairService(
                session_factory=env.factory,
                clock=env.clock,
                authority_reader=_inconclusive,
            )
            with pytest.raises(LiveRepairRefusal) as ctx2:
                service.abandon_residual(plan_id=plan_id, actor="operator")
            assert ctx2.value.reason_code == "LIVE_REPAIR_AUTHORITY_UNKNOWN", ctx2.value.detail

            # (c) a MISSING bound run is unknown too: there is nothing to prove
            # absence with, so the disposition stays refused.
            # The platform cannot produce a plan whose bound run was deleted (the
            # bindings are immutable), so this branch is driven by the smallest
            # inconsistent pair the schema DOES allow: a frozen plan whose envelope
            # names a run that has no row.
            orphan_plan_id = _seed_orphan_repair_step(env, attempt)
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock
            )
            with pytest.raises(LiveRepairRefusal) as ctx3:
                service.abandon_residual(plan_id=orphan_plan_id, actor="operator")
            assert ctx3.value.reason_code == "LIVE_REPAIR_AUTHORITY_UNKNOWN", ctx3.value.detail
            assert ctx3.value.detail["authority"]["reason"] == "RUN_MISSING", ctx3.value.detail
            with env.factory() as session:
                state = session.execute(
                    text(
                        "SELECT state FROM public.live_plan_submissions "
                        "WHERE plan_id = :pid"
                    ),
                    {"pid": orphan_plan_id},
                ).scalar()
            assert str(state) == "repair_required", state
            states = {row["step_no"]: row["state"] for row in _claims(env.factory, plan_id)}
            assert "repair_required" in states.values(), states
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_cnc_release_blocks_when_the_book_moved_beyond_this_plans_own_fills(pg, live_env):
    """The tolerated exposure pin is PROVEN, never assumed.

    The approval pins the book the owner approved against. A multi-leg plan
    legitimately moves it -- but so does ANOTHER plan of the same run, a manual
    trade or a corporate action, and then the frozen delta over-targets a book it
    no longer describes. This test moves the parent's own instrument for a reason
    that is NOT this plan's confirmed fills and proves the release is refused by
    NAME, with nothing placed.
    """
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(client, env, attempt, revision_id)
            plan_id, _sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert body["broker_order_ids"] == ["OID-INFY-EXIT"], body
            calls_before = len(env.broker.calls)

            # The reducing leg fills AS CONFIRMED ...
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-INFY-EXIT",
                trade_id="TR-EXIT-OWN",
                quantity=66,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            # ... and something ELSE also moved the same instrument on the same
            # book: an order this plan never submitted (another plan of the run, or
            # a manual trade), owned by the same run so it lands in the strategy's
            # own attributed projection.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-OTHER-PLAN",
                trade_id="TR-OTHER-PLAN",
                quantity=10,
                side="BUY",
                symbol=INFY,
                token=INFY_TOKEN,
            )

            counts = await env.consumer().poll_once()
            # The reduction resolved; the dependent increase did NOT.
            assert counts["filled"] == 1, counts
            assert counts["sequence_released"] == 0, counts
            assert counts["sequence_blocked"] >= 1, counts
            assert len(env.broker.calls) == calls_before, "a moved book released a frozen delta"
            claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert claim["state"] == "withheld", dict(claim)
            assert (
                claim["detail"]["release_blocked"]
                == "LIVE_SEQUENCE_BOOK_MOVED_BEYOND_OWN_FILLS"
            ), dict(claim["detail"])
            proof = dict(claim["detail"].get("release_blocked_detail") or {})
            assert proof.get("identity") == (
                "frozen_current + own_filled_delta == current_attributed"
            ), proof
            mismatch = (proof.get("mismatches") or [{}])[0]
            assert int(mismatch.get("frozen_current_quantity")) == 66, proof
            assert int(mismatch.get("own_filled_delta")) == -66, proof
            assert int(mismatch.get("expected_quantity")) == 0, proof
            assert int(mismatch.get("actual_quantity")) == 10, proof
            assert list(claim["broker_order_ids"]) == []
            return plan_id
        finally:
            await client.aclose()

    plan_id = asyncio.run(_run())
    parent = _execution(env.factory, plan_id)
    assert parent["state"] in ("executing", "planned", "blocked"), dict(parent)


def test_mis_squareoff_is_refused_when_the_child_authority_is_gone(pg, live_env):
    """A dead child's square-off is NOT this path's to place.

    The hosted release pass needs the child's PERSISTED live authority (run open +
    live, active token with ``intents:submit``, a live hosting lease at the current
    attempt). Once that is gone the pass refuses a named blocker and places
    nothing: it never mints or renews new-risk authority for a dead child. Risk
    reduction for that case belongs to the platform's OWN control-plane exit
    (``WorkerProtectionRuntime`` -> ``submit_worker_protection_exit`` ->
    ``exit_control_strategy`` -> ``_exit_live_worker_run``), which submits under a
    control-plane token holding ``runs:exit`` rather than the child's credential.
    This test pins the boundary: hosted refuses, and no square-off evidence is
    invented by a refused release.
    """
    from sqlalchemy import text

    env = _Env(pg, live_env, broker=_FakeBroker(order_ids=("OID-MIS-ENTRY", "OID-MIS-EXIT")))
    _seed_catalog(env.factory)

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            strategy_id = attempt["strategy_id"]

            def _mis_payload(target_quantity):
                return {
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": INFY_TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": INFY,
                        "product": "MIS",
                        "target_quantity": int(target_quantity),
                        "reference_price": 1500.0,
                    },
                }

            entry = await _submit_proposal(
                client, attempt, _mis_payload(5), account_scope=env.account_scope
            )
            assert entry.status_code < 400, entry.text
            executed, _reservation = await _execute(
                client, strategy_id, entry.json()["plan"]["plan_id"]
            )
            assert executed.status_code < 400, executed.text
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="OID-MIS-ENTRY",
                trade_id="TR-MIS-DEAD-1",
                quantity=5,
                side="BUY",
                symbol=INFY,
                token=INFY_TOKEN,
                product="MIS",
            )
            counts = await env.consumer().poll_once()
            assert counts["filled"] == 1, counts

            exit_proposal = await _submit_proposal(
                client, attempt, _mis_payload(0), account_scope=env.account_scope
            )
            assert exit_proposal.status_code < 400, exit_proposal.text
            exit_plan_id = exit_proposal.json()["plan"]["plan_id"]
            exit_executed, _res2 = await _execute(client, strategy_id, exit_plan_id)
            assert exit_executed.status_code < 400, exit_executed.text
            assert exit_executed.json()["broker_order_ids"] == [], exit_executed.text
            calls_after_freeze = len(env.broker.calls)
            parent = _execution(env.factory, exit_plan_id)
            spec = parent["step_spec"][0]
            assert _claim(env.factory, exit_plan_id, spec["step_no"])["state"] == "withheld"

            # The attempt ends: the credential is revoked and the job is stopped.
            _stop_attempt(env, attempt)

            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 0, counts
            assert counts["sequence_blocked"] >= 1, counts
            assert len(env.broker.calls) == calls_after_freeze, (
                "a dead child's square-off was placed by the hosted path"
            )
            claim = _claim(env.factory, exit_plan_id, spec["step_no"])
            assert claim["state"] == "withheld", dict(claim)
            assert claim["detail"]["release_blocked"] in {
                "TOKEN_NOT_ACTIVE",
                "HOSTED_STOP_REQUESTED",
            }, dict(claim["detail"])
            assert list(claim["broker_order_ids"]) == []
            with env.factory() as session:
                evidence = session.execute(
                    text(
                        "SELECT COUNT(*) FROM public.strategy_squareoff_evidence "
                        "WHERE strategy_run_id = :run"
                    ),
                    {"run": attempt["run_id"]},
                ).scalar()
            assert int(evidence or 0) == 0, "a refused release invented square-off evidence"
            return exit_plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_a_partial_or_unknown_reduction_never_releases_the_dependent_buy(pg, live_env):
    """S3 §5: a reduction that has not resolved keeps the staged buy withheld - silently.

    A partial fill and a transport-unknown reduction are both "not filled yet": the
    buy stays withheld, no order is placed for it, and because the reduction may
    still resolve NO blocker is recorded while the protocol is still waiting. The
    fake broker is never asked for a second order.
    """
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(
            order_ids=("C11-P1-ENTRY", "C11-P1-EXIT", "C11-P2-ENTRY", "C11-P2-EXIT"),
            fail_on=4,
        ),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            # -- plan 1: the reduction PARTIALLY fills (still live work).
            partial_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, partial_attempt, revision_id, order_id="C11-P1-ENTRY"
            )
            partial_plan, partial_sell, partial_buy, body = await _rebalance_plan(
                client, env, partial_attempt, revision_id
            )
            assert body["broker_order_ids"] == ["C11-P1-EXIT"], body
            assert partial_sell["release_rule"] == "immediate", partial_sell
            assert partial_buy["release_rule"] == "staged_funding_gate", partial_buy
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=partial_attempt["run_id"],
                order_id="C11-P1-EXIT",
                trade_id=f"TR-C11-PARTIAL-{uuid.uuid4().hex[:6]}",
                quantity=30,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            counts = await env.consumer().poll_once()
            assert counts["partial"] == 1, counts
            assert counts["sequence_released"] == 0, counts
            partial_claim = _claim(env.factory, partial_plan, partial_buy["step_no"])
            assert partial_claim["state"] == "withheld", dict(partial_claim)
            assert "release_blocked" not in dict(partial_claim["detail"] or {}), dict(
                partial_claim["detail"]
            )
            assert list(partial_claim["broker_order_ids"]) == []
            calls_after_partial = len(env.broker.calls)

            # -- plan 2: the reduction's SEND outcome is UNKNOWN (transport lost).
            unknown_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, unknown_attempt, revision_id, order_id="C11-P2-ENTRY"
            )
            unknown_plan, _unknown_sell, unknown_buy, body = await _rebalance_plan(
                client, env, unknown_attempt, revision_id
            )
            # The send raised, so the claim carries no order reference at all.
            assert not body.get("broker_order_ids"), body
            # (``_FakeBroker`` records the ATTEMPT before it raises, so this count
            # includes the failed reduction send.)
            calls_after_reductions = len(env.broker.calls)
            assert calls_after_reductions == calls_after_partial + 2, [
                call[0].payload for call in env.broker.calls
            ]
            sell_claim = _claim(
                env.factory, unknown_plan, _unknown_sell["step_no"]
            )
            assert sell_claim["state"] == "uncertain", dict(sell_claim)
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 0, counts
            unknown_buy_claim = _claim(env.factory, unknown_plan, unknown_buy["step_no"])
            assert unknown_buy_claim["state"] == "withheld", dict(unknown_buy_claim)
            assert "release_blocked" not in dict(unknown_buy_claim["detail"] or {}), dict(
                unknown_buy_claim["detail"]
            )
            # Two more passes (one from a SECOND executor instance) change nothing:
            # an unresolved reduction is waiting, and an uncertain send is never
            # repeated.
            await env.executor.release_sequence()
            await _fresh_executor(env).release_sequence()
            assert len(env.broker.calls) == calls_after_reductions, [
                call[0].payload for call in env.broker.calls
            ]
            # The DEPENDENT increase was never sent for either plan.
            assert [
                call
                for call in env.broker.calls
                if call[0].payload["order"]["transaction_type"] == "BUY"
                and call[0].payload["order"]["tradingsymbol"] == RELIANCE
            ] == []
            assert _claim(env.factory, partial_plan, partial_buy["step_no"])["state"] == "withheld"
            assert _claim(env.factory, unknown_plan, unknown_buy["step_no"])["state"] == "withheld"
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_a_rejected_reduction_names_the_blocked_steps_and_only_dead_authority_abandons_the_buy(
    pg, live_env
):
    """S3 §5 + decision 4: the owner is told WHICH reduction blocked the buy.

    A terminally REJECTED reduction can never fund its dependent buy, so the buy's
    claim names ``STAGED_FUNDING_REDUCTION_NOT_CONFIRMED`` together with the blocked
    funding step. The buy is still never auto-released: only the bounded disposition
    resolves it, only once the plan's authority is PROVABLY gone, and then the
    parent's own rule releases the unused capacity (nothing filled).
    """
    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("C11-R-ENTRY", "C11-R-EXIT")),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, attempt, revision_id, order_id="C11-R-ENTRY"
            )
            plan_id, sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert body["broker_order_ids"] == ["C11-R-EXIT"], body
            calls_after_reduction = len(env.broker.calls)

            # The broker terminally REJECTS the sale: no fill, no residual.
            _ingest_rejected_order(
                env,
                order_id="C11-R-EXIT",
                run_id=attempt["run_id"],
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await env.consumer().poll_once()
            assert counts["rejected"] == 1, counts
            assert counts["sequence_released"] == 0, counts
            assert counts["sequence_blocked"] >= 1, counts
            assert len(env.broker.calls) == calls_after_reduction, [
                call[0].payload for call in env.broker.calls
            ]
            buy_claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert buy_claim["state"] == "withheld", dict(buy_claim)
            assert list(buy_claim["broker_order_ids"]) == []
            buy_detail = dict(buy_claim["detail"])
            assert (
                buy_detail["release_blocked"] == "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED"
            ), buy_detail
            blocked = dict(buy_detail["release_blocked_detail"])
            assert blocked["blocked_funding_steps"] == [int(sell_spec["step_no"])], blocked
            assert blocked["funding_leg_states"] == {
                str(sell_spec["step_no"]): "rejected"
            }, blocked

            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )
            # While the attempt is LIVE the bounded disposition refuses: another leg
            # (or the buy's own run) might still be worked.
            with pytest.raises(LiveRepairRefusal) as ctx:
                service.abandon_staged_dependent(plan_id=plan_id, actor="operator")
            assert ctx.value.reason_code == "LIVE_AUTHORITY_STILL_ACTIVE", ctx.value.detail

            # UNREADABLE authority is not evidence of absence: still refused.
            def _unreadable(*_args, **_kwargs):
                raise RuntimeError("authority source offline")

            unknown = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_unreadable
            )
            with pytest.raises(LiveRepairRefusal) as ctx2:
                unknown.abandon_staged_dependent(plan_id=plan_id, actor="operator")
            assert ctx2.value.reason_code == "LIVE_REPAIR_AUTHORITY_UNKNOWN", ctx2.value.detail
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"

            # The operator stops the attempt: the authority is provably gone.
            _stop_attempt(env, attempt)
            result = service.abandon_staged_dependent(
                plan_id=plan_id,
                step_no=buy_spec["step_no"],
                actor="operator",
                reason="the reduction was rejected; nothing will fund this buy",
            )
            assert result["state"] == "residual_abandoned", result
            record = dict(result["disposition"])
            assert record["disposition"] == "staged_dependent_abandoned", record
            assert record["prior_state"] == "withheld", record
            assert record["send_outcome"] == "not_sent", record
            assert record["blocker"] == "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED", record
            assert record["blocked_funding_steps"] == [int(sell_spec["step_no"])], record
            assert [leg["state"] for leg in record["funding_legs"]] == ["rejected"], record
            assert record["funding_legs"][0]["filled_quantity"] == 0, record
            # NOTHING filled anywhere in the parent: the unused capacity goes back.
            assert record["capacity_state"] == "settled", record
            assert record["capacity_released"] is True, record
            reservation = env.executor.ledger.for_plan(plan_id)
            assert str(reservation["status"]) == "released", reservation
            claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert claim["state"] == "residual_abandoned", dict(claim)
            assert list(claim["broker_order_ids"]) == []
            assert _execution(env.factory, plan_id)["state"] == "settled"

            # Exactly one audit row and one barrier write for the decision.
            assert _audit_rows(env, plan_id) == 1
            barrier_events = _barrier_event_count(env, plan_id)
            again = service.abandon_staged_dependent(
                plan_id=plan_id, step_no=buy_spec["step_no"], actor="operator"
            )
            assert again["idempotent"] is True, again
            assert again["disposition"]["disposition"] == "staged_dependent_abandoned"
            assert _audit_rows(env, plan_id) == 1
            assert _barrier_event_count(env, plan_id) == barrier_events
            # And the buy was NEVER sent: the fake broker only ever saw the reduction
            # (plus the opening buy of the book).
            assert len(env.broker.calls) == calls_after_reduction, [
                call[0].payload for call in env.broker.calls
            ]
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_a_staged_dependent_disposition_consumes_capacity_a_real_reduction_fill_backs(
    pg, live_env
):
    """S3 §5: a residual disposition consumes, never frees, capacity a fill backs.

    The reduction partially fills and is then terminally cancelled, so the owner
    dispositions the residual: the buy stays withheld (never auto-released) and,
    once the authority is gone, the bounded dependent disposition resolves it. 40
    shares really filled, so the parent's capacity is CONSUMED rather than handed
    back as if nothing had happened.
    """
    from backend.strategies.live_repair import LiveRepairService

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("C11-C-ENTRY", "C11-C-EXIT")),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, attempt, revision_id, order_id="C11-C-ENTRY"
            )
            plan_id, sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert body["broker_order_ids"] == ["C11-C-EXIT"], body
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="C11-C-EXIT",
                trade_id=f"TR-C11-RESIDUAL-{uuid.uuid4().hex[:6]}",
                quantity=40,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
                terminal=False,
            )
            await _drive_step_to_repair_required(
                env, order_id="C11-C-EXIT", filled=40, cancelled_residual=26
            )
            _stop_attempt(env, attempt)
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )

            # The REDUCTION's residual is dispositioned; the buy stays withheld and
            # the capacity is retained for it.
            sell_result = service.abandon_residual(
                plan_id=plan_id, step_no=sell_spec["step_no"], actor="operator"
            )
            assert sell_result["disposition"]["capacity_retained"] is True, sell_result
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"

            # The next pass names the DEAD funding leg on the buy - and STILL sends
            # nothing: the disposition is the operator's decision, not the pass's.
            calls_before = len(env.broker.calls)
            counts = await env.executor.release_sequence()
            assert counts["released"] == 0, counts
            assert len(env.broker.calls) == calls_before, [
                call[0].payload for call in env.broker.calls
            ]
            buy_claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert buy_claim["state"] == "withheld", dict(buy_claim)
            buy_detail = dict(buy_claim["detail"])
            assert (
                buy_detail["release_blocked"] == "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED"
            ), buy_detail
            assert dict(buy_detail["release_blocked_detail"])["blocked_funding_steps"] == [
                int(sell_spec["step_no"])
            ]

            result = service.abandon_staged_dependent(
                plan_id=plan_id, step_no=buy_spec["step_no"], actor="operator"
            )
            record = dict(result["disposition"])
            assert record["disposition"] == "staged_dependent_abandoned", record
            assert record["funding_legs"][0]["state"] == "residual_abandoned", record
            assert record["funding_legs"][0]["filled_quantity"] == 40, record
            assert record["capacity_consumed"] is True, record
            assert record["capacity_released"] is False, record
            reservation = env.executor.ledger.for_plan(plan_id)
            assert str(reservation["status"]) == "consumed", reservation
            assert _execution(env.factory, plan_id)["state"] == "settled"
            assert len(env.broker.calls) == calls_before, [
                call[0].payload for call in env.broker.calls
            ]
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_a_staged_dependent_buy_is_never_retransmitted_and_the_fence_adopts_its_order(
    pg, live_env
):
    """S3 §5: an uncertain buy is never repeated; a proven-sent one is ADOPTED.

    A handler failure leaves the buy ``uncertain`` with work and capacity held, and
    neither a restart nor a SECOND executor instance ever re-sends it. The bounded
    repair for a LOST RESPONSE is the pre-send fence: the platform's own durable
    record names the order the broker DID accept, so the claim adopts that reference
    and ingestion owns it. Nothing is placed twice.
    """
    from backend.strategies.live_dispatch_fence import LiveDispatchFence
    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(
            order_ids=(
                "C11-U-ENTRY",
                "C11-U-EXIT",
                "C11-U-BUY",
                "C11-F-ENTRY",
                "C11-F-EXIT",
                "C11-F-BUY",
            ),
            fail_on=3,
        ),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, attempt, revision_id, order_id="C11-U-ENTRY"
            )
            plan_id, _sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert body["broker_order_ids"] == ["C11-U-EXIT"], body
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id="C11-U-EXIT",
                trade_id=f"TR-C11-UNC-{uuid.uuid4().hex[:6]}",
                quantity=66,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 1, counts
            claim = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert claim["state"] == "uncertain", dict(claim)
            assert list(claim["broker_order_ids"]) == []
            calls_after_uncertain = len(env.broker.calls)
            assert calls_after_uncertain == 3, calls_after_uncertain

            # A restart - including a SECOND executor instance - never repeats it.
            await env.executor.release_sequence()
            await _fresh_executor(env).release_sequence()
            counts = await env.consumer().poll_once()
            assert counts["sequence_released"] == 0, counts
            assert len(env.broker.calls) == calls_after_uncertain, [
                call[0].payload for call in env.broker.calls
            ]
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "uncertain"

            # An uncertain buy is neither abandoned nor re-sent: the repair path
            # refuses it by name.
            _stop_attempt(env, attempt)
            service = LiveRepairService(
                session_factory=env.factory, clock=env.clock, authority_reader=_live_reader(env)
            )
            with pytest.raises(LiveRepairRefusal) as ctx:
                service.abandon_staged_dependent(
                    plan_id=plan_id, step_no=buy_spec["step_no"], actor="operator"
                )
            assert ctx.value.reason_code == "LIVE_REPAIR_NOT_REQUIRED", ctx.value.detail
            with pytest.raises(LiveRepairRefusal) as ctx2:
                service.abandon_residual(
                    plan_id=plan_id, step_no=buy_spec["step_no"], actor="operator"
                )
            assert ctx2.value.reason_code == "LIVE_REPAIR_NOT_REQUIRED", ctx2.value.detail
            assert len(env.broker.calls) == calls_after_uncertain

            # -- the LOST-RESPONSE twin: a claim parked in the 'releasing' window
            # whose durable pre-send record names the order the broker accepted.
            fence_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, fence_attempt, revision_id, order_id="C11-F-ENTRY"
            )
            fence_plan, _fence_sell, fence_buy, body = await _rebalance_plan(
                client, env, fence_attempt, revision_id
            )
            assert body["broker_order_ids"] == ["C11-F-EXIT"], body
            calls_before_adopt = len(env.broker.calls)
            _park_claim_in_releasing(env, plan_id=fence_plan, step_no=fence_buy["step_no"])
            _insert_pre_send_fence_row(
                env,
                plan_id=fence_plan,
                step_no=fence_buy["step_no"],
                broker_order_id="C11-F-ADOPTED",
            )
            repairing = LiveRepairService(
                session_factory=env.factory,
                clock=env.clock,
                dispatch_fence=LiveDispatchFence(session_factory=env.factory),
            )
            adopted = repairing.abandon_residual(
                plan_id=fence_plan, step_no=fence_buy["step_no"], actor="operator"
            )
            assert adopted["state"] == "pending", adopted
            assert adopted["recovered_order_ids"] == ["C11-F-ADOPTED"], adopted
            fence_claim = _claim(env.factory, fence_plan, fence_buy["step_no"])
            assert fence_claim["state"] == "pending", dict(fence_claim)
            assert list(fence_claim["broker_order_ids"]) == ["C11-F-ADOPTED"]
            # The adoption PLACED NOTHING: the fake broker's call count is unchanged.
            assert len(env.broker.calls) == calls_before_adopt, [
                call[0].payload for call in env.broker.calls
            ]
            # A repeat finds ordinary in-flight work and REFUSES: nothing to repair
            # remains, and the adopted reference is never rewritten or re-sent.
            with pytest.raises(LiveRepairRefusal) as ctx3:
                repairing.abandon_residual(
                    plan_id=fence_plan, step_no=fence_buy["step_no"], actor="operator"
                )
            assert ctx3.value.reason_code == "LIVE_REPAIR_NOT_REQUIRED", ctx3.value.detail
            fence_claim = _claim(env.factory, fence_plan, fence_buy["step_no"])
            assert fence_claim["state"] == "pending", dict(fence_claim)
            assert list(fence_claim["broker_order_ids"]) == ["C11-F-ADOPTED"]
            assert len(env.broker.calls) == calls_before_adopt
        finally:
            await client.aclose()

    asyncio.run(_run())


def _ingest_rejected_order(
    env,
    *,
    order_id: str,
    run_id: str,
    symbol: str,
    token: int,
    side: str = "SELL",
    product: str = "CNC",
) -> None:
    """One ordinary ingestion artifact for a terminally REJECTED order: no trades.

    A rejected order has no fill rows at all, so the attribution row that binds the
    order to this plan's run plus a terminal ``order_state_projection`` row IS the
    whole evidence the live outcome consumer reads.
    """
    from sqlalchemy import text

    with env.factory() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, "
                " strategy_run_id, strategy_family, strategy_name, entry_surface, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'target_weights', :run, 'hosted_plan', "
                " :oid, 'live', 'placed')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": f"KA-REJ-{uuid.uuid4().hex[:8]}",
                "account": env.account_scope,
                "run": run_id,
                "oid": order_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, "
                " needs_reconcile, terminal, exchange, tradingsymbol, instrument_token, "
                " product, transaction_type, updated_at) "
                "VALUES (:account, :oid, 'REJECTED', NOW(), 0, false, false, true, 'NSE', "
                " :symbol, :token, :product, :side, NOW())"
            ),
            {
                "account": env.account_scope,
                "oid": order_id,
                "symbol": symbol,
                "token": token,
                "product": str(product).upper(),
                "side": side,
            },
        )
        session.commit()


def _park_claim_in_releasing(env, *, plan_id: str, step_no: int) -> None:
    """Write the crash window a release pass can leave behind: ``releasing``, no order.

    A test can only reach that shape by writing it - the process that leaves it
    behind is dead - and everything that DECIDES about it afterwards reads the
    platform's own durable rows (the claim and the pre-send fence).
    """
    from sqlalchemy import text

    with env.factory() as session:
        session.execute(
            text(
                "UPDATE public.live_plan_submissions SET state = 'releasing', "
                " broker_order_ids = '[]'::jsonb, updated_at = NOW() "
                "WHERE plan_id = :pid AND step_no = :step"
            ),
            {"pid": plan_id, "step": int(step_no)},
        )
        session.commit()


def _insert_pre_send_fence_row(
    env, *, plan_id: str, step_no: int, broker_order_id: str | None = None
) -> None:
    """The durable pre-send record this plan STEP's broker write would have created."""
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
        session.execute(
            text(
                "INSERT INTO public.live_order_intents "
                "(intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, idempotency_key, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'target_weights', 'c11', "
                " 'hosted_plan', :key, :oid, 'live', 'pending')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": f"KAC{uuid.uuid4().hex[:6].upper()}",
                "account": str(plan["account_id"]),
                "run": f"run_{str(plan['strategy_id'])[:12]}",
                "key": f"live-plan:{plan_id}:step:{int(step_no)}",
                "oid": broker_order_id,
            },
        )
        session.commit()


def _seed_orphan_repair_step(env, attempt) -> str:
    """A frozen plan + repair_required claim whose bound run row does not exist.

    The schema admits it (``strategy_proposals.strategy_run_id`` has no foreign
    key, because the envelope is written before the run in some flows), and it is
    exactly the "incomplete evidence" shape the disposition must refuse.
    """
    import json as _json

    from sqlalchemy import text

    proposal_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    with env.factory() as session:
        generation = session.execute(
            text(
                "SELECT id FROM public.instrument_catalog_generations "
                "ORDER BY published_at DESC LIMIT 1"
            )
        ).scalar()
        session.execute(
            text(
                "INSERT INTO public.strategy_proposals (proposal_id, strategy_id, "
                " account_id, evaluation_id, evaluation_kind, strategy_run_id, "
                " target_kind, payload, payload_sha256, status) "
                "VALUES (:pid, :sid, :account, :eval, 'run_now', :run, "
                " 'single_instrument', CAST(:payload AS jsonb), 'sha', 'validated')"
            ),
            {
                "pid": proposal_id,
                "sid": attempt["strategy_id"],
                "account": env.account_scope,
                "eval": f"orphan-{uuid.uuid4().hex[:8]}",
                "run": f"run_missing_{uuid.uuid4().hex[:8]}",
                "payload": _json.dumps({"orphan": True}),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_plans (plan_id, proposal_id, strategy_id, "
                " account_id, plan_kind, plan_hash, logical_plan, resolved_plan, "
                " pinned_catalog_generation) "
                "VALUES (:plan, :pid, :sid, :account, 'single_instrument', 'sha', "
                " CAST(:logical AS jsonb), CAST(:resolved AS jsonb), :gen)"
            ),
            {
                "plan": plan_id,
                "pid": proposal_id,
                "sid": attempt["strategy_id"],
                "account": env.account_scope,
                "logical": _json.dumps({"target_kind": "single_instrument"}),
                "resolved": _json.dumps({"target_kind": "single_instrument", "legs": []}),
                "gen": generation,
            },
        )
        session.execute(
            text(
                "INSERT INTO public.live_plan_submissions (submission_id, plan_id, "
                " step_no, step_ref, strategy_id, account_id, execution_environment, "
                " state, broker_order_ids, delta_snapshot, detail) "
                "VALUES (:sid, :plan, 1, :ref, :strat, :account, 'live', "
                " 'repair_required', '[]'::jsonb, CAST(:delta AS jsonb), "
                " CAST(:detail AS jsonb))"
            ),
            {
                "sid": f"live_sub_{uuid.uuid4().hex}",
                "plan": plan_id,
                "ref": f"live-plan:{plan_id}:step:1",
                "strat": attempt["strategy_id"],
                "account": env.account_scope,
                "delta": _json.dumps({"quantity": 10}),
                "detail": _json.dumps(
                    {"filled_quantity": 4, "residual_quantity": 6, "ordered_quantity": 10}
                ),
            },
        )
        session.commit()
    return plan_id


def _live_reader(env):
    """The PRODUCTION authority reader bound to this test's disposable DB."""
    from backend.strategies.live_authority import live_authority_reader

    return live_authority_reader(env.factory)


def _stop_attempt(env, attempt) -> None:
    """The operator's stop, as the platform records it: revoked credential + reaped lease."""
    from sqlalchemy import text

    with env.factory() as session:
        session.execute(
            text(
                "UPDATE public.algo_worker_tokens SET status = 'revoked' WHERE token_id = "
                "(SELECT token_id FROM public.algo_worker_runs WHERE strategy_run_id = :run)"
            ),
            {"run": attempt["run_id"]},
        )
        session.execute(
            text(
                "UPDATE public.strategy_jobs SET desired_state = 'stopped' "
                "WHERE run_id = :run"
            ),
            {"run": attempt["run_id"]},
        )
        session.commit()


def _barrier_event_count(env, plan_id) -> int:
    from sqlalchemy import text

    with env.factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM public.strategy_execution_barrier_events "
                    "WHERE detail ->> 'plan_id' = CAST(:pid AS text) "
                    "AND execution_environment = 'live'"
                ),
                {"pid": plan_id},
            ).scalar()
            or 0
        )


def _audit_rows(env, plan_id) -> int:
    from sqlalchemy import text

    with env.factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM public.strategy_plan_execution_events "
                    "WHERE plan_id = :pid AND event = 'residual_abandoned'"
                ),
                {"pid": plan_id},
            ).scalar()
            or 0
        )
