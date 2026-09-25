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

The isolated instance also has no Redis (``REDIS_URL`` points at a closed
loopback port, so the best-effort event publish fails immediately instead of
blocking the paper executor on an unresolvable host), and every disposable
database it creates is dropped on the way out unless ``--keep-database`` is
given.

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
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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
    assert_option_dynamic,
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
        "NIFTY 500": (268041, "INDEX", 1, "NSE", "NIFTY 500"),
        "RELIANCE": (738561, "EQ", 1, "NSE", "RELIANCE INDUSTRIES"),
        "INFY": (408065, "EQ", 1, "NSE", "INFOSYSTEMS"),
        "TCS": (2953217, "EQ", 1, "NSE", "TATA CONSULTANCY"),
        "HDFCBANK": (341249, "EQ", 1, "NSE", "HDFC BANK"),
        # The momentum example's synthetic Nifty-500 constituents, plus one
        # bystander instrument that belongs to ANOTHER strategy and must never be
        # touched by this one.
        "MOMENTUM00": (300001, "EQ", 1, "NSE", "MOMENTUM ZERO"),
        "MOMENTUM01": (300002, "EQ", 1, "NSE", "MOMENTUM ONE"),
        "MOMENTUM02": (300003, "EQ", 1, "NSE", "MOMENTUM TWO"),
        "MOMENTUM03": (300004, "EQ", 1, "NSE", "MOMENTUM THREE"),
        # A name that is NOT an index member at entry and becomes one later, so a
        # recurring evaluation has a genuine ADD to make beside its REMOVAL.
        "MOMENTUM04": (300005, "EQ", 1, "NSE", "MOMENTUM FOUR"),
        "BYSTANDER": (310001, "EQ", 1, "NSE", "OTHER STRATEGY HOLDING"),
    },
    "prices": {256265: 22520.0, 738561: 1500.0, 408065: 1450.0, 2953217: 3900.0, 341249: 1650.0},
    #: The last synthetic index print departs sharply ABOVE the trend, so the
    #: index-ticker examples see a genuine bullish setup instead of a flat or
    #: falling one. Quotes are derived from the candle tail plus this premium.
    "index_premium": 40.0,
    "option_underlying": "NIFTY",
    "option_strikes": [22400, 22450, 22500, 22550, 22600, 22650, 22700, 22750],
    # Two expiries: the front month, and the NEXT one a governed expiry roll moves
    # the structure onto. Both are real catalog rows with their own broker tokens,
    # so a roll acquires a different instrument rather than the same one renamed.
    "option_expiries": ["2026-10-29", "2026-11-26"],
    #: ``(expiry, strike) -> {"ce": token, "pe": token}``.
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


# ------------------------------------------------------- momentum fixtures

#: The NSE daily session close the platform's completeness rule uses, and the
#: platform's own finality delay after it.
MOMENTUM_SESSION_CLOSE_IST = time_of_day(15, 30)
MOMENTUM_FINALITY_DELAY_SECONDS = 900
MOMENTUM_IST = timezone(timedelta(hours=5, minutes=30))

MOMENTUM_INDEX_TOKEN = 268041
MOMENTUM_MEMBERS = [
    (300001, "MOMENTUM00"),
    (300002, "MOMENTUM01"),
    (300003, "MOMENTUM02"),
    (300004, "MOMENTUM03"),
]
MOMENTUM_BYSTANDER = (310001, "BYSTANDER")
MOMENTUM_SOURCE = "nifty500_momentum.py"
MOMENTUM_SCHEMA = "nifty500_momentum.schema.json"
MOMENTUM_ACCOUNT = "kite:paper-momentum"
MOMENTUM_BYSTANDER_ACCOUNT = "kite:paper-bystander"


def _momentum_sessions(end: date, count: int) -> List[date]:
    out: List[date] = []
    cursor = end
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(out)


def momentum_reference_now() -> datetime:
    """The instant the fixture's calendar is judged against."""
    return datetime.now(timezone.utc).astimezone(MOMENTUM_IST)


def momentum_latest_completed_session(now: datetime) -> date:
    """The newest session whose close + finality delay has passed.

    Exactly the rule the adapter and the platform's completeness assessment
    apply: a weekday, closed at 15:30 IST, plus the platform's 900 s delay. This
    is what makes the fixture's as-of session the one the VERIFIED calendar can
    prove finished, instead of a fixed past date the calendar would correctly
    call stale.
    """
    cursor = now.date()
    while True:
        if cursor.weekday() < 5:
            close_at = datetime.combine(
                cursor, MOMENTUM_SESSION_CLOSE_IST, tzinfo=MOMENTUM_IST
            )
            if now >= close_at + timedelta(seconds=MOMENTUM_FINALITY_DELAY_SECONDS):
                return cursor
        cursor -= timedelta(days=1)


