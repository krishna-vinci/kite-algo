# pyright: reportArgumentType=false
import json
import os
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.routers.worker_auth import *  # noqa: E402,F403
from backend.api.routers.worker_market import *  # noqa: E402,F403
from backend.api.routers.worker_execution import *  # noqa: E402,F403
from backend.api.routers.worker_protection import *  # noqa: E402,F403
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.api.services.market_data import WorkerInstrumentResolveRequest, WorkerMarketSnapshotRequest, WorkerQuoteRequest  # noqa: E402
from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository, WorkerToken  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.api.services.market_data import WorkerMarketDataService  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


async def _run_to_thread_inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


async def _single_sse(payload: str):
    yield f"event: snapshot\ndata: {payload}\n\n"


def _sqlite_algo_worker_repo() -> SqlAlchemyAlgoWorkerRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, connection_record):
        _ = connection_record
        dbapi_connection.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE public.algo_worker_tokens (
                    token_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    allowed_modes TEXT,
                    allowed_actions TEXT,
                    allowed_templates TEXT,
                    expires_at TEXT,
                    account_scope TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    heartbeat_json TEXT,
                    last_heartbeat_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.algo_worker_runs (
                    strategy_run_id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    account_scope TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_fields_json TEXT,
                    risk_schema_json TEXT,
                    allowed_actions_json TEXT,
                    runtime_state_json TEXT,
                    metadata_json TEXT,
                    worker_session_nonce TEXT,
                    worker_session_claimed_at TEXT,
                    last_heartbeat_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    closed_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.live_order_intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_ref TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    strategy_run_id TEXT NOT NULL,
                    broker_order_id TEXT,
                    basket_execution_id TEXT,
                    basket_leg_index INTEGER,
                    bracket_intent_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.worker_live_execution_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL,
                    trade_id TEXT,
                    client_order_ref TEXT,
                    basket_execution_id TEXT,
                    basket_leg_index INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.bracket_intents (
                    bracket_intent_id TEXT PRIMARY KEY,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    entry_basket_execution_id TEXT,
                    status TEXT NOT NULL,
                    action_required INTEGER NOT NULL DEFAULT 0,
                    action_reason TEXT,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.bracket_actions (
                    action_id TEXT PRIMARY KEY,
                    bracket_intent_id TEXT NOT NULL,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    claimed_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.basket_executions (
                    basket_execution_id TEXT PRIMARY KEY,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    all_or_none INTEGER NOT NULL DEFAULT 0,
                    action_required INTEGER NOT NULL DEFAULT 0,
                    action_reason TEXT,
                    rollback_status TEXT NOT NULL DEFAULT 'none',
                    requested_leg_count INTEGER NOT NULL DEFAULT 0,
                    completed_leg_count INTEGER NOT NULL DEFAULT 0,
                    terminal_leg_count INTEGER NOT NULL DEFAULT 0,
                    total_requested_quantity INTEGER NOT NULL DEFAULT 0,
                    total_filled_quantity INTEGER NOT NULL DEFAULT 0,
                    latest_event_cursor INTEGER,
                    latest_event_at TEXT,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.basket_execution_legs (
                    basket_execution_id TEXT NOT NULL,
                    leg_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    exchange TEXT,
                    tradingsymbol TEXT,
                    product TEXT,
                    transaction_type TEXT,
                    requested_quantity INTEGER NOT NULL DEFAULT 0,
                    broker_order_id TEXT,
                    client_order_ref TEXT,
                    latest_broker_status TEXT,
                    last_seen_filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_price REAL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (basket_execution_id, leg_index)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.worker_execution_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    basket_execution_id TEXT,
                    event_kind TEXT NOT NULL DEFAULT 'execution',
                    event_source TEXT NOT NULL DEFAULT 'legacy_execution',
                    event_type TEXT NOT NULL,
                    related_resource_type TEXT,
                    related_resource_id TEXT,
                    summary TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.order_trade_fills (
                    account_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    instrument_token BIGINT NOT NULL,
                    exchange TEXT,
                    tradingsymbol TEXT,
                    product TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    quantity INT NOT NULL,
                    price NUMERIC,
                    fill_timestamp TEXT,
                    payload_json TEXT,
                    PRIMARY KEY (account_id, trade_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.canonical_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.account_positions (
                    account_id TEXT NOT NULL,
                    instrument_token BIGINT NOT NULL,
                    product TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    tradingsymbol TEXT NOT NULL,
                    net_quantity INT NOT NULL,
                    PRIMARY KEY (account_id, instrument_token, product)
                )
                """
            )
        )
    factory = sessionmaker(bind=engine)
    return SqlAlchemyAlgoWorkerRepository(session_factory=factory)


class _FakeWorkerRepository:
    def __init__(self, *, raw_token="secret-token", token=None):
        self.raw_token = raw_token
        self.token = token or WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:paper-a",
            allowed_modes=["paper", "dry_run"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        self.tokens = {}
        self.runs = {}
        self.intent_results = {}
        self.touched = []
        self.live_open_legs = {}
        self.live_order_attribution_refs = {}
        self.live_broker_positions = {}

    async def claim_run_session(self, strategy_run_id, *, freshness_seconds, claimed_without_heartbeat_seconds):
        _ = (freshness_seconds, claimed_without_heartbeat_seconds)
        run = self.runs.get(strategy_run_id)
        if not run:
            return None
        if run.get("worker_session_nonce"):
            return None
        run["worker_session_nonce"] = f"nonce-{strategy_run_id}"
        run["worker_session_claimed_at"] = datetime.now(timezone.utc)
        return dict(run)

    async def release_run_session(self, strategy_run_id, *, expected_nonce):
        run = self.runs.get(strategy_run_id)
        if not run:
            return None
        if str(run.get("worker_session_nonce") or "") != str(expected_nonce):
            return None
        run["worker_session_nonce"] = None
        run["worker_session_claimed_at"] = None
        return dict(run)

    async def record_run_heartbeat(self, strategy_run_id, *, expected_nonce):
        run = self.runs.get(strategy_run_id)
        if not run:
            return None
        if str(run.get("worker_session_nonce") or "") != str(expected_nonce):
            return None
        run["last_heartbeat_at"] = datetime.now(timezone.utc)
        return dict(run)

    async def list_stale_recovery_runs(self):
        rows = []
        for run in self.runs.values():
            if str(run.get("status") or "") not in {"open", "paused"}:
                continue
            if not run.get("worker_session_nonce") and not run.get("last_heartbeat_at"):
                continue
            runtime_state = dict(run.get("runtime_state") or {})
            protection = dict(runtime_state.get("backend_protection") or {})
            operations = dict(protection.get("operations") or {})
            if protection.get("enabled") and operations.get("exit_on_worker_stale"):
                continue
            rows.append(dict(run))
        return rows

    async def list_exiting_recovery_runs(self):
        return [dict(run) for run in self.runs.values() if str(run.get("status") or "") == "exiting"]

    async def create_token(self, payload, *, raw_token, token_id):
        self.tokens[token_id] = {
            "token_id": token_id,
            "name": payload.name,
            "account_scope": payload.account_scope,
            "allowed_modes": payload.allowed_modes,
            "allowed_actions": payload.allowed_actions,
            "allowed_templates": payload.allowed_templates,
            "status": "active",
            "created_at": None,
            "expires_at": payload.expires_at,
            "last_used_at": None,
        }
        return dict(self.tokens[token_id])

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        self.touched.append(token_id)

    async def create_run(self, token, payload, *, strategy_run_id):
        run = {
            "strategy_run_id": strategy_run_id,
            "token_id": token.token_id,
            "template_id": payload.template_id,
            "account_scope": payload.account_scope,
            "execution_mode": payload.execution_mode,
            "status": "open",
            "summary_fields": payload.summary_fields,
            "risk_schema": payload.risk_schema,
            "allowed_actions": payload.allowed_actions,
            "runtime_state": payload.runtime_state,
            "metadata": payload.metadata,
            "worker_session_nonce": None,
            "worker_session_claimed_at": None,
            "last_heartbeat_at": None,
        }
        self.runs[strategy_run_id] = run
        return dict(run)
    async def get_run(self, strategy_run_id):
        run = self.runs.get(strategy_run_id)
        return dict(run) if run else None

    async def update_run_risk(self, strategy_run_id, patch):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        risk = dict(state.get("risk") or {})
        risk.update(patch)
        state["risk"] = risk
        run["runtime_state"] = state
        run["risk_schema"] = [
            {**field, "value": patch.get(field.get("key"), field.get("value"))}
            for field in run.get("risk_schema", [])
        ]
        return dict(run)

    async def update_run_status(self, strategy_run_id, status, *, state_patch=None):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        if state_patch:
            state.update(state_patch)
        run["status"] = status
        run["runtime_state"] = state
        return dict(run)

    async def update_run_runtime_state(self, strategy_run_id, runtime_state):
        run = self.runs[strategy_run_id]
        run["runtime_state"] = dict(runtime_state)
        return dict(run)

    async def update_run_backend_protection(self, strategy_run_id, protection, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        current = dict(state.get("backend_protection_state") or {})
        if expected_generation is not None and int(current.get("generation") or 0) != int(expected_generation):
            return None
        if expected_triggered_rule is not None and str(current.get("triggered_rule") or "") != str(expected_triggered_rule):
            return None
        if expected_exit_claim_id is not None and str(current.get("exit_claim_id") or "") != str(expected_exit_claim_id):
            return None
        state["backend_protection"] = dict(protection)
        state["backend_protection_state"] = dict(protection_state)
        run["runtime_state"] = state
        return dict(run)

    async def update_run_backend_protection_state(self, strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        current = dict(state.get("backend_protection_state") or {})
        if expected_generation is not None and int(current.get("generation") or 0) != int(expected_generation):
            return None
        if expected_triggered_rule is not None and str(current.get("triggered_rule") or "") != str(expected_triggered_rule):
            return None
        if expected_exit_claim_id is not None and str(current.get("exit_claim_id") or "") != str(expected_exit_claim_id):
            return None
        state["backend_protection_state"] = dict(protection_state)
        run["runtime_state"] = state
        return dict(run)

    async def list_live_strategy_open_legs(self, *, strategy_run_id, account_id):
        return [dict(item) for item in self.live_open_legs.get(strategy_run_id, [])]

    async def get_live_order_attribution_refs(self, *, strategy_run_id, account_id):
        refs = self.live_order_attribution_refs.get(strategy_run_id, {})
        return {
            "broker_order_ids": list(refs.get("broker_order_ids", [])),
            "client_order_refs": list(refs.get("client_order_refs", [])),
        }

    async def list_live_strategy_broker_positions(self, *, strategy_run_id, account_id):
        return [dict(item) for item in self.live_broker_positions.get(strategy_run_id, [])]

    async def get_intent_result(self, strategy_run_id, idempotency_key):
        return self.intent_results.get((strategy_run_id, idempotency_key))

    async def save_intent_result(self, *, token_id, strategy_run_id, request, status, result):
        self.intent_results[(strategy_run_id, request.idempotency_key)] = result
        return result

    async def begin_intent(self, *, token_id, strategy_run_id, request, initial_result, status="pending", db=None):
        _ = (token_id, db)
        key = (strategy_run_id, request.idempotency_key)
        if key not in self.intent_results:
            self.intent_results[key] = dict(initial_result)
            self.intent_results[(strategy_run_id, f"__status__:{request.idempotency_key}")] = status
            return {"status": status, "result": dict(initial_result), "claimed": True}
        existing_status = self.intent_results.get((strategy_run_id, f"__status__:{request.idempotency_key}"), "accepted")
        return {"status": existing_status, "result": dict(self.intent_results[key]), "claimed": False}

    async def finalize_intent_result(self, *, strategy_run_id, idempotency_key, status, result, db=None):
        _ = db
        key = (strategy_run_id, idempotency_key)
        self.intent_results[key] = dict(result)
        self.intent_results[(strategy_run_id, f"__status__:{idempotency_key}")] = status
        return {"status": status, "result": dict(result)}


class _FailingCreateRunRepository(_FakeWorkerRepository):
    async def create_run(self, token, payload, *, strategy_run_id):
        raise SQLAlchemyError("db unavailable")


def _test_client(*, repo, market_data_service=None):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.algo_worker_repository = repo
    if market_data_service is not None:
        app.state.worker_market_data_service = market_data_service
    return TestClient(app)


class AlgoWorkerApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_kite_client_releases_database_before_client_build(self):
        events = []
        fake_session = SimpleNamespace(access_token=" token-123 ")

        class FakeQuery:
            def filter_by(self, **kwargs):
                self.filter_kwargs = kwargs
                return self

            def first(self):
                events.append("read")
                return fake_session

        class FakeDb:
            def query(self, model):
                self.model = model
                return FakeQuery()

            def rollback(self):
                events.append("rollback")

            def close(self):
                events.append("close")

        fake_db = FakeDb()
        expected_client = object()

        def build_client(access_token, *, session_id):
            events.append("build")
            self.assertEqual(access_token, "token-123")
            self.assertEqual(session_id, "system")
            self.assertEqual(events, ["read", "rollback", "close", "build"])
            return expected_client

        service = WorkerMarketDataService()
        with (
            patch("backend.app.database.SessionLocal", return_value=fake_db),
            patch(
                "backend.broker_api.session.kite_session.build_kite_client",
                side_effect=build_client,
            ),
        ):
            client = await service._get_system_kite_client()

        self.assertIs(client, expected_client)

    async def test_system_kite_client_rejects_missing_token_after_releasing_database(self):
        fake_query = SimpleNamespace(
            filter_by=lambda **kwargs: SimpleNamespace(first=lambda: None)
        )
        fake_db = SimpleNamespace(
            query=lambda model: fake_query,
            rollback=Mock(),
            close=Mock(),
        )

        service = WorkerMarketDataService()
        with patch("backend.app.database.SessionLocal", return_value=fake_db):
            with self.assertRaises(HTTPException) as raised:
                await service._get_system_kite_client()

        self.assertEqual(raised.exception.status_code, 401)
        fake_db.rollback.assert_called_once_with()
        fake_db.close.assert_called_once_with()

    def _request(self, repo, *, paper_runtime=None, raw_token="secret-token"):
        return SimpleNamespace(
            headers={"authorization": f"Bearer {raw_token}"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=paper_runtime)),
            is_disconnected=AsyncMock(return_value=False),
        )

    async def test_admin_token_creation_allows_explicit_live_kite_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="kite:AB1234", allowed_modes=["paper", "live"])

        with patch("backend.api.routers.worker_auth.require_app_user", return_value=SimpleNamespace(username="admin")):
            response = await create_worker_token(request, payload)

        self.assertEqual(response.account_scope, "kite:AB1234")
        self.assertIn("live", response.allowed_modes)

    async def test_default_worker_actions_include_market_actions(self):
        self.assertIn("market:read", DEFAULT_WORKER_ACTIONS)
        self.assertIn("market:stream", DEFAULT_WORKER_ACTIONS)
        self.assertIn("funds:read", DEFAULT_WORKER_ACTIONS)
        self.assertIn("gtt:read", DEFAULT_WORKER_ACTIONS)
        self.assertIn("gtt:write", DEFAULT_WORKER_ACTIONS)

    async def test_worker_funds_returns_paper_account_summary(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.paper_runtime_service = SimpleNamespace(
            get_account_summary=AsyncMock(
                return_value={
                    "account_scope": "kite:paper-a",
                    "currency": "INR",
                    "starting_balance": 100000,
                    "available_funds": 82000,
                    "blocked_funds": 18000,
                    "realized_pnl": 1250,
                    "updated_at": "2026-04-26T08:00:00+00:00",
                }
            )
        )

        response = await get_worker_funds(request, mode="paper")

        self.assertEqual(response["source"], "paper_runtime")
        self.assertEqual(response["segments"]["equity"]["available_cash"], 82000)
        self.assertEqual(response["allocation"]["usable_equity_cash"], 82000)

    async def test_worker_run_funds_includes_allocation_remaining(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-1"] = {
            "strategy_run_id": "run-1",
            "token_id": "worker-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {"allocation_cap": 50000},
        }
        request = self._request(repo)
        request.app.state.paper_runtime_service = SimpleNamespace(
            get_account_summary=AsyncMock(
                return_value={
                    "account_scope": "kite:paper-a",
                    "currency": "INR",
                    "starting_balance": 100000,
                    "available_funds": 75000,
                    "blocked_funds": 25000,
                    "realized_pnl": 0,
                    "updated_at": "2026-04-26T08:00:00+00:00",
                }
            ),
            get_strategy_run_pnl=AsyncMock(
                return_value={
                    "currency": "INR",
                    "strategy": {
                        "status": "open",
                        "realized_pnl": 100,
                        "unrealized_pnl": -25,
                        "last_updated_at": "2026-04-26T08:00:00+00:00",
                        "positions": [
                            {
                                "instrument_token": 408065,
                                "exchange": "NSE",
                                "tradingsymbol": "INFY",
                                "product": "CNC",
                                "net_quantity": 10,
                                "average_price": 1500,
                                "last_price": 1510,
                            }
                        ],
                    },
                }
            ),
        )

        response = await get_worker_run_funds(request, "run-1")

        self.assertEqual(response["strategy"]["gross_exposure"], 15100)
        self.assertEqual(response["strategy"]["allocation"]["cap"], 50000)
        self.assertEqual(response["strategy"]["allocation"]["remaining"], 34900)

    async def test_resolve_ticker_endpoint_returns_fake_service_response(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            resolve_ticker=AsyncMock(
                return_value={
                    "symbol": "NSE:INFY",
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        response = await resolve_worker_market_ticker(request, symbol="NSE:INFY")

        self.assertEqual(response["instrument_token"], 408065)
        self.assertEqual(response["symbol"], "NSE:INFY")

    async def test_batch_resolve_returns_instruments_and_missing_fake_response(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            resolve_many=AsyncMock(
                return_value={
                    "instruments": [
                        {
                            "symbol": "NSE:INFY",
                            "instrument_token": 408065,
                            "exchange": "NSE",
                            "tradingsymbol": "INFY",
                        }
                    ],
                    "missing": [999999],
                }
            )
        )

        response = await resolve_worker_market_tickers(
            request,
            WorkerInstrumentResolveRequest(symbols=["NSE:INFY"], instrument_tokens=[999999]),
        )

        self.assertEqual(len(response["instruments"]), 1)
        self.assertEqual(response["instruments"][0]["instrument_token"], 408065)
        self.assertEqual(response["missing"], [999999])

    async def test_quote_endpoint_requires_market_read_action(self):
        token = WorkerToken(
            token_id="worker-1",
            name="limited",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=["runs:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await get_worker_market_quotes(request, WorkerQuoteRequest(symbols=["NSE:INFY"]))

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_quote_endpoint_returns_fake_service_response(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            get_quotes=AsyncMock(
                return_value={
                    "quotes": [
                        {
                            "symbol": "NSE:INFY",
                            "instrument_token": 408065,
                            "last_price": 1520.5,
                            "received_at": "2026-04-25T12:00:00+00:00",
                            "age_ms": 100,
                            "is_stale": False,
                        }
                    ],
                    "missing": [],
                }
            )
        )

        response = await get_worker_market_quotes(request, WorkerQuoteRequest(symbols=["NSE:INFY"], mode="quote"))

        self.assertEqual(response["quotes"][0]["instrument_token"], 408065)
        self.assertFalse(response["quotes"][0]["is_stale"])

    async def test_worker_market_tick_stream_requires_market_stream_action(self):
        token = WorkerToken(
            token_id="worker-1",
            name="limited",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=["market:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await stream_worker_market_ticks(request, symbols="NSE:INFY", tokens=None, mode="quote")

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_worker_market_tick_stream_returns_snapshot_event(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            stream_ticks=lambda request, token, symbols, instrument_tokens, mode: _single_sse('{"ticks": [], "missing": []}')
        )

        response = await stream_worker_market_ticks(request, symbols="NSE:INFY", tokens=None, mode="quote")
        chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertIn("event: snapshot", chunk)

    async def test_worker_market_tick_stream_parses_symbols_and_tokens_for_service(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        captured = {}

        async def fake_stream(request_obj, token, symbols, instrument_tokens, mode):
            captured["symbols"] = symbols
            captured["instrument_tokens"] = instrument_tokens
            captured["mode"] = mode
            yield "event: snapshot\ndata: {\"ticks\": []}\n\n"

        request.app.state.worker_market_data_service = SimpleNamespace(stream_ticks=fake_stream)

        response = await stream_worker_market_ticks(
            request,
            symbols="NSE:INFY, NSE:TCS ,,NSE:SBIN",
            tokens="408065, 2953217, , 779521",
            mode="quote",
        )
        await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(captured["symbols"], ["NSE:INFY", "NSE:TCS", "NSE:SBIN"])
        self.assertEqual(captured["instrument_tokens"], [408065, 2953217, 779521])
        self.assertEqual(captured["mode"], "quote")

    async def test_worker_market_tick_stream_rejects_invalid_token_csv(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await stream_worker_market_ticks(request, symbols="NSE:INFY", tokens="408065,bad-token", mode="quote")

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("tokens", ctx.exception.detail)

    async def test_worker_market_tick_stream_rejects_out_of_range_token_csv(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await stream_worker_market_ticks(request, symbols="NSE:INFY", tokens="999999999999999999999", mode="quote")

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("out-of-range", ctx.exception.detail)

    async def test_worker_market_candles_returns_history_and_current(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            get_candles=AsyncMock(
                return_value={
                    "symbol": "NSE:INFY",
                    "instrument_token": 408065,
                    "interval": "5minute",
                    "candles": [],
                    "current": None,
                }
            )
        )

        response = await get_worker_market_candles(
            request,
            symbol="NSE:INFY",
            instrument_token=None,
            interval="5minute",
            lookback=50,
        )

        self.assertEqual(response["symbol"], "NSE:INFY")
        self.assertEqual(response["interval"], "5minute")

    async def test_worker_market_candles_empty_reader_data_is_stale(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            ),
            candle_reader=SimpleNamespace(get_candles=AsyncMock(return_value={"candles": [], "current": None})),
        )

        response = await service.get_candles(symbol="NSE:INFY", interval="5minute", lookback=50)

        self.assertTrue(response["is_stale"])

    async def test_worker_market_candles_current_falls_back_to_latest_cached_candle(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            ),
            candle_reader=SimpleNamespace(
                get_candles=AsyncMock(
                    return_value={
                        "candles": [
                            {
                                "ts": "2026-04-25T09:15:00+05:30",
                                "open": 1500,
                                "high": 1510,
                                "low": 1490,
                                "close": 1505,
                                "volume": 1000,
                            }
                        ],
                        "current": None,
                    }
                )
            ),
        )

        response = await service.get_candles(symbol="NSE:INFY", interval="day", lookback=1)

        self.assertEqual(response["current"]["close"], 1505.0)
        self.assertEqual(response["current"]["source"], "latest_cached_candle")

    async def test_worker_market_quotes_fall_back_to_broker_quote_when_runtime_tick_missing(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        class FakeKite:
            def quote(self, instruments):
                return {
                    instruments[0]: {
                        "instrument_token": 408065,
                        "last_price": 1525.5,
                        "ohlc": {"open": 1500, "high": 1530, "low": 1495, "close": 1510},
                    }
                }

        with patch.object(service, "_get_system_kite_client", AsyncMock(return_value=FakeKite())):
            response = await service.get_quotes(WorkerQuoteRequest(symbols=["NSE:INFY"], mode="quote"))

        self.assertEqual(response["quotes"][0]["last_price"], 1525.5)
        self.assertEqual(response["missing"], [])

    async def test_worker_market_day_current_uses_broker_quote_ohlc_when_cache_empty(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            ),
            candle_reader=SimpleNamespace(get_candles=AsyncMock(return_value={"candles": [], "current": None})),
        )

        class FakeKite:
            def quote(self, instruments):
                return {
                    instruments[0]: {
                        "instrument_token": 408065,
                        "last_price": 1525.5,
                        "volume": 12345,
                        "ohlc": {"open": 1500, "high": 1530, "low": 1495, "close": 1510},
                    }
                }

        with patch.object(service, "_get_system_kite_client", AsyncMock(return_value=FakeKite())):
            response = await service.get_candles(symbol="NSE:INFY", interval="day", lookback=1)

        self.assertEqual(response["current"]["source"], "broker_quote_ohlc")
        self.assertEqual(response["current"]["close"], 1525.5)
        self.assertFalse(response["is_stale"])

    async def test_worker_market_day_current_uses_runtime_tick_without_candle_reader(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            ),
            market_data_runtime=SimpleNamespace(
                get_tick=AsyncMock(
                    return_value={
                        "instrument_token": 408065,
                        "last_price": 1531.0,
                        "received_at": "2026-04-25T08:00:00+00:00",
                        "ohlc": {"open": 1500, "high": 1535, "low": 1495, "close": 1510},
                    }
                )
            ),
            candle_reader=None,
        )

        response = await service.get_candles(symbol="NSE:INFY", interval="day", lookback=1)

        self.assertEqual(response["current"]["source"], "broker_quote_ohlc")
        self.assertEqual(response["current"]["close"], 1531.0)
        self.assertFalse(response["is_stale"])

    async def test_worker_market_history_forwards_passthrough_request(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        captured = {}
        request.app.state.worker_market_data_service = SimpleNamespace(
            get_historical_candles=AsyncMock(
                side_effect=lambda **kwargs: captured.update(kwargs)
                or {
                    "symbol": "NSE:INFY",
                    "instrument_token": 408065,
                    "timeframe": "day",
                    "candles": [],
                    "ingestion": {"status": "disabled"},
                    "source": "kite_passthrough",
                }
            )
        )

        with (
            patch("backend.app.database.get_db_connection", return_value=Mock()),
            patch(
                "backend.broker_api.market.exchange_calendar.get_calendar_sessions",
                return_value={"calendar_version": 1, "sessions": []},
            ),
        ):
            response = await get_worker_market_history(
                request,
                SimpleNamespace(add_task=lambda *args, **kwargs: None),
                symbol="NSE:INFY",
                instrument_token=None,
                timeframe="day",
                from_ts=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
                to_ts=datetime.fromisoformat("2024-12-31T00:00:00+00:00"),
                ingest=True,
                passthrough=True,
            )

        self.assertEqual(response["source"], "kite_passthrough")
        self.assertEqual(captured["symbol"], "NSE:INFY")
        self.assertEqual(captured["timeframe"], "day")
        self.assertTrue(captured["ingest"])
        self.assertTrue(captured["passthrough"])

    async def test_worker_market_history_accepts_from_date_aliases(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        captured = {}
        request.app.state.worker_market_data_service = SimpleNamespace(
            get_historical_candles=AsyncMock(
                side_effect=lambda **kwargs: captured.update(kwargs)
                or {"symbol": "NSE:INFY", "instrument_token": 408065, "timeframe": "day", "candles": []}
            )
        )

        with (
            patch("backend.app.database.get_db_connection", return_value=Mock()),
            patch(
                "backend.broker_api.market.exchange_calendar.get_calendar_sessions",
                return_value={"calendar_version": 1, "sessions": []},
            ),
        ):
            await get_worker_market_history(
                request,
                SimpleNamespace(add_task=lambda *args, **kwargs: None),
                symbol="NSE:INFY",
                timeframe="day",
                from_date=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
                to_date=datetime.fromisoformat("2024-12-31T00:00:00+00:00"),
            )

        self.assertEqual(captured["from_date"], datetime.fromisoformat("2024-01-01T00:00:00+00:00"))
        self.assertEqual(captured["to_date"], datetime.fromisoformat("2024-12-31T00:00:00+00:00"))

    async def test_worker_market_history_passthrough_treats_naive_kite_dates_as_ist(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        class FakeIngestion:
            def __init__(self, kite):
                self.kite = kite

            async def fetch_raw_records(self, instrument_token, interval, from_date, to_date):
                return [
                    {
                        "date": datetime(2024, 1, 1, 9, 15),
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 10,
                    }
                ]

        with patch("backend.api.services.market_data.WorkerMarketDataService._get_system_kite_client", AsyncMock(return_value=object())):
            with patch("backend.broker_api.market.candle_ingestion.CandleIngestion", FakeIngestion):
                response = await service.get_historical_candles(
                    symbol="NSE:INFY",
                    timeframe="day",
                    from_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    to_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    passthrough=True,
                )

        self.assertEqual(response["candles"][0]["ts"], "2024-01-01T09:15:00+05:30")
        self.assertEqual(response["ingestion"]["status"], "completed")

    async def test_worker_market_history_rejects_unbounded_passthrough_range(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        with self.assertRaises(HTTPException) as ctx:
            await service.get_historical_candles(
                symbol="NSE:INFY",
                timeframe="day",
                from_date=datetime(2010, 1, 1, tzinfo=timezone.utc),
                to_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                passthrough=True,
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("passthrough", ctx.exception.detail)

    async def test_worker_market_history_rejects_naive_request_window(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        with self.assertRaises(HTTPException) as ctx:
            await service.get_historical_candles(
                symbol="NSE:INFY",
                timeframe="day",
                from_date=datetime(2024, 1, 1),
                to_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                passthrough=False,
                ingest=False,
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("timezone", ctx.exception.detail)

    async def test_worker_market_history_db_failure_returns_503(self):
        service = WorkerMarketDataService(
            instruments_repository=SimpleNamespace(
                resolve_market_symbol=lambda symbol: {
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "name": "INFOSYS",
                    "instrument_type": "EQ",
                    "segment": "NSE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "strike": None,
                }
            )
        )

        with patch("backend.broker_api.market.candle_storage.CandleStorage.query_candles", side_effect=RuntimeError("db down")):
            with self.assertRaises(HTTPException) as ctx:
                await service.get_historical_candles(
                    symbol="NSE:INFY",
                    timeframe="day",
                    from_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    to_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    passthrough=False,
                    ingest=False,
                )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("storage query failed", ctx.exception.detail)

    async def test_worker_market_candle_stream_requires_market_stream_action(self):
        token = WorkerToken(
            token_id="worker-1",
            name="limited",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=["market:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await stream_worker_market_candles(request, symbol="NSE:INFY", instrument_token=None, interval="5minute")

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_worker_market_candle_stream_returns_snapshot_chunk(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            stream_candles=lambda request, symbol, instrument_token, interval: _single_sse(
                '{"symbol": "NSE:INFY", "candles": [], "current": null}'
            )
        )

        response = await stream_worker_market_candles(
            request,
            symbol="NSE:INFY",
            instrument_token=None,
            interval="5minute",
        )
        chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertIn("event: snapshot", chunk)

    async def test_worker_market_snapshot_combines_quotes_and_candles(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        request.app.state.worker_market_data_service = SimpleNamespace(
            get_market_snapshot=AsyncMock(
                return_value={
                    "quotes": [],
                    "candles": [],
                    "missing": [],
                    "updated_at": "2026-04-25T00:00:00+00:00",
                }
            )
        )

        response = await get_worker_market_snapshot(
            request,
            WorkerMarketSnapshotRequest(symbols=["NSE:INFY"]),
        )

        self.assertIn("quotes", response)
        self.assertIn("candles", response)

    async def test_admin_token_creation_rejects_live_without_kite_account_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="paper-a", allowed_modes=["live"])

        with patch("backend.api.routers.worker_auth.require_app_user", return_value=SimpleNamespace(username="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_token(request, payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("kite:<broker_user_id>", ctx.exception.detail)

    async def test_admin_token_creation_rejects_live_paper_account_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="kite:paper-a", allowed_modes=["live"])

        with patch("backend.api.routers.worker_auth.require_app_user", return_value=SimpleNamespace(username="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_token(request, payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("real broker", ctx.exception.detail)

    async def test_worker_can_create_paper_run_and_submit_idempotent_basket_intent(self):
        repo = _FakeWorkerRepository()
        paper_runtime = SimpleNamespace(place_basket=AsyncMock(return_value={"mode": "paper", "status": "success", "results": []}))
        request = self._request(repo, paper_runtime=paper_runtime)

        run = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-worker-1",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
                risk_schema=[{"key": "stop_loss_pct", "label": "Stop loss", "type": "number", "value": 1.2}],
            ),
        )

        self.assertEqual(run["strategy_run_id"], "run-worker-1")

        payload = WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key="entry-0001",
            payload={"orders": [{"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1}]},
            metadata={"strategy_run_id": "evil-run", "strategy_name": "Wrong Name"},
        )
        first = await submit_worker_intent(request, "run-worker-1", payload)
        second = await submit_worker_intent(request, "run-worker-1", payload)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "deduped")
        paper_runtime.place_basket.assert_awaited_once()
        call = paper_runtime.place_basket.await_args.kwargs
        self.assertEqual(call["account_scope"], "kite:paper-a")
        self.assertEqual(call["attribution"]["strategy_run_id"], "run-worker-1")
        self.assertEqual(call["attribution"]["source"], "algo_worker")
        self.assertEqual(call["attribution"]["metadata"]["strategy_run_id"], "run-worker-1")
        self.assertNotEqual(call["attribution"]["metadata"]["strategy_run_id"], "evil-run")

    async def test_create_run_database_failure_returns_worker_safe_503(self):
        repo = _FailingCreateRunRepository()
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-create-db-fail",
                    template_id="demo-strategy",
                    account_scope="kite:paper-a",
                    execution_mode="dry_run",
                ),
            )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Worker run persistence unavailable")

    async def test_get_run_includes_backend_positions_field_for_paper_reconciliation(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-paper-positions"] = {
            "strategy_run_id": "run-paper-positions",
            "token_id": "worker-1",
            "template_id": "demo-strategy",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        paper_runtime = SimpleNamespace(
            get_strategy_run_pnl=AsyncMock(
                return_value={
                    "positions": [
                        {
                            "instrument_token": 408065,
                            "exchange": "NSE",
                            "tradingsymbol": "INFY",
                            "product": "MIS",
                            "net_quantity": 1,
                        }
                    ]
                }
            )
        )
        request = self._request(repo, paper_runtime=paper_runtime)

        response = await get_worker_run(request, "run-paper-positions")

        self.assertIn("positions", response)
        self.assertEqual(response["positions"][0]["tradingsymbol"], "INFY")
        self.assertEqual(response["backend_positions"], response["positions"])
        self.assertEqual(response["backend_positions_status"], "available")
        self.assertEqual(response["backend_positions_source"], "paper_runtime")

    async def test_get_run_includes_empty_positions_for_dry_run_reconciliation(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry-positions"] = {
            "strategy_run_id": "run-dry-positions",
            "token_id": "worker-1",
            "template_id": "demo-strategy",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        response = await get_worker_run(request, "run-dry-positions")

        self.assertEqual(response["positions"], [])
        self.assertEqual(response["backend_positions"], [])
        self.assertEqual(response["backend_positions_status"], "available")
        self.assertEqual(response["backend_positions_source"], "dry_run")

    async def test_paper_run_rejects_live_account_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-paper-live-scope",
                    template_id="mean_reversion",
                    account_scope="kite:AB1234",
                    execution_mode="paper",
                ),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("paper account_scope", ctx.exception.detail)

    async def test_safety_check_reports_generic_and_option_projection(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-safe-1"] = {
            "strategy_run_id": "run-safe-1",
            "token_id": "worker-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {
                "backend_protection_state": {
                    "status": "active",
                    "exit_submitted": False,
                    "last_checked_at": "2026-05-06T10:00:00+00:00",
                }
            },
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_protection._option_run_protection_snapshot_for_worker",
            AsyncMock(
                return_value={
                    "applicable": False,
                    "run_status": None,
                    "evaluation_mode": "run_state",
                    "triggered": False,
                    "blocking": False,
                    "blocking_reason": None,
                    "matched_rule": None,
                    "metrics": {},
                    "recommended_exit_orders_count": 0,
                }
            ),
        ):
            response = await get_worker_run_safety_check(request, "run-safe-1")

        self.assertTrue(response["can_trade"])
        self.assertIsNotNone(response["safety_token"])
        self.assertEqual(response["blocking_reasons"], [])
        self.assertEqual(response["generic_protection"]["status"], "active")
        self.assertFalse(response["options_protection"]["applicable"])

    async def test_submit_worker_intent_rejects_stale_safety_token(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-safe-2"] = {
            "strategy_run_id": "run-safe-2",
            "token_id": "worker-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {
                "backend_protection_state": {
                    "status": "active",
                    "exit_submitted": False,
                }
            },
            "metadata": {},
        }
        request = self._request(repo, paper_runtime=SimpleNamespace(place_order=AsyncMock(return_value={"status": "success"})))

        with patch("backend.api.routers.worker_protection._worker_safety_secret", lambda _request: "secret-key"), patch(
            "backend.api.routers.worker_protection._option_run_protection_snapshot_for_worker",
            AsyncMock(
                return_value={
                    "applicable": False,
                    "run_status": None,
                    "evaluation_mode": "run_state",
                    "triggered": False,
                    "blocking": False,
                    "blocking_reason": None,
                    "matched_rule": None,
                    "metrics": {},
                    "recommended_exit_orders_count": 0,
                }
            ),
        ):
            check = await get_worker_run_safety_check(request, "run-safe-2")
            repo.runs["run-safe-2"]["runtime_state"]["backend_protection_state"]["status"] = "triggered"

            with self.assertRaises(HTTPException) as ctx:
                await submit_worker_intent(
                    request,
                    "run-safe-2",
                    WorkerIntentRequest(
                        intent_type="place_order",
                        payload={"order": {"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1}},
                        idempotency_key="run-safe-2:entry:001",
                        metadata={},
                        safety_token=check["safety_token"],
                    ),
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["rejection_reason"], "SAFETY_TOKEN_EXPIRED")

    async def test_safety_check_blocks_when_option_state_is_unavailable(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-safe-3"] = {
            "strategy_run_id": "run-safe-3",
            "token_id": "worker-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {
                "backend_protection_state": {
                    "status": "active",
                    "exit_submitted": False,
                }
            },
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_protection._option_run_protection_snapshot_for_worker",
            AsyncMock(
                return_value={
                    "applicable": True,
                    "run_status": None,
                    "evaluation_mode": "run_state",
                    "triggered": False,
                    "blocking": True,
                    "blocking_reason": "OPTIONS_PROTECTION_STATE_UNAVAILABLE",
                    "matched_rule": None,
                    "metrics": {},
                    "recommended_exit_orders_count": 0,
                }
            ),
        ):
            response = await get_worker_run_safety_check(request, "run-safe-3")

        self.assertFalse(response["can_trade"])
        self.assertIsNone(response["safety_token"])
        self.assertEqual(response["blocking_reasons"], ["OPTIONS_PROTECTION_STATE_UNAVAILABLE"])
        self.assertTrue(response["options_protection"]["applicable"])
        self.assertTrue(response["options_protection"]["blocking"])
        self.assertEqual(response["options_protection"]["blocking_reason"], "OPTIONS_PROTECTION_STATE_UNAVAILABLE")

    async def test_safety_check_requires_configured_secret_outside_pytest_fallback(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-safe-4"] = {
            "strategy_run_id": "run-safe-4",
            "token_id": "worker-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {
                "backend_protection_state": {
                    "status": "active",
                    "exit_submitted": False,
                }
            },
            "metadata": {},
        }
        request = self._request(repo)

        env_overrides = {key: os.environ.get(key) for key in ("WORKER_SAFETY_TOKEN_SECRET", "APP_JWT_SECRET", "PYTEST_CURRENT_TEST")}
        for key in env_overrides:
            os.environ.pop(key, None)
        try:
            with patch(
                "backend.api.routers.worker_protection._option_run_protection_snapshot_for_worker",
                AsyncMock(
                    return_value={
                        "applicable": False,
                        "run_status": None,
                        "evaluation_mode": "run_state",
                        "triggered": False,
                        "blocking": False,
                        "blocking_reason": None,
                        "matched_rule": None,
                        "metrics": {},
                        "recommended_exit_orders_count": 0,
                    }
                ),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await get_worker_run_safety_check(request, "run-safe-4")
        finally:
            for key, value in env_overrides.items():
                if value is not None:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Worker safety token secret is not configured")

    async def test_safety_check_uses_run_state_option_protection_evaluation(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-opt-safe"] = {
            "strategy_run_id": "run-opt-safe",
            "token_id": "worker-1",
            "template_id": "iron_condor",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {"backend_protection_state": {"status": "active", "exit_submitted": False}},
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_protection._option_run_protection_snapshot_for_worker",
            AsyncMock(
                return_value={
                    "applicable": True,
                    "run_status": "entered",
                    "evaluation_mode": "run_state",
                    "triggered": True,
                    "blocking": True,
                    "blocking_reason": "OPTIONS_PROTECTION_TRIGGERED",
                    "matched_rule": {"key": "rule_1", "role": "hard_stop"},
                    "metrics": {"open_quantity": 75},
                    "recommended_exit_orders_count": 4,
                }
            ),
        ):
            response = await get_worker_run_safety_check(request, "run-opt-safe")

        self.assertFalse(response["can_trade"])
        self.assertEqual(response["blocking_reasons"], ["OPTIONS_PROTECTION_TRIGGERED"])
        self.assertEqual(response["options_protection"]["evaluation_mode"], "run_state")
        self.assertTrue(response["options_protection"]["triggered"])

    async def test_live_bound_token_can_create_paper_run_but_not_other_live_scope(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["paper", "dry_run", "live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        paper_run = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-live-token-paper",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
            ),
        )
        self.assertEqual(paper_run["account_scope"], "kite:paper-a")
        fetched = await get_worker_run(request, "run-live-token-paper")
        self.assertEqual(fetched["strategy_run_id"], "run-live-token-paper")

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-live-token-other-live",
                    template_id="mean_reversion",
                    account_scope="kite:ZZ9999",
                    execution_mode="live",
                    metadata={"strategy_family": "indicator_strategy", "strategy_name": "MR"},
                ),
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_paper_worker_run_stores_journal_v2_paper_environment_refs(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["paper", "live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        class _FakeJournalService:
            def ensure_v2_worker_context(self, **kwargs):
                return {
                    "environment_id": f"env:{kwargs['execution_mode']}:{kwargs['account_scope']}",
                    "execution_context_id": f"ctx:{kwargs['strategy_run_id']}",
                    "template_id": "tmpl-ref-1",
                    "variant_id": None,
                    "deployment_id": None,
                    "identity_rule_version": "journal_v2_identity_v1",
                    "grouping_rule_version": "journal_v2_grouping_v1",
                    "ambiguous": False,
                    "resolution_method": "explicit_template_id",
                    "resolution_confidence": "1.0",
                }

        request.app.state.journal_service = _FakeJournalService()

        paper_run = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-live-token-paper-v2",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
                metadata={"strategy_family": "indicator_strategy", "strategy_name": "MR"},
            ),
        )

        assert paper_run["metadata"]["journal_v2"]["environment_id"] == "env:paper:kite:paper-a"

    async def test_live_bound_token_can_read_paper_funds_by_explicit_scope(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["paper", "live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)
        request.app.state.paper_runtime_service = SimpleNamespace(
            get_account_summary=AsyncMock(
                return_value={
                    "account_scope": "kite:paper-a",
                    "currency": "INR",
                    "starting_balance": 100000,
                    "available_funds": 99000,
                    "blocked_funds": 1000,
                    "realized_pnl": 0,
                    "updated_at": "2026-04-26T08:00:00+00:00",
                }
            )
        )

        response = await get_worker_funds(request, mode="paper", account_scope="kite:paper-a")
        self.assertEqual(response["account_scope"], "kite:paper-a")

        with self.assertRaises(HTTPException) as ctx:
            await get_worker_funds(request, mode="live", account_scope="kite:ZZ9999")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_worker_risk_patch_updates_runtime_state_and_schema_values(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-risk",
                template_id="momentum",
                account_scope="kite:paper-a",
                risk_schema=[{"key": "trailing_distance", "label": "Trail", "type": "number", "value": 3.0}],
            ),
        )

        updated = await patch_worker_run_risk(request, "run-risk", WorkerRiskPatchRequest(patch={"trailing_distance": 2.0}))

        self.assertEqual(updated["runtime_state"]["risk"]["trailing_distance"], 2.0)
        self.assertEqual(updated["risk_schema"][0]["value"], 2.0)

    async def test_live_run_requires_strategy_metadata(self):
        token = WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-live",
                    template_id="mean_reversion",
                    account_scope="kite:AB1234",
                    execution_mode="live",
                    metadata={"strategy_family": "indicator_strategy"},
                ),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("strategy_name", ctx.exception.detail)

    async def test_live_run_rejects_unknown_strategy_family(self):
        token = WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-live",
                    template_id="mean_reversion",
                    account_scope="kite:AB1234",
                    execution_mode="live",
                    metadata={"strategy_family": "unknown", "strategy_name": "Mean Reversion"},
                ),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("strategy_family", ctx.exception.detail)


class AlgoWorkerProtectionApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, repo, *, paper_runtime=None, raw_token="secret-token"):
        return SimpleNamespace(
            headers={"authorization": f"Bearer {raw_token}"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=paper_runtime)),
            is_disconnected=AsyncMock(return_value=False),
        )

    async def test_create_run_validates_backend_protection_and_normalizes_runtime_state(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)

        result = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-protection-create",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
                runtime_state={
                    "backend_protection": {
                        "enabled": True,
                        "positions": [
                            {
                                "symbol": "nse:infy",
                                "product": "cnc",
                                "side": "buy",
                                "quantity": 1,
                                "entry_price": 1500,
                                "stoploss_pct": 2,
                            }
                        ],
                    }
                },
            ),
        )

        self.assertEqual(result["runtime_state"]["backend_protection"]["positions"][0]["symbol"], "NSE:INFY")
        self.assertEqual(result["runtime_state"]["backend_protection"]["positions"][0]["product"], "CNC")
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["generation"], 1)
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["status"], "active")

    async def test_create_run_rejects_invalid_backend_protection(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-protection-invalid",
                    template_id="mean_reversion",
                    account_scope="kite:paper-a",
                    execution_mode="paper",
                    runtime_state={"backend_protection": {"enabled": True}},
                ),
            )

        self.assertEqual(ctx.exception.status_code, 422)

    async def test_patch_backend_protection_updates_runtime_state_and_generation(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-protection-patch",
                template_id="momentum",
                account_scope="kite:paper-a",
                execution_mode="paper",
            ),
        )

        result = await patch_worker_run_protection(
            request,
            "run-protection-patch",
            WorkerProtectionPatchRequest(
                backend_protection={
                    "enabled": True,
                    "version": 1,
                    "positions": [
                        {
                            "symbol": "NSE:INFY",
                            "product": "CNC",
                            "side": "BUY",
                            "quantity": 1,
                            "entry_price": 1500,
                            "stoploss_pct": 2,
                        }
                    ],
                },
                reason="rebalance_update",
            ),
        )

        self.assertEqual(result["runtime_state"]["backend_protection"]["version"], 1)
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["generation"], 1)
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["update_reason"], "rebalance_update")

    async def test_patch_backend_protection_increments_version_and_preserves_trailing_when_requested(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        repo.runs["run-protection-preserve"] = {
            "strategy_run_id": "run-protection-preserve",
            "token_id": "worker-1",
            "template_id": "momentum",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": ["edit_risk", "exit_strategy"],
            "runtime_state": {
                "backend_protection": {"enabled": True, "version": 3, "basket": {"stoploss_pct": 5}},
                "backend_protection_state": {
                    "generation": 3,
                    "best_basket_pnl_pct": 6.5,
                    "position_states": {"symbol:NSE:INFY:CNC": {"best_pnl_pct": 3.2}},
                },
            },
            "metadata": {},
        }

        result = await patch_worker_run_protection(
            request,
            "run-protection-preserve",
            WorkerProtectionPatchRequest(
                backend_protection={"enabled": True, "version": 1, "basket": {"stoploss_pct": 4}},
                reason="rebalance_update",
                reset_trailing=False,
            ),
        )

        self.assertEqual(result["runtime_state"]["backend_protection"]["version"], 4)
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["generation"], 4)
        self.assertEqual(result["runtime_state"]["backend_protection_state"]["best_basket_pnl_pct"], 6.5)
        self.assertIn("symbol:NSE:INFY:CNC", result["runtime_state"]["backend_protection_state"]["position_states"])

    async def test_patch_backend_protection_rejects_after_terminal_exit_submission(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        repo.runs["run-protection-locked"] = {
            "strategy_run_id": "run-protection-locked",
            "token_id": "worker-1",
            "template_id": "momentum",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": ["edit_risk", "exit_strategy"],
            "runtime_state": {"backend_protection_state": {"exit_submitted": True}},
            "metadata": {},
        }

        with self.assertRaises(HTTPException) as ctx:
            await patch_worker_run_protection(
                request,
                "run-protection-locked",
                WorkerProtectionPatchRequest(backend_protection={"enabled": False}),
            )

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_patch_backend_protection_rejects_when_exit_claim_in_progress(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        repo.runs["run-protection-claimed"] = {
            "strategy_run_id": "run-protection-claimed",
            "token_id": "worker-1",
            "template_id": "momentum",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": ["edit_risk", "exit_strategy"],
            "runtime_state": {"backend_protection_state": {"generation": 1, "exit_claim_id": "claim-1"}},
            "metadata": {},
        }

        with self.assertRaises(HTTPException) as ctx:
            await patch_worker_run_protection(
                request,
                "run-protection-claimed",
                WorkerProtectionPatchRequest(backend_protection={"enabled": False}),
            )

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_patch_backend_protection_rejects_concurrent_generation_change(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        repo.runs["run-protection-conflict"] = {
            "strategy_run_id": "run-protection-conflict",
            "token_id": "worker-1",
            "template_id": "momentum",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": ["edit_risk", "exit_strategy"],
            "runtime_state": {
                "backend_protection": {"enabled": True, "version": 1, "basket": {"stoploss_pct": 5}},
                "backend_protection_state": {"generation": 1},
            },
            "metadata": {},
        }

        async def conflict_update(*args, **kwargs):
            return None

        repo.update_run_backend_protection = conflict_update

        with self.assertRaises(HTTPException) as ctx:
            await patch_worker_run_protection(
                request,
                "run-protection-conflict",
                WorkerProtectionPatchRequest(backend_protection={"enabled": True, "basket": {"stoploss_pct": 4}}),
            )

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_live_worker_intent_routes_through_live_order_service_with_attribution(self):
        sys.modules.pop("broker_api.orders", None)
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion",
                "entry_surface": "external_algo_worker",
            },
        }
        live_orders = SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_id="OID-LIVE-1", model_dump=lambda mode="json": {"order_id": "OID-LIVE-1"}))
        )
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await submit_worker_intent(
                request,
                "run-live",
                WorkerIntentRequest(
                    intent_type="place_order",
                    idempotency_key="live-0001",
                    payload={
                        "order": {
                            "exchange": "NSE",
                            "tradingsymbol": "INFY",
                            "transaction_type": "BUY",
                            "variety": "regular",
                            "product": "CNC",
                            "order_type": "MARKET",
                            "quantity": 1,
                        }
                    },
                    metadata={"signal": "zscore-cross"},
                ),
            )

        self.assertEqual(response["status"], "accepted")
        live_orders.place_order.assert_awaited_once()
        call = live_orders.place_order.await_args
        req = call.args[1]
        self.assertEqual(req.attribution["strategy_run_id"], "run-live")
        self.assertEqual(req.attribution["strategy_family"], "indicator_strategy")
        self.assertEqual(req.attribution["strategy_name"], "Mean Reversion")
        self.assertEqual(req.attribution["execution_mode"], "live")
        self.assertEqual(req.attribution["account_ref"], "kite:AB1234")
        self.assertEqual(req.attribution["source"], "algo_worker")
        self.assertEqual(call.kwargs["idempotency_key"], "live-0001")

    async def test_live_place_basket_returns_basket_execution_id(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            place_basket=AsyncMock(
                return_value=SimpleNamespace(
                    status="success",
                    basket_status="active",
                    action_required=False,
                    action_reason=None,
                    model_dump=lambda mode="json": {
                        "status": "success",
                        "results": [{"index": 0, "tradingsymbol": "INFY", "order_id": "OID-1", "status": "success"}],
                        "errors": [],
                        "basket_execution_id": "will-be-overridden",
                        "basket_status": "active",
                        "action_required": False,
                        "action_reason": None,
                    },
                )
            )
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "backend.api.routers.worker_execution.basket_execution_store.create_live_basket_execution",
            return_value={"basket_execution_id": "basket-live-1", "status": "submitting", "action_required": False, "action_reason": None},
        ), patch(
            "backend.api.routers.worker_execution.basket_execution_store.get_basket_for_run",
            return_value={"basket_execution_id": "basket-live-1", "status": "active", "action_required": False, "action_reason": None},
        ):
            response = await submit_worker_intent(
                request,
                "run-live-1",
                WorkerIntentRequest(
                    intent_type="place_basket",
                    idempotency_key="basket-1",
                    payload={
                        "basket": {
                            "orders": [
                                {
                                    "exchange": "NSE",
                                    "tradingsymbol": "INFY",
                                    "transaction_type": "BUY",
                                    "variety": "regular",
                                    "product": "CNC",
                                    "order_type": "MARKET",
                                    "quantity": 1,
                                }
                            ]
                        }
                    },
                ),
            )

        self.assertEqual(response["status"], "accepted")
        self.assertTrue(response["result"]["basket_execution_id"])
        self.assertIn(response["result"]["basket_status"], {"submitting", "active", "failed", "completed", "partial"})

    async def test_live_place_basket_deduped_replay_returns_same_basket_execution_id(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            place_basket=AsyncMock(
                return_value=SimpleNamespace(
                    status="success",
                    basket_status="active",
                    action_required=False,
                    action_reason=None,
                    model_dump=lambda mode="json": {
                        "status": "success",
                        "results": [{"index": 0, "tradingsymbol": "INFY", "order_id": "OID-1", "status": "success"}],
                        "errors": [],
                    },
                )
            )
        )

        intent = WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key="basket-dup",
            payload={
                "basket": {
                    "orders": [
                        {
                            "exchange": "NSE",
                            "tradingsymbol": "INFY",
                            "transaction_type": "BUY",
                            "variety": "regular",
                            "product": "CNC",
                            "order_type": "MARKET",
                            "quantity": 1,
                        }
                    ]
                }
            },
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "backend.api.routers.worker_execution.basket_execution_store.create_live_basket_execution",
            return_value={"basket_execution_id": "basket-live-dup", "status": "submitting", "action_required": False, "action_reason": None},
        ), patch(
            "backend.api.routers.worker_execution.basket_execution_store.get_basket_for_run",
            return_value={"basket_execution_id": "basket-live-dup", "status": "active", "action_required": False, "action_reason": None},
        ):
            first = await submit_worker_intent(request, "run-live-1", intent)
            second = await submit_worker_intent(request, "run-live-1", intent)

        self.assertEqual(second["status"], "deduped")
        self.assertEqual(second["result"]["basket_execution_id"], first["result"]["basket_execution_id"])

    async def test_live_place_basket_pending_replay_does_not_resubmit_orders(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.intent_results[("run-live-1", "basket-pending")] = {
            "mode": "live",
            "intent_type": "place_basket",
            "basket_execution_id": "bex_existing",
            "basket_status": "submitting",
            "action_required": False,
            "action_reason": None,
            "result": {"status": "pending", "results": [], "errors": []},
        }
        repo.intent_results[("run-live-1", "__status__:basket-pending")] = "pending"
        request = self._request(repo)
        live_orders = SimpleNamespace(place_basket=AsyncMock())
        request.app.state.algo_worker_orders_service = live_orders

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "backend.api.routers.worker_execution.basket_execution_store.create_live_basket_execution"
        ) as create_basket:
            response = await submit_worker_intent(
                request,
                "run-live-1",
                WorkerIntentRequest(
                    intent_type="place_basket",
                    idempotency_key="basket-pending",
                    payload={
                        "basket": {
                            "orders": [
                                {
                                    "exchange": "NSE",
                                    "tradingsymbol": "INFY",
                                    "transaction_type": "BUY",
                                    "variety": "regular",
                                    "product": "CNC",
                                    "order_type": "MARKET",
                                    "quantity": 1,
                                }
                            ]
                        }
                    },
                ),
            )

        self.assertEqual(response["status"], "deduped")
        self.assertEqual(response["result"]["basket_execution_id"], "bex_existing")
        create_basket.assert_not_called()
        live_orders.place_basket.assert_not_awaited()

    async def test_get_worker_basket_returns_persisted_snapshot(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["runs:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_execution.basket_execution_store.get_basket_for_run",
            return_value={"basket_execution_id": "basket-1", "status": "active", "legs": []},
        ):
            response = await get_worker_basket(request, "run-live-1", "basket-1")

        self.assertEqual(response["basket_execution_id"], "basket-1")
        self.assertIn(response["status"], {"active", "partial", "completed", "failed", "submitting"})

    async def test_list_worker_execution_events_filters_by_basket_and_cursor(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["runs:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_protection.worker_timeline_store.list_events",
            return_value=[
                {
                    "cursor": 11,
                    "strategy_run_id": "run-live-1",
                    "account_id": "kite:AB1234",
                    "basket_execution_id": "basket-1",
                    "event_kind": "execution",
                    "event_source": "basket_runtime",
                    "event_type": "basket.status_changed",
                    "related_resource_type": "basket_execution",
                    "related_resource_id": "basket-1",
                    "summary": None,
                    "payload": {},
                }
            ],
        ):
            response = await list_worker_execution_events(
                request,
                "run-live-1",
                after_cursor=10,
                basket_execution_id="basket-1",
                event_type="basket.status_changed",
                limit=50,
            )

        self.assertTrue(all(item["cursor"] > 10 for item in response["events"]))
        self.assertTrue(all(item.get("basket_execution_id") == "basket-1" for item in response["events"]))

    async def test_list_worker_timeline_filters_by_kind_and_related_ref(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        with patch(
            "backend.api.routers.worker_protection.worker_timeline_store.list_events",
            return_value=[
                {
                    "cursor": 31,
                    "strategy_run_id": "run-live-1",
                    "account_id": "kite:AB1234",
                    "basket_execution_id": None,
                    "event_kind": "decision",
                    "event_source": "worker",
                    "event_type": "decision.entry",
                    "related_resource_type": "basket_execution",
                    "related_resource_id": "basket-1",
                    "summary": "Entered after breakout",
                    "payload": {"decision_type": "entry", "action": "enter"},
                }
            ],
        ):
            response = await list_worker_timeline(
                request,
                "run-live-1",
                event_kind="decision",
                related_resource_type="basket_execution",
                related_resource_id="basket-1",
            )

        self.assertEqual(response["events"][0]["event_kind"], "decision")
        self.assertEqual(response["events"][0]["related_resource_id"], "basket-1")

    async def test_execution_events_stream_drops_non_execution_rows(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }

        class _FakePubSub:
            def __init__(self, request_obj):
                self.request = request_obj
                self.messages = [
                    {
                        "type": "message",
                        "data": json.dumps(
                            {
                                "cursor": 32,
                                "event_kind": "decision",
                                "event_type": "decision.note",
                                "strategy_run_id": "run-live-1",
                            }
                        ),
                    },
                    {
                        "type": "message",
                        "data": json.dumps(
                            {
                                "cursor": 33,
                                "event_kind": "execution",
                                "event_type": "order.updated",
                                "strategy_run_id": "run-live-1",
                                "account_id": "kite:AB1234",
                                "basket_execution_id": None,
                                "payload": {"order_id": "OID-1"},
                            }
                        ),
                    },
                ]

            async def subscribe(self, *_args, **_kwargs):
                return None

            async def get_message(self, **_kwargs):
                if self.messages:
                    return self.messages.pop(0)
                self.request.is_disconnected = AsyncMock(return_value=True)
                return None

            async def unsubscribe(self, *_args, **_kwargs):
                return None

            async def aclose(self):
                return None

        request = self._request(repo)
        fake_redis = SimpleNamespace(pubsub=lambda: _FakePubSub(request))

        with patch("backend.api.routers.worker_protection.get_redis", return_value=fake_redis), patch(
            "backend.api.routers.worker_protection.worker_timeline_store.list_events",
            return_value=[],
        ):
            response = await stream_worker_execution_events(request, "run-live-1")
            chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertIn('"event_type": "order.updated"', chunk)
        self.assertNotIn("decision.note", chunk)

    async def test_execution_events_stream_treats_missing_event_kind_as_legacy_execution(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }

        class _FakePubSub:
            def __init__(self, request_obj):
                self.request = request_obj
                self.messages = [
                    {
                        "type": "message",
                        "data": json.dumps(
                            {
                                "cursor": 40,
                                "event_type": "order.updated",
                                "strategy_run_id": "run-live-1",
                                "account_id": "kite:AB1234",
                                "basket_execution_id": None,
                                "payload": {"order_id": "OID-legacy"},
                            }
                        ),
                    }
                ]

            async def subscribe(self, *_args, **_kwargs):
                return None

            async def get_message(self, **_kwargs):
                if self.messages:
                    return self.messages.pop(0)
                self.request.is_disconnected = AsyncMock(return_value=True)
                return None

            async def unsubscribe(self, *_args, **_kwargs):
                return None

            async def aclose(self):
                return None

        request = self._request(repo)
        fake_redis = SimpleNamespace(pubsub=lambda: _FakePubSub(request))

        with patch("backend.api.routers.worker_protection.get_redis", return_value=fake_redis), patch(
            "backend.api.routers.worker_protection.worker_timeline_store.list_events",
            return_value=[],
        ):
            response = await stream_worker_execution_events(request, "run-live-1")
            chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertIn('"event_type": "order.updated"', chunk)

    async def test_decision_events_require_runs_log_action(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["runs:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_decision_event(
                request,
                "run-live-1",
                WorkerDecisionEventRequest(
                    decision_type="entry",
                    action="enter",
                    summary="Entered on breakout",
                ),
            )

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_decision_events_require_session_nonce_when_run_claimed(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-live",
        }
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_decision_event(
                request,
                "run-live-1",
                WorkerDecisionEventRequest(
                    decision_type="entry",
                    action="enter",
                    summary="Entered on breakout",
                ),
            )

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_decision_event_rejects_unknown_related_ref(self):
        token = WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-1"] = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-1",
            "template_id": "tmpl",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields": [],
            "risk_schema": [],
            "allowed_actions": [],
            "runtime_state": {},
            "metadata": {},
        }
        request = self._request(repo)

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _attach_public_schema(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.close()

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE public.basket_executions (
                        basket_execution_id TEXT PRIMARY KEY,
                        strategy_run_id TEXT NOT NULL
                    )
                    """
                )
            )

        session_factory = sessionmaker(bind=engine)

        with patch("backend.api.routers.worker_protection.SessionLocal", session_factory):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_decision_event(
                    request,
                    "run-live-1",
                    WorkerDecisionEventRequest(
                        decision_type="entry",
                        action="enter",
                        summary="Entered on breakout",
                        related_resource_type="basket_execution",
                        related_resource_id="missing-basket",
                    ),
                )

        self.assertEqual(ctx.exception.status_code, 422)

    async def test_worker_can_list_live_orders_for_grouped_run(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        live_orders = SimpleNamespace(
            orders=lambda kite, corr_id: [
                {
                    "order_id": "260428150255994",
                    "tradingsymbol": "IDEA",
                    "exchange": "NSE",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "status": "COMPLETE",
                    "strategy_run_id": "run-live",
                }
            ]
        )
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await list_worker_orders(request, "run-live")

        self.assertEqual(response["orders"][0]["order_id"], "260428150255994")

    async def test_live_open_leg_detection_works_without_journal_rows(self):
        repo = _sqlite_algo_worker_repo()
        session = repo.session_factory()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, broker_order_id)
                    VALUES (:intent_id, :client_order_ref, :account_id, :strategy_run_id, :broker_order_id)
                    """
                ),
                {
                    "intent_id": "lint-1",
                    "client_order_ref": "KAOPEN01",
                    "account_id": "kite:AB1234",
                    "strategy_run_id": "run-live",
                    "broker_order_id": "OID-1",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO order_trade_fills (
                        account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol,
                        product, transaction_type, quantity, price, fill_timestamp, payload_json
                    ) VALUES (
                        :account_id, :trade_id, :order_id, :instrument_token, :exchange, :tradingsymbol,
                        :product, :transaction_type, :quantity, :price, :fill_timestamp, :payload_json
                    )
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "trade_id": "T-1",
                    "order_id": "OID-1",
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 2,
                    "price": 1500,
                    "fill_timestamp": "2026-04-29T09:15:00Z",
                    "payload_json": "{}",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO account_positions (account_id, instrument_token, product, exchange, tradingsymbol, net_quantity)
                    VALUES (:account_id, :instrument_token, :product, :exchange, :tradingsymbol, :net_quantity)
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "instrument_token": 408065,
                    "product": "MIS",
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "net_quantity": 2,
                },
            )
            session.commit()
        finally:
            session.close()

        legs = await repo.list_live_strategy_open_legs(strategy_run_id="run-live", account_id="kite:AB1234")

        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["net_quantity"], 2)
        self.assertEqual(legs[0]["broker_net_quantity"], 2)
        self.assertEqual(legs[0]["tradingsymbol"], "INFY")

    async def test_live_open_leg_detection_can_recover_order_id_from_canonical_tag(self):
        repo = _sqlite_algo_worker_repo()
        session = repo.session_factory()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, broker_order_id)
                    VALUES (:intent_id, :client_order_ref, :account_id, :strategy_run_id, NULL)
                    """
                ),
                {
                    "intent_id": "lint-2",
                    "client_order_ref": "KATAGRECOVER",
                    "account_id": "kite:AB1234",
                    "strategy_run_id": "run-live-recover",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO canonical_order_events (account_id, order_id, payload_json)
                    VALUES (:account_id, :order_id, :payload_json)
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "order_id": "OID-RECOVER-1",
                    "payload_json": '{"tag":"KATAGRECOVER"}',
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO order_trade_fills (
                        account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol,
                        product, transaction_type, quantity, price, fill_timestamp, payload_json
                    ) VALUES (
                        :account_id, :trade_id, :order_id, :instrument_token, :exchange, :tradingsymbol,
                        :product, :transaction_type, :quantity, :price, :fill_timestamp, :payload_json
                    )
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "trade_id": "T-RECOVER-1",
                    "order_id": "OID-RECOVER-1",
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 1,
                    "price": 1500,
                    "fill_timestamp": "2026-04-29T09:15:00Z",
                    "payload_json": "{}",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO account_positions (account_id, instrument_token, product, exchange, tradingsymbol, net_quantity)
                    VALUES (:account_id, :instrument_token, :product, :exchange, :tradingsymbol, :net_quantity)
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "instrument_token": 408065,
                    "product": "MIS",
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "net_quantity": 1,
                },
            )
            session.commit()
        finally:
            session.close()

        refs = await repo.get_live_order_attribution_refs(strategy_run_id="run-live-recover", account_id="kite:AB1234")
        legs = await repo.list_live_strategy_open_legs(strategy_run_id="run-live-recover", account_id="kite:AB1234")

        self.assertEqual(refs["broker_order_ids"], ["OID-RECOVER-1"])
        self.assertEqual(refs["client_order_refs"], ["KATAGRECOVER"])
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["instrument_token"], 408065)

    async def test_worker_open_legs_use_exact_bridge_without_fuzzy_union(self):
        repo = _sqlite_algo_worker_repo()
        session = repo.session_factory()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO worker_live_execution_links (
                        strategy_run_id, account_id, broker_order_id, trade_id, client_order_ref
                    ) VALUES (
                        :strategy_run_id, :account_id, :broker_order_id, NULL, :client_order_ref
                    )
                    """
                ),
                {
                    "strategy_run_id": "run-live-exact",
                    "account_id": "kite:AB1234",
                    "broker_order_id": "OID-EXACT-1",
                    "client_order_ref": "KAEXACT1",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO order_trade_fills (
                        account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol,
                        product, transaction_type, quantity, price, fill_timestamp, payload_json
                    ) VALUES (
                        :account_id, :trade_id, :order_id, :instrument_token, :exchange, :tradingsymbol,
                        :product, :transaction_type, :quantity, :price, :fill_timestamp, :payload_json
                    )
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "trade_id": "T-EXACT-1",
                    "order_id": "OID-EXACT-1",
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 2,
                    "price": 1500,
                    "fill_timestamp": "2026-04-29T09:15:00Z",
                    "payload_json": "{}",
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO account_positions (account_id, instrument_token, product, exchange, tradingsymbol, net_quantity)
                    VALUES (:account_id, :instrument_token, :product, :exchange, :tradingsymbol, :net_quantity)
                    """
                ),
                {
                    "account_id": "kite:AB1234",
                    "instrument_token": 408065,
                    "product": "MIS",
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "net_quantity": 2,
                },
            )
            session.commit()
        finally:
            session.close()

        refs = await repo.get_live_order_attribution_refs(strategy_run_id="run-live-exact", account_id="kite:AB1234")
        legs = await repo.list_live_strategy_open_legs(strategy_run_id="run-live-exact", account_id="kite:AB1234")
        assert refs["broker_order_ids"] == ["OID-EXACT-1"]
        assert refs["client_order_refs"] == ["KAEXACT1"]
        assert len(legs) == 1
        assert legs[0]["net_quantity"] == 2

    async def test_create_worker_bracket_places_entry_under_bracket_idempotency_domain(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "worker_session_nonce": "nonce-run-live",
        }
        request = self._request(repo)
        request.headers["X-Worker-Session-Nonce"] = "nonce-run-live"
        live_orders = SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_id="OID-BRK-1"))
        )
        request.app.state.algo_worker_orders_service = live_orders

        intents = {}

        class _FakeDB:
            def execute(self, *args, **kwargs):
                return None

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        def _create_bracket_intent(db, *, strategy_run_id, account_id, config, metadata=None, bracket_intent_id=None):
            intents[bracket_intent_id] = {
                "bracket_intent_id": bracket_intent_id,
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "status": "entry_submitting",
                "action_required": False,
                "action_reason": None,
                "config": dict(config or {}),
                "metadata": dict(metadata or {}),
            }
            return dict(intents[bracket_intent_id])

        def _update_bracket_status(db, *, bracket_intent_id, status, action_required=None, action_reason=None, metadata_patch=None, entry_basket_execution_id=None):
            _ = entry_basket_execution_id
            intent = intents[bracket_intent_id]
            intent["status"] = status
            if action_required is not None:
                intent["action_required"] = bool(action_required)
            intent["action_reason"] = action_reason
            if metadata_patch:
                intent.setdefault("metadata", {}).update(dict(metadata_patch))

        def _get_bracket_intent(db, *, strategy_run_id, bracket_intent_id):
            _ = strategy_run_id
            return dict(intents.get(bracket_intent_id) or {})

        def _request_cancel_bracket(db, *, strategy_run_id, bracket_intent_id):
            _ = strategy_run_id
            if bracket_intent_id not in intents:
                raise KeyError(bracket_intent_id)
            intents[bracket_intent_id]["status"] = "cancelling"
            return dict(intents[bracket_intent_id])

        with patch("backend.api.routers.worker_execution.SessionLocal", return_value=_FakeDB()), patch(
            "backend.api.routers.worker_execution.bracket_runtime_store.create_bracket_intent",
            side_effect=_create_bracket_intent,
        ), patch(
            "backend.api.routers.worker_execution.bracket_runtime_store.update_bracket_status",
            side_effect=_update_bracket_status,
        ), patch(
            "backend.api.routers.worker_execution.bracket_runtime_store.get_bracket_intent",
            side_effect=_get_bracket_intent,
        ), patch(
            "backend.api.routers.worker_execution.bracket_runtime_store.request_cancel_bracket",
            side_effect=_request_cancel_bracket,
        ), patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await create_worker_bracket(
                request,
                "run-live",
                WorkerBracketCreateRequest(
                    entry_order={
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 1,
                        "validity": "DAY",
                        "market_protection": -1,
                    },
                    stoploss={
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "SELL",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "SL",
                        "quantity": 1,
                        "trigger_price": 100.0,
                        "price": 99.5,
                        "validity": "DAY",
                    },
                ),
            )

        self.assertTrue(response["bracket_intent_id"])
        self.assertEqual(response["status"], "entry_working")
        self.assertEqual(response["entry_result"]["order_id"], "OID-BRK-1")
        live_orders.place_order.assert_awaited_once()
        self.assertTrue(live_orders.place_order.await_args.kwargs["idempotency_key"].startswith("bracket:"))

    async def test_cancel_worker_bracket_requires_session_nonce_when_claimed(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "worker_session_nonce": "nonce-run-live",
        }
        request = self._request(repo)

        class _FakeDB:
            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        with patch("backend.api.routers.worker_execution.SessionLocal", return_value=_FakeDB()):
            with self.assertRaises(HTTPException) as ctx:
                await cancel_worker_bracket(request, "run-live", "brk-session")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_worker_order_list_matches_by_broker_order_id_or_tag(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": ["OID-1"],
            "client_order_refs": ["KATAG01"],
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            orders=lambda kite, corr_id: [
                {"order_id": "OID-1", "status": "COMPLETE", "tradingsymbol": "INFY"},
                {"order_id": "OID-2", "status": "OPEN", "tag": "KATAG01", "tradingsymbol": "TCS"},
                {"order_id": "OID-X", "status": "OPEN", "tag": "OTHER", "tradingsymbol": "RELIANCE"},
            ]
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await list_worker_orders(request, "run-live")

        self.assertEqual([order["order_id"] for order in response["orders"]], ["OID-1", "OID-2"])

    async def test_worker_trade_list_matches_by_order_id_or_tag(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": ["OID-1"],
            "client_order_refs": ["KATAG01"],
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            trades=lambda kite, corr_id: [
                {"trade_id": "T-1", "order_id": "OID-1", "tradingsymbol": "INFY", "exchange": "NSE", "instrument_token": 408065, "transaction_type": "BUY", "product": "MIS", "average_price": 1500, "quantity": 1},
                {"trade_id": "T-2", "order_id": "OID-2", "tag": "KATAG01", "tradingsymbol": "TCS", "exchange": "NSE", "instrument_token": 2953217, "transaction_type": "BUY", "product": "MIS", "average_price": 3500, "quantity": 1},
                {"trade_id": "T-X", "order_id": "OID-X", "tag": "OTHER", "tradingsymbol": "RELIANCE", "exchange": "NSE", "instrument_token": 738561, "transaction_type": "BUY", "product": "MIS", "average_price": 2500, "quantity": 1},
            ]
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await list_worker_trades(request, "run-live")

        self.assertEqual([trade["trade_id"] for trade in response["trades"]], ["T-1", "T-2"])

    async def test_worker_can_get_order_snapshot_for_grouped_run(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            order_snapshot=lambda kite, order_id, corr_id: {
                "order_id": order_id,
                "status": "COMPLETE",
                "strategy_run_id": "run-live",
                "tradingsymbol": "INFY",
            }
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await get_worker_order(request, "OID-1", strategy_run_id="run-live")

        self.assertEqual(response["order"]["order_id"], "OID-1")

    async def test_worker_order_snapshot_accepts_same_run_via_durable_attribution(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": ["OID-1"],
            "client_order_refs": ["KATAG01"],
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            order_snapshot=lambda kite, order_id, corr_id: {
                "order_id": order_id,
                "status": "COMPLETE",
                "tag": "KATAG01",
                "tradingsymbol": "INFY",
            }
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await get_worker_order(request, "OID-1", strategy_run_id="run-live")

        self.assertEqual(response["order"]["order_id"], "OID-1")

    async def test_worker_order_history_rejects_cross_run_order(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            order_history=lambda kite, order_id, corr_id: [
                {"order_id": order_id, "status": "OPEN", "strategy_run_id": "other-run", "order_timestamp": "2026-04-28T09:15:00Z"}
            ]
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await get_worker_order_history(request, "OID-1", strategy_run_id="run-live")

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_worker_order_history_accepts_same_run_via_durable_attribution_and_rejects_cross_run(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": ["OID-1"],
            "client_order_refs": ["KATAG01"],
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            order_history=lambda kite, order_id, corr_id: [
                {"order_id": order_id, "status": "OPEN", "order_timestamp": "2026-04-28T09:15:00Z", "tag": "KATAG01"}
            ]
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await get_worker_order_history(request, "OID-1", strategy_run_id="run-live")

        self.assertEqual(response["order_id"], "OID-1")

        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": ["OID-Z"],
            "client_order_refs": ["OTHER-TAG"],
        }
        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await get_worker_order_history(request, "OID-1", strategy_run_id="run-live")

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_worker_live_order_routes_reject_non_live_runs(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-paper"] = {
            "strategy_run_id": "run-paper",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await list_worker_orders(request, "run-paper")

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_worker_cancel_order_requires_run_access(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:OTHER",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await cancel_worker_order(request, "260428150255994", WorkerOrderActionRequest(strategy_run_id="run-live"))

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_worker_preview_order_returns_margin_and_charges(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace()

        class _CostContract:
            def journal_payload(self):
                return {"charges_estimate": "2.50", "margin_required": "10.00"}

        with patch.dict(
            sys.modules,
            {"backend.execution_accounting.kite_costs": SimpleNamespace(build_live_order_cost_contract=lambda **kwargs: _CostContract())},
        ):
            with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
                "backend.api.routers.worker_execution.asyncio.to_thread",
                _run_to_thread_inline,
            ):
                response = await preview_worker_order(
                    request,
                    "run-live",
                    WorkerOrderPreviewRequest(
                        order={
                            "exchange": "NSE",
                            "tradingsymbol": "IDEA",
                            "transaction_type": "BUY",
                            "variety": "regular",
                            "product": "MIS",
                            "order_type": "MARKET",
                            "quantity": 1,
                            "validity": "DAY",
                            "market_protection": -1,
                        }
                    ),
                )

        self.assertEqual(response["preview"]["cost_contract"]["charges_estimate"], "2.50")
        self.assertEqual(response["preview"]["cost_contract"]["margin_required"], "10.00")

    async def test_worker_live_exit_dry_run_returns_grouped_exit_plan(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_open_legs["run-live"] = [
            {
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 1,
                "broker_net_quantity": 1,
            }
        ]
        request = self._request(repo)

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="preview", idempotency_key="run:exit:preview:001", dry_run=True))

        self.assertEqual(response["status"], "dry_run")
        self.assertIn("orders", response["exit"])
        self.assertEqual(response["exit"]["orders"][0]["market_protection"], -1)

    async def test_worker_intent_rejects_non_open_run(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-closed"] = {
            "strategy_run_id": "run-closed",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "closed",
        }
        request = self._request(repo, paper_runtime=SimpleNamespace(place_order=AsyncMock()))

        with self.assertRaises(HTTPException) as ctx:
            await submit_worker_intent(
                request,
                "run-closed",
                WorkerIntentRequest(intent_type="place_order", idempotency_key="closed-0001", payload={"order": {}}),
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("open strategy runs", ctx.exception.detail)

    async def test_paper_worker_exit_does_not_close_run_when_exit_is_blocked(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-paper-blocked"] = {
            "strategy_run_id": "run-paper-blocked",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "metadata": {},
            "runtime_state": {},
        }
        paper_runtime = SimpleNamespace(
            exit_strategy=AsyncMock(
                return_value={
                    "mode": "paper",
                    "status": "blocked",
                    "message": "reconciliation mismatch",
                }
            )
        )
        request = self._request(repo, paper_runtime=paper_runtime)

        response = await exit_worker_run(request, "run-paper-blocked", WorkerExitRequest(reason="operator"))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["run"]["status"], "open")

    async def test_live_worker_exit_closes_when_reconciled_strategy_is_already_flat(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        paper_runtime = SimpleNamespace(exit_strategy=AsyncMock())
        request = self._request(repo, paper_runtime=paper_runtime)

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 0}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="target reached"))

        self.assertEqual(response["mode"], "live")
        self.assertEqual(response["status"], "closed")
        self.assertEqual(response["run"]["status"], "closed")
        self.assertEqual(repo.runs["run-live"]["runtime_state"]["exit_reason"], "target reached")
        paper_runtime.exit_strategy.assert_not_called()

    async def test_live_worker_exit_defers_when_no_grouped_legs_but_broker_exposure_still_exists(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_broker_positions["run-live"] = [
            {
                "account_id": "kite:AB1234",
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 1,
            }
        ]
        paper_runtime = SimpleNamespace(exit_strategy=AsyncMock())
        request = self._request(repo, paper_runtime=paper_runtime)

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="target reached"))

        self.assertEqual(response["status"], "deferred")
        self.assertTrue(response["deferred"])
        self.assertEqual(repo.runs["run-live"]["status"], "open")
        self.assertNotIn("exit_reason", repo.runs["run-live"].get("runtime_state") or {})
        paper_runtime.exit_strategy.assert_not_called()

    async def test_live_worker_exit_defers_when_direct_broker_orders_show_exposure_before_projection_catches_up(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_order_attribution_refs["run-live"] = {
            "broker_order_ids": [],
            "client_order_refs": ["KATAG01"],
        }
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = SimpleNamespace(
            orders=lambda kite, corr_id: [
                {
                    "order_id": "OID-1",
                    "tag": "KATAG01",
                    "instrument_token": 408065,
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "product": "CNC",
                }
            ]
        )
        fake_kite = SimpleNamespace(
            access_token="token",
            positions=lambda: {
                "net": [
                    {
                        "instrument_token": 408065,
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "product": "CNC",
                        "quantity": 1,
                        "average_price": 100.0,
                        "last_price": 101.0,
                    }
                ]
            },
        )

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=fake_kite), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="target reached"))

        self.assertEqual(response["status"], "deferred")
        self.assertTrue(response["deferred"])
        self.assertEqual(response["broker_positions"][0]["tradingsymbol"], "INFY")
        self.assertEqual(repo.runs["run-live"]["status"], "open")

    async def test_live_worker_exit_places_reducing_basket_and_keeps_run_exiting_until_flat(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_open_legs["run-live"] = [
            {
                "journal_run_id": "11111111-1111-4111-8111-111111111111",
                "account_id": "kite:AB1234",
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 1,
                "broker_net_quantity": 1,
            }
        ]
        live_orders = SimpleNamespace(
            place_basket=AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "status": "success",
                        "results": [{"index": 0, "tradingsymbol": "INFY", "order_id": "OID-EXIT", "status": "success"}],
                        "errors": [],
                    }
                )
            )
        )
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="operator exit", idempotency_key="exit-0001"))

        self.assertEqual(response["mode"], "live")
        self.assertEqual(response["status"], "exiting")
        self.assertEqual(repo.runs["run-live"]["status"], "exiting")
        live_orders.place_basket.assert_awaited_once()
        planned_orders = repo.runs["run-live"]["runtime_state"]["live_exit"]["orders"]
        self.assertEqual(planned_orders[0]["transaction_type"], "SELL")
        self.assertEqual(planned_orders[0]["quantity"], 1)
        self.assertEqual(planned_orders[0]["market_protection"], -1)
        self.assertEqual(planned_orders[0]["attribution"]["strategy_run_id"], "run-live")
        self.assertEqual(live_orders.place_basket.await_args.kwargs["idempotency_key"], "exit-0001")

    async def test_live_worker_exit_rejects_when_broker_position_cannot_cover_attributed_leg(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_open_legs["run-live"] = [
            {
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 3,
                "broker_net_quantity": 1,
            }
        ]
        live_orders = SimpleNamespace(place_basket=AsyncMock())
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("backend.api.routers.worker_execution._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_execution._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("backend.api.routers.worker_execution.asyncio.to_thread", _run_to_thread_inline):
            with self.assertRaises(HTTPException) as ctx:
                await exit_worker_run(request, "run-live", WorkerExitRequest(reason="operator exit"))

        self.assertEqual(ctx.exception.status_code, 409)
        live_orders.place_basket.assert_not_called()

    async def test_worker_run_pnl_snapshot_returns_zeroes_for_dry_run(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry"] = {
            "strategy_run_id": "run-dry",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)

        response = await get_worker_run_pnl(request, "run-dry")

        self.assertEqual(response["strategy_run_id"], "run-dry")
        self.assertEqual(response["execution_mode"], "dry_run")
        self.assertEqual(response["totals"]["net_pnl"], 0.0)
        self.assertFalse(response["is_realtime"])
        self.assertEqual(response["legs"], [])

    async def test_worker_run_pnl_snapshot_returns_paper_grouped_totals_and_legs(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-paper"] = {
            "strategy_run_id": "run-paper",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "metadata": {},
        }
        paper_runtime = SimpleNamespace(
            get_strategy_run_pnl=AsyncMock(
                return_value={
                    "currency": "INR",
                    "strategy": {
                        "status": "open",
                        "realized_pnl": 10.0,
                        "unrealized_pnl": 5.5,
                        "gross_pnl": 15.5,
                        "charges": 1.25,
                        "net_pnl": 14.25,
                        "last_updated_at": "2026-04-25T12:00:00+00:00",
                        "positions": [
                            {
                                "instrument_token": 408065,
                                "exchange": "NSE",
                                "tradingsymbol": "INFY",
                                "product": "CNC",
                                "net_quantity": 1,
                                "side": "LONG",
                                "average_price": 100.0,
                                "last_price": 105.5,
                                "realized_pnl": 10.0,
                                "unrealized_pnl": 5.5,
                                "gross_pnl": 15.5,
                                "charges": 1.25,
                                "net_pnl": 14.25,
                            }
                        ],
                    },
                }
            )
        )
        request = self._request(repo, paper_runtime=paper_runtime)

        response = await get_worker_run_pnl(request, "run-paper")

        self.assertEqual(response["totals"]["gross_pnl"], 15.5)
        self.assertEqual(response["totals"]["charges"], 1.25)
        self.assertEqual(response["legs"][0]["tradingsymbol"], "INFY")
        self.assertEqual(response["legs"][0]["net_pnl"], 14.25)

    async def test_worker_run_pnl_snapshot_returns_live_grouped_totals_and_legs(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-pnl"] = {
            "strategy_run_id": "run-live-pnl",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_journal_repository = SimpleNamespace(
            find_source_link=lambda **kwargs: SimpleNamespace(run_id="11111111-1111-4111-8111-111111111111"),
            list_execution_facts=lambda run_id: [
                SimpleNamespace(
                    id=1,
                    source_type="live_fill",
                    side="BUY",
                    quantity=1,
                    price=100.0,
                    fees_amount=0.8,
                    taxes_amount=0.2,
                    slippage_amount=0.0,
                    fill_timestamp=datetime.fromisoformat("2026-04-25T12:00:00+00:00"),
                    payload={"broker_fill": {"instrument_token": 408065, "exchange": "NSE", "tradingsymbol": "INFY", "product": "CNC"}},
                )
            ],
        )
        request.app.state.algo_worker_realtime_positions_service = SimpleNamespace(
            get_positions=AsyncMock(
                return_value={
                    "NSE:INFY:CNC": SimpleNamespace(
                        instrument_token=408065,
                        product="CNC",
                        quantity=1,
                        last_price=101.5,
                        last_reconciled_at="2026-04-25T12:00:05+00:00",
                    )
                }
            )
        )

        response = await get_worker_run_pnl(request, "run-live-pnl")

        self.assertEqual(response["totals"]["realized_pnl"], 0.0)
        self.assertEqual(response["totals"]["unrealized_pnl"], 1.5)
        self.assertEqual(response["totals"]["charges"], 1.0)
        self.assertEqual(response["totals"]["net_pnl"], 0.5)
        self.assertEqual(response["legs"][0]["broker_net_quantity"], 1)
        self.assertFalse(response["is_stale"])

    async def test_worker_run_pnl_snapshot_marks_live_leg_stale_when_broker_quantity_sign_is_opposite(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-stale"] = {
            "strategy_run_id": "run-live-stale",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_journal_repository = SimpleNamespace(
            find_source_link=lambda **kwargs: SimpleNamespace(run_id="11111111-1111-4111-8111-111111111111"),
            list_execution_facts=lambda run_id: [
                SimpleNamespace(
                    id=1,
                    source_type="live_fill",
                    side="BUY",
                    quantity=1,
                    price=100.0,
                    fees_amount=0.0,
                    taxes_amount=0.0,
                    slippage_amount=0.0,
                    fill_timestamp=datetime.fromisoformat("2026-04-25T12:00:00+00:00"),
                    payload={"broker_fill": {"instrument_token": 408065, "exchange": "NSE", "tradingsymbol": "INFY", "product": "CNC"}},
                )
            ],
        )
        request.app.state.algo_worker_realtime_positions_service = SimpleNamespace(
            get_positions=AsyncMock(
                return_value={
                    "NSE:INFY:CNC": SimpleNamespace(
                        instrument_token=408065,
                        product="CNC",
                        quantity=-1,
                        last_price=101.5,
                        last_reconciled_at="2026-04-25T12:00:05+00:00",
                    )
                }
            )
        )

        response = await get_worker_run_pnl(request, "run-live-stale")

        self.assertTrue(response["is_stale"])
        self.assertTrue(response["legs"][0]["is_stale"])

    async def test_worker_run_pnl_snapshot_falls_back_to_live_attribution_legs_without_journal_link(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-fallback"] = {
            "strategy_run_id": "run-live-fallback",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        repo.live_open_legs["run-live-fallback"] = [
            {
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 1,
                "broker_net_quantity": 1,
            }
        ]
        request = self._request(repo)
        request.app.state.algo_worker_journal_repository = SimpleNamespace(
            find_source_link=lambda **kwargs: None,
        )
        request.app.state.algo_worker_realtime_positions_service = SimpleNamespace(
            get_positions=AsyncMock(
                return_value={
                    "NSE:INFY:CNC": SimpleNamespace(
                        instrument_token=408065,
                        product="CNC",
                        quantity=1,
                        average_price=100.0,
                        last_price=101.5,
                        last_reconciled_at="2026-04-25T12:00:05+00:00",
                    )
                }
            )
        )

        response = await get_worker_run_pnl(request, "run-live-fallback")

        self.assertEqual(response["position_count"], 1)
        self.assertEqual(response["legs"][0]["tradingsymbol"], "INFY")
        self.assertEqual(response["legs"][0]["average_price"], 100.0)
        self.assertEqual(response["totals"]["unrealized_pnl"], 1.5)
        self.assertEqual(response["totals"]["charges"], 0.0)
        self.assertFalse(response["is_stale"])

    async def test_worker_run_pnl_stream_returns_sse_snapshot(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry-stream"] = {
            "strategy_run_id": "run-dry-stream",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)

        response = await stream_worker_run_pnl(request, "run-dry-stream", interval_seconds=0.25)
        chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertIn("run-dry-stream", chunk)
        self.assertIn("data:", chunk)

    async def test_worker_run_pnl_stream_refreshes_run_status_between_events(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry-stream-status"] = {
            "strategy_run_id": "run-dry-stream-status",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)
        request.is_disconnected = AsyncMock(side_effect=[False, False, True])

        response = await stream_worker_run_pnl(request, "run-dry-stream-status", interval_seconds=0.25)
        first = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]
        repo.runs["run-dry-stream-status"]["status"] = "closed"
        second = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertIn('"status": "open"', first)
        self.assertIn('"status": "closed"', second)


class AlgoWorkerRepositoryMappingTests(unittest.TestCase):
    def test_run_view_includes_run_session_fields(self):
        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(text("INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, status) VALUES ('worker-1', 'Worker', 'hash', 'active')"))
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode, status,
                        worker_session_nonce, worker_session_claimed_at, last_heartbeat_at
                    ) VALUES (
                        'run-1', 'worker-1', 'tmpl', 'kite:paper-a', 'paper', 'open',
                        'nonce-1', '2026-05-06T09:15:00+00:00', '2026-05-06T09:16:00+00:00'
                    )
                    """
                )
            )
            db.commit()

        row = repo._get_run_sync("run-1")
        if row is None:
            self.fail("expected run row")
        self.assertEqual(row["worker_session_nonce"], "nonce-1")
        self.assertIsNotNone(row["worker_session_claimed_at"])
        self.assertIsNotNone(row["last_heartbeat_at"])

    def test_claim_run_session_single_winner(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-claim"] = {
            "strategy_run_id": "run-claim",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
        }
        first = asyncio.run(repo.claim_run_session("run-claim", freshness_seconds=60, claimed_without_heartbeat_seconds=120))
        second = asyncio.run(repo.claim_run_session("run-claim", freshness_seconds=60, claimed_without_heartbeat_seconds=120))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_release_run_session_clears_claim_keeps_last_heartbeat(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-release"] = {
            "strategy_run_id": "run-release",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-1",
            "worker_session_claimed_at": datetime.now(timezone.utc),
            "last_heartbeat_at": datetime.now(timezone.utc),
        }

        released = asyncio.run(repo.release_run_session("run-release", expected_nonce="nonce-1"))
        if released is None:
            self.fail("expected released row")
        self.assertIsNone(released["worker_session_nonce"])
        self.assertIsNone(released["worker_session_claimed_at"])
        self.assertIsNotNone(released["last_heartbeat_at"])

    def test_record_run_heartbeat_rejects_mismatched_nonce(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-heartbeat"] = {
            "strategy_run_id": "run-heartbeat",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-1",
            "worker_session_claimed_at": datetime.now(timezone.utc),
        }

        rejected = asyncio.run(repo.record_run_heartbeat("run-heartbeat", expected_nonce="wrong"))
        accepted = asyncio.run(repo.record_run_heartbeat("run-heartbeat", expected_nonce="nonce-1"))
        self.assertIsNone(rejected)
        if accepted is None:
            self.fail("expected accepted row")
        self.assertIsNotNone(accepted["last_heartbeat_at"])

    def test_run_view_with_worker_falls_back_to_token_heartbeat_for_legacy_run(self):
        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_tokens (
                        token_id, name, token_hash, status, last_heartbeat_at, heartbeat_json
                    ) VALUES (
                        'worker-legacy', 'Worker', 'hash-legacy', 'active',
                        '2026-05-06T09:16:00+00:00', '{"worker_id":"w-1"}'
                    )
                    """
                )
            )
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode, status
                    ) VALUES (
                        'run-legacy-heartbeat', 'worker-legacy', 'tmpl', 'kite:paper-a', 'paper', 'open'
                    )
                    """
                )
            )
            db.commit()

        rows = repo._list_runs_for_control_plane_sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["token_last_heartbeat_at"]), "2026-05-06T09:16:00+00:00")
        self.assertEqual(str(rows[0]["last_heartbeat_at"]), "2026-05-06T09:16:00+00:00")

    async def test_stale_recovery_list_includes_claimed_without_heartbeat_runs(self):
        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(text("INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, status) VALUES ('worker-2', 'Worker', 'hash-2', 'active')"))
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode, status,
                        worker_session_nonce, worker_session_claimed_at
                    ) VALUES (
                        'run-claimed-no-heartbeat', 'worker-2', 'tmpl', 'kite:paper-a', 'paper', 'open',
                        'nonce-claimed', '2020-01-01T00:00:00+00:00'
                    )
                    """
                )
            )
            db.commit()

        rows = await repo.list_stale_recovery_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_run_id"], "run-claimed-no-heartbeat")

    def test_claim_session_returns_nonce_for_owned_run(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-claim-route"] = {
            "strategy_run_id": "run-claim-route",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )

        response = asyncio.run(claim_worker_run_session(request, "run-claim-route"))
        self.assertEqual(response["strategy_run_id"], "run-claim-route")
        self.assertTrue(response["worker_session_nonce"])

    def test_claim_session_endpoint_returns_nonce_for_owned_sqlalchemy_run(self):
        from backend.api.routers import worker_auth as worker_auth_module

        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_tokens (
                        token_id, name, token_hash, status, allowed_modes, allowed_actions,
                        allowed_templates, account_scope
                    ) VALUES (
                        'worker-claim-endpoint', 'Worker', :token_hash, 'active',
                        :allowed_modes, :allowed_actions, :allowed_templates, 'kite:paper-a'
                    )
                    """
                ),
                {
                    "token_hash": _hash_token("secret-token"),
                    "allowed_modes": json.dumps(["paper", "dry_run"]),
                    "allowed_actions": json.dumps(["runs:create", "runs:read", "heartbeat"]),
                    "allowed_templates": json.dumps([]),
                },
            )
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode,
                        status, runtime_state_json, metadata_json
                    ) VALUES (
                        'run-claim-endpoint', 'worker-claim-endpoint', 'sdk-mean-reversion',
                        'kite:paper-a', 'dry_run', 'open', '{}', '{}'
                    )
                    """
                )
            )
            db.commit()

        app = FastAPI()
        app.state.algo_worker_repository = repo
        app.include_router(worker_auth_module.router, prefix="/api")
        client = TestClient(app)

        response = client.post(
            "/api/algo-workers/worker/runs/run-claim-endpoint/claim-session",
            headers={"Authorization": "Bearer secret-token"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["strategy_run_id"], "run-claim-endpoint")
        self.assertTrue(payload["worker_session_nonce"])

    async def test_submit_worker_intent_rejects_missing_session_nonce(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-session-enforced"] = {
            "strategy_run_id": "run-session-enforced",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-active",
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=SimpleNamespace(place_order=AsyncMock(return_value={"status": "success"})))),
            is_disconnected=AsyncMock(return_value=False),
        )

        with self.assertRaises(HTTPException) as exc:
            await submit_worker_intent(
                request,
                "run-session-enforced",
                WorkerIntentRequest(intent_type="place_order", payload={"order": {}}, idempotency_key="run-session-enforced:intent:1"),
            )
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail["rejection_reason"], "WORKER_SESSION_REQUIRED")

    async def test_submit_worker_intent_accepts_legacy_run_without_claimed_session(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-legacy"] = {
            "strategy_run_id": "run-legacy",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=SimpleNamespace(place_order=AsyncMock(return_value={"status": "success"})))),
            is_disconnected=AsyncMock(return_value=False),
        )

        response = await submit_worker_intent(
            request,
            "run-legacy",
            WorkerIntentRequest(intent_type="place_order", payload={"order": {}}, idempotency_key="run-legacy:intent:1"),
        )
        self.assertEqual(response["status"], "accepted")

    async def test_stale_recovery_list_excludes_protection_owned_runs(self):
        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(text("INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, status) VALUES ('worker-1', 'Worker', 'hash', 'active')"))
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode, status,
                        runtime_state_json, worker_session_nonce, last_heartbeat_at
                    ) VALUES (
                        'run-protected', 'worker-1', 'tmpl', 'kite:paper-a', 'live', 'open',
                        '{"backend_protection":{"enabled":true,"operations":{"exit_on_worker_stale":true}}}',
                        'nonce-1', '2026-05-06T09:00:00+00:00'
                    )
                    """
                )
            )
            db.commit()

        rows = await repo.list_stale_recovery_runs()
        self.assertEqual(rows, [])

    async def test_exiting_recovery_list_includes_exiting_runs(self):
        repo = _sqlite_algo_worker_repo()
        with repo.session_factory() as db:
            db.execute(text("INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, status) VALUES ('worker-1', 'Worker', 'hash', 'active')"))
            db.execute(
                text(
                    """
                    INSERT INTO public.algo_worker_runs (
                        strategy_run_id, token_id, template_id, account_scope, execution_mode, status
                    ) VALUES (
                        'run-exiting', 'worker-1', 'tmpl', 'kite:paper-a', 'live', 'exiting'
                    )
                    """
                )
            )
            db.commit()

        rows = await repo.list_exiting_recovery_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_run_id"], "run-exiting")

    async def test_release_session_and_run_heartbeat_routes(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-session-routes"] = {
            "strategy_run_id": "run-session-routes",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-1",
            "worker_session_claimed_at": datetime.now(timezone.utc),
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token", "X-Worker-Session-Nonce": "nonce-1"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )
        heartbeat = await heartbeat_worker_run_session(request, "run-session-routes", payload=SimpleNamespace())
        self.assertEqual(heartbeat["status"], "ok")
        released = await release_worker_run_session(request, "run-session-routes")
        self.assertEqual(released["status"], "released")

    async def test_claimed_session_route_conflict_and_missing_nonce(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-claim-conflict"] = {
            "strategy_run_id": "run-claim-conflict",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-existing",
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )

        with self.assertRaises(HTTPException) as claim_exc:
            await claim_worker_run_session(request, "run-claim-conflict")
        self.assertEqual(claim_exc.exception.status_code, 409)
        self.assertEqual(claim_exc.exception.detail["rejection_reason"], "WORKER_SESSION_CONFLICT")

        with self.assertRaises(HTTPException) as heartbeat_exc:
            await heartbeat_worker_run_session(request, "run-claim-conflict", payload=SimpleNamespace())
        self.assertEqual(heartbeat_exc.exception.status_code, 409)
        self.assertEqual(heartbeat_exc.exception.detail["rejection_reason"], "WORKER_SESSION_REQUIRED")

    async def test_worker_run_read_surface_includes_health_fields(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-health"] = {
            "strategy_run_id": "run-health",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {
                "runtime_recovery": {"recovery_status": "action_required", "action_required": True}
            },
            "metadata": {},
            "worker_session_nonce": "nonce-1",
            "last_heartbeat_at": datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )

        with patch("backend.api.routers.worker_auth._utcnow", return_value=datetime(2026, 5, 6, 9, 10, tzinfo=timezone.utc)):
            run = await get_worker_run(request, "run-health")

        self.assertEqual(run["health_status"], "disconnected")
        self.assertEqual(run["heartbeat_age_sec"], 600)
        self.assertEqual(run["session_status"], "stale")
        self.assertEqual(run["recovery_status"], "action_required")
        self.assertTrue(run["recovery_action_required"])

    async def test_worker_gtt_routes_reject_non_live_account_scope(self):
        token = WorkerToken(
            token_id="worker-1",
            name="paper-worker",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )

        with self.assertRaises(HTTPException) as exc:
            await list_worker_gtts(request)

        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(exc.exception.detail["rejection_reason"], "WORKER_ACCOUNT_SCOPE_UNSUPPORTED")

    async def test_worker_gtt_routes_accept_legacy_actions_and_place_trigger(self):
        token = WorkerToken(
            token_id="worker-1",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["runs:read", "intents:submit"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )
        payload = {
            "type": "single",
            "condition": {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "trigger_values": [1500.0],
                "last_price": 1495.0,
            },
            "orders": [
                {
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "transaction_type": "BUY",
                    "quantity": 1,
                    "product": "CNC",
                    "price": 1501.0,
                }
            ],
        }

        with patch("backend.api.routers.worker_market._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_market.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "broker_api.orders.gtt_service.place_gtt",
            AsyncMock(return_value=SimpleNamespace(model_dump=lambda mode="json": {"trigger_id": 321})),
        ):
            response = await create_worker_gtt_trigger(request, payload)

        self.assertEqual(response["trigger_id"] if isinstance(response, dict) else response.trigger_id, 321)

    async def test_worker_gtt_not_found_is_normalized(self):
        token = WorkerToken(
            token_id="worker-1",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["runs:read"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )

        with patch("backend.api.routers.worker_market._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_market.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "broker_api.orders.gtt_service.get_gtt",
            side_effect=HTTPException(status_code=404, detail="GTT trigger 999 not found"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await get_worker_gtt(request, 999)

        self.assertEqual(exc.exception.status_code, 404)
        self.assertEqual(exc.exception.detail["rejection_reason"], "GTT_TRIGGER_NOT_FOUND")

    async def test_worker_gtt_provider_unavailable_is_normalized(self):
        token = WorkerToken(
            token_id="worker-1",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=["intents:submit"],
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
            is_disconnected=AsyncMock(return_value=False),
        )
        payload = {
            "type": "single",
            "condition": {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "trigger_values": [1500.0],
                "last_price": 1495.0,
            },
            "orders": [
                {
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "transaction_type": "BUY",
                    "quantity": 1,
                    "product": "CNC",
                    "price": 1501.0,
                }
            ],
        }

        with patch("backend.api.routers.worker_market._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "backend.api.routers.worker_market.asyncio.to_thread",
            _run_to_thread_inline,
        ), patch(
            "broker_api.orders.gtt_service.modify_gtt",
            AsyncMock(side_effect=HTTPException(status_code=503, detail="Provider timeout or downtime.")),
        ):
            with self.assertRaises(HTTPException) as exc:
                await modify_worker_gtt_trigger(request, 77, payload)

        self.assertEqual(exc.exception.status_code, 503)
        self.assertEqual(exc.exception.detail["rejection_reason"], "GTT_PROVIDER_UNAVAILABLE")

    async def test_worker_mutations_reject_conflicting_session_nonce(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-conflict"] = {
            "strategy_run_id": "run-conflict",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "runtime_state": {},
            "metadata": {},
            "worker_session_nonce": "nonce-expected",
        }
        request = SimpleNamespace(
            headers={"authorization": "Bearer secret-token", "X-Worker-Session-Nonce": "nonce-wrong"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=SimpleNamespace(place_order=AsyncMock(return_value={"status": "success"}), exit_strategy=AsyncMock(return_value={"status": "success"})))),
            is_disconnected=AsyncMock(return_value=False),
        )

        with self.assertRaises(HTTPException) as intent_exc:
            await submit_worker_intent(
                request,
                "run-conflict",
                WorkerIntentRequest(intent_type="place_order", payload={"order": {}}, idempotency_key="run-conflict:intent:1"),
            )
        self.assertEqual(intent_exc.exception.status_code, 409)
        self.assertEqual(intent_exc.exception.detail["rejection_reason"], "WORKER_SESSION_CONFLICT")

        with self.assertRaises(HTTPException) as risk_exc:
            await patch_worker_run_risk(request, "run-conflict", WorkerRiskPatchRequest(patch={"x": 1}))
        self.assertEqual(risk_exc.exception.status_code, 409)
        self.assertEqual(risk_exc.exception.detail["rejection_reason"], "WORKER_SESSION_CONFLICT")

        with self.assertRaises(HTTPException) as protection_exc:
            await patch_worker_run_protection(
                request,
                "run-conflict",
                WorkerProtectionPatchRequest(backend_protection={"enabled": False}),
            )
        self.assertEqual(protection_exc.exception.status_code, 409)
        self.assertEqual(protection_exc.exception.detail["rejection_reason"], "WORKER_SESSION_CONFLICT")

        with self.assertRaises(HTTPException) as exit_exc:
            await exit_worker_run(request, "run-conflict", WorkerExitRequest(reason="x"))
        self.assertEqual(exit_exc.exception.status_code, 409)
        self.assertEqual(exit_exc.exception.detail["rejection_reason"], "WORKER_SESSION_CONFLICT")

    def test_run_view_with_worker_includes_heartbeat_fields(self):
        repository = SqlAlchemyAlgoWorkerRepository(session_factory=lambda: None)
        row = {
            "strategy_run_id": "run-live-1",
            "token_id": "worker-token-1",
            "template_id": "mean-reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "summary_fields_json": "[]",
            "risk_schema_json": "[]",
            "allowed_actions_json": '["exit_strategy"]',
            "runtime_state_json": "{}",
            "metadata_json": '{"strategy_name": "Mean Reversion"}',
            "created_at": None,
            "updated_at": None,
            "closed_at": None,
            "worker_name": "ml-box-worker",
            "last_heartbeat_at": datetime.fromisoformat("2026-04-25T12:00:00+00:00"),
            "heartbeat_json": '{"worker_id": "w-1", "metrics": {"machine_id": "ml-box-01"}}',
        }

        payload = repository._run_view_with_worker(row)

        self.assertEqual(payload["worker_name"], "ml-box-worker")
        self.assertEqual(payload["heartbeat_json"]["worker_id"], "w-1")
        self.assertEqual(payload["heartbeat_json"]["metrics"]["machine_id"], "ml-box-01")
        self.assertEqual(payload["last_heartbeat_at"], datetime.fromisoformat("2026-04-25T12:00:00+00:00"))


def test_worker_tick_websocket_sends_snapshot_then_ticks():
    repo = _FakeWorkerRepository(raw_token="secret-token")

    async def fake_stream_ticks_ws(websocket, token, symbols, instrument_tokens, mode):
        assert symbols == ["NSE:NIFTY 50"]
        assert instrument_tokens == []
        assert mode == "quote"
        yield "snapshot", {"ticks": [], "missing": []}

    market_data_service = SimpleNamespace(stream_ticks_ws=fake_stream_ticks_ws)
    with _test_client(repo=repo, market_data_service=market_data_service) as client:
        with client.websocket_connect("/api/algo-workers/worker/ws/market/ticks?token=secret-token&symbols=NSE:NIFTY%2050&mode=quote") as ws:
            first = ws.receive_json()
            assert first["event"] == "snapshot"


def test_worker_tick_websocket_requires_market_stream_permission():
    token = WorkerToken(
        token_id="worker-1",
        name="limited",
        account_scope="kite:paper-a",
        allowed_modes=["paper"],
        allowed_actions=["market:read"],
        allowed_templates=[],
    )
    repo = _FakeWorkerRepository(raw_token="secret-token", token=token)

    async def fake_stream_ticks_ws(websocket, token, symbols, instrument_tokens, mode):
        yield "snapshot", {"ticks": [], "missing": []}

    market_data_service = SimpleNamespace(stream_ticks_ws=fake_stream_ticks_ws)
    with _test_client(repo=repo, market_data_service=market_data_service) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/algo-workers/worker/ws/market/ticks?token=secret-token&symbols=NSE:NIFTY%2050&mode=quote"):
                pass


def test_worker_run_pnl_websocket_rejects_unknown_run():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    with _test_client(repo=repo) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/algo-workers/worker/ws/runs/missing-run/pnl?token=secret-token"):
                pass


def test_worker_run_pnl_websocket_rejects_invalid_interval_seconds():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    repo.runs["run-1"] = {
        "strategy_run_id": "run-1",
        "token_id": "worker-1",
        "template_id": "mean_reversion",
        "account_scope": "kite:paper-a",
        "execution_mode": "paper",
        "status": "open",
        "metadata": {},
    }
    with _test_client(repo=repo) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/algo-workers/worker/ws/runs/run-1/pnl?token=secret-token&interval_seconds=abc"):
                pass


# ---------------------------------------------------------------------------
# Investment read-route contract locks (kite-algo-worker SDK 0.7.6)
# ---------------------------------------------------------------------------

from backend.api.routers.worker_market import router as worker_market_router  # noqa: E402

_WORKER_API_FIXTURES = Path(__file__).parent.parent / "fixtures" / "worker_api" / "v1"
_INVESTMENT_AUTH = {"Authorization": "Bearer secret-token"}


def _worker_api_fixture(name):
    return json.loads((_WORKER_API_FIXTURES / name).read_text())


def _investment_client(repo):
    app = FastAPI()
    app.include_router(worker_market_router, prefix="/api")
    app.state.algo_worker_repository = repo
    return TestClient(app)


def test_worker_index_constituents_route_locks_v1_contract():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fixture = _worker_api_fixture("nifty500_constituents.json")
    with _investment_client(repo) as client:
        with patch(
            "backend.broker_api.instruments.index_ingestion.get_worker_index_snapshot",
            return_value=dict(fixture),
        ):
            response = client.get(
                "/api/algo-workers/worker/market/indices/Nifty500",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 200
    constituent_payload = response.json()
    assert constituent_payload["schema_version"] == 1
    assert constituent_payload["source_list"] == "Nifty500"
    assert constituent_payload["complete"] is True
    assert all(member["exchange"] == "NSE" for member in constituent_payload["members"])


def test_worker_index_status_route_locks_v1_contract():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fixture = _worker_api_fixture("nifty500_status.json")
    with _investment_client(repo) as client:
        with patch(
            "backend.broker_api.instruments.index_ingestion.get_worker_index_status",
            return_value=dict(fixture),
        ):
            response = client.get(
                "/api/algo-workers/worker/market/indices/Nifty500/status",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 200
    status_payload = response.json()
    assert status_payload["schema_version"] == 1
    assert status_payload["source_list"] == "Nifty500"
    assert status_payload["complete"] is True
    assert status_payload["actual_member_count"] == 500


def test_worker_market_calendar_route_locks_v1_contract():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fixture = _worker_api_fixture("calendar.json")
    fake_conn = Mock()
    with _investment_client(repo) as client:
        with (
            patch("backend.app.database.get_db_connection", return_value=fake_conn),
            patch(
                "backend.broker_api.market.exchange_calendar.get_calendar_sessions",
                return_value=dict(fixture),
            ) as sessions_mock,
        ):
            response = client.get(
                "/api/algo-workers/worker/market/calendar?from=2026-09-01&to=2026-12-31&exchange=NSE&segment=CM",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 200
    calendar_payload = response.json()
    assert calendar_payload["schema_version"] == 1
    assert calendar_payload["exchange"] == "NSE"
    assert calendar_payload["segment"] == "CM"
    assert calendar_payload["calendar_version"] >= 1
    assert isinstance(calendar_payload["sessions"], list) and calendar_payload["sessions"]
    assert calendar_payload["sessions"][0]["session_type"] == "REGULAR"
    assert sessions_mock.call_args.kwargs["exchange"] == "NSE"
    assert sessions_mock.call_args.kwargs["segment"] == "CM"


def test_worker_market_calendar_status_route_locks_v1_contract():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fixture = _worker_api_fixture("calendar_status.json")
    fake_conn = Mock()
    with _investment_client(repo) as client:
        with (
            patch("backend.app.database.get_db_connection", return_value=fake_conn),
            patch(
                "backend.broker_api.market.exchange_calendar.get_calendar_status",
                return_value=dict(fixture),
            ) as status_mock,
        ):
            response = client.get(
                "/api/algo-workers/worker/market/calendar/status?exchange=nse&segment=cm",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 200
    status_payload = response.json()
    assert status_payload["schema_version"] == 1
    assert status_payload["source"] == "exchange_calendar_refresh"
    assert status_payload["exchange"] == "NSE"
    assert status_payload["segment"] == "CM"
    assert status_payload["active_calendar_version"] == 3
    assert status_payload["coverage_start"] == "2026-01-01"
    assert status_payload["coverage_end"] == "2026-12-31"
    assert status_payload["complete"] is True
    # Coverage through 2026-12-31 is outside the 45-day window from 2026-09-05.
    assert status_payload["expiry_warning"] is False
    assert status_mock.call_args.args[1] == "NSE"
    assert status_mock.call_args.args[2] == "CM"


def test_worker_market_calendar_status_route_reports_missing_schema_as_503():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fake_conn = Mock()
    from backend.broker_api.market.exchange_calendar import CalendarSchemaMigrationRequired

    with _investment_client(repo) as client:
        with (
            patch("backend.app.database.get_db_connection", return_value=fake_conn),
            patch(
                "backend.broker_api.market.exchange_calendar.get_calendar_status",
                side_effect=CalendarSchemaMigrationRequired("EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"),
            ),
        ):
            response = client.get(
                "/api/algo-workers/worker/market/calendar/status",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 503
    assert response.json()["detail"]["rejection_reason"] == "EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"


def test_worker_account_portfolio_route_locks_v1_contract():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    fixture = _worker_api_fixture("portfolio_success.json")
    with _investment_client(repo) as client:
        with (
            patch(
                "backend.api.routers.worker_market._load_live_kite_for_worker_account_scope",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "backend.broker_api.account.portfolio_snapshot.build_portfolio_snapshot",
                return_value=dict(fixture),
            ) as snapshot_mock,
        ):
            response = client.get(
                "/api/algo-workers/worker/account/portfolio",
                headers=_INVESTMENT_AUTH,
            )
    assert response.status_code == 200
    portfolio_payload = response.json()
    assert portfolio_payload["schema_version"] == 1
    assert portfolio_payload["account_scope"] == "kite:SANITIZED"
    assert portfolio_payload["coherent"] is True
    assert portfolio_payload["coherence_skew_ms"] >= 0
    assert portfolio_payload["funds"]["equity"]["available"]["cash"] == 50000
    assert snapshot_mock.call_args.args[1] == "kite:paper-a"


def test_worker_investment_read_routes_reject_unsupported_schema_version():
    repo = _FakeWorkerRepository(raw_token="secret-token")
    routes = [
        "/api/algo-workers/worker/market/indices/Nifty500",
        "/api/algo-workers/worker/market/indices/Nifty500/status",
        "/api/algo-workers/worker/market/calendar?from=2026-09-01&to=2026-12-31",
        "/api/algo-workers/worker/market/calendar/status",
        "/api/algo-workers/worker/account/portfolio",
    ]
    with _investment_client(repo) as client:
        for route in routes:
            response = client.get(f"{route}&schema_version=2" if "?" in route else f"{route}?schema_version=2", headers=_INVESTMENT_AUTH)
            assert response.status_code == 422, route
            assert response.json()["detail"]["rejection_reason"] == "UNSUPPORTED_SCHEMA_VERSION", route


if __name__ == "__main__":
    unittest.main()
