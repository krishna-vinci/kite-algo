#!/usr/bin/env python
"""Phase 5 harness: real API + supervisor child + disposable PostgreSQL.

REAL here: a uniquely named disposable PostgreSQL database on
``127.0.0.1:15433`` (never production 15432) migrated to the real alembic head
and dropped afterwards; the production routers served over loopback; the real
``backend.strategies.supervisor`` claiming the job, calling the lifecycle API
and spawning the real child (``python -m kite_algo_worker.hosted <example>``)
with a lifecycle-issued child credential; and the real governed pipeline
(proposal -> frozen plan -> durable execution request -> owner approval or an
owner-issued grant -> reservation -> structural approval -> execution ->
attributed book).

SIMULATED boundaries only: the market-data source (deterministic synthetic
quotes/candles and one synthetic option chain) and the broker (everything is
paper). No production configuration is read, no notification is sent, no live
gate is opened, and no shared schema is reset.

    timeout 900 .venv/bin/python examples/hosted_platform/run_phase5_acceptance.py

``--keep-database`` keeps the disposable database for inspection;
``--only`` runs a subset of the scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = Path(__file__).resolve().parent
EVIDENCE_DIR = EXAMPLES / "evidence"
WORKSPACE = REPO / ".phase5-workspace"

sys.path.insert(0, str(REPO))

# The isolated-instance plumbing (disposable database lifecycle, loopback API
# server, operator session, supervisor launcher, terminal-state waits) is shared
# with the earlier acceptance driver instead of being duplicated.
from examples.hosted_acceptance import run_acceptance as acc  # noqa: E402
from examples.hosted_platform._assertions import (  # noqa: E402
    assert_recovery,
    assert_scenario,
)

ACCOUNT_SCOPE = "kite:paper-phase5"


def account_for(label: str) -> str:
    """One isolated paper account per scenario.

    Scenarios must not share capital: a portfolio target sized against an account
    a previous example already spent would refuse with CAPACITY_EXCEEDED for an
    unrelated reason. Each example therefore gets its own account.
    """
    return f"kite:paper-{label}"

STORAGE: Dict[str, Any] = {
    "generation": str(uuid.uuid4()),
    "instruments": {
        "NIFTY 50": (256265, "INDEX", 1, "NSE", "NIFTY 50"),
        "RELIANCE": (738561, "EQ", 1, "NSE", "RELIANCE INDUSTRIES"),
        "INFY": (408065, "EQ", 1, "NSE", "INFOSYSTEMS"),
        "TCS": (2953217, "EQ", 1, "NSE", "TATA CONSULTANCY"),
        "HDFCBANK": (341249, "EQ", 1, "NSE", "HDFC BANK"),
    },
    "prices": {256265: 22520.0, 738561: 1500.0, 408065: 1450.0, 2953217: 3900.0, 341249: 1650.0},
    #: The last synthetic index print departs sharply ABOVE the trend, so the
    #: index-ticker examples see a genuine bullish setup instead of a flat or
    #: falling one. Quotes are derived from the candle tail plus this premium.
    "index_premium": 40.0,
    "option_underlying": "NIFTY",
    "option_expiry": "2026-10-29",
    "option_strikes": [22400, 22450, 22500, 22550, 22600, 22650, 22700, 22750],
    "option_tokens": {},
}

RESULT: Dict[str, Any] = {"steps": [], "scenarios": {}, "errors": []}


def step(name: str, **detail: Any) -> None:
    entry = {"step": name, "at": time.time(), **detail}
    RESULT["steps"].append({key: value for key, value in entry.items() if _jsonable(value)})
    print(f"[phase5] {name} {json.dumps(detail, default=str)}", flush=True)


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def fail(where: str, exc: BaseException) -> None:
    RESULT["errors"].append({"where": where, "error": repr(exc), "trace": traceback.format_exc()})
    print(f"[phase5] FAIL {where}: {exc!r}", file=sys.stderr, flush=True)


# ------------------------------------------------------------- boundaries


def _synthetic_candles() -> List[Dict[str, Any]]:
    """A deterministic rising series so the indicator signal is reproducible."""
    base = datetime(2026, 9, 23, 3, 45, tzinfo=timezone.utc)
    price = 22300.0
    rows: List[Dict[str, Any]] = []
    for index in range(40):
        price += 6.5
        rows.append(
            {
                "ts": (base + timedelta(minutes=5 * index)).isoformat(),
                "open": price - 3.0,
                "high": price + 4.0,
                "low": price - 5.0,
                "close": price,
                "volume": 1000 + index * 10,
            }
        )
    return rows


class SyntheticMarket:
    """The market-data boundary: deterministic quotes, candles and one chain."""

    def __init__(self) -> None:
        self.candle_rows = _synthetic_candles()
        for index, strike in enumerate(STORAGE["option_strikes"]):
            STORAGE["option_tokens"][strike] = {"ce": 50000 + index * 2, "pe": 50001 + index * 2}

    def option_price(self, token: int) -> float:
        spot = float(STORAGE["prices"][256265])
        for strike, tokens in STORAGE["option_tokens"].items():
            distance = abs(float(strike) - spot)
            base = max(5.0, 250.0 - distance)
            if token == tokens["ce"]:
                return round(base, 2)
            if token == tokens["pe"]:
                return round(max(5.0, base * 0.9), 2)
        return 100.0

    def price_for(self, token: int) -> float:
        if int(token) == 256265:
            # The index print departs above the trend's own last close, so the
            # "price above the indicator" setup is real rather than accidental.
            return float(self.candle_rows[-1]["close"]) + float(STORAGE["index_premium"])
        price = STORAGE["prices"].get(int(token))
        return float(price) if price is not None else self.option_price(int(token))

    def quote_payload(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **instrument,
            "mode": "quote",
            "last_price": self.price_for(int(instrument["instrument_token"])),
            "change": 0.0,
            "ohlc": None,
            "volume": 1000,
            "last_quantity": 10,
            "average_price": None,
            "buy_quantity": None,
            "sell_quantity": None,
            "depth": None,
            "depth_available": False,
            "depth_unavailable_reason": "synthetic",
            "last_trade_time": None,
            "exchange_timestamp": None,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "age_ms": 5,
            "is_stale": False,
        }

    def option_snapshot(self, underlying: str) -> Dict[str, Any]:
        spot = float(STORAGE["prices"][256265])
        rows = []
        for strike in STORAGE["option_strikes"]:
            tokens = STORAGE["option_tokens"][strike]
            rows.append(
                {
                    "strike": strike,
                    "CE": {
                        "token": tokens["ce"],
                        "tsym": f"NIFTY26OCT{strike}CE",
                        "ltp": self.option_price(tokens["ce"]),
                        "oi": 1200,
                        "iv": 0.14,
                        "delta": round(max(0.05, 1.0 - abs(strike - spot) / 500.0), 4),
                        "gamma": 0.0002,
                        "theta": -6.5,
                        "vega": 12.0,
                        "rho": 1.2,
                    },
                    "PE": {
                        "token": tokens["pe"],
                        "tsym": f"NIFTY26OCT{strike}PE",
                        "ltp": self.option_price(tokens["pe"]),
                        "oi": 1300,
                        "iv": 0.15,
                        "delta": round(-max(0.05, 1.0 - abs(strike - spot) / 500.0), 4),
                        "gamma": 0.0002,
                        "theta": -6.0,
                        "vega": 11.5,
                        "rho": -1.1,
                    },
                }
            )
        return {
            "underlying": underlying.upper(),
            "spot_ltp": spot,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "expiries": [STORAGE["option_expiry"]],
            "per_expiry": {
                STORAGE["option_expiry"]: {
                    "atm_strike": 22500,
                    "rows": rows,
                    "forward": spot,
                    "sigma_expiry": 0.14,
                }
            },
        }


class SyntheticOptionManager:
    """The options session-manager boundary: one deterministic snapshot."""

    class _Repo:
        def normalize_underlying_symbol(self, value: str):
            return str(value).strip().upper(), None

    def __init__(self, market: SyntheticMarket) -> None:
        self.instrument_repo = self._Repo()
        self._market = market

    def normalize_underlying_symbol(self, value: str) -> str:
        return str(value).strip().upper()

    def get_snapshot(self, underlying: str):
        if str(underlying).strip().upper() != STORAGE["option_underlying"]:
            return None
        return self._market.option_snapshot(underlying)

    def get_watchlist(self):
        return [{"underlying": STORAGE["option_underlying"], "is_running": True}]

    async def start_sessions(self, items, replace: bool = False):  # noqa: ANN001
        return {"ok": True}


# ------------------------------------------------------------- API process


def build_app(session_factory, market: SyntheticMarket):
    from fastapi import FastAPI

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import auth as auth_module
    from backend.api.routers import hosted_lifecycle, strategies, worker_auth, worker_execution
    from backend.api.routers import worker_executions, worker_market, worker_proposals, worker_universes
    from backend.api.services.market_data import WorkerMarketDataService
    from backend.options.api import worker_options_router
    from backend.options.api.market_router import get_options_session_manager
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore

    class HarnessMarketService(WorkerMarketDataService):
        """The production service with ONLY its market-data source replaced."""

        async def get_quotes(self, request):  # noqa: ANN001
            resolved = await self.resolve_many(
                symbols=request.symbols, instrument_tokens=request.instrument_tokens
            )
            return {
                "quotes": [market.quote_payload(item) for item in resolved["instruments"]],
                "missing": list(resolved["missing"]),
            }

        async def get_candles(  # noqa: ANN001
            self, *, symbol=None, instrument_token=None, interval="5minute", lookback=50
        ):
            instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
            rows = list(market.candle_rows)[-int(lookback) :]
            candles = [self._normalize_candle(row, is_complete=True) for row in rows]
            candles = [row for row in candles if row is not None]
            return {
                "symbol": instrument["symbol"],
                "instrument_token": instrument["instrument_token"],
                "interval": interval,
                "candles": candles,
                "current": candles[-1] if candles else None,
                "is_stale": not candles,
            }

    app = FastAPI(title="phase5 loopback API")
    for router in (
        auth_module.router,
        worker_auth.router,
        worker_execution.router,
        worker_proposals.router,
        worker_executions.router,
        worker_market.router,
        worker_universes.router,
        strategies.router,
        hosted_lifecycle.router,
    ):
        app.include_router(router, prefix="/api")
    # The options worker router already declares its own full
    # ``/api/algo-workers/worker/options`` prefix, so it is mounted as-is.
    app.include_router(worker_options_router.router)

    app.state.strategies_session_factory = session_factory
    app.state.attribution_store = SqlAttributionStore(session_factory=session_factory)
    app.state.algo_worker_repository = SqlAlchemyAlgoWorkerRepository(session_factory)
    app.state.paper_runtime_service = PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory),
        market_data_runtime=acc.SyntheticQuotes(),
    )
    # The shared pipeline the operator routes and the dispatcher both use. The
    # paper executor is the production one; only the market boundary above is
    # simulated.
    from backend.strategies.admission import AdmissionService
    from backend.strategies.approvals import ApprovalService
    from backend.strategies.execution import PaperPlanExecutor
    from backend.strategies.plan_pipeline import PlanExecutionPipeline
    from backend.strategies.proposals import ProposalStore
    from backend.strategies.reservations import ReservationLedger

    app.state.plan_execution_pipeline = PlanExecutionPipeline(
        session_factory,
        proposal_store=ProposalStore(session_factory=session_factory),
        admission_service=AdmissionService(session_factory=session_factory),
        reservation_ledger=ReservationLedger(session_factory=session_factory),
        approval_service=ApprovalService(session_factory=session_factory),
        paper_executor_factory=lambda: PaperPlanExecutor(
            session_factory=session_factory,
            paper_service=app.state.paper_runtime_service,
        ),
    )
    app.state.worker_market_data_service = HarnessMarketService()
    app.state.options_session_manager = SyntheticOptionManager(market)
    # The harness drives ONE bounded dispatcher pass per poll using the SAME
    # service the production loop would use (same pipeline, same executors).
    from backend.strategies.execution_dispatcher import HostedExecutionDispatcher
    from backend.strategies.execution_requests import ExecutionRequestService

    app.state.hosted_execution_dispatcher = HostedExecutionDispatcher(
        session_factory,
        service=ExecutionRequestService(
            session_factory, pipeline=app.state.plan_execution_pipeline
        ),
    )
    app.dependency_overrides[get_options_session_manager] = lambda: app.state.options_session_manager
    return app


# ------------------------------------------------------------- fixtures


def seed_catalog(session_factory) -> None:
    from sqlalchemy import text

    generation = STORAGE["generation"]
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": generation},
        )
        for symbol, (token, kind, lot, exchange, name) in STORAGE["instruments"].items():
            instrument_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{symbol}"))
            # The catalog's public key normalizes the ticker: ``NIFTY 50`` is
            # stored as ``NSE:NIFTY50`` and resolved through that key.
            public_key = f"{exchange}:{symbol.upper().replace(' ', '')}"
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records ("
                    " instrument_id, identity_key, public_key, exchange, tradingsymbol,"
                    " lifecycle_status,"
                    " current_generation_id, instrument_type, lot_size, tick_size, name)"
                    " VALUES (:iid, :ikey, :pkey, :ex, :sym, 'active', :gen, :kind, :lot, 0.05,"
                    "         :name)"
                ),
                {
                    "iid": instrument_id,
                    "ikey": public_key,
                    "pkey": public_key,
                    "ex": exchange,
                    "sym": symbol.upper(),
                    "gen": generation,
                    "kind": kind,
                    "lot": lot,
                    "name": name,
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings ("
                    " mapping_id, instrument_id, broker, broker_exchange, broker_symbol,"
                    " broker_token, valid_from_generation, is_current)"
                    " VALUES (:mid, :iid, 'kite', :ex, :sym, :token, :gen, true)"
                ),
                {
                    "mid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:map:{symbol}")),
                    "iid": instrument_id,
                    "ex": exchange,
                    "sym": symbol.upper(),
                    "token": str(token),
                    "gen": generation,
                },
            )
        for strike, tokens in STORAGE["option_tokens"].items():
            for kind, token in (("CE", tokens["ce"]), ("PE", tokens["pe"])):
                symbol = f"NIFTY26OCT{strike}{kind}"
                instrument_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{symbol}"))
                public_key = f"NFO:{symbol}"
                session.execute(
                    text(
                        "INSERT INTO public.instrument_catalog_records ("
                        " instrument_id, identity_key, public_key, exchange, tradingsymbol,"
                        " lifecycle_status,"
                        " current_generation_id, instrument_type, expiry, lot_size, tick_size,"
                        " underlying, strike, option_type)"
                        " VALUES (:iid, :ikey, :pkey, 'NFO', :sym, 'active', :gen, :kind, :expiry,"
                        "         50, 0.05, 'NIFTY', :strike, :opt)"
                    ),
                    {
                        "iid": instrument_id,
                        "ikey": public_key,
                        "pkey": public_key,
                        "sym": symbol,
                        "gen": generation,
                        "kind": kind,
                        "expiry": STORAGE["option_expiry"],
                        "strike": strike,
                        "opt": kind,
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO public.instrument_broker_mappings ("
                        " mapping_id, instrument_id, broker, broker_exchange, broker_symbol,"
                        " broker_token, valid_from_generation, is_current)"
                        " VALUES (:mid, :iid, 'kite', 'NFO', :sym, :token, :gen, true)"
                    ),
                    {
                        "mid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:map:{symbol}")),
                        "iid": instrument_id,
                        "sym": symbol,
                        "token": str(token),
                        "gen": generation,
                    },
                )
        session.commit()


def seed_universe(session_factory) -> str:
    """One owner-owned explicit universe, created through the PRODUCTION service.

    The revision is produced by ``UniverseService.resolve_membership`` (the same
    code an operator's resolve call uses), so the membership format, the
    coverage block and the persisted revision are the platform's own - not a
    fixture that only looks like them.
    """
    from backend.workflows.universes import UniverseService

    name = "phase5-equities"
    # Explicit members are exchange-qualified public keys, which is the format
    # the universe service validates and persists. The weights compiler resolves
    # either spelling, so the same revision works for both readers.
    members = ["NSE:RELIANCE", "NSE:INFY", "NSE:TCS", "NSE:HDFCBANK"]
    service = UniverseService(session_factory=session_factory)
    service.create_universe(
        owner_id="app:admin",
        name=name,
        kind="explicit",
        source_config={"members": members},
    )
    service.resolve_membership("app:admin", name)
    return name


# ------------------------------------------------------------- scenarios


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "index_indicator": {
        "source": "index_indicator_strategy.py",
        "schema": "index_indicator.schema.json",
        "expects_open_exposure": True,
        "autonomous": False,
        "expected_requests": 1,
        # A manual request waits for its owner; nothing may be ordered before it.
        "expects_manual": True,
        "expected_orders": 1,
        "expected_order_rows": [
            {"tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 5}
        ],
        "expected_positions": {"RELIANCE": 5},
        "final_marker": "settled: coverage=known pending=0 positions=",
        "expected_settlement_axes": {"attribution_scoped_flatness": "failed"},
        "params": {
            "index_symbol": "NIFTY 50",
            "interval": "5minute",
            "lookback": 40,
            "indicator": "ema",
            "period": 9,
            "traded_symbol": "RELIANCE",
            "traded_exchange": "NSE",
            "product": "CNC",
            "quantity": 5,
            "deadline_seconds": 240,
        },
    },
    "options_adjustment": {
        "source": "options_index_setup_adjustment.py",
        "schema": "options_index_setup.schema.json",
        "autonomous": False,
        "expects_manual": True,
        # The entry runs, then the SAME strategy discovers its option run from
        # owned_work()["option_runs"] and submits one governed CLOSE. Two requests
        # total, and a repeat observation must not add a third.
        "expected_requests": 2,
        "expected_status_sequence": ["executed", "executed"],
        "requires_option_close": True,
        "final_marker": "structure closed with no outstanding work",
        # The options lane keeps its own book, so its settlement axes are the
        # evidence: quiescence is proven by the operator reconciliation, the
        # equity attribution stays flat (an option structure writes no equity leg),
        # the domain state is terminal and no evaluation authority remains.
        "expected_settlement_axes": {
            "attribution_scoped_flatness": "satisfied",
            "terminal_domain_state": "satisfied",
            "no_live_evaluation_authority": "satisfied",
        },
        "params": {
            "underlying": "NIFTY",
            "index_ticker": "NSE:NIFTY50",
            "interval": "5minute",
            "lookback": 40,
            "rsi_period": 5,
            "bullish_rsi_level": 40,
            "long_offset_points": 0,
            "short_offset_points": 100,
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
            "deadline_seconds": 240,
        },
    },
    "universe_equal_weight": {
        "source": "index_universe_equal_weight.py",
        "schema": "index_universe_equal_weight.schema.json",
        "expects_open_exposure": True,
        "autonomous": False,
        "expects_manual": True,
        "expected_requests": 1,
        "uses_universe": True,
        "expected_orders": 4,
        # The owner's admission allocation IS the frozen sizing basis: it must be
        # the same number the strategy is sizing against, or the realized notional
        # is a different budget than the one the strategy declared.
        "allocation_inr": 400000.0,
        "max_notional_inr": 400000.0,
        # Exact quantities: the frozen weights against the 400000 allocation and
        # the fixture prices, floored to the catalog lot.
        "expected_order_rows": [
            {"tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 65},
            {"tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 66},
            {"tradingsymbol": "TCS", "transaction_type": "BUY", "quantity": 25},
            {"tradingsymbol": "HDFCBANK", "transaction_type": "BUY", "quantity": 59},
        ],
        "expected_positions": {
            "RELIANCE": 65,
            "INFY": 66,
            "TCS": 25,
            "HDFCBANK": 59,
        },
        "final_marker": "rebalance dispatched and settled with no outstanding work",
        "expected_settlement_axes": {"attribution_scoped_flatness": "failed"},
        "params": {
            "universe": "phase5-equities",
            "budget_inr": 400000,
            "cash_buffer_pct": 0.02,
            "product": "CNC",
            "exchange": "NSE",
            "deadline_seconds": 240,
        },
    },
    "basis_mismatch": {
        # The SAME example, sized against a budget the owner's recorded allocation
        # does not match. The platform must refuse by name before a plan,
        # reservation or order exists, and the strategy must NAME that refusal
        # rather than crash on a missing plan id.
        "source": "index_universe_equal_weight.py",
        "schema": "index_universe_equal_weight.schema.json",
        "autonomous": False,
        "expects_deferral": True,
        "expected_requests": 0,
        "expected_orders": 0,
        "deferral_markers": ["CAPITAL_BASIS_MISMATCH"],
        "final_marker": "no action: the platform refused the weights plan",
        # The owner's allocation (500000) deliberately differs from the strategy's
        # stated budget (400000).
        "allocation_inr": 500000.0,
        "params": {
            "universe": "phase5-equities",
            "budget_inr": 400000,
            "cash_buffer_pct": 0.02,
            "product": "CNC",
            "exchange": "NSE",
            "deadline_seconds": 120,
        },
    },
    "autonomous": {
        "source": "index_indicator_strategy.py",
        "schema": "index_indicator.schema.json",
        "expects_open_exposure": True,
        "autonomous": True,
        "expected_requests": 1,
        "expected_orders": 1,
        "expected_order_rows": [
            {"tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 3}
        ],
        "expected_positions": {"INFY": 3},
        "final_marker": "settled: coverage=known pending=0 positions=",
        "expected_settlement_axes": {"attribution_scoped_flatness": "failed"},
        "params": {
            "index_symbol": "NIFTY 50",
            "interval": "5minute",
            "lookback": 40,
            "indicator": "ema",
            "period": 9,
            "traded_symbol": "INFY",
            "traded_exchange": "NSE",
            "product": "CNC",
            "quantity": 3,
            "deadline_seconds": 240,
        },
    },
}


def run_scenario(
    label: str,
    spec: Dict[str, Any],
    *,
    app: Any,
    session_factory,
    operator,
    base_url: str,
    port: int,
    timeout: float,
) -> Dict[str, Any]:
    """One example strategy through the real child/API/PostgreSQL path."""
    source = (EXAMPLES / spec["source"]).read_text()
    schema = json.loads((EXAMPLES / spec["schema"]).read_text())
    scenario_account = account_for(label)
    created = operator.post(
        "/api/strategies",
        json={
            "name": f"phase5 {label}",
            "description": f"Phase 5 example: {spec['source']}",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": scenario_account,
            "max_duration_s": 3600,
            "progress_deadline_s": 900,
            "stale_exit_policy": "none",
        },
    )
    strategy_id = str(created["strategy_id"])
    version = operator.post(
        f"/api/strategies/{strategy_id}/versions",
        json={
            "source": source,
            "parameters_schema": schema,
            "capabilities": {"trade": True, "data": True},
        },
    )
    version_id = str(version["version_id"])
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": float(spec.get("allocation_inr") or 500000.0)},
    )
    if spec["autonomous"]:
        operator.put(
            f"/api/strategies/{strategy_id}/authorization",
            json={"mode": "autonomous", "reason": "phase5 harness"},
        )
        operator.post(
            f"/api/strategies/{strategy_id}/authorization/grants",
            json={
                "idempotency_key": f"phase5-grant-{strategy_id}",
                "version_id": version_id,
                "execution_environment": "paper",
            },
        )
    # No hidden parameters: the strategy and account come from the persisted run.
    job = operator.post(
        f"/api/strategies/{strategy_id}/jobs",
        json={
            "version_id": version_id,
            "job_kind": "finite",
            "execution_mode": "paper",
            "params": dict(spec["params"]),
            "idempotency_key": f"phase5-job-{uuid.uuid4().hex[:8]}",
        },
    )
    job_id = str((job.get("job") or {}).get("id") or "")
    if not job_id:
        body = job.get("job") if isinstance(job.get("job"), dict) else job
        job_id = str(
            body.get("job_id") or body.get("id") or ""
        )
    # Publish the book's FIRST state before the child reads it. Publication is an
    # on-demand operational step (there is no scheduler), so a strategy that has
    # no publication yet correctly reports "unknown" - this makes the book
    # authoritative rather than letting the example treat unknown as flat.
    _publish_positions(operator, strategy_id)
    step(f"{label}_created", strategy_id=strategy_id, job_id=job_id, autonomous=spec["autonomous"])

    supervisor_result: Dict[str, Any] = {}

    # Probe: run the real child command once against this instance and keep its
    # raw output. This separates "the platform could not start the child" from
    # "the child ran and decided to do nothing".
    def _supervise() -> None:
        try:
            supervisor_result.update(acc.run_supervisor(base_url, port, WORKSPACE / label, job_id))
        except BaseException as exc:  # noqa: BLE001 - reported in the evidence
            fail(f"{label}_supervisor", exc)
            supervisor_result["error"] = repr(exc)

    thread = threading.Thread(target=_supervise, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    orders_before_approval: Optional[int] = None
    child_exited = False
    while time.monotonic() < deadline:
        for row in _requests_for(session_factory, strategy_id):
            request_id = str(row["request_id"])
            status = str(row["status"])
            if status == "awaiting_approval":
                if orders_before_approval is None:
                    # The manual contract: a request waits for its owner decision,
                    # and nothing may have been ordered before that decision.
                    orders_before_approval = _paper_order_count(
                        session_factory, scenario_account
                    )
                operator.post(
                    f"/api/strategies/{strategy_id}/execution-requests/{request_id}/approve",
                    json={"reason": "phase5 harness approval"},
                )
            elif status == "queued":
                # The bounded production dispatcher performs this in-process.
                # BOTH modes reach the dispatcher: an approval queues the work,
                # and an autonomous request queues itself under its grant.
                _dispatch_once(app)
                # Attribution is published on demand in production; the harness
                # triggers it so the strategy's own book becomes authoritative
                # instead of staying an unpublished projection.
                _publish_positions(operator, strategy_id)
        # The CHILD decides when it is done. A terminal request count is not the
        # end of the scenario: stopping the attempt here would cut off the child's
        # own final read of its book, which is the whole point of the example.
        if supervisor_result:
            child_exited = True
            break
        time.sleep(0.5)

    attempt = _job_attempt(session_factory, job_id)
    if not child_exited:
        # Bounded cleanup of the attempt THIS harness started, and an honest
        # failure: a child that never exited is not a green scenario.
        try:
            operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/stop", json={"attempt": attempt}
            )
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            fail(f"{label}_stop", exc)
        fail(
            f"{label}_child_timeout",
            AssertionError(f"the supervised child never exited within {timeout}s"),
        )
    thread.join(timeout=30)

    # The production unblock path is also the terminal transition: it records the
    # durable quiescence proof, closes the linked worker run and moves the attempt
    # to a terminal state in ONE transaction. It is the operator action that
    # follows a finished attempt, never a kill of a running child. An attempt that
    # still HOLDS exposure is refused by name (OPEN_EXPOSURE) - that refusal is the
    # platform working, not a harness error, and it is recorded as evidence.
    reconciliation: Dict[str, Any] = {}
    try:
        reconciliation = {
            "status": "reconciled",
            "response": operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            ),
        }
    except Exception as exc:  # noqa: BLE001 - classified below, never hidden
        text = str(exc)
        if "OPEN_EXPOSURE" in text and spec.get("expects_open_exposure"):
            reconciliation = {"status": "refused", "reason": "OPEN_EXPOSURE"}
        elif "blocking_reasons" in text:
            reconciliation = {"status": "refused", "reason": text[:400]}
            fail(f"{label}_reconcile", exc)
        else:
            reconciliation = {"status": "error", "reason": text[:400]}
            fail(f"{label}_reconcile", exc)
    try:
        terminal_job = acc.wait_for_terminal_job(session_factory, job_id, deadline_s=60.0)
    except Exception as exc:  # noqa: BLE001
        fail(f"{label}_terminal_job", exc)
        terminal_job = {}

    evidence = _collect_scenario_evidence(
        session_factory, strategy_id, account_id=scenario_account
    )
    if orders_before_approval is not None:
        evidence["orders_before_approval"] = orders_before_approval
    evidence["reconciliation"] = reconciliation
    evidence["settlement"] = _collect_settlement(operator, strategy_id)
    child_log_path = WORKSPACE / label / "logs" / f"{job_id}.log"
    evidence["child_log"] = (
        child_log_path.read_text()[-4000:] if child_log_path.exists() else ""
    )
    acceptance = assert_scenario(label, spec, evidence, supervisor_result)
    if not acceptance["ok"]:
        for failure in acceptance["failures"]:
            fail(f"{label}_acceptance", AssertionError(failure))
    attempt_path = WORKSPACE / label / "attempts" / f"{job_id}.json"
    child_log = WORKSPACE / label / "logs" / f"{job_id}.log"
    scenario_debug = {
        "attempt": attempt_path.read_text() if attempt_path.exists() else None,
        "child_log": child_log.read_text()[-2000:] if child_log.exists() else None,
        "supervisor": {key: str(value) for key, value in supervisor_result.items()},
    }
    scenario = {
        "strategy_id": strategy_id,
        "job_id": job_id,
        "autonomous": spec["autonomous"],
        "terminal_job": {key: str(value) for key, value in terminal_job.items()},
        "supervisor": supervisor_result,
        "debug": scenario_debug,
        "acceptance": acceptance,
        "evidence": evidence,
    }
    RESULT["scenarios"][label] = scenario
    step(
        f"{label}_finished",
        requests=len(evidence["requests"]),
        executed=len([row for row in evidence["requests"] if row["status"] == "executed"]),
        events=len(evidence["execution_events"]),
        job_status=scenario["terminal_job"].get("status"),
    )
    return scenario


def _requests_for(session_factory, strategy_id: str) -> List[Dict[str, Any]]:
    """This run's durable execution requests, newest last."""
    from sqlalchemy import text

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT request_id, plan_id, status, authorization_mode, decision_kind"
                    "  FROM hosted_execution_requests WHERE strategy_id = :sid ORDER BY created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]


def _dispatch_once(app) -> Dict[str, Any]:  # noqa: ANN001
    """One bounded production dispatcher pass (no threads, no polling loop).

    Uses the SAME dispatcher instance the app built, so its service carries the
    production pipeline and executors.
    """
    dispatcher = getattr(app.state, "hosted_execution_dispatcher", None)
    if dispatcher is None:
        raise RuntimeError("the harness app has no hosted execution dispatcher")
    return asyncio.run(dispatcher.poll_once())


def _job_attempt(session_factory, job_id: str) -> int:
    """The attempt number the operator actions must pin."""
    from sqlalchemy import text

    with session_factory() as session:
        row = session.execute(
            text("SELECT attempt FROM public.strategy_jobs WHERE id = :jid"),
            {"jid": job_id},
        ).first()
    return int(row[0]) if row is not None else 1


def _paper_order_count(session_factory, account_id: str) -> int:
    from sqlalchemy import text

    with session_factory() as session:
        return int(
            session.execute(
                text("SELECT COUNT(*) FROM public.paper_orders WHERE account_scope = :account"),
                {"account": account_id},
            ).scalar()
            or 0
        )


def _collect_settlement(operator, strategy_id: str, environment: str = "paper") -> Any:
    """The platform's OWN four-axis settlement assessment for this book.

    This is the production route (``POST /settlement/assess``), not a harness
    reimplementation: the axes, the barrier version and the rollup are the
    platform's. ``None`` means the assessment could not be collected, which the
    assertions treat as a failure rather than as "settled".
    """
    try:
        return operator.post(
            f"/api/strategies/{strategy_id}/settlement/assess",
            json={"environment": environment},
        )
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        fail("settlement_assess", exc)
        return None


def _publish_positions(operator, strategy_id: str, environment: str = "paper") -> None:
    """Publish this strategy's attributed book (the production on-demand rebuild).

    Attribution is not a scheduler side effect: without this the executor and the
    strategy read an unpublished projection, which is honestly reported as
    "unknown" rather than as a flat book.
    """
    try:
        operator.post(
            f"/api/strategies/{strategy_id}/positions/rebuild?environment={environment}"
        )
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        fail("positions_rebuild", exc)


def _collect_scenario_evidence(
    session_factory, strategy_id: str, *, account_id: str = ACCOUNT_SCOPE
) -> Dict[str, Any]:
    from sqlalchemy import text

    with session_factory() as session:
        evidence: Dict[str, Any] = {
            "requests": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT request_id, plan_id, status, authorization_mode, decision_kind,"
                        " refusal_code, execution_detail"
                        "  FROM hosted_execution_requests WHERE strategy_id = :sid ORDER BY created_at"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "plans": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT plan_id, plan_kind, logical_plan, resolved_plan FROM strategy_plans"
                        " WHERE strategy_id = :sid ORDER BY created_at"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "reservations": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT plan_id, status, reserved_notional_inr, released_at, release_reason"
                        "  FROM strategy_reservations WHERE strategy_id = :sid ORDER BY created_at"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "execution_events": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT e.plan_id, e.step_no, e.event, e.filled_quantity, e.refusal_reason"
                        "  FROM strategy_plan_execution_events e JOIN strategy_plans l"
                        "    ON l.plan_id = e.plan_id WHERE l.strategy_id = :sid"
                        " ORDER BY e.created_at"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "paper_orders": [
                dict(row)
                for row in session.execute(
                    text(
                    "SELECT order_id, tradingsymbol, transaction_type, quantity, status"
                        "  FROM public.paper_orders WHERE account_scope = :account ORDER BY updated_at"
                    ),
                    {"account": account_id},
                ).mappings()
            ],
            "attributed_positions": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT tradingsymbol, product, net_quantity, projection_version"
                        "  FROM strategy_position_projection WHERE strategy_id = :sid"
                        " ORDER BY projection_version"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "jobs": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT id, status, run_id, attempt, process_cleanup_state"
                        "  FROM public.strategy_jobs WHERE strategy_id = :sid"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            "grants": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT grant_id, version_id, execution_environment, revoked_at"
                        "  FROM hosted_execution_grants WHERE strategy_id = :sid"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
            # The options lane keeps its own book: these are the runs this
            # strategy's plans created, with their own durable status.
            "option_runs": [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT b.option_run_id, b.phase, s.status AS run_status,"
                        " s.product, s.legs, s.completed_legs"
                        "  FROM public.strategy_plan_option_runs b"
                        "  JOIN option_run_states s ON s.strategy_run_id = b.option_run_id"
                        " WHERE b.strategy_id = :sid"
                        " ORDER BY b.created_at"
                    ),
                    {"sid": strategy_id},
                ).mappings()
            ],
        }
    return evidence