def _momentum_series(sessions: List[date], *, above: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for position, session in enumerate(sessions):
        if above:
            close = 500.0 + position * 0.5
        elif position < len(sessions) - 30:
            close = 500.0 + position * 0.5
        else:
            close = (
                500.0
                + (len(sessions) - 30) * 0.5
                - (position - (len(sessions) - 30)) * 8.0
            )
        rows.append({"session": session, "close": round(close, 2)})
    return rows


class MomentumFixture:
    """Synthetic daily history + verified calendar for the momentum example.

    The as-of session is the newest session the platform's own completion rule
    (close plus the finality delay) allows, computed from the real clock, and the
    synthetic history is synthesized THROUGH it. A fixed past date cannot be used
    any more: the seeded calendar carries verified closes through today, so a
    fixed past tape is a stale feed and the adapter refuses it by name
    (``INDEX_HISTORY_STALE``).

    While the current session is still open the provider returns that session's
    UNFINISHED bar explicitly, after the as-of bar. That is the honest shape of a
    live range - the platform's own finality verdict is ``False`` for it - and it
    is what keeps the previous completed bar usable.
    """

    def __init__(
        self,
        *,
        breadth_ok: bool = True,
        now: Optional[datetime] = None,
        members: Optional[List[Any]] = None,
    ):
        self.now = now or momentum_reference_now()
        self.as_of = momentum_latest_completed_session(self.now)
        # The name the rest of the harness uses for "the newest session this
        # fixture's tape carries as a completed bar".
        self.history_end = self.as_of
        self.anchor = _momentum_sessions(self.as_of, 6)[0]
        self.history_sessions = _momentum_sessions(self.as_of, 320)
        self.breadth_ok = breadth_ok
        # The membership snapshot this evaluation is asked to rebalance against.
        # A RECURRING sequence changes it between jobs: a dropped name is a real
        # SELL and an added one is a real BUY, so a rebalance arises from the
        # strategy's own index input rather than from a seeded book.
        self.member_rows_source = list(members or MOMENTUM_MEMBERS)
        self.member_rows = {
            token: _momentum_series(self.history_sessions, above=breadth_ok)
            for token, _symbol in self.member_rows_source
        }
        self.index_rows = _momentum_series(self.history_sessions, above=True)
        # The still-open current session, when there is one: a weekday after the
        # as-of session (so before that session's close + the finality delay).
        self.open_session: Optional[date] = None
        today = self.now.date()
        if today > self.as_of and today.weekday() < 5:
            self.open_session = today
            for series in (self.index_rows, *self.member_rows.values()):
                series.append({"session": today, "close": series[-1]["close"]})

    @property
    def due_day_of_month(self) -> int:
        """A calendar day whose resolved session IS the as-of session.

        ``resolve`` returns the first verified session on or after the configured
        day, and the as-of session is a verified session on its own day, so this
        always resolves to the as-of session: the run is due.
        """
        return self.as_of.day

    @property
    def deferral_day_of_month(self) -> int:
        """A calendar day whose resolved session is provably NOT the as-of one.

        Late in the month, day 1 has already resolved to the month's FIRST
        session. Early in the month, the as-of day plus one resolves to a LATER
        session (or to the month's final session when that day has none), never to
        the as-of session itself. Either way the run is not due.
        """
        return 1 if self.as_of.day > 15 else self.as_of.day + 1

    def calendar_rows(self):
        """Every calendar day in the fetched range, weekends included.

        The platform's calendar reader refuses a range that is not covered day by
        day, so the synthetic document carries HOLIDAY rows for the weekends
        exactly as an imported official document would.

        Coverage runs to the end of the month containing the LATEST of the
        fixture's as-of session and the real today, because the adapter asks for
        the verified calendar through the month end. Every weekday carries the
        session close (nothing is faked as a holiday and no close is left empty):
        the sessions after the as-of session are simply not yet finished.
        """
        start = self.anchor - timedelta(days=420)
        latest = max(self.as_of, self.now.date())
        end = date(latest.year, latest.month, 28)
        while True:
            try:
                end = end.replace(day=end.day + 1)
            except ValueError:
                break
        rows = []
        cursor = start
        while cursor <= end:
            rows.append(
                {
                    "session_date": cursor,
                    "session_type": "REGULAR" if cursor.weekday() < 5 else "HOLIDAY",
                }
            )
            cursor += timedelta(days=1)
        return rows

    def history_payload(self, token: int, from_date: str, to_date: str) -> Dict[str, Any]:
        if int(token) == MOMENTUM_INDEX_TOKEN:
            rows = self.index_rows
        else:
            rows = self.member_rows.get(int(token)) or []
        candles = [
            {
                "ts": f"{row['session'].isoformat()}T00:00:00+05:30",
                "open": row["close"],
                "high": row["close"],
                "low": row["close"],
                "close": row["close"],
                "volume": 1000,
                "is_complete": True,
            }
            for row in rows
            if from_date <= row["session"].isoformat() <= to_date
        ]
        return {
            "timeframe": "day",
            "candles": candles,
            # Honest before the route's own completeness assessment replaces it:
            # the newest returned bar is the still-open current session exactly
            # when this fixture added one.
            "last_candle_final": self.open_session is None,
            "complete": True,
        }

    def members(self) -> List[Dict[str, Any]]:
        return [
            {
                "instrument_token": token,
                "exchange": "NSE",
                "tradingsymbol": symbol,
                "series": "EQ",
                "company_name": symbol,
                "sector": None,
                "source_url": None,
                "last_refreshed_at": None,
            }
            for token, symbol in self.member_rows_source
        ]


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


#: The NFO symbol month codes, spelled out so a fixture symbol never depends on
#: the process locale.
_MONTH_CODES = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def _option_expiry_code(expiry: str) -> str:
    """``2026-10-29`` -> ``26OCT``: the symbol code for one fixture expiry."""
    parsed = date.fromisoformat(str(expiry))
    return f"{str(parsed.year)[2:]}{_MONTH_CODES[parsed.month - 1]}"


class SyntheticMarket:
    """The market-data boundary: deterministic quotes, candles and one chain."""

    def __init__(self) -> None:
        self.candle_rows = _synthetic_candles()
        # Daily history for the momentum example. The fixture is (re)built per
        # scenario by ``set_momentum``; the default is a month-end, breadth-passing
        # book so an unrelated scenario can never be handed a momentum payload by
        # accident.
        self.momentum = MomentumFixture()
        for expiry_index, expiry in enumerate(STORAGE["option_expiries"]):
            for index, strike in enumerate(STORAGE["option_strikes"]):
                base = 50000 + expiry_index * 1000 + index * 2
                STORAGE["option_tokens"][(expiry, strike)] = {"ce": base, "pe": base + 1}

    def set_momentum(self, fixture: "MomentumFixture") -> None:
        self.momentum = fixture

    def option_price(self, token: int) -> float:
        spot = float(STORAGE["prices"][256265])
        for (_expiry, strike), tokens in STORAGE["option_tokens"].items():
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
        per_expiry = {
            expiry: {
                "atm_strike": 22500,
                "rows": self._expiry_rows(expiry, spot=spot),
                "forward": spot,
                "sigma_expiry": 0.14,
            }
            for expiry in STORAGE["option_expiries"]
        }
        return {
            "underlying": underlying.upper(),
            "spot_ltp": spot,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "expiries": list(STORAGE["option_expiries"]),
            "per_expiry": per_expiry,
        }

    def _expiry_rows(self, expiry: str, *, spot: float) -> List[Dict[str, Any]]:
        code = _option_expiry_code(expiry)
        rows = []
        for strike in STORAGE["option_strikes"]:
            tokens = STORAGE["option_tokens"][(expiry, strike)]
            rows.append(
                {
                    "strike": strike,
                    "CE": {
                        "token": tokens["ce"],
                        "tsym": f"NIFTY{code}{strike}CE",
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
                        "tsym": f"NIFTY{code}{strike}PE",
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
        return rows


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

        async def get_historical_candles(  # noqa: ANN001
            self,
            *,
            symbol=None,
            instrument_token=None,
            timeframe="day",
            from_date=None,
            to_date=None,
            ingest=True,
            passthrough=False,
            background_tasks=None,
        ):
            """Daily history from the synthetic document, in the real response shape.

            Only the SOURCE is replaced: the route still runs the production
            completeness assessment against the disposable database's verified
            calendar, so a session the calendar does not cover stays refused.
            """
            instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
            token = int(instrument["instrument_token"])
            payload = market.momentum.history_payload(token, _iso_day(from_date), _iso_day(to_date))
            payload.update(
                {
                    "symbol": instrument["symbol"],
                    "instrument_token": token,
                    "interval": "day",
                    "from": _iso_day(from_date),
                    "to": _iso_day(to_date),
                    "count": len(payload["candles"]),
                    "source": "synthetic_daily_document",
                }
            )
            return payload

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
        for (expiry, strike), tokens in STORAGE["option_tokens"].items():
            for kind, token in (("CE", tokens["ce"]), ("PE", tokens["pe"])):
                symbol = f"NIFTY{_option_expiry_code(expiry)}{strike}{kind}"
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
                        "expiry": expiry,
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
        # The example trades ONE covered vertical (a bull call spread); the
        # version declares it, and declines naked exposure.
        "risk_policy": {
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        },
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
        # evidence: quiescence is proven by the CONTINUATION itself clearing the
        # attempt's block (the operator route answers HOSTED_JOB_NOT_BLOCKED,
        # because there is nothing left to reconcile), the equity attribution
        # stays flat (an option structure writes no equity leg), the domain state
        # is terminal and no evaluation authority remains.
        "expects_self_cleared_block": True,
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
    "options_restart_hold_close": {
        # Phase B1: the SAME persistent strategy across a restart between the
        # entry and its close. Job 1 freezes and ENTERS the structure, then
        # finishes with it HELD. Job 2 is a fresh supervised child: it discovers
        # that same durable option run from owned_work()["option_runs"], is
        # REFUSED a second entry for it, submits no duplicate, and closes it.
        "source": "options_index_setup_adjustment.py",
        "schema": "options_index_setup.schema.json",
        "risk_policy": {
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        },
        "options_recurring": True,
        "autonomous": False,
        "expects_manual": True,
        "final_marker": "structure closed with no outstanding work",
        "recurring_jobs": [
            {"phase": "entry", "params": {"hold_after_entry": True}},
            {"phase": "close", "params": {"duplicate_entry_probe": True}},
        ],
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
    "options_protection_handover": {
        # Phase B2.4 S3: the SAME persistent strategy, but with a DECLARED
        # protection policy (``exit_on_worker_stale``), held across two
        # evaluations. Job 1 enters the structure and finishes with it HELD; job
        # 2 continues the same durable run and closes it. Between them the
        # structure is protected by its OWNER ROW, which survives job 1's run
        # closure and is transferred to job 2's successor run at creation.
        "source": "options_index_setup_adjustment.py",
        "schema": "options_index_setup.schema.json",
        "risk_policy": {
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        },
        "options_recurring": True,
        # A protection policy the strategy actually declares: not "none".
        "stale_exit_policy": "exit_on_worker_stale",
        "protection_handover": True,
        "autonomous": False,
        "expects_manual": True,
        "final_marker": "structure closed with no outstanding work",
        "recurring_jobs": [
            {"phase": "entry", "params": {"hold_after_entry": True}},
            {"phase": "close", "params": {}},
        ],
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
    "options_dynamic_resize_roll": {
        # Phase B2.2 S5: ONE persistent delta-neutral straddle with protective
        # wings, managed across four supervised evaluations against the SAME
        # durable option run. Job 1 ENTERS at one unit; job 2 RESIZES to two
        # units (and asks whether a duplicate entry, and an adjustment frozen
        # against the previous generation, would be admitted - neither is); job 3
        # ROLLS the same legs and roles onto the next expiry; job 4 EXITS. Every
        # job is a fresh supervised child process, and every fact the acceptance
        # asserts is read from the platform's own rows.
        "source": "options_dynamic_straddle.py",
        "schema": "options_dynamic_straddle.schema.json",
        # The example trades ONE covered shape: a short straddle whose wings are
        # long, which the classifier names ``iron_butterfly``. It is never naked.
        "risk_policy": {
            "allowed_structure_families": ["iron_butterfly"],
            "naked_permitted": False,
        },
        "option_dynamic": True,
        "autonomous": False,
        "expects_manual": True,
        # The harness's broker boundary prices every option leg at its synthetic
        # flat quote, so a two-leg acquire over a still-held generation needs real
        # paper margin: the resize holds ~675k and the roll's overlap peaks near
        # 1.35m. The scenario therefore STARTS this account with 2.5m rather than
        # discovering the shortfall at fill time (which is C1.2's live problem,
        # and is deliberately not papered over here).
        "paper_starting_balance": 2500000.0,
        "recurring_jobs": [
            {"phase": "entry", "params": {}},
            {
                "phase": "resize",
                "params": {
                    "resize_units": 2,
                    "force_resize": True,
                    "duplicate_entry_probe": True,
                    "stale_basis_probe": True,
                },
            },
            {"phase": "roll", "params": {"roll_to_expiry": "2026-11-26"}},
            {"phase": "exit", "params": {"exit_position": True}},
        ],
        "params": {
            "underlying": "NIFTY",
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
            "short_offset_points": 0,
            # The fixture ladder is 22400..22750, so a 100-point wing is the
            # widest the harness can freeze. The declared width is a parameter,
            # and the example refuses by name when the chain cannot provide it.
            "wing_width_points": 100,
            "base_units": 1,
            "deadline_seconds": 240,
        },
        # The run's own generations: entry 1, resized 2, rolled 3, exited 3.
        "expected_generations": [1, 2, 3, 3],
        "expected_units_by_generation": {1: 1, 2: 2, 3: 2},
        "entered_after_adjust": [1, 2],
        "expected_final_status": "exited",
        "expected_edges": {"entry": 1, "adjust": 2, "exit": 1},
        "initial_expiry": "2026-10-29",
        "rolled_expiry": "2026-11-26",
        "option_lot_size": 50,
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
    # -- Nifty-500 momentum (preview / paper-experimental) -------------------
    "momentum_manual_entry": {
        # Review-first monthly rebalance on a session the schedule IS due for:
        # the request must WAIT for the owner, nothing may be ordered before that
        # decision, and the delta quantities land only after approval.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "autonomous": False,
        "expects_manual": True,
        "schedule": "due",
        "breadth_ok": True,
        "expected_requests": 1,
        "expected_entry_order_count": len(MOMENTUM_MEMBERS),
    },
    "momentum_autonomous_entry": {
        # The same monthly decision, admitted by an owner-issued grant with no
        # interactive approval, and then the production no-op: a SECOND job on the
        # same version sees the book the first job produced and submits nothing.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "autonomous": True,
        "expects_manual": False,
        "schedule": "due",
        "breadth_ok": True,
        "expected_requests": 1,
        "expected_entry_order_count": len(MOMENTUM_MEMBERS),
        # A second job on the same version is refused STRATEGY_BLOCKED while the
        # first attempt holds exposure and has not been reconciled - the
        # platform's own rule, recorded in the evidence rather than worked
        # around. The monthly no-op axis ("the book already matches the target")
        # is covered by the focused unit suite instead.
    },
    "momentum_breadth_exit": {
        # Breadth fails with real holdings: the strategy exits ITS OWN book with
        # exact quantities and never enters. A second strategy's book in the same
        # account must be byte-identical afterwards.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "autonomous": False,
        "expects_manual": True,
        # The breadth gate is decided before the schedule (a failed gate exits
        # the strategy's own book on ANY session), so this scenario keeps the
        # default MONTHLY_LAST_SESSION parameter and does not depend on it.
        "breadth_ok": False,
        "seed_positions": [
            {"tradingsymbol": "MOMENTUM00", "instrument_token": 300001, "net_quantity": 20},
            {"tradingsymbol": "MOMENTUM01", "instrument_token": 300002, "net_quantity": 10},
        ],
        "expected_exit_orders": [
            {"tradingsymbol": "MOMENTUM00", "transaction_type": "SELL", "quantity": 20},
            {"tradingsymbol": "MOMENTUM01", "transaction_type": "SELL", "quantity": 10},
        ],
        "expected_requests": 1,
    },
    "momentum_recurring_sequence": {
        # ONE persistent strategy across evaluations: entry into an index
        # membership, a healthy held completion, the next evaluation (a genuine
        # NO-OP on an unchanged book), a membership-driven sell/add REBALANCE,
        # and finally a breadth EXIT of the strategy's own book. Every job is a
        # fresh supervised child process against the same durable strategy, so a
        # "restart between evaluations" is the normal path rather than a special
        # one. The book is never seeded: each evaluation reads what the previous
        # one actually filled.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "recurring": True,
        "autonomous": True,
        "expects_manual": False,
        "recurring_jobs": [
            {"phase": "entry", "breadth_ok": True, "membership": "base", "schedule": "due"},
            {"phase": "noop", "breadth_ok": True, "membership": "base", "schedule": "due"},
            {
                "phase": "rebalance",
                "breadth_ok": True,
                "membership": "rebased",
                "schedule": "due",
            },
            {"phase": "exit", "breadth_ok": False, "membership": "rebased", "schedule": "due"},
        ],
    },
    "momentum_recurring_sequence_manual": {
        # The SAME persistent sequence admitted by explicit owner approval: same
        # one strategy, same durable book, same four evaluations, each approval
        # landing while the attempt that waits for it is still alive.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "recurring": True,
        "autonomous": False,
        "expects_manual": True,
        "recurring_jobs": [
            {"phase": "entry", "breadth_ok": True, "membership": "base", "schedule": "due"},
            {"phase": "noop", "breadth_ok": True, "membership": "base", "schedule": "due"},
            {
                "phase": "rebalance",
                "breadth_ok": True,
                "membership": "rebased",
                "schedule": "due",
            },
            {"phase": "exit", "breadth_ok": False, "membership": "rebased", "schedule": "due"},
        ],
    },
    "momentum_recurring_zero_cash_rebalance": {
        # The financial-correctness case: the account is left with GENUINELY
        # insufficient free cash. The example sizes each name from
        # ``budget_inr / TOP_N``, so a budget of 1,485,000 buys 150 shares of each
        # of the four members - roughly 900,000 of the account's 1,000,000 - and
        # leaves ~100,000 free: far less than the ~225,000 the next evaluation's
        # increase actually costs. The
        # rebalance therefore has to SELL the dropped name and BUY the added one,
        # funded by that sale. The runner reads the paper account's OWN
        # available_funds before the rebalance and FAILS unless it is strictly
        # below the increase, so "insufficient free cash" is asserted, not
        # assumed.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "recurring": True,
        "autonomous": True,
        "expects_manual": False,
        "allocation_inr": 1485000.0,
        "recurring_jobs": [
            {
                "phase": "entry",
                "breadth_ok": True,
                "membership": "base",
                "schedule": "due",
                "budget_inr": 1485000.0,
            },
            {
                "phase": "rebalance",
                "breadth_ok": True,
                "membership": "rebased",
                "schedule": "due",
                "budget_inr": 1485000.0,
                "insufficient_free_cash": True,
            },
        ],
    },
    "momentum_mid_month_deferral": {
        # Off-schedule: the configured monthly calendar day resolves to a
        # DIFFERENT verified session of the month than the as-of session (late in
        # the month that day has already resolved; early in the month it resolves
        # later). The schedule is decided against the whole verified month, so the
        # correct answer is no entry, no request and no order.
        "source": MOMENTUM_SOURCE,
        "schema": MOMENTUM_SCHEMA,
        "momentum": True,
        "autonomous": False,
        "expects_manual": False,
        "schedule": "defer",
        "breadth_ok": True,
        "expects_deferral": True,
        "child_markers": ["off the monthly rebalance session; no entry", "regime RISK_ON"],
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
            # The version's OWN risk policy (B2.5); the options specs declare one.
            "risk_policy": spec.get("risk_policy"),
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
    #
    # B2.1's continuation clears its OWN block once the evaluation proved
    # quiescence, so for a scenario that declares ``expects_self_cleared_block``
    # the operator route is EXPECTED to answer HOSTED_JOB_NOT_BLOCKED. That answer
    # is the continuation proof - the assertion below, not an error to swallow.
    reconciliation: Dict[str, Any] = {}
    try:
        reconciliation = {
            "status": "reconciled",
            "response": operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            ),
        }
        if spec.get("expects_self_cleared_block"):
            reconciliation = {"status": "error", "reason": "the attempt was still blocked"}
            fail(
                f"{label}_reconcile",
                AssertionError(
                    "the healthy continuation did not clear its own block: the operator "
                    "route reconciled instead of answering HOSTED_JOB_NOT_BLOCKED"
                ),
            )
    except Exception as exc:  # noqa: BLE001 - classified below, never hidden
        text = str(exc)
        if "HOSTED_JOB_NOT_BLOCKED" in text and spec.get("expects_self_cleared_block"):
            reconciliation = {"status": "not_blocked", "reason": "HOSTED_JOB_NOT_BLOCKED"}
        elif "OPEN_EXPOSURE" in text and spec.get("expects_open_exposure"):
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


def _projection_rows(session_factory, strategy_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT tradingsymbol, net_quantity, product, exchange"
                    "  FROM public.strategy_position_projection"
                    " WHERE strategy_id = :sid ORDER BY tradingsymbol"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]


def _paper_orders(session_factory, account_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT tradingsymbol, transaction_type, quantity, status, average_price"
                    "  FROM public.paper_orders WHERE account_scope = :account"
                    " ORDER BY placed_at, order_id"
                ),
                {"account": account_id},
            ).mappings()
        ]


def run_momentum_scenario(
    label: str,
    spec: Dict[str, Any],
    *,
    app: Any,  # noqa: ANN001
    market: "SyntheticMarket",
    session_factory,
    operator,
    base_url: str,
    port: int,
    timeout: float,
) -> Dict[str, Any]:
    """The Nifty-500 momentum example through the real child/API/PostgreSQL path.

    Unlike the other examples this one needs three boundaries shaped for it: the
    synthetic daily history document, the verified calendar rows, and the
    membership snapshot. Everything else - the router, the authorization, the
    proposal/execution pipeline, the paper executor, the supervisor child and the
    reconciliation - is production code.
    """
    fixture = MomentumFixture(breadth_ok=bool(spec.get("breadth_ok", True)))
    market.set_momentum(fixture)
    seed_momentum_calendar(session_factory, fixture)
    install_momentum_constituents(fixture)

    source = (EXAMPLES / MOMENTUM_SOURCE).read_text()
    schema = json.loads((EXAMPLES / MOMENTUM_SCHEMA).read_text())
    account = account_for(label)

    created = operator.post(
        "/api/strategies",
        json={
            "name": f"momentum {label}",
            "description": "momentum example scenario",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
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
            # The version's OWN risk policy (B2.5); the options specs declare one.
            "risk_policy": spec.get("risk_policy"),
        },
    )
    version_id = str(version["version_id"])
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": 500000.0},
    )
    if spec["autonomous"]:
        operator.put(
            f"/api/strategies/{strategy_id}/authorization",
            json={"mode": "autonomous", "reason": "phase5 momentum harness"},
        )
        operator.post(
            f"/api/strategies/{strategy_id}/authorization/grants",
            json={
                "idempotency_key": f"momentum-grant-{strategy_id}",
                "version_id": version_id,
                "execution_environment": "paper",
            },
        )

    # A second strategy in the same account, holding its own instrument. Its book
    # must be byte-identical before and after: an example that "exits" must not
    # reach into somebody else's positions.
    bystander = operator.post(
        "/api/strategies",
        json={
            "name": f"momentum bystander {label}",
            "description": "untouched control book",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
            "progress_deadline_s": 900,
            "stale_exit_policy": "none",
        },
    )
    bystander_id = str(bystander["strategy_id"])
    seed_projection(
        session_factory,
        bystander_id,
        account,
        [{"tradingsymbol": "BYSTANDER", "instrument_token": 310001, "net_quantity": 7}],
    )
    before = _projection_rows(session_factory, bystander_id)

    if spec.get("seed_positions"):
        seed_projection(session_factory, strategy_id, account, spec["seed_positions"])
    else:
        seed_projection(session_factory, strategy_id, account, [])

    params = {
        "budget_inr": 500000,
        "regime_anchor_date": fixture.anchor.isoformat(),
        "rebalance_kind": "MONTHLY_LAST_SESSION",
        # This fixture catalog normalizes an index's public key by stripping the
        # space (``NSE:NIFTY500``); production keeps it (``NSE:NIFTY 500``, token
        # 268041, verified against the live catalog). The scenario therefore names
        # the coordinate THIS catalog resolves, which is exactly what the guide
        # asks a strategy to do.
        "index_symbol": "NSE:NIFTY500",
        "deadline_seconds": 60,
    }
    # The fixture's as-of session is the newest session the verified calendar can
    # prove finished, so the monthly decision is scheduled explicitly rather than
    # left to depend on which day the suite happens to run:
    #   ``due``   - a calendar day whose resolved session IS the as-of session;
    #   ``defer`` - a calendar day whose resolved session is provably a different
    #               verified session of the month (so the run is not due).
    # A scenario that does not name a schedule keeps MONTHLY_LAST_SESSION (the
    # breadth exit is decided before the schedule, and the algorithm's
    # last-session coverage lives in the focused unit suite).
    if spec.get("schedule") == "due":
        params["rebalance_kind"] = "MONTHLY_CALENDAR_DAY"
        params["rebalance_day_of_month"] = fixture.due_day_of_month
    elif spec.get("schedule") == "defer":
        params["rebalance_kind"] = "MONTHLY_CALENDAR_DAY"
        params["rebalance_day_of_month"] = fixture.deferral_day_of_month
    params.update(spec.get("params") or {})

    jobs_evidence: List[Dict[str, Any]] = []
    jobs_runtime: List[Dict[str, Any]] = []
    orders_before_approval: Optional[int] = None
    job_ids: List[str] = []
    for job_index in range(int(spec.get("jobs") or 1)):
        # Publish this strategy's own book before the child reads it. After the
        # first job the production rebuild reflects the fills it produced, so a
        # second job on the same version sees a settled book rather than an
        # unknown one.
        #
        # A scenario that SEEDS its book must not rebuild first: the production
        # rebuild would replace the seeded rows with the real (empty) paper book
        # and the scenario would silently test the wrong thing.
        if not (spec.get("seed_positions") and job_index == 0):
            _publish_positions(operator, strategy_id)
        job = operator.post(
            f"/api/strategies/{strategy_id}/jobs",
            json={
                "version_id": version_id,
                "job_kind": "finite",
                "execution_mode": "paper",
                "params": dict(params),
                "idempotency_key": f"momentum-job-{uuid.uuid4().hex[:8]}",
            },
        )
        body = job.get("job") if isinstance(job.get("job"), dict) else job
        job_id = str(body.get("job_id") or body.get("id") or "")
        if not job_id:
            fail(f"{label}_job", AssertionError("the operator API returned no job id"))
            break
        job_ids.append(job_id)
        step(f"{label}_job_created", job_index=job_index, job_id=job_id)

        supervisor_result: Dict[str, Any] = {}

        def _supervise() -> None:
            try:
                supervisor_result.update(
                    acc.run_supervisor(base_url, port, WORKSPACE / f"{label}-{job_index}", job_id)
                )
            except BaseException as exc:  # noqa: BLE001 - reported in the evidence
                fail(f"{label}_supervisor", exc)
                supervisor_result["error"] = repr(exc)

        thread = threading.Thread(target=_supervise, daemon=True)
        thread.start()

        deadline = time.monotonic() + timeout
        child_exited = False
        approvals_while_child_alive = 0
        requests_at_child_exit: List[Dict[str, Any]] = []
        request_timeline: List[Dict[str, Any]] = []

        def _record_timeline(rows: List[Dict[str, Any]]) -> None:
            observed = {str(row["request_id"]): str(row["status"]) for row in rows}
            if not request_timeline or request_timeline[-1]["statuses"] != observed:
                request_timeline.append(
                    {"at": datetime.now(timezone.utc).isoformat(), "statuses": observed}
                )

        while time.monotonic() < deadline:
            rows = _requests_for(session_factory, strategy_id)
            _record_timeline(rows)
            for row in rows:
                request_id = str(row["request_id"])
                status = str(row["status"])
                if status == "awaiting_approval":
                    if orders_before_approval is None:
                        orders_before_approval = _paper_order_count(session_factory, account)
                    if not spec["autonomous"]:
                        # The owner's decision goes through the REAL HTTP route
                        # WHILE the child is still alive. A hosted child's
                        # authority is attempt-scoped, so an approval that lands
                        # after the attempt ended is refused HOSTED_ATTEMPT_FENCED
                        # by the claim: waiting on the owner only means anything
                        # while the attempt that waits for it still holds its
                        # lease. Approving after the child exited would produce a
                        # green-looking run built on a fenced attempt, so the
                        # harness approves here and asserts below that the child
                        # exited ON ITS OWN once every request was terminal.
                        try:
                            operator.post(
                                f"/api/strategies/{strategy_id}/execution-requests/"
                                f"{request_id}/approve",
                                json={"reason": "phase5 momentum approval"},
                            )
                            approvals_while_child_alive += 1
                        except Exception as exc:  # noqa: BLE001 - reported, not hidden
                            fail(f"{label}_approve", exc)
                elif status == "queued":
                    # The bounded production dispatcher performs this in-process.
                    _dispatch_once(app)
                    _publish_positions(operator, strategy_id)
            if supervisor_result:
                child_exited = True
                # Nothing is approved or dispatched on the child's behalf after it
                # exits: the child's own exit is the end of child-side work. The
                # rows read here are what the child left behind, so a request the
                # child walked away from is evidence, never something to finish
                # for it.
                requests_at_child_exit = _requests_for(session_factory, strategy_id)
                _record_timeline(requests_at_child_exit)
                break
            time.sleep(0.5)

        attempt = _job_attempt(session_factory, job_id)
        if not child_exited:
            try:
                operator.post(
                    f"/api/strategies/{strategy_id}/jobs/{job_id}/stop",
                    json={"attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001
                fail(f"{label}_stop", exc)
            fail(
                f"{label}_child_timeout",
                AssertionError(f"the supervised child never exited within {timeout}s"),
            )
        thread.join(timeout=30)
        try:
            operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            )
        except Exception as exc:  # noqa: BLE001 - recorded, and asserted below
            step(f"{label}_reconcile_refused", job_index=job_index, reason=str(exc)[:200])
        try:
            acc.wait_for_terminal_job(session_factory, job_id, deadline_s=60.0)
        except Exception as exc:  # noqa: BLE001
            fail(f"{label}_terminal_job", exc)
        evidence = _collect_scenario_evidence(session_factory, strategy_id, account_id=account)
        evidence["supervisor"] = dict(supervisor_result)
        evidence["job_id"] = job_id
        jobs_runtime.append(
            {
                "job_index": job_index,
                "job_id": job_id,
                "child_exited": child_exited,
                "approvals_while_child_alive": approvals_while_child_alive,
                "requests_at_child_exit": requests_at_child_exit,
                "request_timeline": request_timeline,
            }
        )
        log_path = WORKSPACE / f"{label}-{job_index}" / "logs" / f"{job_id}.log"
        evidence["child_log"] = log_path.read_text()[-4000:] if log_path.exists() else ""
        jobs_evidence.append(evidence)

    after = _projection_rows(session_factory, bystander_id)
    orders = _paper_orders(session_factory, account)
    first = jobs_evidence[0] if jobs_evidence else {}
    aggregate_requests = sum(len(job.get("requests") or []) for job in jobs_evidence)

    failures: List[str] = []
    if not jobs_evidence:
        failures.append("no job produced evidence")
    for index, job in enumerate(jobs_evidence):
        outcome = str((job.get("supervisor") or {}).get("outcome") or "")
        exit_code = (job.get("supervisor") or {}).get("exit_code")
        if outcome != "exited" or int(exit_code if exit_code is not None else -1) != 0:
            failures.append(
                f"job {index}: the child did not exit 0 on its own "
                f"(outcome={outcome!r}, exit={exit_code!r})"
            )
    for runtime in jobs_runtime:
        index = runtime["job_index"]
        # The child may only walk away from a request the platform already
        # resolved. A non-terminal request at child exit is exactly the bug this
        # scenario must expose: the attempt ended, the request stayed parked, and
        # any later approval acted on a fenced attempt.
        pending = [
            {"request_id": str(row["request_id"]), "status": str(row["status"])}
            for row in runtime["requests_at_child_exit"]
            if str(row["status"]) not in {"executed", "refused", "rejected"}
        ]
        if runtime["child_exited"] and pending:
            failures.append(
                f"job {index}: the child exited while its execution request was "
                f"still non-terminal: {pending!r}"
            )
        if spec.get("expects_manual") and not runtime["approvals_while_child_alive"]:
            failures.append(
                f"job {index}: no owner approval reached the platform while the "
                "attempt that waited for it was alive"
            )
        if spec["autonomous"] and runtime["approvals_while_child_alive"]:
            failures.append(
                f"job {index}: an autonomous run was approved by hand "
                f"({runtime['approvals_while_child_alive']} approvals)"
            )
    if before != after:
        failures.append(
            f"the bystander strategy's book changed: before={before!r} after={after!r}"
        )
    if spec.get("expects_deferral"):
        if aggregate_requests:
            failures.append(f"expected no execution request, saw {aggregate_requests}")
        if orders:
            failures.append(f"expected no paper order, saw {len(orders)}")
    # The child must have reached the decision the scenario is about. Without
    # this, an adapter that refuses everything for the wrong reason (a wiring
    # bug, an unavailable provider) would look exactly like a correct deferral.
    for marker in spec.get("child_markers") or []:
        if not any(marker in str(job.get("child_log") or "") for job in jobs_evidence):
            failures.append(f"no child logged {marker!r}")
    if spec.get("expected_exit_orders"):
        wanted = sorted(
            (
                str(row["tradingsymbol"]),
                str(row["transaction_type"]).upper(),
                int(row["quantity"]),
            )
            for row in spec["expected_exit_orders"]
        )
        actual = sorted(
            (
                str(row["tradingsymbol"]),
                str(row["transaction_type"]).upper(),
                int(row["quantity"]),
            )
            for row in orders
        )
        if actual != wanted:
            failures.append(f"exit orders {actual!r} did not equal the seeded book {wanted!r}")
    if spec.get("expected_entry_order_count") is not None:
        if len(orders) != int(spec["expected_entry_order_count"]):
            failures.append(
                f"expected {spec['expected_entry_order_count']} entry orders, saw {len(orders)}"
            )
        for row in orders:
            if str(row["transaction_type"]).upper() != "BUY" or int(row["quantity"]) <= 0:
                failures.append(f"entry order is not a positive BUY: {row!r}")
    if spec.get("expects_manual") and orders_before_approval not in (0,):
        failures.append(
            f"orders existed before the owner's decision (orders_before_approval="
            f"{orders_before_approval!r})"
        )
    if spec.get("expected_requests") is not None and aggregate_requests != int(
        spec["expected_requests"]
    ):
        failures.append(
            f"expected {spec['expected_requests']} execution requests, saw {aggregate_requests}"
        )
    if spec.get("second_job_adds_nothing"):
        if len(jobs_evidence) < 2:
            failures.append("the no-op check needs a second job")
        elif len(jobs_evidence[1].get("requests") or []) != 0:
            failures.append("the second job submitted a request for an unchanged book")
    if not spec["autonomous"] and spec.get("expects_manual") and not any(
        str(row.get("status")) == "executed"
        for job in jobs_evidence
        for row in (job.get("requests") or [])
    ):
        failures.append("the approved request never reached 'executed'")

    for failure in failures:
        fail(f"{label}_acceptance", AssertionError(failure))

    scenario = {
        "strategy_id": strategy_id,
        "bystander_strategy_id": bystander_id,
        "job_ids": job_ids,
        "autonomous": spec["autonomous"],
        # The fixture is judged against the real clock, so the run records the
        # sessions it decided on and the schedule it was given: the evidence is
        # self-describing instead of depending on when it happened to run.
        "as_of_session": fixture.as_of.isoformat(),
        "open_session": fixture.open_session.isoformat() if fixture.open_session else None,
        "regime_anchor_date": params["regime_anchor_date"],
        "schedule": {
            "rebalance_kind": params["rebalance_kind"],
            "rebalance_day_of_month": params.get("rebalance_day_of_month"),
            "expected_due": spec.get("schedule") == "due",
        },
        "orders_before_approval": orders_before_approval,
        "paper_orders": orders,
        "bystander_before": before,
        "bystander_after": after,
        "jobs": jobs_evidence,
        "jobs_runtime": jobs_runtime,
        "acceptance": {"ok": not failures, "failures": failures},
        "first_evidence": first,
    }
    RESULT["scenarios"][label] = scenario
    step(
        f"{label}_finished",
        jobs=len(jobs_evidence),
        requests=aggregate_requests,
        orders=len(orders),
        approvals=sum(item["approvals_while_child_alive"] for item in jobs_runtime),
        bystander_untouched=before == after,
    )
    return scenario


MOMENTUM_REBASED_MEMBERS = [
    (300002, "MOMENTUM01"),
    (300003, "MOMENTUM02"),
    (300004, "MOMENTUM03"),
    (300005, "MOMENTUM04"),
]


def run_momentum_recurring_scenario(
    label: str,
    spec: Dict[str, Any],
    *,
    app: Any,  # noqa: ANN001
    market: "SyntheticMarket",
    session_factory,
    operator,
    base_url: str,
    port: int,
    timeout: float,
) -> Dict[str, Any]:
    """One persistent momentum strategy across evaluations (the Phase A core).

    Every job is a fresh supervised child on the SAME durable strategy and the
    same frozen version. The book each evaluation reads is the one the previous
    evaluation actually filled (published through the production rebuild), never
    a seeded row: entry, a no-op, a membership-driven sell/add rebalance, and a
    breadth exit.
    """
    source = (EXAMPLES / MOMENTUM_SOURCE).read_text()
    schema = json.loads((EXAMPLES / MOMENTUM_SCHEMA).read_text())
    account = account_for(label)

    created = operator.post(
        "/api/strategies",
        json={
            "name": f"momentum recurring {label}",
            "description": "recurring momentum sequence",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
            "progress_deadline_s": 900,
            # No standing protection policy: the supported recurring shape. A
            # protected strategy refuses the handover by name rather than leaving
            # two protection owners (root-accepted bounded limit).
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
            # The version's OWN risk policy (B2.5); the options specs declare one.
            "risk_policy": spec.get("risk_policy"),
        },
    )
    version_id = str(version["version_id"])
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": float(spec.get("allocation_inr") or 500000.0)},
    )
    # The authorization setup is BRANCHED on the spec. A "manual" scenario that
    # silently installed an autonomous grant would produce requests with
    # authorization_mode=autonomous / decision_kind=automatic, and every
    # "owner approval" it recorded would be a no-op against an already-authorized
    # request: a green-looking manual run that never exercised the owner's
    # decision at all.
    if spec["autonomous"]:
        operator.put(
            f"/api/strategies/{strategy_id}/authorization",
            json={"mode": "autonomous", "reason": "phase5 recurring harness"},
        )
        operator.post(
            f"/api/strategies/{strategy_id}/authorization/grants",
            json={
                "idempotency_key": f"recurring-grant-{strategy_id}",
                "version_id": version_id,
                "execution_environment": "paper",
            },
        )

    bystander = operator.post(
        "/api/strategies",
        json={
            "name": f"momentum recurring bystander {label}",
            "description": "untouched control book",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
            "progress_deadline_s": 900,
            "stale_exit_policy": "none",
        },
    )
    bystander_id = str(bystander["strategy_id"])
    seed_projection(
        session_factory,
        bystander_id,
        account,
        [{"tradingsymbol": "BYSTANDER", "instrument_token": 310001, "net_quantity": 7}],
    )
    bystander_before = _projection_rows(session_factory, bystander_id)

    failures: List[str] = []
    jobs_evidence: List[Dict[str, Any]] = []
    jobs_runtime: List[Dict[str, Any]] = []
    phases: List[Dict[str, Any]] = []
    job_specs = list(spec.get("recurring_jobs") or [])
    orders_so_far = 0
    requests_so_far = 0

    for job_index, job_spec in enumerate(job_specs):
        phase = str(job_spec.get("phase") or f"job{job_index}")
        members = (
            MOMENTUM_REBASED_MEMBERS
            if str(job_spec.get("membership")) == "rebased"
            else MOMENTUM_MEMBERS
        )
        fixture = MomentumFixture(
            breadth_ok=bool(job_spec.get("breadth_ok", True)), members=members
        )
        market.set_momentum(fixture)
        seed_momentum_calendar(session_factory, fixture)
        install_momentum_constituents(fixture)

        params = {
            # The strategy's OWN affordability bound for this evaluation. A
            # scenario may change it between jobs (a resize), which is how the
            # zero-free-cash case is produced WITHOUT inflating the account.
            "budget_inr": float(
                job_spec.get("budget_inr") or spec.get("budget_inr") or 500000
            ),
            "regime_anchor_date": fixture.anchor.isoformat(),
            "rebalance_kind": "MONTHLY_LAST_SESSION",
            "index_symbol": "NSE:NIFTY500",
            "deadline_seconds": 60,
        }
        if job_spec.get("schedule") == "due":
            params["rebalance_kind"] = "MONTHLY_CALENDAR_DAY"
            params["rebalance_day_of_month"] = fixture.due_day_of_month

        # The production rebuild is what makes the previous evaluation's fills
        # visible to this one: the durable book is read from the platform, never
        # handed to the child.
        _publish_positions(operator, strategy_id)
        before_book = _projection_rows(session_factory, strategy_id)
        paper_available_before = _paper_available_funds(session_factory, account)
        orders_before = len(_paper_orders(session_factory, account))
        requests_before = len(_requests_for(session_factory, strategy_id))

        job = operator.post(
            f"/api/strategies/{strategy_id}/jobs",
            json={
                "version_id": version_id,
                "job_kind": "finite",
                "execution_mode": "paper",
                "params": dict(params),
                "idempotency_key": f"recurring-job-{uuid.uuid4().hex[:8]}",
            },
        )
        body = job.get("job") if isinstance(job.get("job"), dict) else job
        job_id = str(body.get("job_id") or body.get("id") or "")
        if not job_id:
            fail(f"{label}_{phase}_job", AssertionError("the operator API returned no job id"))
            break
        step(f"{label}_{phase}_job_created", job_index=job_index, job_id=job_id)

        supervisor_result: Dict[str, Any] = {}

        def _supervise() -> None:
            try:
                supervisor_result.update(
                    acc.run_supervisor(
                        base_url, port, WORKSPACE / f"{label}-{phase}", job_id
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - reported in the evidence
                fail(f"{label}_{phase}_supervisor", exc)
                supervisor_result["error"] = repr(exc)

        thread = threading.Thread(target=_supervise, daemon=True)
        thread.start()

        deadline = time.monotonic() + timeout
        child_exited = False
        requests_at_child_exit: List[Dict[str, Any]] = []
        approvals_while_child_alive = 0
        orders_before_approval: Optional[int] = None
        while time.monotonic() < deadline:
            rows = _requests_for(session_factory, strategy_id)
            for row in rows:
                status = str(row["status"])
                if status == "awaiting_approval" and not spec["autonomous"]:
                    if orders_before_approval is None:
                        orders_before_approval = len(_paper_orders(session_factory, account))
                    # The owner's decision goes through the REAL HTTP route while
                    # the attempt that waits for it is still alive: a hosted
                    # child's authority is attempt-scoped, so an approval that
                    # lands after the child exited would be refused
                    # HOSTED_ATTEMPT_FENCED by the claim.
                    try:
                        operator.post(
                            f"/api/strategies/{strategy_id}/execution-requests/"
                            f"{row['request_id']}/approve",
                            json={"reason": "phase5 recurring approval"},
                        )
                        approvals_while_child_alive += 1
                    except Exception as exc:  # noqa: BLE001 - reported, not hidden
                        fail(f"{label}_{phase}_approve", exc)
                elif status == "queued":
                    _dispatch_once(app)
                    _publish_positions(operator, strategy_id)
            if supervisor_result:
                child_exited = True
                requests_at_child_exit = _requests_for(session_factory, strategy_id)
                break
            time.sleep(0.5)

        attempt = _job_attempt(session_factory, job_id)
        if not child_exited:
            try:
                operator.post(
                    f"/api/strategies/{strategy_id}/jobs/{job_id}/stop",
                    json={"attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001
                fail(f"{label}_{phase}_stop", exc)
            failures.append(f"{phase}: the supervised child never exited within {timeout}s")
        thread.join(timeout=30)
        # No operator reconciliation: the eligible completion clears its own
        # block. A refusal to reconcile (the job is not blocked) is the expected,
        # recorded outcome here.
        try:
            operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            )
            failures.append(
                f"{phase}: an operator reconciliation was required, so the healthy "
                "completion did not clear its own block"
            )
        except Exception as exc:  # noqa: BLE001 - expected: HOSTED_JOB_NOT_BLOCKED
            if "HOSTED_JOB_NOT_BLOCKED" not in str(exc):
                failures.append(f"{phase}: unexpected reconciliation answer {str(exc)[:200]}")
        try:
            acc.wait_for_terminal_job(session_factory, job_id, deadline_s=60.0)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{phase}: the job never reached a terminal state ({exc})")

        after_book = _projection_rows(session_factory, strategy_id)
        all_orders = _paper_orders(session_factory, account)
        job_orders = all_orders[orders_before:]
        job_requests = _requests_for(session_factory, strategy_id)[requests_before:]
        job_requests_at_exit = requests_at_child_exit[requests_before:]
        orders_so_far = len(all_orders)
        requests_so_far = len(_requests_for(session_factory, strategy_id))

        outcome = str(supervisor_result.get("outcome") or "")
        exit_code = supervisor_result.get("exit_code")
        if outcome != "exited" or int(exit_code if exit_code is not None else -1) != 0:
            failures.append(
                f"{phase}: the child did not exit 0 on its own "
                f"(outcome={outcome!r}, exit={exit_code!r})"
            )
        pending = [
            str(row["status"])
            for row in job_requests_at_exit
            if str(row["status"]) not in {"executed", "refused", "rejected"}
        ]
        if child_exited and pending:
            failures.append(f"{phase}: the child exited with non-terminal requests {pending!r}")

        if spec.get("expects_manual") and phase != "noop":
            if not approvals_while_child_alive:
                failures.append(
                    f"{phase}: no owner approval reached the platform while the "
                    "attempt that waited for it was alive"
                )
            # The baseline is THIS evaluation's own starting count: earlier
            # evaluations in the same persistent strategy have their own fills,
            # and comparing against zero would measure the whole history.
            if orders_before_approval != orders_before:
                failures.append(
                    f"{phase}: orders existed before the owner's decision "
                    f"({(orders_before_approval or 0) - orders_before} new since "
                    f"this evaluation started)"
                )
            non_manual = [
                (str(row["request_id"]), str(row.get("authorization_mode")),
                 str(row.get("decision_kind")))
                for row in job_requests
                if str(row.get("decision_kind")) != "manual"
                or str(row.get("authorization_mode")) != "approval_based"
            ]
            if non_manual:
                failures.append(
                    f"{phase}: a trading request was not owner-decided "
                    f"(authorization_mode, decision_kind): {non_manual!r}"
                )
        if spec["autonomous"] and approvals_while_child_alive:
            failures.append(
                f"{phase}: an autonomous run was approved by hand "
                f"({approvals_while_child_alive} approvals)"
            )

        buys = [row for row in job_orders if str(row["transaction_type"]).upper() == "BUY"]
        sells = [row for row in job_orders if str(row["transaction_type"]).upper() == "SELL"]
        held_names = {str(row["tradingsymbol"]) for row in after_book if int(row["net_quantity"] or 0)}
        members_now = {symbol for _token, symbol in members}

        if phase == "entry":
            if len(buys) != len(members):
                failures.append(
                    f"entry: expected {len(members)} buys, saw {len(buys)} ({job_orders!r})"
                )
            if sells:
                failures.append(f"entry: an opening evaluation sold {sells!r}")
            if not held_names or not held_names <= members_now:
                failures.append(f"entry: the book {sorted(held_names)} is not the membership")
        elif phase == "noop":
            if job_orders:
                failures.append(f"noop: an unchanged book produced orders {job_orders!r}")
            if job_requests:
                failures.append(f"noop: an unchanged book produced a request {job_requests!r}")
            if before_book != after_book:
                failures.append(
                    f"noop: the book moved without a fill: {before_book!r} -> {after_book!r}"
                )
        elif phase == "rebalance":
            dropped = {str(row["tradingsymbol"]) for row in before_book} - members_now
            added = members_now - {str(row["tradingsymbol"]) for row in before_book}
            if not sells:
                failures.append(f"rebalance: the dropped member(s) {sorted(dropped)} were not sold")
            if not buys:
                failures.append(f"rebalance: the added member(s) {sorted(added)} were not bought")
            sold_names = {str(row["tradingsymbol"]) for row in sells}
            bought_names = {str(row["tradingsymbol"]) for row in buys}
            if sold_names != dropped:
                failures.append(f"rebalance: sold {sorted(sold_names)} but dropped {sorted(dropped)}")
            if bought_names != added:
                failures.append(f"rebalance: bought {sorted(bought_names)} but added {sorted(added)}")
        if job_spec.get("insufficient_free_cash"):
            # The account's OWN funds are the evidence, read from the paper
            # runtime's durable account row - never an inflated harness balance.
            increase_notional = _increase_notional_inr(
                session_factory, strategy_id, job_orders
            )
            if paper_available_before is None:
                failures.append(f"{phase}: the paper account's funds could not be read")
            elif increase_notional is None or increase_notional <= 0:
                failures.append(
                    f"{phase}: the evaluation was expected to INCREASE exposure "
                    f"(orders={job_orders!r})"
                )
            elif float(paper_available_before) >= float(increase_notional):
                failures.append(
                    f"{phase}: free cash {paper_available_before!r} was not actually "
                    f"short of the increase {increase_notional!r}"
                )
            else:
                step(
                    f"{label}_{phase}_insufficient_free_cash",
                    paper_available_before_inr=paper_available_before,
                    increase_notional_inr=increase_notional,
                )

        if phase == "exit":
            if not sells:
                failures.append("exit: a failed breadth gate produced no sell")
            if buys:
                failures.append(f"exit: a defensive evaluation bought {buys!r}")
            if after_book:
                failures.append(f"exit: the strategy's own book was not emptied: {after_book!r}")
            sold_quantities = {
                str(row["tradingsymbol"]): int(row["quantity"]) for row in sells
            }
            for row in before_book:
                symbol = str(row["tradingsymbol"])
                if int(sold_quantities.get(symbol, 0)) != int(row["net_quantity"]):
                    failures.append(
                        f"exit: {symbol} held {row['net_quantity']} but sold "
                        f"{sold_quantities.get(symbol)}"
                    )

        evidence = _collect_scenario_evidence(session_factory, strategy_id, account_id=account)
        log_path = WORKSPACE / f"{label}-{phase}" / "logs" / f"{job_id}.log"
        evidence["child_log"] = log_path.read_text()[-4000:] if log_path.exists() else ""
        jobs_evidence.append(evidence)
        jobs_runtime.append(
            {
                "phase": phase,
                "job_index": job_index,
                "job_id": job_id,
                "child_exited": child_exited,
                "requests_at_child_exit": len(requests_at_child_exit),
            }
        )
        phases.append(
            {
                "phase": phase,
                "job_id": job_id,
                "membership": "rebased" if members is MOMENTUM_REBASED_MEMBERS else "base",
                "book_before": before_book,
                "book_after": after_book,
                "paper_available_before_inr": paper_available_before,
                "orders": job_orders,
                "requests": job_requests,
                "approvals_while_child_alive": approvals_while_child_alive,
                "orders_before_approval": orders_before_approval,
                "supervisor": dict(supervisor_result),
            }
        )

    bystander_after = _projection_rows(session_factory, bystander_id)
    if bystander_before != bystander_after:
        failures.append(
            f"the bystander strategy's book changed: "
            f"{bystander_before!r} -> {bystander_after!r}"
        )
    if len(phases) != len(job_specs):
        failures.append(f"only {len(phases)} of {len(job_specs)} evaluations ran")

    for failure in failures:
        fail(f"{label}_acceptance", AssertionError(failure))

    scenario = {
        "strategy_id": strategy_id,
        "bystander_strategy_id": bystander_id,
        "job_ids": [row["job_id"] for row in phases],
        "autonomous": spec["autonomous"],
        "phases": phases,
        "bystander_before": bystander_before,
        "bystander_after": bystander_after,
        "jobs": jobs_evidence,
        "jobs_runtime": jobs_runtime,
        "acceptance": {"ok": not failures, "failures": failures},
    }
    RESULT["scenarios"][label] = scenario
    step(
        f"{label}_finished",
        evaluations=len(phases),
        orders=sum(len(row["orders"]) for row in phases),
        bystander_untouched=bystander_before == bystander_after,
    )
    return scenario


def run_options_recurring_scenario(
    label: str,
    spec: Dict[str, Any],
    *,
    app: Any,  # noqa: ANN001
    session_factory,
    operator,
    base_url: str,
    port: int,
    timeout: float,
) -> Dict[str, Any]:
    """One persistent options strategy across an evaluation RESTART (Phase B1).

    Two supervised children on the SAME durable strategy and version:

    * job 1 freezes and ENTERS the structure, then finishes with it HELD (its own
      child process is disposable; the structure is not);
    * job 2 (a fresh process, after the runner restarted) discovers that same
      durable option run from ``owned_work()["option_runs"]``, asks the platform
      whether a SECOND entry for the held structure would be admitted, submits no
      duplicate - and closes the one structure it owns.

    The evidence is the platform's own: ONE option run, ONE entry edge, one close,
    and the duplicate probe refused BY NAME. Nothing here is asserted from a
    child's self-report.
    """
    source = (EXAMPLES / spec["source"]).read_text()
    schema = json.loads((EXAMPLES / spec["schema"]).read_text())
    account = account_for(label)

    created = operator.post(
        "/api/strategies",
        json={
            "name": f"options recurring {label}",
            "description": "restart between the entry and its close",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
            "progress_deadline_s": 900,
            # A scenario may DECLARE a standing protection policy (B2.4's
            # handover case does); the B1 restart case declares none.
            "stale_exit_policy": str(spec.get("stale_exit_policy") or "none"),
        },
    )
    strategy_id = str(created["strategy_id"])
    version = operator.post(
        f"/api/strategies/{strategy_id}/versions",
        json={
            "source": source,
            "parameters_schema": schema,
            "capabilities": {"trade": True, "data": True},
            # The version's OWN risk policy (B2.5); the options specs declare one.
            "risk_policy": spec.get("risk_policy"),
        },
    )
    version_id = str(version["version_id"])
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": float(spec.get("allocation_inr") or 500000.0)},
    )

    failures: List[str] = []
    jobs_evidence: List[Dict[str, Any]] = []
    phases: List[Dict[str, Any]] = []
    #: B2.4 S3 handover checkpoints: who owned the structure at each instant.
    protection_checkpoints: List[Dict[str, Any]] = []
    job_specs = list(spec.get("recurring_jobs") or [])
    requests_so_far = 0

    for job_index, job_spec in enumerate(job_specs):
        phase = str(job_spec.get("phase") or f"job{job_index}")
        params = dict(spec["params"])
        params.update(dict(job_spec.get("params") or {}))

        # The production rebuild is what makes the previous evaluation's fills
        # visible to this one, and what makes the attributed book authoritative.
        _publish_positions(operator, strategy_id)
        requests_before = len(_requests_for(session_factory, strategy_id))
        orders_before = len(_paper_orders(session_factory, account))

        job = operator.post(
            f"/api/strategies/{strategy_id}/jobs",
            json={
                "version_id": version_id,
                "job_kind": "finite",
                "execution_mode": "paper",
                "params": dict(params),
                "idempotency_key": f"options-recurring-job-{uuid.uuid4().hex[:8]}",
            },
        )
        body = job.get("job") if isinstance(job.get("job"), dict) else job
        job_id = str(body.get("job_id") or body.get("id") or "")
        if not job_id:
            fail(f"{label}_{phase}_job", AssertionError("the operator API returned no job id"))
            break
        step(f"{label}_{phase}_job_created", job_index=job_index, job_id=job_id)

        supervisor_result: Dict[str, Any] = {}

        def _supervise() -> None:
            try:
                supervisor_result.update(
                    acc.run_supervisor(
                        base_url, port, WORKSPACE / f"{label}-{phase}", job_id
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - reported in the evidence
                fail(f"{label}_{phase}_supervisor", exc)
                supervisor_result["error"] = repr(exc)

        thread = threading.Thread(target=_supervise, daemon=True)
        thread.start()

        deadline = time.monotonic() + timeout
        child_exited = False
        approvals_while_child_alive = 0
        orders_before_approval: Optional[int] = None
        requests_at_child_exit: List[Dict[str, Any]] = []
        #: The owner row BEFORE the attempt is released (B2.4 S3). Taken once the
        #: child's own worker run exists AND this strategy has an active owner
        #: row, so it shows the predecessor owning the structure while it holds.
        during_run_checkpoint: Optional[Dict[str, Any]] = None
        while time.monotonic() < deadline:
            for row in _requests_for(session_factory, strategy_id):
                status = str(row["status"])
                if status == "awaiting_approval":
                    if orders_before_approval is None:
                        orders_before_approval = len(_paper_orders(session_factory, account))
                    # The owner's decision goes through the REAL HTTP route while
                    # the attempt that waits for it is still alive.
                    try:
                        operator.post(
                            f"/api/strategies/{strategy_id}/execution-requests/"
                            f"{row['request_id']}/approve",
                            json={"reason": "phase5 options recurring approval"},
                        )
                        approvals_while_child_alive += 1
                    except Exception as exc:  # noqa: BLE001 - reported, not hidden
                        fail(f"{label}_{phase}_approve", exc)
                elif status == "queued":
                    _dispatch_once(app)
                    _publish_positions(operator, strategy_id)
            if spec.get("protection_handover") and during_run_checkpoint is None:
                phase_run_id = _job_run_id(session_factory, job_id)
                if phase_run_id:
                    checkpoint = _protection_checkpoint(
                        session_factory,
                        strategy_id,
                        phase=phase,
                        at="during_run",
                    )
                    if checkpoint["active"]:
                        # A triggered exit RESOLVES its worker run before it can
                        # submit anything: record the production resolver's own
                        # answer while the owner row is live.
                        checkpoint["resolution_probe"] = _protection_resolution_probe(
                            session_factory,
                            worker_run_id=phase_run_id,
                            account_id=account,
                        )
                        if job_index > 0 and phases:
                            checkpoint["superseded_resolution_probe"] = (
                                _protection_resolution_probe(
                                    session_factory,
                                    worker_run_id=str(phases[0].get("run_id") or ""),
                                    account_id=account,
                                )
                            )
                        during_run_checkpoint = checkpoint
            if supervisor_result:
                child_exited = True
                requests_at_child_exit = _requests_for(session_factory, strategy_id)
                break
            time.sleep(0.5)

        attempt = _job_attempt(session_factory, job_id)
        if not child_exited:
            try:
                operator.post(
                    f"/api/strategies/{strategy_id}/jobs/{job_id}/stop",
                    json={"attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001
                fail(f"{label}_{phase}_stop", exc)
            failures.append(f"{phase}: the supervised child never exited within {timeout}s")
        thread.join(timeout=30)
        # The continuation path must clear the finished attempt by itself: an
        # operator reconciliation being REQUIRED would mean the held structure
        # (or its restart) needed a human.
        try:
            operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            )
            failures.append(
                f"{phase}: an operator reconciliation was required, so the healthy "
                "completion did not clear its own block"
            )
        except Exception as exc:  # noqa: BLE001 - expected: HOSTED_JOB_NOT_BLOCKED
            if "HOSTED_JOB_NOT_BLOCKED" not in str(exc):
                failures.append(f"{phase}: unexpected reconciliation answer {str(exc)[:200]}")
        try:
            acc.wait_for_terminal_job(session_factory, job_id, deadline_s=60.0)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{phase}: the job never reached a terminal state ({exc})")

        outcome = str(supervisor_result.get("outcome") or "")
        exit_code = supervisor_result.get("exit_code")
        if outcome != "exited" or int(exit_code if exit_code is not None else -1) != 0:
            failures.append(
                f"{phase}: the child did not exit 0 on its own "
                f"(outcome={outcome!r}, exit={exit_code!r})"
            )
        pending = [
            str(row["status"])
            for row in requests_at_child_exit[requests_before:]
            if str(row["status"]) not in {"executed", "refused", "rejected"}
        ]
        if child_exited and pending:
            failures.append(f"{phase}: the child exited with non-terminal requests {pending!r}")
        if not approvals_while_child_alive:
            failures.append(
                f"{phase}: no owner approval reached the platform while the "
                "attempt that waited for it was alive"
            )
        if orders_before_approval != orders_before:
            failures.append(
                f"{phase}: orders existed before the owner's decision "
                f"({(orders_before_approval or 0) - orders_before} new since this "
                "evaluation started)"
            )

        evidence = _collect_scenario_evidence(session_factory, strategy_id, account_id=account)
        log_path = WORKSPACE / f"{label}-{phase}" / "logs" / f"{job_id}.log"
        evidence["child_log"] = log_path.read_text()[-4000:] if log_path.exists() else ""
        job_requests = _requests_for(session_factory, strategy_id)[requests_before:]
        jobs_evidence.append(evidence)
        job_run_id = _job_run_id(session_factory, job_id) or ""
        if spec.get("protection_handover"):
            # The owner row BEFORE this attempt was released, and the one AFTER
            # its block cleared (which is when the next evaluation may start).
            if during_run_checkpoint is not None:
                during_run_checkpoint["expected_owner_run_id"] = job_run_id
                protection_checkpoints.append(during_run_checkpoint)
            protection_checkpoints.append(
                _protection_checkpoint(
                    session_factory,
                    strategy_id,
                    phase=phase,
                    at="after_block_cleared",
                )
            )
        phases.append(
            {
                "phase": phase,
                "job_id": job_id,
                "job_index": job_index,
                "run_id": job_run_id,
                "child_exited": child_exited,
                "params": dict(params),
                "requests": job_requests,
                "orders": _paper_orders(session_factory, account)[orders_before:],
                "option_runs": list(evidence.get("option_runs") or []),
                "child_log_tail": str(evidence.get("child_log") or "")[-1200:],
                "supervisor": dict(supervisor_result),
            }
        )
        requests_so_far = len(_requests_for(session_factory, strategy_id))

    # -- the whole-platform facts this scenario exists to prove ---------------
    final_evidence = _collect_scenario_evidence(session_factory, strategy_id, account_id=account)
    option_runs = list(final_evidence.get("option_runs") or [])
    entry_edges = [row for row in option_runs if str(row.get("phase")) == "entry"]
    exit_edges = [row for row in option_runs if str(row.get("phase")) == "exit"]
    # One row per EDGE: the same run is reported once for its entry plan and once
    # for its close plan, so the RUN count is the distinct id count.
    distinct_runs = {str(row.get("option_run_id") or "") for row in option_runs}
    if len(distinct_runs) != 1:
        failures.append(
            f"a restart across one structure produced {len(distinct_runs)} option runs "
            f"(statuses={[str(row.get('run_status')) for row in option_runs]})"
        )
    if len(entry_edges) != 1:
        failures.append(
            f"{len(entry_edges)} entry edges exist; an equivalent structure must never "
            "be opened twice"
        )
    if len(exit_edges) != 1:
        failures.append(f"{len(exit_edges)} close plans were submitted; exactly one was owed")
    closed = {str(row.get("run_status") or "").lower() for row in option_runs}
    if not closed or not closed.issubset({"exited", "settled", "closed"}):
        failures.append(f"the structure this strategy owns is not closed (statuses={sorted(closed)})")
    if option_runs:
        from sqlalchemy import text as _text

        with session_factory() as session:
            run = (
                session.execute(
                    _text(
                        "SELECT status, legs, completed_legs FROM option_run_states "
                        "WHERE strategy_run_id = :r"
                    ),
                    {"r": str(option_runs[0]["option_run_id"])},
                )
                .mappings()
                .first()
            )
        phases.append({"phase": "_final_run", "run": dict(run) if run else {}})

    probe = [
        dict(row)
        for row in _requests_for(session_factory, strategy_id)
        if str(row.get("refusal_code") or "") == "OPTION_STRUCTURE_ALREADY_OPEN"
    ]
    probes_expected = sum(
        1
        for job_spec in job_specs
        if bool(dict(job_spec.get("params") or {}).get("duplicate_entry_probe"))
    )
    if probes_expected != len(probe):
        failures.append(
            f"{probes_expected} duplicate-entry probe(s) were requested but the "
            f"platform refused {len(probe)}"
        )

    if spec.get("protection_handover"):
        failures.extend(_protection_handover_failures(protection_checkpoints, phases))

    for failure in failures:
        fail(f"{label}_acceptance", AssertionError(failure))

    scenario = {
        "strategy_id": strategy_id,
        "autonomous": False,
        "phases": phases,
        "requests_total": requests_so_far,
        "option_runs": option_runs,
        "duplicate_entry_refusals": probe,
        "protection_owner_checkpoints": protection_checkpoints,
        "jobs": jobs_evidence,
        "acceptance": {"ok": not failures, "failures": failures},
    }
    RESULT["scenarios"][label] = scenario
    step(
        f"{label}_finished",
        evaluations=len(phases),
        option_runs=len(distinct_runs),
        duplicate_entry_refusals=len(probe),
    )
    return scenario


def _option_run_state_row(session_factory, option_run_id: str) -> Dict[str, Any]:
    """One option run's OWN durable row: status, held legs, metadata, trades.

    ``jsonb`` arrives decoded on PostgreSQL, but the harness normalises either
    shape so a "no legs" read can never be a list of characters.
    """
    from sqlalchemy import text

    with session_factory() as session:
        row = (
            session.execute(
                text(
                    "SELECT status, legs, completed_legs, trades, metadata"
                    "  FROM public.option_run_states WHERE strategy_run_id = :r"
                ),
                {"r": str(option_run_id)},
            )
            .mappings()
            .first()
        )
    if row is None:
        return {}
    state = dict(row)
    for key in ("legs", "completed_legs", "trades", "metadata"):
        value = state.get(key)
        if isinstance(value, str):
            try:
                state[key] = json.loads(value)
            except ValueError:
                state[key] = [] if key != "metadata" else {}
    for key in ("legs", "completed_legs", "trades"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("metadata"), dict):
        state["metadata"] = {}
    return state


def _run_open_by_leg(state: Mapping[str, Any]) -> Dict[str, int]:
    """The run's own confirmed open per leg id, from its recorded trades only."""
    open_by_leg: Dict[str, int] = {}
    for trade in list(state.get("trades") or []):
        leg_id = str((trade or {}).get("leg_id") or "")
        quantity = int((trade or {}).get("quantity") or 0)
        side = str((trade or {}).get("transaction_type") or "").upper()
        open_by_leg[leg_id] = open_by_leg.get(leg_id, 0) + (quantity if side == "BUY" else -quantity)
    return open_by_leg


def _leg_units(state: Mapping[str, Any]) -> List[int]:
    """Each held leg's size in structure units (quantity floored by its lot)."""
    units: List[int] = []
    for leg in list(state.get("legs") or []):
        lot = int((leg or {}).get("lot_size") or 0)
        quantity = abs(int((leg or {}).get("quantity") or 0))
        units.append(quantity // lot if lot > 0 else 0)
    return units


def _run_generation(state: Mapping[str, Any]) -> int:
    try:
        generation = int((state.get("metadata") or {}).get("structure_generation") or 1)
    except (TypeError, ValueError):
        return 1
    return generation if generation >= 1 else 1


def _option_run_checkpoint(session_factory, option_run_id: str) -> Dict[str, Any]:
    """The platform's own view of the run after one supervised evaluation."""
    state = _option_run_state_row(session_factory, option_run_id)
    expiries = {
        str((leg or {}).get("expiry_key") or "")
        for leg in list(state.get("legs") or [])
        if str((leg or {}).get("expiry_key") or "")
    }
    return {
        "status": str(state.get("status") or "unknown"),
        "generation": _run_generation(state),
        "leg_units": _leg_units(state),
        "expiry": expiries.pop() if len(expiries) == 1 else "",
    }


def _option_run_facts(session_factory, strategy_id: str, option_run_id: str) -> Dict[str, Any]:
    """The final facts about one run: its own legs, its ledger, its generations."""
    state = _option_run_state_row(session_factory, option_run_id)
    legs = [dict(leg) for leg in list(state.get("legs") or []) if isinstance(leg, dict)]
    held_ids = {str(leg.get("leg_id") or "") for leg in legs}
    released: set = set()
    for generation in list((state.get("metadata") or {}).get("structure_generation_history") or []):
        for leg in list((generation or {}).get("legs") or []):
            if isinstance(leg, dict) and leg.get("leg_id"):
                released.add(str(leg["leg_id"]))
    from sqlalchemy import text

    with session_factory() as session:
        phases = [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT phase FROM public.strategy_plan_option_runs"
                    " WHERE option_run_id = :r ORDER BY created_at, plan_id"
                ),
                {"r": str(option_run_id)},
            )
        ]
        runs = [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT DISTINCT option_run_id FROM public.strategy_plan_option_runs"
                    " WHERE strategy_id = :sid"
                ),
                {"sid": strategy_id},
            )
        ]
    return {
        "run_count": len([value for value in runs if value]),
        "edges": [{"phase": phase} for phase in phases],
        "final": {
            "status": str(state.get("status") or "unknown"),
            "generation": _run_generation(state),
            "leg_units": _leg_units(state),
            "leg_expiries": [
                str(leg.get("expiry_key") or "")
                for leg in legs
                if str(leg.get("expiry_key") or "")
            ],
            "open_by_leg": _run_open_by_leg(state),
            "released_leg_ids": sorted(released),
            "held_leg_ids": sorted(value for value in held_ids if value),
        },
    }


def _option_request_rows(session_factory, strategy_id: str) -> List[Dict[str, Any]]:
    """This strategy's durable execution requests with their refusal detail."""
    from sqlalchemy import text

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT request_id, plan_id, status, authorization_mode, decision_kind,"
                    " refusal_code, refusal_detail, execution_detail"
                    "  FROM hosted_execution_requests WHERE strategy_id = :sid ORDER BY created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]


def _fund_paper_account(app: Any, account_scope: str, starting_balance: Any) -> None:
    """Give ONE scenario's simulated paper account a declared starting balance.

    The harness's broker boundary is the paper runtime, whose starting capital is
    the harness's own choice. A scenario that carries a structure through a roll's
    OVERLAP window (both generations held at once) needs more free paper margin
    than the default, and the production service's own ``ensure_account`` is how
    that account is created - no balance is inflated mid-run to make an assertion
    pass.
    """
    if starting_balance is None:
        return
    service = getattr(app.state, "paper_runtime_service", None)
    if service is None:
        raise RuntimeError("the harness app has no paper runtime service to fund")
    from decimal import Decimal

    asyncio.run(
        service.ensure_account(
            str(account_scope), starting_balance=Decimal(str(starting_balance))
        )
    )


def run_options_dynamic_scenario(
    label: str,
    spec: Dict[str, Any],
    *,
    app: Any,  # noqa: ANN001
    session_factory,
    operator,
    base_url: str,
    port: int,
    timeout: float,
) -> Dict[str, Any]:
    """ONE persistent option structure across entry / resize / roll / exit (B2.2).

    Each evaluation is a fresh supervised child process against the SAME durable
    strategy and version, so "a restart between evaluations" is the normal path
    rather than a special one. The child discovers its own run from
    ``owned_work()["option_runs"]`` and submits ONE desired state per evaluation;
    the platform owns validation, admission, sequencing, the hedge gate and the
    run's lifecycle.

    The evidence is the platform's own (``option_run_states``,
    ``strategy_plan_option_runs`` and ``hosted_execution_requests``): one run, the
    generations in order, leg sizes that match the declared units, the old
    generation flat after the roll, and the named refusals. Nothing here is
    asserted from a child's self-report.
    """
    source = (EXAMPLES / spec["source"]).read_text()
    schema = json.loads((EXAMPLES / spec["schema"]).read_text())
    account = account_for(label)
    _fund_paper_account(app, account, spec.get("paper_starting_balance"))

    created = operator.post(
        "/api/strategies",
        json={
            "name": f"options dynamic {label}",
            "description": "entry, resize, expiry roll and exit of one structure",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": account,
            "max_duration_s": 1800,
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
            # The version's OWN risk policy (B2.5); the options specs declare one.
            "risk_policy": spec.get("risk_policy"),
        },
    )
    version_id = str(version["version_id"])
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": float(spec.get("allocation_inr") or 500000.0)},
    )

    failures: List[str] = []
    phases: List[Dict[str, Any]] = []
    option_run_id = ""
    checkpoints: List[Dict[str, Any]] = []
    job_specs = list(spec.get("recurring_jobs") or [])

    for job_index, job_spec in enumerate(job_specs):
        phase = str(job_spec.get("phase") or f"job{job_index}")
        params = dict(spec["params"])
        params.update(dict(job_spec.get("params") or {}))

        # The production rebuild is what makes the previous evaluation's fills
        # visible to this one, and what makes the attributed book authoritative.
        _publish_positions(operator, strategy_id)
        requests_before = len(_option_request_rows(session_factory, strategy_id))
        orders_before = len(_paper_orders(session_factory, account))

        try:
            job = operator.post(
                f"/api/strategies/{strategy_id}/jobs",
                json={
                    "version_id": version_id,
                    "job_kind": "finite",
                    "execution_mode": "paper",
                    "params": dict(params),
                    "idempotency_key": f"options-dynamic-job-{uuid.uuid4().hex[:8]}",
                },
            )
        except Exception as exc:  # noqa: BLE001 - a blocked strategy is a named failure
            failures.append(f"{phase}: the platform refused a new evaluation ({str(exc)[:200]})")
            break
        body = job.get("job") if isinstance(job.get("job"), dict) else job
        job_id = str(body.get("job_id") or body.get("id") or "")
        if not job_id:
            fail(f"{label}_{phase}_job", AssertionError("the operator API returned no job id"))
            break
        step(f"{label}_{phase}_job_created", job_index=job_index, job_id=job_id)

        supervisor_result: Dict[str, Any] = {}

        def _supervise() -> None:
            try:
                supervisor_result.update(
                    acc.run_supervisor(base_url, port, WORKSPACE / f"{label}-{phase}", job_id)
                )
            except BaseException as exc:  # noqa: BLE001 - reported in the evidence
                fail(f"{label}_{phase}_supervisor", exc)
                supervisor_result["error"] = repr(exc)

        thread = threading.Thread(target=_supervise, daemon=True)
        thread.start()

        deadline = time.monotonic() + timeout
        child_exited = False
        approvals_while_child_alive = 0
        orders_before_approval: Optional[int] = None
        requests_at_child_exit: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            for row in _option_request_rows(session_factory, strategy_id):
                status = str(row["status"])
                if status == "awaiting_approval":
                    if orders_before_approval is None:
                        orders_before_approval = len(_paper_orders(session_factory, account))
                    # The owner's decision goes through the REAL HTTP route while
                    # the attempt that waits for it is still alive.
                    try:
                        operator.post(
                            f"/api/strategies/{strategy_id}/execution-requests/"
                            f"{row['request_id']}/approve",
                            json={"reason": "phase5 dynamic options approval"},
                        )
                        approvals_while_child_alive += 1
                    except Exception as exc:  # noqa: BLE001 - reported, not hidden
                        fail(f"{label}_{phase}_approve", exc)
                elif status == "queued":
                    _dispatch_once(app)
                    _publish_positions(operator, strategy_id)
            if supervisor_result:
                child_exited = True
                requests_at_child_exit = _option_request_rows(session_factory, strategy_id)
                break
            time.sleep(0.5)

        attempt = _job_attempt(session_factory, job_id)
        if not child_exited:
            try:
                operator.post(
                    f"/api/strategies/{strategy_id}/jobs/{job_id}/stop",
                    json={"attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001
                fail(f"{label}_{phase}_stop", exc)
            failures.append(f"{phase}: the supervised child never exited within {timeout}s")
        thread.join(timeout=30)
        # The continuation path must clear the finished attempt by itself: an
        # operator reconciliation being REQUIRED would mean the held structure (or
        # its adjustment) needed a human.
        try:
            operator.post(
                f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
                json={"attempt": attempt},
            )
            failures.append(
                f"{phase}: an operator reconciliation was required, so the healthy "
                "completion did not clear its own block"
            )
        except Exception as exc:  # noqa: BLE001 - expected: HOSTED_JOB_NOT_BLOCKED
            if "HOSTED_JOB_NOT_BLOCKED" not in str(exc):
                failures.append(f"{phase}: unexpected reconciliation answer {str(exc)[:200]}")
        try:
            acc.wait_for_terminal_job(session_factory, job_id, deadline_s=60.0)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{phase}: the job never reached a terminal state ({exc})")

        outcome = str(supervisor_result.get("outcome") or "")
        exit_code = supervisor_result.get("exit_code")
        if outcome != "exited" or int(exit_code if exit_code is not None else -1) != 0:
            failures.append(
                f"{phase}: the child did not exit 0 on its own "
                f"(outcome={outcome!r}, exit={exit_code!r})"
            )
        pending = [
            str(row["status"])
            for row in _option_request_rows(session_factory, strategy_id)
            if str(row["status"]) not in {"executed", "refused", "rejected"}
        ]
        if child_exited and pending:
            failures.append(f"{phase}: the child exited with non-terminal requests {pending!r}")
        if not approvals_while_child_alive:
            failures.append(
                f"{phase}: no owner approval reached the platform while the "
                "attempt that waited for it was alive"
            )
        if orders_before_approval != orders_before:
            failures.append(
                f"{phase}: orders existed before the owner's decision "
                f"({(orders_before_approval or 0) - orders_before} new since this "
                "evaluation started)"
            )

        # The run this structure lives in, resolved from the strategy's own edge.
        if not option_run_id:
            runs = _option_run_ids(session_factory, strategy_id)
            if len(runs) == 1:
                option_run_id = runs[0]
        checkpoint = (
            _option_run_checkpoint(session_factory, option_run_id) if option_run_id else {}
        )
        checkpoints.append({"phase": phase, **checkpoint})
        job_requests = _option_request_rows(session_factory, strategy_id)[requests_before:]
        log_path = WORKSPACE / f"{label}-{phase}" / "logs" / f"{job_id}.log"
        phases.append(
            {
                "phase": phase,
                "job_id": job_id,
                "job_index": job_index,
                "child_exited": child_exited,
                "params": dict(params),
                "requests": job_requests,
                "orders": _paper_orders(session_factory, account)[orders_before:],
                "checkpoint": checkpoint,
                "child_log_tail": (
                    log_path.read_text()[-2000:] if log_path.exists() else ""
                ),
                "supervisor": dict(supervisor_result),
            }
        )

    if not option_run_id:
        failures.append("no option run exists for this strategy after the evaluations")
    facts: Dict[str, Any] = {}
    if option_run_id:
        facts = _option_run_facts(session_factory, strategy_id, option_run_id)
    facts["checkpoints"] = checkpoints
    facts["refusals"] = [
        {
            "request_id": str(row.get("request_id") or ""),
            "status": str(row.get("status") or ""),
            "refusal_code": str(row.get("refusal_code") or ""),
            "decision_kind": str(row.get("decision_kind") or ""),
            # A pre-execution refusal records its stage beside the code
            # (``refusal_detail``); a dispatch-time refusal records it in the
            # execution detail. Both are read so "refused before approval" is a
            # fact about the platform's own row, not about which route wrote it.
            "stage": str(
                dict(row.get("refusal_detail") or {}).get("stage")
                or dict(row.get("execution_detail") or {}).get("stage")
                or ""
            ),
        }
        for row in _option_request_rows(session_factory, strategy_id)
        if row.get("refusal_code")
    ]
    acceptance = assert_option_dynamic(facts, spec)
    for failure in acceptance["failures"]:
        failures.append(failure)
    for failure in failures:
        fail(f"{label}_acceptance", AssertionError(failure))

    scenario = {
        "strategy_id": strategy_id,
        "autonomous": False,
        "option_run_id": option_run_id,
        "phases": phases,
        "checkpoints": checkpoints,
        "facts": facts,
        "requests": _option_request_rows(session_factory, strategy_id),
        "acceptance": {"ok": not failures, "failures": failures, **acceptance},
    }
    RESULT["scenarios"][label] = scenario
    step(
        f"{label}_finished",
        evaluations=len(phases),
        generations=[row.get("generation") for row in checkpoints],
        option_runs=int(facts.get("run_count") or 0),
    )
    return scenario


def _option_run_ids(session_factory, strategy_id: str) -> List[str]:
    """Every option run this strategy's plan edges point at, de-duplicated."""
    from sqlalchemy import text

    with session_factory() as session:
        return sorted(
            {
                str(row[0])
                for row in session.execute(
                    text(
                        "SELECT option_run_id FROM public.strategy_plan_option_runs"
                        " WHERE strategy_id = :sid"
                    ),
                    {"sid": str(strategy_id)},
                )
            }
        )


def _job_run_id(session_factory, job_id: str) -> Optional[str]:
    """The worker run the launch bound to this attempt, once it exists."""
    from sqlalchemy import text

    with session_factory() as session:
        value = session.execute(
            text("SELECT run_id FROM public.strategy_jobs WHERE id = :j"),
            {"j": str(job_id)},
        ).scalar_one_or_none()
    return None if value is None else str(value)


def _protection_owner_snapshot(session_factory, strategy_id: str) -> Dict[str, Any]:
    """This strategy's protection OWNER rows and their append-only events (B2.4).

    Protection is carried by the owner row, not by the owning worker run's status,
    so the row is the fact to check at a handover: exactly one row per structure,
    one owner, one policy.
    """
    from sqlalchemy import text

    with session_factory() as session:
        active = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT o.option_run_id, o.owner_run_id, o.owner_epoch,"
                    "       o.policy_version, o.action_state, o.state,"
                    "       s.status AS option_run_status"
                    "  FROM public.option_protection_owners o"
                    "  LEFT JOIN option_run_states s"
                    "    ON s.strategy_run_id = o.option_run_id"
                    " WHERE o.strategy_id = :sid AND o.state = 'active'"
                    " ORDER BY o.option_run_id"
                ),
                {"sid": str(strategy_id)},
            ).mappings()
        ]
        events = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT e.option_run_id, e.event, e.owner_epoch, e.owner_run_id"
                    "  FROM public.option_protection_owner_events e"
                    "  JOIN public.option_protection_owners o"
                    "    ON o.option_run_id = e.option_run_id"
                    " WHERE o.strategy_id = :sid"
                    " ORDER BY e.created_at, e.event"
                ),
                {"sid": str(strategy_id)},
            ).mappings()
        ]
        statuses = sorted(
            {
                str(value or "")
                for value in session.execute(
                    text(
                        "SELECT s.status FROM public.strategy_plan_option_runs b"
                        "  JOIN option_run_states s ON s.strategy_run_id = b.option_run_id"
                        " WHERE b.strategy_id = :sid"
                    ),
                    {"sid": str(strategy_id)},
                ).scalars()
            }
        )
    return {"active": active, "events": events, "option_run_statuses": statuses}


def _protection_checkpoint(
    session_factory, strategy_id: str, *, phase: str, at: str
) -> Dict[str, Any]:
    """One handover checkpoint: who owned the structure at this instant."""
    snapshot = _protection_owner_snapshot(session_factory, strategy_id)
    statuses = list(snapshot["option_run_statuses"])
    terminal = bool(statuses) and all(
        status in ("exited", "settled") for status in statuses
    )
    return {
        "phase": phase,
        "at": at,
        "active": snapshot["active"],
        "events": snapshot["events"],
        "option_run_statuses": statuses,
        "option_run_terminal": terminal,
    }


def _protection_resolution_probe(
    session_factory, *, worker_run_id: str, account_id: str
) -> Dict[str, Any]:
    """Which option run the PRODUCTION staged-exit engine resolves (B2.4 S3).

    A protective exit's first step is this resolution. The scenario cannot make a
    stale-exit trigger deterministic inside its own timing, so it asks the same
    resolver a triggered exit asks - the production call, not a fake trigger -
    and asserts the answer follows the ACTIVE owner row across the handover.
    """

    from backend.options.protection.staged_exit import StagedStructureExit

    run_id = str(worker_run_id or "")
    try:
        run, resolution = StagedStructureExit(
            session_factory=session_factory
        ).resolve_run_for_worker_run(worker_run_id=run_id, account_id=str(account_id or ""))
    except Exception as exc:  # noqa: BLE001 - reported as its own probe result
        return {
            "worker_run_id": run_id,
            "resolved": None,
            "reason": f"{type(exc).__name__}",
            "error": str(exc)[:200],
        }
    return {
        "worker_run_id": run_id,
        "resolved": None if run is None else str(run.strategy_run_id),
        "reason": str(resolution.get("reason") or ""),
        "source": str(resolution.get("source") or ""),
    }


def _protection_handover_failures(
    checkpoints: List[Dict[str, Any]], phases: List[Dict[str, Any]]
) -> List[str]:
    """Every named invariant the B2.4 handover scenario exists to prove."""
    failures: List[str] = []
    if len(checkpoints) < 3:
        failures.append(
            f"only {len(checkpoints)} protection-owner checkpoint(s) were captured"
        )
    policy_versions: set = set()
    owners_seen: List[set] = []
    for checkpoint in checkpoints:
        where = f"{checkpoint['phase']}/{checkpoint['at']}"
        active = list(checkpoint.get("active") or [])
        owners = {str(row.get("owner_run_id") or "") for row in active}
        owners_seen.append(owners)
        if len(active) > 1:
            failures.append(f"{where}: {len(active)} active owner rows for one strategy")
        if len(owners) > 1:
            failures.append(f"{where}: owner rows disagree about the owner {sorted(owners)}")
        if not active and not checkpoint.get("option_run_terminal"):
            failures.append(
                f"{where}: no ACTIVE owner row while the structure is not terminal "
                f"(run statuses={checkpoint.get('option_run_statuses')})"
            )
        expected = str(checkpoint.get("expected_owner_run_id") or "")
        if expected and active and owners != {expected}:
            failures.append(
                f"{where}: the owner row names {sorted(owners)} but this attempt's "
                f"worker run is {expected!r}"
            )
        # A protective exit's FIRST step is resolving its worker run through the
        # production engine. After a handover that must find the structure the
        # owner row names - the creation binding still names the predecessor.
        resolution = checkpoint.get("resolution_probe")
        if resolution is not None and active:
            owner_option_run = str(active[0].get("option_run_id") or "")
            if str(resolution.get("resolved") or "") != owner_option_run:
                failures.append(
                    f"{where}: a triggered exit resolved "
                    f"{resolution.get('resolved')!r} for the owning worker run, not "
                    f"{owner_option_run!r} (reason={resolution.get('reason')!r})"
                )
        superseded = checkpoint.get("superseded_resolution_probe")
        if superseded is not None and superseded.get("resolved") is not None:
            failures.append(
                f"{where}: a SUPERSEDED worker run still resolved "
                f"{superseded['resolved']!r} after the handover"
            )
        for row in active:
            policy_versions.add(str(row.get("policy_version") or ""))

    if len(policy_versions) > 1:
        failures.append(
            f"the protection policy changed across the handover: {sorted(policy_versions)}"
        )

    phase_run_ids = [str(item.get("run_id") or "") for item in phases]
    observed: set = set().union(*owners_seen) if owners_seen else set()
    if phase_run_ids and phase_run_ids[0]:
        if phase_run_ids[0] not in observed:
            failures.append(
                "the predecessor was never the recorded protection owner "
                f"(owners seen={sorted(observed)})"
            )
    if len(phase_run_ids) > 1 and phase_run_ids[1]:
        if phase_run_ids[1] not in observed:
            failures.append(
                "the successor never became the recorded protection owner "
                f"(owners seen={sorted(observed)})"
            )
    # The predecessor must OWN the structure while its attempt runs AND after its
    # block cleared: that is the protection that survives the closure, and both
    # checkpoints name the SAME run.
    first_phase = str((phases[0] if phases else {}).get("phase") or "")
    phase_one = [cp for cp in checkpoints if str(cp.get("phase")) == first_phase]
    if not phase_one or any(
        {str(row.get("owner_run_id") or "") for row in (cp.get("active") or [])}
        != {phase_run_ids[0]}
        for cp in phase_one
        if cp.get("active")
    ):
        failures.append(
            "the predecessor did not own the structure at every point before the "
            f"successor existed (run={phase_run_ids[0] if phase_run_ids else ''})"
        )

    # Every checkpoint snapshots the SAME append-only log, so de-duplicate by the
    # event's own identity before counting: one transfer is one epoch advance.
    events = {
        (
            str(event.get("option_run_id") or ""),
            str(event.get("event") or ""),
            str(event.get("owner_epoch") or ""),
            str(event.get("owner_run_id") or ""),
        )
        for checkpoint in checkpoints
        for event in checkpoint.get("events") or []
    }
    transfers = [event for event in events if event[1] == "transferred"]
    if len(transfers) != 1:
        failures.append(
            f"the handover recorded {len(transfers)} transfer event(s), not exactly one"
        )
    return failures


def _paper_available_funds(session_factory, account_id: str) -> Optional[float]:
    """The paper account's OWN free cash, straight from its durable row.

    This is the real account-funds evidence: nothing here inflates a balance to
    make a scenario pass.
    """
    from sqlalchemy import text

    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT available_funds FROM public.paper_accounts"
                " WHERE account_scope = :account"
            ),
            {"account": account_id},
        ).first()
    return None if row is None else float(row[0])


