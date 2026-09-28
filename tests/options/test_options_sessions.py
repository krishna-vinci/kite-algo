from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, cast

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.modules.setdefault("mibian", types.ModuleType("mibian"))
if "numba" not in sys.modules:
    numba_stub: Any = types.ModuleType("numba")

    def _njit(*_args, **_kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    numba_stub.njit = _njit
    sys.modules["numba"] = numba_stub

from backend.broker_api.options.options_greeks import black76_greeks, black76_price
from backend.broker_api.options import options_sessions as sessions_module
from backend.broker_api.options.options_sessions import OptionsSession, OptionsSessionManager
from backend.options.market import session_pins
from backend.platform.options_settings import OptionsSettings


class _FakeMarketData:
    def __init__(self, ticks: Dict[int, Dict[str, Any]]):
        self.latest_ticks = ticks


class _ManagerStub:
    def __init__(self, ticks: Dict[int, Dict[str, Any]]):
        self.market_data = _FakeMarketData(ticks)


class _TickListenerMarketData:
    def __init__(self):
        self.callback = None

    def add_tick_listener(self, callback):
        self.callback = callback

        def _unsubscribe():
            self.callback = None

        return _unsubscribe


class _DirtySession:
    def __init__(self):
        self.underlying = "NIFTY"
        self.desired_tokens = {10, 11}
        self.snapshot = {}
        self.dirty_count = 0

    def mark_dirty(self):
        self.dirty_count += 1


def _make_chain(strikes, forward, T, sigma_for_strike):
    """Builds inst_by_strike + ticks for a synthetic chain priced off given sigmas."""
    ticks: Dict[int, Dict[str, Any]] = {}
    inst_by_strike: Dict[float, Dict[str, Any]] = {}
    token = 1000
    for strike in strikes:
        sigma = sigma_for_strike(strike)
        ce_price = float(black76_price("CE", forward, strike, T, sigma))
        pe_price = float(black76_price("PE", forward, strike, T, sigma))
        ce_token, pe_token = token, token + 1
        token += 2
        ticks[ce_token] = {"last_price": ce_price}
        ticks[pe_token] = {"last_price": pe_price}
        inst_by_strike[strike] = {
            "CE": {"instrument_token": ce_token},
            "PE": {"instrument_token": pe_token},
        }
    return inst_by_strike, ticks


def test_per_strike_iv_recovers_skewed_smile_with_put_wing_above_atm():
    forward = 20000.0
    T = 0.05
    atm_strike = 20000.0
    sigma_atm = 0.15
    sigma_put_wing = 0.28
    sigma_call_wing = 0.12
    strikes = [19700.0, 19800.0, 19900.0, 20000.0, 20100.0, 20200.0, 20300.0]

    def sigma_for(strike: float) -> float:
        if strike == atm_strike:
            return sigma_atm
        if strike < forward:
            return sigma_put_wing
        return sigma_call_wing

    inst_by_strike, ticks = _make_chain(strikes, forward, T, sigma_for)
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub(ticks)))

    per_strike_sigma, iv_source = session._solve_per_strike_iv(
        strikes, atm_strike, forward, T, sigma_atm, inst_by_strike
    )

    assert iv_source == ["per_strike"] * len(strikes)

    put_wing_iv = per_strike_sigma[strikes.index(19700.0)]
    atm_iv = per_strike_sigma[strikes.index(20000.0)]
    call_wing_iv = per_strike_sigma[strikes.index(20300.0)]

    assert put_wing_iv == pytest.approx(sigma_put_wing, abs=1e-3)
    assert call_wing_iv == pytest.approx(sigma_call_wing, abs=1e-3)
    assert atm_iv == pytest.approx(sigma_atm, abs=1e-3)
    assert put_wing_iv > atm_iv