def scenario_recovery(session_factory) -> Dict[str, Any]:
    """The restart matrix through the real recovery service on PostgreSQL.

    Four abandoned claims for four different plans: nothing sent (a withheld
    live claim), accepted (a broker order id), authoritatively rejected (a
    post-send rejection with broker evidence), and uncertain (a ``releasing``
    claim with no outcome). Each answer must be the one the evidence supports,
    and nothing may be replayed.
    """
    from sqlalchemy import text

    from backend.strategies.execution_requests import ExecutionRequestService
    from backend.strategies.models import HostedExecutionRequest
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    service = ExecutionRequestService(session_factory)
    suffix = uuid.uuid4().hex[:8]
    run_id = f"run-{suffix}"
    plans: Dict[str, str] = {}
    requests: Dict[str, str] = {}
    now = datetime.now(timezone.utc)

    # A real hosted strategy + version (the repository is the production path).
    repo = SqlAlchemyStrategyRepository(session_factory)
    hosted = repo.create_strategy(
        owner_id="app:admin",
        name=f"phase5 recovery {suffix}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT_SCOPE,
        max_duration_s=3600,
        progress_deadline_s=900,
        stale_exit_policy="none",
    )
    strategy_id = str(hosted.id)

    with session_factory() as session:
        for name in ("withheld", "accepted", "rejected", "uncertain"):
            plan_id = str(uuid.uuid4())
            plans[name] = plan_id
            request_id = str(uuid.uuid4())
            requests[name] = request_id
            proposal_id = str(uuid.uuid4())
            # Every request needs a frozen plan and its canonical strategy row:
            # the FK contract requires real parents, not placeholder text.
            session.execute(
                text(
                    "INSERT INTO public.strategies (id, owner_id, account_scope, name,"
                    " created_at, updated_at)"
                    " VALUES (:sid, 'app:admin', :account, :name, NOW(), NOW())"
                    " ON CONFLICT DO NOTHING"
                ),
                {"sid": strategy_id, "account": ACCOUNT_SCOPE, "name": f"recovery {suffix}"},
            )
            session.execute(
                text(
                    "INSERT INTO strategy_proposals ("
                    " proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind,"
                    " strategy_run_id, target_kind, payload, payload_sha256, status, created_at)"
                    " VALUES (:prop, :sid, :account, :eval_id, 'run_now', :run,"
                    "         'single_instrument', '{}'::jsonb, :hash, 'validated', NOW())"
                ),
                {
                    "prop": proposal_id,
                    "sid": strategy_id,
                    "account": ACCOUNT_SCOPE,
                    "eval_id": f"recovery-{name}-{suffix}",
                    "run": run_id,
                    "hash": "p" * 64,
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans ("
                    " plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash,"
                    " logical_plan, resolved_plan, pinned_catalog_generation)"
                    " VALUES (:plan, :prop, :sid, :account, 'single_instrument', :hash,"
                    "         '{}'::jsonb, '{}'::jsonb, :gen)"
                ),
                {
                    "plan": plan_id,
                    "prop": proposal_id,
                    "sid": strategy_id,
                    "account": ACCOUNT_SCOPE,
                    "hash": "h" * 64,
                    "gen": STORAGE["generation"],
                },
            )
            session.execute(
                text(
                    "INSERT INTO hosted_execution_requests ("
                    " request_id, owner_id, strategy_id, canonical_strategy_id, account_id,"
                    " execution_environment, strategy_run_id, version_id, source_sha256,"
                    " policy_hash, plan_id, plan_hash, authorization_mode, status,"
                    " idempotency_key, request_hash, dispatch_claim_id, dispatch_claimed_at,"
                    " created_at, updated_at)"
                    " VALUES (:rid, 'app:admin', :sid, :sid, :account, 'paper', :run, 'v1',"
                    "         :hash, :hash, :plan, :hash, 'approval_based', 'dispatching',"
                    "         :key, :hash, :claim, :claimed, :at, :at)"
                ),
                {
                    "rid": request_id,
                    "sid": strategy_id,
                    "account": ACCOUNT_SCOPE,
                    "run": run_id,
                    "plan": plan_id,
                    "hash": "h" * 64,
                    "key": f"recovery-{name}",
                    "claim": f"claim-{name}",
                    "claimed": now - timedelta(hours=2),
                    "at": now - timedelta(hours=2),
                },
            )
            if name == "withheld":
                session.execute(
                    text(
                        "INSERT INTO live_plan_submissions ("
                        " submission_id, plan_id, step_no, step_ref, strategy_id, account_id,"
                        " execution_environment, state, broker_order_ids, delta_snapshot, detail,"
                        " created_at, updated_at)"
                        " VALUES (:sid_, :plan, 1, :ref, :sid, :account, 'paper', 'withheld',"
                        "         '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW(), NOW())"
                    ),
                    {
                        "sid_": str(uuid.uuid4()),
                        "plan": plan_id,
                        "ref": f"plan:{plan_id}:step:1",
                        "sid": strategy_id,
                        "account": ACCOUNT_SCOPE,
                    },
                )
            elif name == "accepted":
                session.execute(
                    text(
                        "INSERT INTO live_plan_submissions ("
                        " submission_id, plan_id, step_no, step_ref, strategy_id, account_id,"
                        " execution_environment, state, broker_order_ids, delta_snapshot, detail,"
                        " created_at, updated_at)"
                        " VALUES (:sid_, :plan, 1, :ref, :sid, :account, 'paper', 'pending',"
                        "         '[\"240001\"]'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW(), NOW())"
                    ),
                    {
                        "sid_": str(uuid.uuid4()),
                        "plan": plan_id,
                        "ref": f"plan:{plan_id}:step:1",
                        "sid": strategy_id,
                        "account": ACCOUNT_SCOPE,
                    },
                )
            elif name == "rejected":
                session.execute(
                    text(
                        "INSERT INTO strategy_plan_execution_events ("
                        " id, plan_id, step_no, event, refusal_reason, actor_id, detail, created_at)"
                        " VALUES (:eid, :plan, 1, 'rejected', 'BROKER_REJECTED', 'app:admin',"
                        "         '{\"rejection_code\": \"RMS:Margin\"}'::jsonb, NOW())"
                    ),
                    {"eid": str(uuid.uuid4()), "plan": plan_id},
                )
            else:
                session.execute(
                    text(
                        "INSERT INTO live_plan_submissions ("
                        " submission_id, plan_id, step_no, step_ref, strategy_id, account_id,"
                        " execution_environment, state, broker_order_ids, delta_snapshot, detail,"
                        " created_at, updated_at)"
                        " VALUES (:sid_, :plan, 1, :ref, :sid, :account, 'paper', 'releasing',"
                        "         '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW(), NOW())"
                    ),
                    {
                        "sid_": str(uuid.uuid4()),
                        "plan": plan_id,
                        "ref": f"plan:{plan_id}:step:1",
                        "sid": strategy_id,
                        "account": ACCOUNT_SCOPE,
                    },
                )
        session.commit()

    counts = service.recover_abandoned_claims(timeout_seconds=60, now=now)
    statuses = {
        name: str(service.get(request_id)["status"]) for name, request_id in requests.items()
    }
    outcomes = {
        name: str(dict(service.get(request_id).get("execution_detail") or {}).get("outcome_state") or "")
        for name, request_id in requests.items()
    }
    from sqlalchemy import select

    with session_factory() as session:
        remaining = len(
            session.execute(
                select(HostedExecutionRequest.request_id).where(
                    HostedExecutionRequest.status == "dispatching",
                    HostedExecutionRequest.strategy_id == strategy_id,
                )
            )
            .scalars()
            .all()
        )
    scenario = {
        "strategy_id": strategy_id,
        "counts": counts,
        "statuses": statuses,
        "outcome_states": outcomes,
        "still_dispatching": remaining,
    }
    acceptance = assert_recovery(scenario)
    scenario["acceptance"] = acceptance
    if not acceptance["ok"]:
        for failure in acceptance["failures"]:
            fail("recovery_acceptance", AssertionError(failure))
    RESULT["scenarios"]["recovery"] = scenario
    step("recovery_finished", **{key: str(value) for key, value in scenario.items() if key != "strategy_id"})
    return scenario


# ------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-database", action="store_true")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--only", default="", help="comma-separated scenario labels")
    args = parser.parse_args(argv)

    market = SyntheticMarket()
    db_name, dsn = acc.create_database()
    acc._export_isolated_env(dsn)
    os.environ["HOSTED_SUPERVISOR_WORKSPACE"] = str(WORKSPACE)
    # This isolated instance's own paper account. The hosted-strategy allowlist
    # is a server setting; nothing outside this process (and no production
    # environment or allowlist) is touched.
    os.environ["HOSTED_STRATEGY_ACCOUNT_SCOPES"] = ",".join(
        [ACCOUNT_SCOPE, *(account_for(name) for name in SCENARIOS)]
    )
    step("database_created", database=db_name)
    port = acc.free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = None
    try:
        acc.migrate(dsn)

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        engine = create_engine(dsn, poolclass=NullPool)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        seed_catalog(session_factory)
        seed_universe(session_factory)
        step("fixtures_seeded")

        app = build_app(session_factory, market)
        server = acc.ApiServer(app, port)
        server.start()
        _wait_for_api(base_url)
        step("api_serving", base=base_url)

        operator = acc.Operator(base_url)
        operator.login()
        step("operator_login")

        selected = {item.strip() for item in args.only.split(",") if item.strip()}

        def wanted(label: str) -> bool:
            return not selected or label in selected

        for label, spec in SCENARIOS.items():
            if not wanted(label):
                continue
            if spec.get("not_scored"):
                # An example the harness does not drive end to end is recorded as a
                # snapshot with its reason, never as a green scenario. A deferral
                # (a strategy that must submit NOTHING) is NOT this case: it runs
                # for real and is asserted on the absence of a request plus the
                # child's own named reason.
                RESULT["scenarios"][label] = {
                    "status": "not_scored",
                    "reason": str(spec["not_scored"]),
                }
                step(f"{label}_not_scored", reason=str(spec["not_scored"]))
                continue
            try:
                run_scenario(
                    label,
                    spec,
                    app=app,
                    session_factory=session_factory,
                    operator=operator,
                    base_url=base_url,
                    port=port,
                    timeout=args.timeout,
                )
            except BaseException as exc:  # noqa: BLE001 - one scenario failing is reported
                fail(f"{label}_scenario", exc)

        if wanted("recovery"):
            try:
                scenario_recovery(session_factory)
            except BaseException as exc:  # noqa: BLE001
                fail("recovery_scenario", exc)
    except BaseException as exc:  # noqa: BLE001 - never hidden
        fail("run", exc)
        print(traceback.format_exc(), file=sys.stderr)
    finally:
        if server is not None:
            server.stop()
        RESULT["ok"] = not RESULT["errors"]
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = EVIDENCE_DIR / f"phase5-{stamp}.json"
        path.write_text(json.dumps(RESULT, indent=2, default=str))
        step("evidence_written", path=str(path), ok=RESULT["ok"])
        if not args.keep_database:
            shutil.rmtree(WORKSPACE, ignore_errors=True)
            try:
                acc.drop_database(db_name)
                step("database_dropped", database=db_name)
            except Exception as exc:  # noqa: BLE001
                fail("drop_database", exc)
    return 0 if RESULT["ok"] else 1


def _wait_for_api(base_url: str, deadline_s: float = 30.0) -> None:
    import httpx

    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/api/hosted-supervisor/jobs", timeout=2.0)
            return
        except Exception:  # noqa: BLE001 - the socket is not up yet
            time.sleep(0.2)
    raise RuntimeError("the loopback API did not start")


if __name__ == "__main__":
    raise SystemExit(main())