def _increase_notional_inr(
    session_factory, strategy_id: str, orders: List[Dict[str, Any]]
) -> Optional[float]:
    """What the executed BUYs cost, priced from the frozen plan's own legs."""
    from sqlalchemy import text

    buys = [row for row in orders if str(row["transaction_type"]).upper() == "BUY"]
    if not buys:
        return None
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT resolved_plan FROM public.strategy_plans"
                " WHERE strategy_id = :sid ORDER BY created_at DESC LIMIT 5"
            ),
            {"sid": strategy_id},
        ).scalars().all()
    prices: Dict[str, float] = {}
    for resolved in rows:
        for leg in dict(resolved or {}).get("legs") or []:
            symbol = str(leg.get("tradingsymbol") or "").upper()
            price = leg.get("reference_price")
            if symbol and price is not None:
                prices.setdefault(symbol, float(price))
    total = 0.0
    for row in buys:
        # The REAL money the fill cost, taken from the paper order itself. The
        # plan's frozen reference price is only a fallback: in this harness the
        # paper runtime quotes a different synthetic price than the momentum
        # fixture's history, and the assertion must use what was actually paid.
        paid = row.get("average_price")
        price = float(paid) if paid is not None else prices.get(str(row["tradingsymbol"]).upper())
        if price is None:
            return None
        total += abs(int(row["quantity"])) * price
    return total


