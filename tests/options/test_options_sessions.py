from __future__ import annotations

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
from backend.broker_api.options.options_sessions import OptionsSession


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
