from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, cast

import pytest

sys.modules.setdefault("mibian", types.ModuleType("mibian"))
if "numba" not in sys.modules:
    numba_stub: Any = types.ModuleType("numba")

    def _njit(*_args, **_kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    numba_stub.njit = _njit
    sys.modules["numba"] = numba_stub

from backend.broker_api.options.options_greeks import black76_price
from backend.broker_api.options import options_sessions as sessions_module
from backend.broker_api.options.options_sessions import OptionsSession, OptionsSessionManager


class _FakeMarketData:
    def __init__(self, ticks: Dict[int, Dict[str, Any]]):
        self.latest_ticks = ticks


class _ManagerStub:
    def __init__(self, ticks: Dict[int, Dict[str, Any]]):
        self.market_data = _FakeMarketData(ticks)


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


class _SessionRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), value.strip().upper()


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


def test_ensure_session_starts_once_and_is_bounded(monkeypatch):
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "NIFTY,BANKNIFTY")
    starts: list = []
    manager = _counting_manager(monkeypatch, starts=starts)

    assert asyncio.run(manager.ensure_session("NIFTY")) is True
    # Idempotent: an existing session is not started a second time.
    assert asyncio.run(manager.ensure_session("nifty")) is True
    assert starts == [("NIFTY", 12, 5)]

    # Bounded: an underlying outside the configured set starts nothing.
    assert asyncio.run(manager.ensure_session("FINNIFTY")) is False
    assert starts == [("NIFTY", 12, 5)]


def test_ensure_session_contains_a_failed_start(monkeypatch):
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "NIFTY")
    starts: list = []
    manager = _counting_manager(monkeypatch, starts=starts, fail=True)

    assert asyncio.run(manager.ensure_session("NIFTY")) is False
    assert starts == [("NIFTY", 12, 5)]
    # A half-started session is not left behind, so a later attempt can retry.
    assert "NIFTY" not in manager.sessions


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

    def get_option_instruments_for_strikes(self, underlying, expiry, strikes):
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