def test_per_strike_iv_falls_back_to_expiry_sigma_when_solve_fails():
    forward = 20000.0
    T = 0.05
    atm_strike = 20000.0
    sigma_expiry = 0.15
    strikes = [19900.0, 20000.0, 20100.0]

    inst_by_strike, ticks = _make_chain(strikes, forward, T, lambda _s: sigma_expiry)

    # Corrupt the OTM call wing's price so the solver cannot converge
    # (price <= 0 is an immediate no-solve in the kernel).
    call_wing_token = inst_by_strike[20100.0]["CE"]["instrument_token"]
    ticks[call_wing_token]["last_price"] = 0.0

    session = OptionsSession("NIFTY", cast(Any, _ManagerStub(ticks)))

    per_strike_sigma, iv_source = session._solve_per_strike_iv(
        strikes, atm_strike, forward, T, sigma_expiry, inst_by_strike
    )

    call_wing_idx = strikes.index(20100.0)
    assert iv_source[call_wing_idx] == "expiry_fallback"
    assert per_strike_sigma[call_wing_idx] == pytest.approx(sigma_expiry)

    # Unaffected strikes still solve per-strike.
    put_wing_idx = strikes.index(19900.0)
    assert iv_source[put_wing_idx] == "per_strike"


def test_vectorized_session_greeks_match_per_contract_results(monkeypatch):
    forward = 20030.0
    spot = 20000.0
    T = 0.05
    sigma = 0.18
    strikes = [19900.0, 19950.0, 20000.0, 20050.0, 20100.0]
    inst_by_strike, ticks = _make_chain(strikes, forward, T, lambda _strike: sigma)
    ticks[256265] = {"last_price": spot}

    instruments = []
    for strike, sides in inst_by_strike.items():
        for option_type, inst in sides.items():
            instruments.append(
                {
                    **inst,
                    "strike": strike,
                    "option_type": option_type,
                    "tradingsymbol": f"NIFTY-{strike:g}-{option_type}",
                    "lot_size": 50,
                }
            )

    class _Repo:
        @staticmethod
        def nearest_strike(values, value):
            return min(values, key=lambda strike: abs(strike - value))

        @staticmethod
        def get_option_instruments_for_strikes(_underlying, _expiry, requested):
            requested_set = set(requested)
            return [row for row in instruments if row["strike"] in requested_set]

    manager = cast(Any, _ManagerStub(ticks))
    manager.instrument_repo = _Repo()
    manager.dropped_tokens = {}
    session = OptionsSession("NIFTY", manager)
    expiry = date(2026, 10, 6)
    session.spot_token = 256265
    session.expiries = [expiry]
    session.strikes_by_expiry = {expiry: strikes}
    monkeypatch.setattr(session, "_time_to_expiry", lambda _expiry: T)

    # Force one wing's per-strike IV solve to fail. It should retain today's
    # expiry-fallback IV and Greeks behavior.
    ticks[inst_by_strike[20100.0]["CE"]["instrument_token"]]["last_price"] = 0.0
    array_calls = 0
    real_arrays = sessions_module.black76_greeks_arrays

    def _count_arrays(*args, **kwargs):
        nonlocal array_calls
        array_calls += 1
        return real_arrays(*args, **kwargs)

    monkeypatch.setattr(sessions_module, "black76_greeks_arrays", _count_arrays)
    per_expiry, _tokens, _spot, _ranks = session._run_computation()

    rows = per_expiry[expiry.isoformat()]["rows"]
    assert array_calls == 1
    for row in rows:
        for option_type in ("CE", "PE"):
            contract = row[option_type]
            assert contract is not None
            expected = black76_greeks(option_type, forward, row["strike"], T, contract["iv"])
            assert contract["delta"] == pytest.approx(expected["delta"], abs=1e-9)
            assert contract["gamma"] == pytest.approx(expected["gamma"], abs=1e-9)
            assert contract["theta"] == pytest.approx(expected["theta"] / 365.0, abs=1e-9)
            assert contract["vega"] == pytest.approx(expected["vega"] / 100.0, abs=1e-9)
            assert contract["rho"] == expected["rho"]

    unsolved = next(row for row in rows if row["strike"] == 20100.0)["CE"]
    assert unsolved["iv_source"] == "expiry_fallback"
    assert unsolved["iv"] == pytest.approx(sigma, abs=1e-3)


