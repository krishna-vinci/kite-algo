"""live_quote_for_leg reads the frozen broker coordinates of a resolved plan leg."""

import asyncio

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import backend.api.services.market_data as market_data  # noqa: E402
from backend.strategies.live_readers import live_quote_for_leg  # noqa: E402


def test_resolved_leg_quotes_by_broker_token(monkeypatch):
    """Resolved plans carry broker_token/broker_exchange, not instrument_token."""
    seen = []

    class FakeService:
        async def get_quotes(self, request):
            seen.append((list(request.symbols), list(request.instrument_tokens)))
            return {
                "quotes": [
                    {"instrument_token": 424961, "tradingsymbol": "ITC", "symbol": "NSE:ITC",
                     "last_price": 266.0, "received_at": "2026-09-28T09:33:58+00:00"}
                ],
                "missing": [],
            }

    monkeypatch.setattr(market_data, "WorkerMarketDataService", FakeService)
    leg = {
        "instrument_id": "9cacd7fb-0443-4c84-87ca-36a75d594615",
        "exchange": "NSE",
        "broker_exchange": "NSE",
        "broker_token": 424961,
        "broker_symbol": "ITC",
        "tradingsymbol": "ITC",
    }

    quote = asyncio.run(live_quote_for_leg(leg))

    assert seen == [([], [424961])]
    assert quote["ltp"] == 266.0


def test_leg_without_token_resolves_exchange_qualified_symbol(monkeypatch):
    seen = []

    class FakeService:
        async def get_quotes(self, request):
            seen.append(list(request.symbols))
            return {
                "quotes": [{"instrument_token": 424961, "tradingsymbol": "ITC",
                            "symbol": "NSE:ITC", "last_price": 266.0}],
                "missing": [],
            }

    monkeypatch.setattr(market_data, "WorkerMarketDataService", FakeService)

    quote = asyncio.run(live_quote_for_leg({"exchange": "NSE", "tradingsymbol": "ITC"}))

    assert seen == [["NSE:ITC"]]
    assert quote["ltp"] == 266.0
