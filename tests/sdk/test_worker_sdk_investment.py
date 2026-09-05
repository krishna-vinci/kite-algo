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


@pytest.fixture
def load_worker_fixture():
    def _load(name):
        return json.loads((FIXTURES / name).read_text())

    return _load


def test_index_snapshot_preserves_nse_identity(load_worker_fixture):
    from kite_algo_worker import WorkerIndexConstituentsSnapshot

    model = WorkerIndexConstituentsSnapshot.model_validate(
        load_worker_fixture("nifty500_constituents.json")
    )
    assert model.source_list == "Nifty500"
    assert model.member_count == len(model.members)
    assert model.complete is True
    assert {member.exchange for member in model.members} == {"NSE"}


def test_index_model_is_not_hardcoded_to_nifty500(load_worker_fixture):
    from kite_algo_worker import WorkerIndexConstituentsSnapshot

    payload = load_worker_fixture("nifty500_constituents.json")
    payload["source_list"] = "Nifty50"
    model = WorkerIndexConstituentsSnapshot.model_validate(payload)
    assert model.source_list == "Nifty50"


def test_index_status_model_is_source_list_scoped(load_worker_fixture):
    from kite_algo_worker import WorkerIndexConstituentStatus

    model = WorkerIndexConstituentStatus.model_validate(
        load_worker_fixture("nifty500_status.json")
    )
    assert model.source_list == "Nifty500"
    assert model.complete is True
    assert model.expected_member_count == 500
    assert model.actual_member_count == 500


def test_calendar_snapshot_parses_sessions(load_worker_fixture):
    from kite_algo_worker import WorkerCalendarSession, WorkerMarketCalendarSnapshot

    model = WorkerMarketCalendarSnapshot.model_validate(
        load_worker_fixture("calendar.json")
    )
    assert model.exchange == "NSE"
    assert model.segment == "CM"
    assert isinstance(model.sessions[0], WorkerCalendarSession)


def test_portfolio_snapshot_keeps_broker_fields(load_worker_fixture):
    from kite_algo_worker import WorkerAccountPortfolioSnapshot

    model = WorkerAccountPortfolioSnapshot.model_validate(
        load_worker_fixture("portfolio_success.json")
    )
    assert model.account_scope == "kite:SANITIZED"
    assert model.coherent is True
    assert model.funds["equity"]["available"]["cash"] == 50000


def test_portfolio_snapshot_preserves_degraded_coherence(load_worker_fixture):
    from kite_algo_worker import WorkerAccountPortfolioSnapshot

    model = WorkerAccountPortfolioSnapshot.model_validate(
        load_worker_fixture("portfolio_degraded.json")
    )
    assert model.coherent is False
    assert model.coherence_skew_ms > 0


def test_envelope_rejects_invalid_schema_version_and_identity(load_worker_fixture):
    from kite_algo_worker import WorkerIndexConstituentsSnapshot

    payload = load_worker_fixture("nifty500_constituents.json")

    bad_version = dict(payload)
    bad_version["schema_version"] = 0
    with pytest.raises(ValueError):
        WorkerIndexConstituentsSnapshot.model_validate(bad_version)

    missing_source = dict(payload)
    missing_source["source"] = ""
    with pytest.raises(ValueError):
        WorkerIndexConstituentsSnapshot.model_validate(missing_source)

    missing_list = dict(payload)
    missing_list["source_list"] = ""
    with pytest.raises(ValueError):
        WorkerIndexConstituentsSnapshot.model_validate(missing_list)


def test_snapshot_models_preserve_additive_fields(load_worker_fixture):
    from kite_algo_worker import WorkerMarketCalendarSnapshot

    payload = load_worker_fixture("calendar.json")
    payload["brand_new_server_field"] = {"note": "additive"}
    model = WorkerMarketCalendarSnapshot.model_validate(payload)
    assert model.raw["brand_new_server_field"] == {"note": "additive"}
    dumped = model.model_dump()
    assert dumped["brand_new_server_field"] == {"note": "additive"}


# ---------------------------------------------------------------------------
# Transport tests: raw and snapshot client methods
# ---------------------------------------------------------------------------

