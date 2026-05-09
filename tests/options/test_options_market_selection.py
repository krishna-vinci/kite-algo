from datetime import date

from backend.options.market.models import ResolvedOptionContract
from backend.options.market.repository import OffsetResolutionRequest, resolve_offset_from_repository
from backend.options.market.selection import resolve_delta_contract, resolve_offset_contract, resolve_offset_strike


def test_resolve_offset_contract_for_call_otm_uses_canonical_atm_direction():
    contract = resolve_offset_contract(
        underlying="NIFTY",
        expiry=date(2026, 5, 28),
        option_type="CE",
        offset="OTM2",
        atm_strike=25000,
        available_strikes=[24900, 24950, 25000, 25050, 25100, 25150],
        tradingsymbol_by_strike={25100.0: "NIFTY26MAY25100CE"},
        instrument_token_by_strike={25100.0: 101},
        lot_size=75,
        tick_size=0.05,
        ltp_by_strike={25100.0: 123.45},
    )

    assert isinstance(contract, ResolvedOptionContract)
    assert contract.strike == 25100.0
    assert contract.tradingsymbol == "NIFTY26MAY25100CE"


def test_resolve_offset_contract_for_put_itm_moves_up_from_atm():
    contract = resolve_offset_contract(
        underlying="NIFTY",
        expiry=date(2026, 5, 28),
        option_type="PE",
        offset="ITM1",
        atm_strike=25000,
        available_strikes=[24950, 25000, 25050],
        tradingsymbol_by_strike={25050.0: "NIFTY26MAY25050PE"},
        instrument_token_by_strike={25050.0: 202},
        lot_size=75,
        tick_size=0.05,
        ltp_by_strike={25050.0: 111.0},
    )

    assert contract.strike == 25050.0
    assert contract.option_type == "PE"


def test_resolve_offset_strike_uses_available_strike_universe_not_fixed_step():
    # Unequal strike spacing: 24980 and 25030 around ATM-like 25003
    strike = resolve_offset_strike(
        option_type="CE",
        offset="OTM1",
        atm_strike=25003,
        available_strikes=[24920, 24980, 25030, 25110],
    )

    assert strike == 25030.0


class _Provider:
    def __init__(self, strikes):
        self._strikes = strikes

    def get_distinct_strikes(self, underlying: str, expiry: date):
        return list(self._strikes)


def test_resolve_offset_from_repository_delegates_to_provider_strikes():
    request = OffsetResolutionRequest(
        underlying="NIFTY",
        expiry=date(2026, 5, 28),
        option_type="PE",
        offset="OTM1",
        atm_strike=25000,
    )

    strike = resolve_offset_from_repository(
        request,
        provider=_Provider([24950, 25000, 25070]),
    )
    assert strike == 24950.0


def test_resolve_delta_contract_uses_snapshot_delta_nearest_match():
    contract = resolve_delta_contract(
        underlying="NIFTY",
        expiry=date(2026, 5, 28),
        option_type="CE",
        delta_target=0.45,
        contracts_by_strike={
            25000.0: {"tsym": "NIFTY26MAY25000CE", "token": 101, "ltp": 120.0, "delta": 0.52},
            25050.0: {"tsym": "NIFTY26MAY25050CE", "token": 102, "ltp": 99.0, "delta": 0.44},
            25100.0: {"tsym": "NIFTY26MAY25100CE", "token": 103, "ltp": 81.0, "delta": 0.36},
        },
    )

    assert contract.resolver == "delta"
    assert contract.strike == 25050.0
    assert contract.resolution_meta["resolved_delta"] == 0.44


def test_resolve_delta_contract_treats_positive_put_target_as_magnitude():
    contract = resolve_delta_contract(
        underlying="NIFTY",
        expiry=date(2026, 5, 28),
        option_type="PE",
        delta_target=0.35,
        contracts_by_strike={
            24900.0: {"tsym": "NIFTY26MAY24900PE", "token": 201, "ltp": 70.0, "delta": -0.29},
            24950.0: {"tsym": "NIFTY26MAY24950PE", "token": 202, "ltp": 84.0, "delta": -0.36},
        },
    )

    assert contract.strike == 24950.0
    assert contract.resolution_meta["delta_comparison"] == "magnitude"
