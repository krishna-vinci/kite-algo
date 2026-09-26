"""Hosted LIVE acceptance driven entirely through the PUBLIC PRODUCTION ROUTES.

This is the Phase 1 acceptance evidence the earlier suite did not have. Nothing
here hand-inserts the credential, the job, the run binding, the proposal or the
plan: they are produced by the platform's own routes, in this order:

1. the operator logs in through ``/api/auth/login`` and creates the strategy,
   immutable version, admission policy and job through the owner routes;
2. the **supervisor lifecycle route** claims the job and calls
   ``POST /api/hosted-supervisor/jobs/{id}/prepare``, which mints the child
   credential and creates the worker run + run binding (the child token is shown
   exactly once, and the test uses that token, never a manufactured one);
3. the child submits its ``single_instrument`` proposal over
   ``/api/algo-workers/worker/proposals`` with that token, producing the frozen,
   run-bound plan;
4. the operator reserves, approves and EXECUTES that plan over the public route,
   which dispatches through the live executor to the FAKE BROKER BOUNDARY only;
5. the platform's ordinary ingestion artifacts (``live_order_intents``,
   ``order_trade_fills``, ``order_state_projection``) drive the real
   ``LiveOutcomeConsumer``: attributed +10, then a reducing exit to 0;
6. the operator stops the attempt and reconciles it over
   ``POST .../jobs/{id}/reconciliation``, which records the durable settlement
   proof, unblocks, closes the bound worker run and appends the audit row in ONE
   transaction.

Only the broker intent handler, the market quote and the margin/funds reading
are faked. Operator auth, the supervisor credential, the hosted-attempt
authority, the readers, the claim/ledger/barrier, the reconciliation collector
and the reconciliation route are all production code.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)

SUPERVISOR_CREDENTIAL = "live-acceptance-supervisor-credential"
APP_JWT_SECRET = "live-acceptance-jwt-secret"
APP_ADMIN_PASSWORD = "live-acceptance-operator-password"
SYMBOL = "RELIANCE"
TOKEN = 738561


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live_routes_{uuid.uuid4().hex[:10]}"
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


@pytest.fixture(scope="module")
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    try:
        # The migration environment resolves its URL from ``DATABASE_URL``; bind
        # it to THIS disposable database before Alembic runs.
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
        # The disposable database is dropped even when migration or a test
        # raises, so a failed run never leaves state behind.
        _drop_db(name)


@pytest.fixture(scope="module")
def live_env(pg):
    """The isolated deployment environment: disposable DB + live enabled.

    ``HOSTED_LIVE_ENABLED=true`` is this test deployment's own configuration; it
    is never set for production by this test, and it is removed at teardown.
    """
    broker_user_id = f"liveacc{uuid.uuid4().hex[:6]}"
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


def _seed_catalog(factory) -> str:
    from sqlalchemy import text

    generation = str(uuid.uuid4())
    instrument_id = str(uuid.uuid4())
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": generation},
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_records "
                "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
                " instrument_type, lot_size, tick_size, current_generation_id) "
                "VALUES (:iid, 'NSE:RELIANCE', 'NSE:RELIANCE', 'NSE', 'RELIANCE', 'active', 'EQ', 1, 0.05, :gen)"
            ),
            {"gen": generation, "iid": instrument_id},
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_broker_mappings "
                "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                " valid_from_generation, is_current) "
                "VALUES (:mid, :iid, 'kite', 'NSE', 'RELIANCE', :token, :gen, TRUE)"
            ),
            {"token": TOKEN, "gen": generation, "iid": instrument_id, "mid": str(uuid.uuid4())},
        )
        session.commit()
    return instrument_id


class _FakeBroker:
    """The ONLY broker boundary: it accepts and returns an order id. Never fills."""

    def __init__(self, order_ids):
        self.calls = []
        self._order_ids = list(order_ids)

    async def handle(self, intent, *, context=None):
        self.calls.append((intent, dict(context or {})))
        index = min(len(self.calls) - 1, len(self._order_ids) - 1)
        return {"result": {"order_id": self._order_ids[index]}}


def _build_app(factory, broker, *, now):
    from fastapi import FastAPI

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import auth as auth_module
    from backend.api.routers import hosted_lifecycle, strategies, worker_auth, worker_execution
    from backend.api.routers import worker_proposals
    from backend.strategies.attribution import SqlAttributionStore
    from backend.strategies.execution import PaperPlanExecutor
    from backend.strategies.live_service import LivePlanExecutor
    from backend.strategies.settlement import ExecutionBarrier

    app = FastAPI(title="hosted live route acceptance")
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
    # The paper runtime stays unset: a live plan must never reach it, and a
    # missing paper runtime makes that mistake a loud 503 rather than a silent
    # paper execution.
    app.state.paper_runtime_service = None
    app.state.paper_plan_executor = PaperPlanExecutor(session_factory=factory)
    # The FAKE BROKER plus a deterministic quote and a stated margin. The
    # authority/positions/fills/session readers remain the production ones.
    app.state.live_plan_executor = LivePlanExecutor(
        session_factory=factory,
        intent_handler=broker,
        quote_reader=lambda leg: {
            "instrument_id": str(leg.get("instrument_id") or ""),
            "ltp": 1500.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
        margin_reader=lambda account, plan: {
            "usable": 500_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )
    _ = now
    return app


def _asgi_client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://live-acceptance"
    )


async def _operator_client(app):
    # Importing the app can load a ``.env`` (``load_dotenv`` fills UNSET names
    # only), so this isolated instance's own operator credential is re-asserted
    # at the moment of login. The login route still verifies it: nothing here is
    # bypassed.
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


async def _post(client, path, *, json_body=None, headers=None):
    response = await client.post(path, json=json_body, headers=headers)
    return response


def _ingest_fill(factory, *, account_id, run_id, order_id, trade_id, quantity, side):
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'single_instrument', :run, 'hosted_plan', "
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
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, :tid, :oid, :token, 'NSE', :symbol, 'CNC', :side, :qty, 1500.0, NOW(), true)"
            ),
            {
                "account": account_id,
                "tid": trade_id,
                "oid": order_id,
                "token": TOKEN,
                "symbol": SYMBOL,
                "side": side,
                "qty": int(quantity),
            },
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, needs_reconcile, "
                " terminal, exchange, tradingsymbol, instrument_token, product, transaction_type, updated_at) "
                "VALUES (:account, :oid, 'COMPLETE', NOW(), :qty, false, false, true, 'NSE', :symbol, "
                " :token, 'CNC', :side, NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = 'COMPLETE', "
                " last_seen_filled_quantity = :qty, terminal = true"
            ),
            {
                "account": account_id,
                "oid": order_id,
                "qty": int(quantity),
                "symbol": SYMBOL,
                "token": TOKEN,
                "side": side,
            },
        )
        session.commit()


def _mark_account_ingest_complete(factory, account_id):
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO account_ingest_state (account_id, last_complete_ingest_at, ingest_generation, status) "
                "VALUES (:account, NOW(), 1, 'idle') "
                "ON CONFLICT (account_id) DO UPDATE SET last_complete_ingest_at = NOW(), status = 'idle'"
            ),
            {"account": account_id},
        )
        session.commit()


def _attributed_quantity(factory, strategy_id, account_id):
    from sqlalchemy import text

    with factory() as session:
        value = session.execute(
            text(
                "SELECT COALESCE(SUM(net_quantity), 0) FROM strategy_position_projection "
                "WHERE strategy_id = :sid AND account_id = :account AND execution_environment = 'live'"
            ),
            {"sid": strategy_id, "account": account_id},
        ).scalar()
    return int(value or 0)


def test_hosted_live_acceptance_through_public_routes(pg, live_env):
    """prepare-issued credential -> live execute -> ingestion -> exit -> reconcile."""
    from sqlalchemy import text

    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    factory = pg["factory"]
    account_scope = live_env["account_scope"]
    broker_user_id = live_env["broker_user_id"]
    instrument_id = _seed_catalog(factory)

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.kite_sessions (session_id, access_token, broker_user_id, created_at) "
                "VALUES ('system', 'acceptance-access-token', :uid, NOW())"
            ),
            {"uid": broker_user_id},
        )
        session.commit()

    broker = _FakeBroker(order_ids=("OID-ACC-ENTRY", "OID-ACC-EXIT"))
    app = _build_app(factory, broker, now=datetime.now(timezone.utc))

    async def _run():
        client = await _operator_client(app)
        try:
            # ---------------------------------------------------------- setup
            created = await _post(
                client,
                "/api/strategies",
                json_body={
                    "name": "Live route acceptance",
                    "description": None,
                    "execution_mode": "live",
                    "job_kind": "finite",
                    "account_scope": account_scope,
                    "max_duration_s": 3600,
                    "progress_deadline_s": 900,
                    "stale_exit_policy": "none",
                },
            )
            assert created.status_code < 400, created.text
            strategy_id = str(created.json()["strategy_id"])

            version = await _post(
                client,
                f"/api/strategies/{strategy_id}/versions",
                json_body={
                    "source": "print('live acceptance')",
                    "parameters_schema": {"type": "object", "properties": {}},
                    "capabilities": {"trade": True, "data": True},
                },
            )
            assert version.status_code < 400, version.text
            version_id = str(version.json().get("version_id") or "1")

            policy = await client.put(
                f"/api/strategies/{strategy_id}/admission-policy",
                json={"allocation_inr": 1_000_000.0},
            )
            assert policy.status_code < 400, policy.text

            job = await _post(
                client,
                f"/api/strategies/{strategy_id}/jobs",
                json_body={
                    "version_id": version_id,
                    "job_kind": "finite",
                    "execution_mode": "live",
                    "params": {},
                    "idempotency_key": f"live-acc-{uuid.uuid4().hex[:8]}",
                },
            )
            assert job.status_code < 400, job.text
            job_body = job.json().get("job") or {}
            job_id = str(job_body.get("job_id") or job_body.get("id") or "")
            assert job_id, job.text

            # ------------------------- real supervisor claim + prepare route
            claim = await _post(
                client,
                f"/api/hosted-supervisor/jobs/{job_id}/claim",
                json_body={
                    "lease_owner": "acceptance-supervisor",
                    "expected_lease_epoch": 0,
                    "expected_attempt": 1,
                    "lease_until": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                },
                headers=_supervisor_headers(),
            )
            assert claim.status_code < 400, claim.text

            prepared = await _post(
                client,
                f"/api/hosted-supervisor/jobs/{job_id}/prepare",
                json_body={
                    "lease_owner": "acceptance-supervisor",
                    "lease_epoch": int(claim.json()["lease_epoch"]),
                    "attempt": 1,
                },
                headers=_supervisor_headers(),
            )
            assert prepared.status_code < 400, prepared.text
            prepared_body = prepared.json()
            # The credential is the one the lifecycle just minted, not a fixture.
            child_token = str(prepared_body["worker_token"])
            run_id = str(prepared_body["run_id"])
            assert prepared_body["execution_mode"] == "live"
            # The run has claimed a session nonce, so the child (and this test,
            # posing as it) must present it: freshness is part of the route
            # authority, not a formality.
            child_headers = {
                "Authorization": f"Bearer {child_token}",
                "X-Worker-Session-Nonce": str(prepared_body["session_nonce"]),
            }

            # -------------------------- child proposal over the worker route
            proposal = await _post(
                client,
                "/api/algo-workers/worker/proposals",
                json_body={
                    "evaluation_id": f"eval-{uuid.uuid4().hex[:8]}",
                    "evaluation_kind": "run_now",
                    "strategy_run_id": run_id,
                    "strategy_id": strategy_id,
                    "account_scope": account_scope,
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": SYMBOL,
                        "product": "CNC",
                        "target_quantity": 10,
                        "reference_price": 1500.0,
                    },
                },
                headers=child_headers,
            )
            assert proposal.status_code < 400, proposal.text
            plan = proposal.json()["plan"]
            entry_plan_id = str(plan["plan_id"])
            assert plan["resolved_plan"]["legs"][0]["instrument_id"] == instrument_id

            # ---------------------------------------- entry through the route
            reserved = await _post(client, f"/api/strategies/{strategy_id}/plans/{entry_plan_id}/reserve")
            assert reserved.status_code < 400, reserved.text
            reservation = reserved.json()
            assert reservation["execution_environment"] == "live", reservation

            # The live book must be PUBLISHED before the approval pins it: a
            # never-published book is unknown, not flat, so the executor refuses
            # to size a step against it. The publication is the production
            # on-demand recompute, not a fixture insert.
            published = await _post(
                client, f"/api/strategies/{strategy_id}/positions/rebuild?environment=live"
            )
            assert published.status_code < 400, published.text
            assert published.json()["projection_version"] >= 1, published.text

            approved = await _post(
                client,
                f"/api/strategies/{strategy_id}/plans/{entry_plan_id}/approval",
                json_body={
                    "reservation_id": reservation["reservation_id"],
                    "validity_seconds": 900,
                },
            )
            assert approved.status_code < 400, approved.text

            entry = await _post(
                client, f"/api/strategies/{strategy_id}/plans/{entry_plan_id}/execute"
            )
            assert entry.status_code < 400, entry.text
            assert entry.json()["broker_order_ids"] == ["OID-ACC-ENTRY"], entry.text

            # ----------------------------- ordinary ingestion + real consumer
            _ingest_fill(
                factory,
                account_id=account_scope,
                run_id=run_id,
                order_id="OID-ACC-ENTRY",
                trade_id="TR-ACC-1",
                quantity=10,
                side="BUY",
            )
            _mark_account_ingest_complete(factory, account_scope)
            consumer = LiveOutcomeConsumer(session_factory=factory)
            counts = await consumer.poll_once()
            assert counts["filled"] == 1, counts
            assert _attributed_quantity(factory, strategy_id, account_scope) == 10

            # --------------------------------------- reducing exit (target 0)
            exit_proposal = await _post(
                client,
                "/api/algo-workers/worker/proposals",
                json_body={
                    "evaluation_id": f"eval-{uuid.uuid4().hex[:8]}",
                    "evaluation_kind": "run_now",
                    "strategy_run_id": run_id,
                    "strategy_id": strategy_id,
                    "account_scope": account_scope,
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": SYMBOL,
                        "product": "CNC",
                        "target_quantity": 0,
                        "reference_price": 1500.0,
                    },
                },
                headers=child_headers,
            )
            assert exit_proposal.status_code < 400, exit_proposal.text
            exit_plan_id = str(exit_proposal.json()["plan"]["plan_id"])

            exit_reserved = await _post(
                client, f"/api/strategies/{strategy_id}/plans/{exit_plan_id}/reserve"
            )
            assert exit_reserved.status_code < 400, exit_reserved.text
            exit_approved = await _post(
                client,
                f"/api/strategies/{strategy_id}/plans/{exit_plan_id}/approval",
                json_body={
                    "reservation_id": exit_reserved.json()["reservation_id"],
                    "validity_seconds": 900,
                },
            )
            assert exit_approved.status_code < 400, exit_approved.text
            exit_result = await _post(
                client, f"/api/strategies/{strategy_id}/plans/{exit_plan_id}/execute"
            )
            assert exit_result.status_code < 400, exit_result.text
            assert exit_result.json()["broker_order_ids"] == ["OID-ACC-EXIT"], exit_result.text
            intent, _context = broker.calls[-1]
            assert intent.payload["order"]["transaction_type"] == "SELL"
            assert intent.payload["order"]["quantity"] == 10

            _ingest_fill(
                factory,
                account_id=account_scope,
                run_id=run_id,
                order_id="OID-ACC-EXIT",
                trade_id="TR-ACC-2",
                quantity=10,
                side="SELL",
            )
            counts = await consumer.poll_once()
            assert counts["filled"] == 1, counts
            assert _attributed_quantity(factory, strategy_id, account_scope) == 0

            # ------------------------------------------ stop + operator close
            stopped = await _post(
                client,
                f"/api/strategies/{strategy_id}/jobs/{job_id}/stop",
                json_body={"attempt": 1},
            )
            assert stopped.status_code < 400, stopped.text

            released = await _post(
                client,
                f"/api/hosted-supervisor/jobs/{job_id}/release",
                json_body={"lease_owner": "acceptance-supervisor", "lease_epoch": 1, "attempt": 1},
                headers=_supervisor_headers(),
            )
            assert released.status_code < 400, released.text

            cleanup = await _post(
                client,
                f"/api/hosted-supervisor/jobs/{job_id}/process-cleanup",
                json_body={
                    "lease_owner": "acceptance-supervisor",
                    "lease_epoch": 1,
                    "attempt": 1,
                    "state": "confirmed",
                },
                headers=_supervisor_headers(),
            )
            assert cleanup.status_code < 400, cleanup.text

            reconciled = await _post(
                client,
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json_body={"attempt": 1},
            )
            assert reconciled.status_code < 400, reconciled.text
            body = reconciled.json()
            assert body["status"] == "reconciled", body
            assert body["replacement_blocked"] is False
            assert body["audit_id"]
            assert body["evidence"]["execution_mode"] == "live"
            assert body["evidence"]["exposure_state"] == "flat", body["evidence"]
            assert body["evidence"]["work_state"] == "settled", body["evidence"]
            assert body["evidence"]["quiescence_state"] == "verified", body["evidence"]

            # STRICT ATTEMPT AUTHORITY: the closed attempt's credential cannot
            # open a new evaluation. Stopping the job revoked the child token, so
            # the proposal route refuses it rather than minting a plan for a
            # terminal attempt.
            after_close = await _post(
                client,
                "/api/algo-workers/worker/proposals",
                json_body={
                    "evaluation_id": f"eval-{uuid.uuid4().hex[:8]}",
                    "evaluation_kind": "run_now",
                    "strategy_run_id": run_id,
                    "strategy_id": strategy_id,
                    "account_scope": account_scope,
                    "target_kind": "single_instrument",
                    "payload": {
                        "instrument_token": TOKEN,
                        "exchange": "NSE",
                        "tradingsymbol": SYMBOL,
                        "product": "CNC",
                        "target_quantity": 5,
                        "reference_price": 1500.0,
                    },
                },
                headers=child_headers,
            )
            assert after_close.status_code >= 400, after_close.text
            with factory() as session:
                extra_plans = session.execute(
                    text(
                        "SELECT COUNT(*) FROM public.strategy_plans WHERE strategy_id = :sid"
                    ),
                    {"sid": strategy_id},
                ).scalar()
            assert int(extra_plans or 0) == 2, "a closed attempt opened a new plan"
            return {"strategy_id": strategy_id, "job_id": job_id, "run_id": run_id, "audit_id": body["audit_id"]}
        finally:
            await client.aclose()

    # The funds/margin boundary is the second fake: the route's own margin
    # reader is replaced by a stated amount so nothing calls a real broker. Every
    # other route reader (session, authority, attribution, ledger) stays real.
    from unittest.mock import patch as _patch

    from backend.api.routers import strategies as strategies_module

    with _patch.object(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 500_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    ):
        result = asyncio.run(_run())

    # The bound worker run is CLOSED in the same transaction as the unblock, and
    # the proof + audit rows are durable. A stopped child with an open run is not
    # a closed exposure.
    with factory() as session:
        run_row = session.execute(
            text("SELECT status, closed_at FROM public.algo_worker_runs WHERE strategy_run_id = :run"),
            {"run": result["run_id"]},
        ).mappings().first()
        assert run_row is not None
        assert str(run_row["status"]) == "closed", dict(run_row)
        assert run_row["closed_at"] is not None

        job_row = session.execute(
            text(
                "SELECT status, reconciled_at, process_cleanup_state FROM public.strategy_jobs "
                "WHERE id = :jid"
            ),
            {"jid": result["job_id"]},
        ).mappings().first()
        assert str(job_row["status"]) == "stopped", dict(job_row)
        assert job_row["reconciled_at"] is not None
        assert str(job_row["process_cleanup_state"]) == "confirmed"

        proof = session.execute(
            text(
                "SELECT barrier_version, quiet_since_version FROM public.strategy_execution_barriers "
                "WHERE account_id = :account AND strategy_id = :sid AND execution_environment = 'live'"
            ),
            {"account": account_scope, "sid": result["strategy_id"]},
        ).mappings().first()
        assert proof is not None, "no live settlement barrier row"
        assert int(proof["quiet_since_version"] or -1) == int(proof["barrier_version"] or 0)

        events = session.execute(
            text(
                "SELECT event, COUNT(*) AS n FROM public.strategy_execution_barrier_events "
                "WHERE account_id = :account AND strategy_id = :sid AND execution_environment = 'live' "
                "AND event = 'work_resolved' GROUP BY event"
            ),
            {"account": account_scope, "sid": result["strategy_id"]},
        ).fetchall()
        # Exactly one work_resolved per step: two steps, no duplicates.
        assert [int(row[1]) for row in events] == [2], [tuple(r) for r in events]

        audit = session.execute(
            text(
                "SELECT outcome, reason_code FROM public.strategy_job_reconciliations WHERE id = :aid"
            ),
            {"aid": result["audit_id"]},
        ).mappings().first()
        assert audit is not None, "no reconciliation audit row"

        states = [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT state FROM public.live_plan_submissions WHERE account_id = :account "
                    "AND strategy_id = :sid ORDER BY created_at"
                ),
                {"account": account_scope, "sid": result["strategy_id"]},
            ).fetchall()
        ]
        assert states == ["filled", "filled"], states


def test_live_launch_and_prepare_routes_follow_the_deployment_setting(pg, live_env):
    """The deployment setting gates LAUNCH through the production routes.

    ``HOSTED_LIVE_ENABLED`` is unset for this deployment, so a live launch (a
    queued job) and a live credential handoff must both refuse by name. The mode
    stays representable in the registry — the vocabulary admits it — but nothing
    can create or start live work.
    """
    factory = pg["factory"]
    account_scope = live_env["account_scope"]
    app = _build_app(factory, _FakeBroker(order_ids=("OID-GATE-1",)), now=datetime.now(timezone.utc))
    saved = os.environ.pop("HOSTED_LIVE_ENABLED", None)
    try:
        async def _run():
            client = await _operator_client(app)
            try:
                created = await _post(
                    client,
                    "/api/strategies",
                    json_body={
                        "name": "Live gate acceptance",
                        "description": None,
                        "execution_mode": "live",
                        "job_kind": "finite",
                        "account_scope": account_scope,
                        "max_duration_s": 600,
                        "progress_deadline_s": 300,
                        "stale_exit_policy": "none",
                    },
                )
                # Representable: the registry admits the live mode.
                assert created.status_code < 400, created.text
                strategy_id = str(created.json()["strategy_id"])
                version = await _post(
                    client,
                    f"/api/strategies/{strategy_id}/versions",
                    json_body={
                        "source": "print('gate')",
                        "parameters_schema": {"type": "object", "properties": {}},
                        "capabilities": {"trade": True},
                    },
                )
                assert version.status_code < 400, version.text
                version_id = str(version.json().get("version_id") or "1")

                refused = await _post(
                    client,
                    f"/api/strategies/{strategy_id}/jobs",
                    json_body={
                        "version_id": version_id,
                        "job_kind": "finite",
                        "execution_mode": "live",
                        "params": {},
                        "idempotency_key": f"gate-{uuid.uuid4().hex[:8]}",
                    },
                )
                assert refused.status_code == 409, refused.text
                assert refused.json()["detail"]["rejection_reason"] == "LIVE_DISABLED", refused.text
                assert refused.json()["detail"]["setting"] == "HOSTED_LIVE_ENABLED"

                with factory() as session:
                    from sqlalchemy import text

                    jobs = session.execute(
                        text("SELECT COUNT(*) FROM public.strategy_jobs WHERE strategy_id = :sid"),
                        {"sid": strategy_id},
                    ).scalar()
                assert int(jobs or 0) == 0, "a disabled deployment created live work"

                # With the setting ON the same launch is accepted; the
                # credential handoff is then refused once it is turned back off,
                # so a job that predates the flip cannot be started either.
                os.environ["HOSTED_LIVE_ENABLED"] = "true"
                job = await _post(
                    client,
                    f"/api/strategies/{strategy_id}/jobs",
                    json_body={
                        "version_id": version_id,
                        "job_kind": "finite",
                        "execution_mode": "live",
                        "params": {},
                        "idempotency_key": f"gate-{uuid.uuid4().hex[:8]}",
                    },
                )
                assert job.status_code < 400, job.text
                job_id = str((job.json().get("job") or {}).get("job_id") or "")
                claim = await _post(
                    client,
                    f"/api/hosted-supervisor/jobs/{job_id}/claim",
                    json_body={
                        "lease_owner": "gate-supervisor",
                        "expected_lease_epoch": 0,
                        "expected_attempt": 1,
                        "lease_until": (
                            datetime.now(timezone.utc) + timedelta(minutes=10)
                        ).isoformat(),
                    },
                    headers=_supervisor_headers(),
                )
                assert claim.status_code < 400, claim.text
                os.environ.pop("HOSTED_LIVE_ENABLED", None)
                prepare = await _post(
                    client,
                    f"/api/hosted-supervisor/jobs/{job_id}/prepare",
                    json_body={
                        "lease_owner": "gate-supervisor",
                        "lease_epoch": int(claim.json()["lease_epoch"]),
                        "attempt": 1,
                    },
                    headers=_supervisor_headers(),
                )
                assert prepare.status_code == 409, prepare.text
                assert prepare.json()["detail"]["rejection_reason"] == "LIVE_DISABLED", prepare.text
                with factory() as session:
                    from sqlalchemy import text

                    handoffs = session.execute(
                        text(
                            "SELECT token_id, handoff_at FROM public.strategy_jobs WHERE id = :jid"
                        ),
                        {"jid": job_id},
                    ).mappings().first()
                assert handoffs["token_id"] is None, dict(handoffs)
                assert handoffs["handoff_at"] is None, dict(handoffs)
            finally:
                await client.aclose()

        asyncio.run(_run())
    finally:
        if saved is not None:
            os.environ["HOSTED_LIVE_ENABLED"] = saved