_SYNC_CALENDAR_STATUS_PAYLOAD = {
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


def _sync_client():
    from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient

    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_request(client, payload):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params")})
        return dict(payload)

    client._request = fake_request
    return calls


def test_index_constituent_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import KiteAlgoWorkerClient, WorkerIndexConstituentsSnapshot

    client = _sync_client()
    snapshot_payload = load_worker_fixture("nifty500_constituents.json")
    status_payload = load_worker_fixture("nifty500_status.json")

    calls = _capture_request(client, snapshot_payload)
    raw = client.get_index_constituents("Nifty500")
    assert type(raw) is dict
    assert calls == [{"method": "GET", "path": "/worker/market/indices/Nifty500", "params": {"schema_version": 1}}]

    client2 = _sync_client()
    calls2 = _capture_request(client2, snapshot_payload)
    snapshot = client2.get_index_constituents_snapshot("Nifty500")
    assert isinstance(snapshot, WorkerIndexConstituentsSnapshot)
    assert calls2 == [{"method": "GET", "path": "/worker/market/indices/Nifty500", "params": {"schema_version": 1}}]

    client3 = _sync_client()
    calls3 = _capture_request(client3, status_payload)
    status = client3.get_index_constituent_status_snapshot("Nifty500")
    assert type(status).__name__ == "WorkerIndexConstituentStatus"
    assert calls3[0]["path"] == "/worker/market/indices/Nifty500/status"
    assert calls3[0]["params"] == {"schema_version": 1}
    raw_status = client3.get_index_constituent_status("Nifty500")
    assert type(raw_status) is dict
    assert len(calls3) == 2


def test_market_calendar_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerMarketCalendarSnapshot

    client = _sync_client()
    calendar_payload = load_worker_fixture("calendar.json")
    calls = _capture_request(client, calendar_payload)

    raw = client.get_market_calendar("2026-09-01", "2026-12-31", exchange="NSE", segment="CM")
    assert type(raw) is dict
    assert calls[0] == {
        "method": "GET",
        "path": "/worker/market/calendar",
        "params": {"from": "2026-09-01", "to": "2026-12-31", "exchange": "NSE", "segment": "CM", "schema_version": 1},
    }

    client2 = _sync_client()
    _capture_request(client2, calendar_payload)
    snapshot = client2.get_market_calendar_snapshot("2026-09-01", "2026-12-31", exchange="NSE", segment="CM")
    assert isinstance(snapshot, WorkerMarketCalendarSnapshot)


def test_calendar_status_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerMarketCalendarStatus

    client = _sync_client()
    calls = _capture_request(client, dict(_SYNC_CALENDAR_STATUS_PAYLOAD))

    raw = client.get_market_calendar_status(exchange="NSE", segment="CM")
    assert type(raw) is dict
    assert calls[0] == {
        "method": "GET",
        "path": "/worker/market/calendar/status",
        "params": {"exchange": "NSE", "segment": "CM", "schema_version": 1},
    }

    client2 = _sync_client()
    _capture_request(client2, dict(_SYNC_CALENDAR_STATUS_PAYLOAD))
    snapshot = client2.get_market_calendar_status_snapshot(exchange="NSE", segment="CM")
    assert isinstance(snapshot, WorkerMarketCalendarStatus)
    assert snapshot.active_calendar_version == 4
    assert snapshot.coverage_end == "2026-12-31"
    assert snapshot.expiry_warning is False


def test_account_portfolio_methods_use_server_contract(load_worker_fixture):
    from kite_algo_worker import WorkerAccountPortfolioSnapshot

    client = _sync_client()
    portfolio_payload = load_worker_fixture("portfolio_success.json")
    calls = _capture_request(client, portfolio_payload)

    raw = client.get_account_portfolio(account_scope="kite:SANITIZED")
    assert type(raw) is dict
    assert calls[0] == {
        "method": "GET",
        "path": "/worker/account/portfolio",
        "params": {"schema_version": 1, "account_scope": "kite:SANITIZED"},
    }

    client2 = _sync_client()
    _capture_request(client2, portfolio_payload)
    snapshot = client2.get_account_portfolio_snapshot(account_scope="kite:SANITIZED")
    assert isinstance(snapshot, WorkerAccountPortfolioSnapshot)


def test_investment_methods_normalize_exchange_segment_and_validate_input(load_worker_fixture):
    client = _sync_client()
    calendar_payload = load_worker_fixture("calendar.json")
    calls = _capture_request(client, calendar_payload)

    client.get_market_calendar("2026-09-01", "2026-12-31", exchange="nse", segment="cm")
    assert calls[0]["params"]["exchange"] == "NSE"
    assert calls[0]["params"]["segment"] == "CM"

    client.get_market_calendar_status(exchange="nse", segment="cm")
    assert calls[1]["params"]["exchange"] == "NSE"
    assert calls[1]["params"]["segment"] == "CM"


def test_investment_methods_reject_invalid_arguments():
    client = _sync_client()
    calls = _capture_request(client, {})

    with pytest.raises(ValueError):
        client.get_index_constituents("")
    with pytest.raises(ValueError):
        client.get_index_constituent_status("   ")
    with pytest.raises(ValueError):
        client.get_market_calendar("", "2026-12-31")
    with pytest.raises(ValueError):
        client.get_market_calendar("2026-09-01", "")
    with pytest.raises(ValueError):
        client.get_market_calendar("2026-12-31", "2026-09-01")
    with pytest.raises(ValueError):
        client.get_market_calendar("not-a-date", "2026-12-31")
    with pytest.raises(ValueError):
        client.get_market_calendar("2026-09-01", "2026-12-31", exchange="  ")
    with pytest.raises(ValueError):
        client.get_market_calendar_status(exchange="NSE", segment="")
    assert calls == []