def test_max_pain_cache_refreshes_after_configured_interval(monkeypatch):
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})))
    rows = [{"strike": 100, "CE": {"oi": 10}, "PE": {"oi": 20}}]
    now = 100.0
    calls = 0

    monkeypatch.setenv("OPTIONS_MAX_PAIN_REFRESH_S", "30")
    monkeypatch.setattr(sessions_module.time, "monotonic", lambda: now)

    def _compute(_rows):
        nonlocal calls
        calls += 1
        return float(calls * 100)

    monkeypatch.setattr(sessions_module, "compute_bounded_max_pain", _compute)
    assert session._cached_max_pain("2026-10-06", rows) == 100.0
    now = 129.9
    assert session._cached_max_pain("2026-10-06", rows) == 100.0
    now = 130.0
    assert session._cached_max_pain("2026-10-06", rows) == 200.0
    assert calls == 2


class _SessionRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), value.strip().upper()


@pytest.fixture()
def pin_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.executescript(
            """
            CREATE TABLE public.option_protection_owners (
                option_run_id TEXT PRIMARY KEY, state TEXT NOT NULL
            );
            CREATE TABLE public.option_run_states (
                strategy_run_id TEXT PRIMARY KEY, legs TEXT NOT NULL
            );
            CREATE TABLE public.strategy_plan_option_runs (
                plan_id TEXT PRIMARY KEY, option_run_id TEXT NOT NULL, phase TEXT NOT NULL
            );
            CREATE TABLE public.strategy_jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL
            );
            CREATE TABLE public.strategy_proposals (
                proposal_id TEXT PRIMARY KEY, job_id TEXT
            );
            CREATE TABLE public.strategy_plans (
                plan_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
                plan_kind TEXT NOT NULL, resolved_plan TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            );
            """
        )
        dbapi_connection.commit()

    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_required_underlyings_pins_active_position_from_frozen_plan(
    pin_session_factory,
):
    with pin_session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.option_run_states (strategy_run_id, legs) "
                "VALUES ('opt-1', :legs)"
            ),
            {"legs": json.dumps([{"tradingsymbol": "NIFTY26OCT25000CE"}])},
        )
        session.execute(
            text(
                "INSERT INTO public.option_protection_owners (option_run_id, state) "
                "VALUES ('opt-1', 'active')"
            )
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_plans "
                "(plan_id, proposal_id, plan_kind, resolved_plan, created_at) "
                "VALUES ('plan-1', 'proposal-1', 'option_structure', :plan, :created_at)"
            ),
            {
                "plan": json.dumps({"underlying": "NIFTY"}),
                "created_at": datetime.now(timezone.utc),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_plan_option_runs "
                "(plan_id, option_run_id, phase) VALUES ('plan-1', 'opt-1', 'entry')"
            )
        )
        session.commit()

    assert session_pins.required_underlyings(pin_session_factory) == {
        "NIFTY": {"position"}
    }


def test_required_underlyings_pins_recent_option_plan_for_running_job(
    pin_session_factory,
):
    with pin_session_factory() as session:
        session.execute(
            text("INSERT INTO public.strategy_jobs (id, status) VALUES ('job-1', 'running')")
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_proposals (proposal_id, job_id) "
                "VALUES ('proposal-1', 'job-1')"
            )
        )
        session.execute(
            text(
                "INSERT INTO public.strategy_plans "
                "(plan_id, proposal_id, plan_kind, resolved_plan, created_at) "
                "VALUES ('plan-1', 'proposal-1', 'option_structure', :plan, :created_at)"
            ),
            {
                "plan": json.dumps({"underlying": "BANKNIFTY"}),
                "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            },
        )
        session.commit()

    assert session_pins.required_underlyings(pin_session_factory) == {
        "BANKNIFTY": {"strategy"}
    }


def _counting_manager(monkeypatch, *, starts: list, fail: bool = False):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))

    async def _fake_start(underlying, window_size=12, cadence_sec=5):
        starts.append((underlying, window_size, cadence_sec))
        if fail:
            raise RuntimeError("no spot token")
        manager.sessions[underlying] = cast(Any, object())

    async def _noop_converge():
        return None

    monkeypatch.setattr(manager, "start_session", _fake_start)
    monkeypatch.setattr(manager, "_converge_subscriptions", _noop_converge)
    return manager


