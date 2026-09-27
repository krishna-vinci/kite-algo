"""The production chain resolver names the exchange the contract lists on."""

from __future__ import annotations

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.strategies.compiler.chain_resolver import build_production_chain_resolver  # noqa: E402


class _Market:
    def resolve_selection(self, underlying, payload):
        return {"resolved": [{"instrument_token": 1, "tradingsymbol": f"{underlying}X"}]}


def test_sensex_selection_resolves_on_bfo_and_nifty_on_nfo():
    resolve = build_production_chain_resolver(_Market())
    sensex = resolve(underlying="SENSEX", expiry="2026-10-01", option_type="CE")
    nifty = resolve(underlying="NIFTY", expiry="2026-09-29", option_type="CE")
    assert sensex["exchange"] == "BFO"
    assert nifty["exchange"] == "NFO"
