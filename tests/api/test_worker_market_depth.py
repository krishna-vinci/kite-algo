from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from backend.api.services.market_data import WorkerMarketDataService, WorkerQuoteRequest  # noqa: E402


_INSTRUMENT = {
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


def _service_for_tick(tick):
    service = WorkerMarketDataService()
    service.resolve_many = AsyncMock(return_value={"instruments": [_INSTRUMENT], "missing": []})
    service._get_tick = AsyncMock(return_value=tick)
    return service


def test_full_tick_preserves_ordered_depth_levels_and_stale_metadata():
    received_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    service = _service_for_tick(
        {
            "instrument_token": 408065,
            "last_price": 1525.5,
            "received_at": received_at,
            "depth": {
                "buy": [
                    {"price": 1525.45, "quantity": 100, "orders": 3},
                    {"price": 1525.4, "quantity": 250, "orders": 5},
                ],
                "sell": [{"price": 1525.55, "quantity": 80, "orders": 2}],
            },
        }
    )

    response = asyncio.run(service.get_quotes(WorkerQuoteRequest(symbols=["NSE:INFY"], mode="full")))
    quote = response["quotes"][0]

    assert quote["depth_available"] is True
    assert quote["depth_unavailable_reason"] is None
    assert quote["depth"] == {
        "buy": [
            {"price": 1525.45, "quantity": 100, "orders": 3},
            {"price": 1525.4, "quantity": 250, "orders": 5},
        ],
        "sell": [{"price": 1525.55, "quantity": 80, "orders": 2}],
    }
    assert quote["is_stale"] is True


def test_ltp_only_tick_reports_depth_unavailable_without_synthetic_levels():
    service = _service_for_tick(
        {
            "instrument_token": 408065,
            "last_price": 1525.5,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    response = asyncio.run(service.get_quotes(WorkerQuoteRequest(symbols=["NSE:INFY"])))
    quote = response["quotes"][0]

    assert quote["depth"] is None
    assert quote["depth_available"] is False
    assert quote["depth_unavailable_reason"] == "depth_not_supplied_by_feed"


def test_empty_market_depth_is_available_and_distinct_from_missing_feed_data():
    service = _service_for_tick(
        {
            "instrument_token": 408065,
            "last_price": 1525.5,
            "depth": {"buy": [], "sell": []},
        }
    )

    response = asyncio.run(service.get_quotes(WorkerQuoteRequest(symbols=["NSE:INFY"])))
    quote = response["quotes"][0]

    assert quote["depth"] == {"buy": [], "sell": []}
    assert quote["depth_available"] is True
    assert quote["depth_unavailable_reason"] is None


def test_invalid_depth_is_rejected_without_inventing_levels():
    service = _service_for_tick(
        {
            "instrument_token": 408065,
            "last_price": 1525.5,
            "depth": {"buy": [{"price": "not-a-price"}], "sell": []},
        }
    )

    response = asyncio.run(service.get_quotes(WorkerQuoteRequest(symbols=["NSE:INFY"])))
    quote = response["quotes"][0]

    assert quote["depth"] is None
    assert quote["depth_available"] is False
    assert quote["depth_unavailable_reason"] == "depth_payload_invalid"