def test_ensure_session_starts_once_and_is_bounded_to_available_underlyings(monkeypatch):
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "NIFTY,BANKNIFTY")
    starts: list = []
    manager = _counting_manager(monkeypatch, starts=starts)

    assert asyncio.run(manager.ensure_session("NIFTY")) is True
    # Idempotent: an existing session is not started a second time.
    assert asyncio.run(manager.ensure_session("nifty")) is True
    assert starts == [("NIFTY", 12, 5)]

    # On-demand reads may start any supported underlying, not only always-on.
    assert asyncio.run(manager.ensure_session("FINNIFTY")) is True
    assert starts == [("NIFTY", 12, 5), ("FINNIFTY", 12, 5)]

    # The fixed available-underlyings contract remains the boundary.
    assert asyncio.run(manager.ensure_session("UNKNOWN")) is False
    assert starts == [("NIFTY", 12, 5), ("FINNIFTY", 12, 5)]


def test_ensure_session_contains_a_failed_start(monkeypatch):
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "NIFTY")
    starts: list = []
    manager = _counting_manager(monkeypatch, starts=starts, fail=True)

    assert asyncio.run(manager.ensure_session("NIFTY")) is False
    assert starts == [("NIFTY", 12, 5)]
    # A half-started session is not left behind, so a later attempt can retry.
    assert "NIFTY" not in manager.sessions


def test_apply_settings_updates_running_sessions_and_starts_always_on(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))

    class _FakeSession:
        def __init__(self):
            self.underlying = "BANKNIFTY"
            self.window_size = 17
            self.cadence_sec = 5
            self.tick_driven = True
            self.min_interval_sec = 1.0
            self.calls = []

        async def update_config(self, window_size, cadence_sec):
            self.calls.append((window_size, cadence_sec))
            self.cadence_sec = cadence_sec

    session = _FakeSession()
    manager.sessions = {"BANKNIFTY": cast(Any, session)}
    ensured = []

    async def _ensure(underlying, window_size=12, cadence_sec=None):
        ensured.append((underlying, window_size, cadence_sec))
        return True

    monkeypatch.setattr(manager, "ensure_session", _ensure)
    settings = OptionsSettings(
        always_on=["NIFTY"],
        cadence_sec=3,
        tick_driven=False,
        min_interval_sec=0.5,
        idle_stop_minutes=9,
        source="db",
        updated_at=None,
        updated_by="app:admin",
    )

    result = asyncio.run(manager.apply_settings(settings))

    assert result == {"NIFTY": True}
    assert manager.always_on == {"NIFTY"}
    assert manager.idle_stop_minutes == 9
    assert session.calls == [(17, 3)]
    assert session.tick_driven is False
    assert session.min_interval_sec == 0.5
    assert ensured == [("NIFTY", 12, 3)]


def test_snapshot_read_touches_and_schedules_one_ensure(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))
    starts = 0

    async def _ensure(_underlying):
        nonlocal starts
        starts += 1
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(manager, "ensure_session", _ensure)

    async def _exercise():
        assert manager.get_snapshot("nifty") is None
        touched = manager.last_used["NIFTY"]
        assert manager.get_snapshot("NIFTY") is None
        assert manager.last_used["NIFTY"] >= touched
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        manager.close()

    asyncio.run(_exercise())
    assert starts == 1


def test_reaper_stops_idle_on_demand_but_never_always_on(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))
    manager.sessions = {"NIFTY": cast(Any, object()), "FINNIFTY": cast(Any, object())}
    manager.always_on = {"NIFTY"}
    manager.idle_stop_minutes = 15
    manager.last_used = {"NIFTY": 0.0, "FINNIFTY": 0.0}
    stopped = []

    async def _stop(underlying):
        stopped.append(underlying)
        manager.sessions.pop(underlying, None)

    monkeypatch.setattr(manager, "stop_session", _stop)
    monkeypatch.setattr(
        "backend.strategies.market_session.session_state",
        lambda _exchange: {"open": True, "reason": "open"},
    )
    asyncio.run(manager._reap_idle_sessions(now_monotonic=901.0))
    assert stopped == ["FINNIFTY"]
    assert "NIFTY" in manager.sessions


def test_reaper_never_stops_an_idle_pinned_session(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))
    manager.sessions = {"NIFTY": cast(Any, object())}
    manager.pins = {"NIFTY": {"strategy"}}
    manager.idle_stop_minutes = 15
    manager.last_used = {"NIFTY": 0.0}
    stopped = []

    async def _stop(underlying):
        stopped.append(underlying)

    monkeypatch.setattr(manager, "stop_session", _stop)
    monkeypatch.setattr(sessions_module, "_after_nfo_close", lambda: False)

    asyncio.run(manager._reap_idle_sessions(now_monotonic=901.0))

    assert stopped == []


