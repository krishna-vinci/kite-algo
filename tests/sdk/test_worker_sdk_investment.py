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
