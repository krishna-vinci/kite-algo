import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "worker_api" / "v1"

_CALENDAR_STATUS_PAYLOAD = {
    "schema_version": 1,
    "source": "exchange_calendar_refresh",
    "retrieved_at": "2026-08-29T09:00:01+00:00",
    "exchange": "NSE",
    "segment": "CM",
    "active_calendar_version": 4,
    "coverage_start": "2025-01-01",
    "coverage_end": "2026-12-31",
    "complete": True,
    "expiry_warning": False,
}


@pytest.fixture
def load_worker_fixture():
    def _load(name):
        return json.loads((FIXTURES / name).read_text())

    return _load


def _async_client():
    from kite_algo_worker import AlgoWorkerConfig
    from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient

    return AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_request(client, payload):
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params")})
        return dict(payload)

    # AsyncKiteAlgoWorkerClient is a frozen dataclass.
    object.__setattr__(client, "_request", fake_request)
    return calls


def test_async_index_constituent_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerIndexConstituentStatus, WorkerIndexConstituentsSnapshot

    async def main():
        client = _async_client()
        snapshot_payload = load_worker_fixture("nifty500_constituents.json")
        status_payload = load_worker_fixture("nifty500_status.json")

        calls = _capture_request(client, snapshot_payload)
        raw = await client.get_index_constituents("Nifty500")
        assert type(raw) is dict
        assert calls == [{"method": "GET", "path": "/worker/market/indices/Nifty500", "params": {"schema_version": 1}}]

        snapshot = await client.get_index_constituents_snapshot("Nifty500")
        assert isinstance(snapshot, WorkerIndexConstituentsSnapshot)

        calls2 = _capture_request(client, status_payload)
        raw_status = await client.get_index_constituent_status("Nifty500")
        assert type(raw_status) is dict
        assert calls2[0]["path"] == "/worker/market/indices/Nifty500/status"
        assert calls2[0]["params"] == {"schema_version": 1}
        status = await client.get_index_constituent_status_snapshot("Nifty500")
        assert isinstance(status, WorkerIndexConstituentStatus)

    asyncio.run(main())


def test_async_market_calendar_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerMarketCalendarSnapshot, WorkerMarketCalendarStatus

    async def main():
        client = _async_client()
        calendar_payload = load_worker_fixture("calendar.json")

        calls = _capture_request(client, calendar_payload)
        raw = await client.get_market_calendar("2026-09-01", "2026-12-31", exchange="NSE", segment="CM")
        assert type(raw) is dict
        assert calls[0] == {
            "method": "GET",
            "path": "/worker/market/calendar",
            "params": {"from": "2026-09-01", "to": "2026-12-31", "exchange": "NSE", "segment": "CM", "schema_version": 1},
        }
        snapshot = await client.get_market_calendar_snapshot("2026-09-01", "2026-12-31", exchange="NSE", segment="CM")
        assert isinstance(snapshot, WorkerMarketCalendarSnapshot)

        calls2 = _capture_request(client, dict(_CALENDAR_STATUS_PAYLOAD))
        raw_status = await client.get_market_calendar_status(exchange="NSE", segment="CM")
        assert type(raw_status) is dict
        assert calls2[0] == {
            "method": "GET",
            "path": "/worker/market/calendar/status",
            "params": {"exchange": "NSE", "segment": "CM", "schema_version": 1},
        }
        status = await client.get_market_calendar_status_snapshot(exchange="NSE", segment="CM")
        assert isinstance(status, WorkerMarketCalendarStatus)

    asyncio.run(main())


def test_async_account_portfolio_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerAccountPortfolioSnapshot

    async def main():
        client = _async_client()
        portfolio_payload = load_worker_fixture("portfolio_success.json")

        calls = _capture_request(client, portfolio_payload)
        raw = await client.get_account_portfolio(account_scope="kite:SANITIZED")
        assert type(raw) is dict
        assert calls[0] == {
            "method": "GET",
            "path": "/worker/account/portfolio",
            "params": {"schema_version": 1, "account_scope": "kite:SANITIZED"},
        }
        snapshot = await client.get_account_portfolio_snapshot(account_scope="kite:SANITIZED")
        assert isinstance(snapshot, WorkerAccountPortfolioSnapshot)

    asyncio.run(main())


def test_async_investment_methods_reject_invalid_arguments():
    async def main():
        client = _async_client()
        calls = _capture_request(client, {})

        with pytest.raises(ValueError):
            await client.get_index_constituents("")
        with pytest.raises(ValueError):
            await client.get_market_calendar("2026-12-31", "2026-09-01")
        with pytest.raises(ValueError):
            await client.get_market_calendar_status(exchange="NSE", segment="")
        with pytest.raises(ValueError):
            await client.get_account_portfolio(account_scope="")
        assert calls == []

    asyncio.run(main())