def test_after_close_releases_strategy_pin_but_not_position_pin(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))
    manager.sessions = {
        "NIFTY": cast(Any, object()),
        "BANKNIFTY": cast(Any, object()),
    }
    manager.pins = {
        "NIFTY": {"position"},
        "BANKNIFTY": {"strategy"},
    }
    stopped = []

    async def _stop(underlying):
        stopped.append(underlying)

    monkeypatch.setattr(manager, "stop_session", _stop)
    monkeypatch.setattr(sessions_module, "_after_nfo_close", lambda: True)

    asyncio.run(manager._reap_idle_sessions(now_monotonic=100_000.0))

    assert stopped == ["BANKNIFTY"]


def test_pin_read_error_keeps_previous_pins():
    def _broken_factory():
        raise RuntimeError("pin store unavailable")

    manager = OptionsSessionManager(
        cast(Any, object()),
        cast(Any, _SessionRepo()),
        session_factory=_broken_factory,
    )
    manager.pins = {"NIFTY": {"position"}}

    asyncio.run(manager._refresh_pins())

    assert manager.pins == {"NIFTY": {"position"}}


def test_pin_refresh_releases_strategy_at_close_and_starts_position(
    monkeypatch,
):
    manager = OptionsSessionManager(
        cast(Any, object()),
        cast(Any, _SessionRepo()),
        session_factory=cast(Any, object()),
    )
    ensured = []

    async def _ensure(underlying, window_size=12, cadence_sec=None):
        ensured.append((underlying, window_size, cadence_sec))
        return True

    monkeypatch.setattr(
        session_pins,
        "required_underlyings",
        lambda _factory: {
            "NIFTY": {"position", "strategy"},
            "BANKNIFTY": {"strategy"},
        },
    )
    monkeypatch.setattr(sessions_module, "_after_nfo_close", lambda: True)
    monkeypatch.setattr(manager, "ensure_session", _ensure)

    asyncio.run(manager._refresh_pins())

    assert manager.pins == {"NIFTY": {"position"}}
    assert ensured == [("NIFTY", 12, 5)]


def test_zero_idle_minutes_never_reaps_during_market_hours(monkeypatch):
    manager = OptionsSessionManager(cast(Any, object()), cast(Any, _SessionRepo()))
    manager.sessions = {"FINNIFTY": cast(Any, object())}
    manager.idle_stop_minutes = 0
    manager.last_used = {"FINNIFTY": 0.0}
    stopped = []

    async def _stop(underlying):
        stopped.append(underlying)

    monkeypatch.setattr(manager, "stop_session", _stop)
    monkeypatch.setattr(
        "backend.strategies.market_session.session_state",
        lambda _exchange: {"open": True, "reason": "open"},
    )
    asyncio.run(manager._reap_idle_sessions(now_monotonic=100_000.0))
    assert stopped == []


def test_tick_listener_routes_tokens_to_matching_sessions(monkeypatch):
    market = _TickListenerMarketData()
    manager = OptionsSessionManager(cast(Any, market), cast(Any, object()))
    session = _DirtySession()
    manager.sessions[session.underlying] = cast(Any, session)

    class _Redis:
        async def set(self, *_args, **_kwargs):
            return None

        async def publish(self, *_args, **_kwargs):
            return None

    async def _noop_converge():
        return None

    monkeypatch.setattr(sessions_module, "get_redis", lambda: _Redis())
    monkeypatch.setattr(manager, "_converge_subscriptions", _noop_converge)

    async def _exercise():
        await manager.on_session_update(session)
        market.callback(10, {})
        market.callback(99, {})

    asyncio.run(_exercise())
    assert session.dirty_count == 1


