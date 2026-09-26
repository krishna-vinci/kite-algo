"""Unit tests for ``backend.options.market.greeks.aggregate_run_greeks``.

Signed-quantity aggregation of per-contract Greeks for a set of OPEN legs: the
whole aggregate is refused (never a partial sum) when a leg's contract is
missing from the chain snapshot or its Greeks are stale.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.options.market.greeks import aggregate_run_greeks

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


def _contract(strike, ce=None, pe=None):
    return {"strike": strike, "ce": ce, "pe": pe}


def _packet(tsym, *, delta, gamma, theta, vega, updated_at=NOW):
    return {
        "tsym": tsym,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "updated_at": updated_at.isoformat(),
    }


def test_aggregate_sums_signed_quantity_times_delta_for_open_legs():
    contracts = [
        _contract(
            22500,
            ce=_packet("NIFTY22500CE", delta=0.5, gamma=0.001, theta=-2.0, vega=8.0),
        ),
        _contract(
            22300,
            pe=_packet("NIFTY22300PE", delta=-0.3, gamma=0.0008, theta=-1.5, vega=6.0),
        ),
    ]
    # Short 75 of the CE, long 75 of the PE.
    legs = [("NIFTY22500CE", -75.0), ("NIFTY22300PE", 75.0)]

    result = aggregate_run_greeks(legs, contracts, now=NOW)

    assert result["available"] is True
    assert result["reason"] is None
    assert result["delta"] == -75.0 * 0.5 + 75.0 * -0.3
    assert result["vega"] == -75.0 * 8.0 + 75.0 * 6.0


def test_aggregate_refuses_whole_result_when_one_leg_is_missing():
    contracts = [
        _contract(22500, ce=_packet("NIFTY22500CE", delta=0.5, gamma=0.001, theta=-2.0, vega=8.0)),
    ]
    legs = [("NIFTY22500CE", -75.0), ("NIFTY22300PE", 75.0)]

    result = aggregate_run_greeks(legs, contracts, now=NOW)

    assert result["available"] is False
    assert result["reason"] == "missing"
    assert result["delta"] is None
    assert result["vega"] is None


def test_aggregate_refuses_whole_result_when_a_greek_is_stale():
    stale_packet = _packet(
        "NIFTY22500CE",
        delta=0.5,
        gamma=0.001,
        theta=-2.0,
        vega=8.0,
        updated_at=datetime(2026, 9, 26, 9, 59, 45, tzinfo=timezone.utc),
    )
    contracts = [_contract(22500, ce=stale_packet)]
    legs = [("NIFTY22500CE", -75.0)]

    result = aggregate_run_greeks(legs, contracts, now=NOW, max_age_seconds=10.0)

    assert result["available"] is False
    assert result["reason"] == "stale"


def test_aggregate_reports_no_open_legs_reason_for_an_empty_leg_set():
    result = aggregate_run_greeks([], [], now=NOW)

    assert result["available"] is False
    assert result["reason"] == "no_open_legs"