def _requests_for(session_factory, strategy_id: str) -> List[Dict[str, Any]]:
    """This run's durable execution requests, newest last."""
    from sqlalchemy import text

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT request_id, plan_id, status, authorization_mode, decision_kind"
                    ", refusal_code"
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


def _iso_day(value: Any) -> str:
    if value is None:
        return "1970-01-01"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _sha256(path: Path) -> str:
    """The digest of a file this run actually used, recorded in the evidence."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_momentum_calendar(session_factory, fixture: "MomentumFixture") -> None:
    """Publish the synthetic document into the REAL calendar tables.

    The worker calendar and daily-completeness readers are production code; they
    read ``exchange_calendar_*`` from the disposable database. Seeding the rows
    (weekends included, as an imported official document has them) means the
    harness exercises that reader instead of replacing it.
    """
    from sqlalchemy import text

    document_id = 990001
    rows = fixture.calendar_rows()
    with session_factory() as session:
        session.execute(
            text(
                "DELETE FROM public.exchange_calendar_sessions"
                " WHERE exchange='NSE' AND segment='CM'"
            )
        )
        session.execute(
            text(
                "DELETE FROM public.exchange_calendar_source_documents"
                " WHERE exchange='NSE' AND segment='CM'"
            )
        )
        session.execute(
            text(
                "INSERT INTO public.exchange_calendar_source_documents ("
                " source_document_id, exchange, segment, official_source_reference,"
                " official_source_document_sha256, canonical_csv_sha256, parser_version,"
                " calendar_version, actor, reason, imported_at)"
                " VALUES (:doc, 'NSE', 'CM', 'synthetic://momentum',"
                "         :sha, :sha, 'phase5-harness', 1, 'phase5-harness',"
                "         'isolated synthetic calendar', NOW())"
            ),
            {"doc": document_id, "sha": "0" * 64},
        )
        for row in rows:
            session.execute(
                text(
                    "INSERT INTO public.exchange_calendar_sessions ("
                    " exchange, segment, session_date, calendar_version, session_type,"
                    " opens_at, closes_at, verified, source_document_id)"
                    " VALUES ('NSE', 'CM', :day, 1, :kind, :opens, :closes, true, :doc)"
                ),
                {
                    "day": row["session_date"],
                    "kind": row["session_type"],
                    "opens": time_of_day(9, 15),
                    "closes": time_of_day(15, 30),
                    "doc": document_id,
                },
            )
        session.commit()


def install_momentum_constituents(fixture: "MomentumFixture") -> None:
    """Serve the synthetic Nifty-500 membership through the production route.

    Only the SOURCE is replaced: the route, its authorization and its response
    contract are the production ones. Production reads the constituent table;
    this isolated instance hands the reader the same shape from the fixture.
    """
    from backend.broker_api.instruments import index_ingestion

    snapshot = {
        "schema_version": 1,
        "source": "synthetic://momentum",
        "source_as_of": datetime.now(timezone.utc).isoformat(),
        "effective_date": fixture.history_end.isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source_list": "Nifty500",
        "complete": True,
        "member_count": len(fixture.members()),
        "checksum": "synthetic",
        "members": fixture.members(),
    }

    def _snapshot(source_list: str):
        normalized = str(source_list).strip()
        if normalized.lower() != "nifty500":
            raise ValueError(f"Unsupported index source_list: {source_list}")
        return dict(snapshot)

    index_ingestion.get_worker_index_snapshot = _snapshot  # type: ignore[assignment]


def seed_projection(
    session_factory,
    strategy_id: str,
    account_id: str,
    positions: List[Dict[str, Any]],
    *,
    environment: str = "paper",
) -> None:
    """Publish a KNOWN attributed book without manufacturing a strategy action.

    This is fixture setup for the platform's own projection tables, exactly as
    the operator's rebuild would have written them after earlier fills. The
    strategy never writes here, and no execution is claimed from it.
    """
    from sqlalchemy import text

    with session_factory() as session:
        session.execute(
            text(
                "DELETE FROM public.strategy_position_projection"
                " WHERE strategy_id = :sid AND account_id = :aid"
            ),
            {"sid": strategy_id, "aid": account_id},
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_projection_state ("
                " account_id, strategy_id, execution_environment, projection_version,"
                " content_sha256, last_rebuild_at, updated_at)"
                " VALUES (:aid, :sid, :env, 1, :sha, NOW(), NOW())"
                " ON CONFLICT (account_id, strategy_id, execution_environment)"
                " DO UPDATE SET projection_version = 1, content_sha256 = :sha,"
                "               last_rebuild_at = NOW(), updated_at = NOW()"
            ),
            {"aid": account_id, "sid": strategy_id, "env": environment, "sha": "a" * 64},
        )
        for row in positions:
            symbol = str(row["tradingsymbol"]).upper()
            session.execute(
                text(
                    "INSERT INTO public.strategy_position_projection ("
                    " account_id, strategy_id, execution_environment, identity_kind,"
                    " identity_key, product, canonical_instrument_id, instrument_token,"
                    " exchange, tradingsymbol, net_quantity, unresolved_reason,"
                    " projection_version)"
                    " VALUES (:aid, :sid, :env, 'canonical', :key, 'CNC', :iid, :token,"
                    "         'NSE', :sym, :qty, NULL, 1)"
                ),
                {
                    "aid": account_id,
                    "sid": strategy_id,
                    "env": environment,
                    "key": f"NSE:{symbol}",
                    "iid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{symbol}")),
                    "token": int(row["instrument_token"]),
                    "sym": symbol,
                    "qty": int(row["net_quantity"]),
                },
            )
        session.commit()


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
                        "SELECT e.plan_id, e.step_no, e.event, e.filled_quantity,"
                        "       e.refusal_reason, e.detail"
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

    # Provenance: the digests of the exact inputs this run drives, so an evidence
    # file can never be read against a different revision of the example.
    RESULT["inputs"] = {
        "nifty500_momentum.py": _sha256(EXAMPLES / MOMENTUM_SOURCE),
        "nifty500_momentum.schema.json": _sha256(EXAMPLES / MOMENTUM_SCHEMA),
        "run_phase5_acceptance.py": _sha256(Path(__file__).resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    market = SyntheticMarket()
    db_name, dsn = acc.create_database()
    acc._export_isolated_env(dsn)
    # This instance runs WITHOUT Redis: no notification is sent and no event
    # fan-out is consumed, so every best-effort publish fails by design. The
    # inherited/default URL names a host that does not resolve here, which made
    # each publish block ~5 s on connect; with a multi-leg paper plan that stall
    # (not the governed work) became the whole dispatch latency and pushed the
    # governed request past the child's own wait bound. Pointing the side-channel
    # at a CLOSED loopback port keeps the publish a fast, honest failure so the
    # harness measures the execution path rather than a DNS timeout.
    os.environ["REDIS_URL"] = f"redis://127.0.0.1:{acc.free_port()}/0"
    # Several runtime readers (the exchange-calendar reader above all) open their
    # own connection from the DB_* variables rather than DATABASE_URL. Point them
    # at the disposable database: this instance must never read production.
    parsed = urllib.parse.urlsplit(dsn)
    os.environ.update(
        {
            "DB_HOST": parsed.hostname or "127.0.0.1",
            "DB_PORT": str(parsed.port or 5432),
            "DB_NAME": parsed.path.lstrip("/"),
            "DB_USER": urllib.parse.unquote(parsed.username or "postgres"),
            "DB_PASSWORD": urllib.parse.unquote(parsed.password or ""),
        }
    )
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
                if spec.get("option_dynamic"):
                    run_options_dynamic_scenario(
                        label,
                        spec,
                        app=app,
                        session_factory=session_factory,
                        operator=operator,
                        base_url=base_url,
                        port=port,
                        timeout=args.timeout,
                    )
                elif spec.get("options_recurring"):
                    run_options_recurring_scenario(
                        label,
                        spec,
                        app=app,
                        session_factory=session_factory,
                        operator=operator,
                        base_url=base_url,
                        port=port,
                        timeout=args.timeout,
                    )
                elif spec.get("recurring"):
                    run_momentum_recurring_scenario(
                        label,
                        spec,
                        app=app,
                        market=market,
                        session_factory=session_factory,
                        operator=operator,
                        base_url=base_url,
                        port=port,
                        timeout=args.timeout,
                    )
                elif spec.get("momentum"):
                    run_momentum_scenario(
                        label,
                        spec,
                        app=app,
                        market=market,
                        session_factory=session_factory,
                        operator=operator,
                        base_url=base_url,
                        port=port,
                        timeout=args.timeout,
                    )
                else:
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