def test_cadence_coalesces_dirty_marks_and_respects_min_interval(monkeypatch):
    monkeypatch.setenv("OPTIONS_CHAIN_MIN_INTERVAL_S", "0.25")
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})), cadence_sec=5)
    session.is_running = True
    monkeypatch.setattr(session, "_refresh_expiries", _async_noop)
    compute_count = 0

    async def _count_compute():
        nonlocal compute_count
        compute_count += 1

    monkeypatch.setattr(session, "_compute_and_publish", _count_compute)

    async def _exercise():
        task = asyncio.create_task(session._run_cadence())
        for _ in range(5):
            session.mark_dirty()
        await asyncio.sleep(0.5)
        session.is_running = False
        task.cancel()
        await task

    asyncio.run(_exercise())
    assert compute_count == 1


def test_cadence_timer_computes_without_dirty_mark(monkeypatch):
    monkeypatch.setenv("OPTIONS_CHAIN_MIN_INTERVAL_S", "0.25")
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})), cadence_sec=0.3)
    session.is_running = True
    monkeypatch.setattr(session, "_refresh_expiries", _async_noop)
    computed = asyncio.Event()

    async def _count_compute():
        computed.set()

    monkeypatch.setattr(session, "_compute_and_publish", _count_compute)

    async def _exercise():
        task = asyncio.create_task(session._run_cadence())
        await asyncio.wait_for(computed.wait(), timeout=0.4)
        session.is_running = False
        task.cancel()
        await task

    asyncio.run(_exercise())


async def _async_noop():
    return None


def test_rank_tokens_keeps_spot_and_atm_before_far_wings():
    ranks = {
        1: (0, 0, 0),        # spot
        10: (1, 0, 0),       # near ATM CE
        11: (1, 0, 0),       # near ATM PE
        20: (1, 0, 3),       # far-expiry ATM
        30: (1, 5, 0),       # near wing
        40: (1, 25, 4),      # far wing
    }
    kept, dropped = sessions_module.rank_tokens(ranks, cap=4)
    assert kept == [1, 10, 11, 20]
    assert dropped == [30, 40]


class _SubscribingMarketData:
    def __init__(self):
        self.subscriptions = None

    async def set_owner_subscriptions(self, owner_id, tokens):
        self.subscriptions = dict(tokens)

    async def delete_owner(self, owner_id):
        self.subscriptions = {}


class _RankedSession:
    def __init__(self, ranks):
        self.token_ranks = dict(ranks)
        self.desired_tokens = set(ranks)


def test_converge_truncates_by_rank_and_reports_drops(monkeypatch):
    market = _SubscribingMarketData()
    manager = OptionsSessionManager(cast(Any, market), cast(Any, object()))
    manager.sessions = {
        "NIFTY": cast(Any, _RankedSession({1: (0, 0, 0), 10: (1, 0, 0), 99: (1, 30, 4)})),
        "SENSEX": cast(Any, _RankedSession({2: (0, 0, 0), 12: (1, 1, 0)})),
    }
    monkeypatch.setattr(sessions_module, "TOKEN_CAP", 4)
    asyncio.run(manager._converge_subscriptions())
    assert set(market.subscriptions) == {1, 2, 10, 12}
    assert manager.dropped_tokens == {"NIFTY": 1, "SENSEX": 0}


def test_nearest_expiry_keeps_the_configured_window():
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})))
    strikes = [20000.0 + 50 * i for i in range(-100, 101)]
    assert session._expiry_window(expiry_index=0, strikes=strikes, center=20000.0, sigma=0.15, T=0.25) == 12


def test_far_expiry_widens_to_cover_ten_delta_and_is_capped():
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})))
    strikes = [20000.0 + 50 * i for i in range(-100, 101)]
    # 14 days, 15% vol: 20000*(exp(1.2816*0.15*sqrt(14/365))-1) ~= 758 pts -> 16 strikes
    assert session._expiry_window(expiry_index=1, strikes=strikes, center=20000.0, sigma=0.15, T=14 / 365) == 16
    # 90 days would need ~44 strikes: capped
    assert session._expiry_window(expiry_index=2, strikes=strikes, center=20000.0, sigma=0.15, T=90 / 365) == 30


def test_far_expiry_without_sigma_keeps_the_configured_window():
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})))
    strikes = [20000.0 + 50 * i for i in range(-100, 101)]
    assert session._expiry_window(expiry_index=1, strikes=strikes, center=20000.0, sigma=None, T=0.1) == 12


class _ForwardRepo:
    def __init__(self, table):
        self.table = table  # strike -> (ce_token, pe_token)
        self.calls = 0

    def get_option_instruments_for_strikes(self, underlying, expiry, strikes):
        self.calls += 1
        rows = []
        for strike in strikes:
            if strike in self.table:
                ce, pe = self.table[strike]
                rows.append({"strike": strike, "option_type": "CE", "instrument_token": ce})
                rows.append({"strike": strike, "option_type": "PE", "instrument_token": pe})
        return rows


class _ForwardManager:
    def __init__(self, table, ticks):
        self.instrument_repo = _ForwardRepo(table)
        self.market_data = _FakeMarketData(ticks)


def test_forward_uses_strike_parity_not_spot():
    # True forward 20030. Parity: C - P = F - K at every strike.
    table = {19950.0: (1, 2), 20000.0: (3, 4), 20050.0: (5, 6)}
    ticks = {
        1: {"last_price": 150.0}, 2: {"last_price": 70.0},    # 19950: 20030-19950=80
        3: {"last_price": 120.0}, 4: {"last_price": 90.0},    # 20000: 30
        5: {"last_price": 95.0}, 6: {"last_price": 115.0},    # 20050: -20
    }
    from datetime import date as _date

    session = OptionsSession("NIFTY", cast(Any, _ForwardManager(table, ticks)))
    forward, ce, pe = session._compute_forward(
        _date(2026, 10, 6), 20000.0, 20012.0, strikes=[19900.0, 19950.0, 20000.0, 20050.0, 20100.0]
    )
    assert forward == 20030.0
    assert (ce, pe) == (120.0, 90.0)


def test_forward_skips_strikes_with_a_zero_price():
    table = {20000.0: (3, 4), 20050.0: (5, 6)}
    ticks = {3: {"last_price": 120.0}, 4: {"last_price": 90.0}, 5: {"last_price": 0.0}, 6: {"last_price": 115.0}}
    from datetime import date as _date

    session = OptionsSession("NIFTY", cast(Any, _ForwardManager(table, ticks)))
    forward, _ce, _pe = session._compute_forward(
        _date(2026, 10, 6), 20000.0, 20012.0, strikes=[20000.0, 20050.0]
    )
    assert forward == 20030.0


def test_forward_instruments_are_cached_for_the_same_candidate_strikes():
    table = {20000.0: (3, 4), 20050.0: (5, 6)}
    ticks = {
        3: {"last_price": 120.0},
        4: {"last_price": 90.0},
        5: {"last_price": 95.0},
        6: {"last_price": 115.0},
    }
    manager = _ForwardManager(table, ticks)
    session = OptionsSession("NIFTY", cast(Any, manager))
    expiry = date(2026, 10, 6)

    for _ in range(2):
        forward, _ce, _pe = session._compute_forward(
            expiry,
            20000.0,
            20012.0,
            strikes=[20000.0, 20050.0],
        )
        assert forward == 20030.0

    assert manager.instrument_repo.calls == 1


def test_missing_spot_reuses_last_value_but_reports_not_live():
    session = OptionsSession("NIFTY", cast(Any, _ManagerStub({})))
    session.spot_token = 256265
    session.last_spot_ltp = 20000.0
    session.expiries = []
    _per_expiry, _tokens, spot, _ranks = session._run_computation()
    assert spot == 20000.0
    assert session.last_spot_live is False
    assert session.last_spot_age_sec is None


def test_live_spot_reports_its_age():
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=3)
    session = OptionsSession(
        "NIFTY", cast(Any, _ManagerStub({256265: {"last_price": 20010.0, "exchange_timestamp": stamp}}))
    )
    session.spot_token = 256265
    session.expiries = []
    _per_expiry, _tokens, spot, _ranks = session._run_computation()
    assert spot == 20010.0
    assert session.last_spot_live is True
    assert 2.0 <= session.last_spot_age_sec < 30.0


def test_a_tick_for_a_stopped_session_is_ignored():
    class _ListeningMarketData:
        def add_tick_listener(self, callback):
            self.callback = callback
            return lambda: None

    market = _ListeningMarketData()
    manager = OptionsSessionManager(cast(Any, market), cast(Any, object()))
    manager._token_sessions = {10: {"NIFTY"}}
    manager.sessions = {}
    market.callback(10, {"last_price": 1.0})  # must not raise
