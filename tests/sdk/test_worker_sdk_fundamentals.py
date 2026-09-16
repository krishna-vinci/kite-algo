"""0.7.7 SDK coverage: typed fundamentals methods (sync + async)."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fundamentals" / "v1"


FEATURES_PAYLOAD = {
    "schema_version": 1,
    "source": "screener",
    "retrieved_at": "2026-09-05T10:00:00+00:00",
    "features": [
        {
            "symbol": "RELIANCE",
            "statement_scope": "consolidated",
            "company_name": "Reliance Industries",
            "market_cap_cr": 1234.0,
            "current_price": 456.0,
            "stock_pe": 28.4,
            "ttm_revenue": 420.0,
            "quarterly_revenue_yoy_pct": 12.5,
            "promoter_holding_pct": 54.0,
            "as_of_date": "2026-06-30",
            "scraped_at": "2026-09-05T09:59:00+00:00",
            "brand_new_server_field": {"note": "additive"},
        }
    ],
    "missing_symbols": ["TCS"],
}

STATUS_PAYLOAD = {
    "schema_version": 1,
    "source": "screener",
    "retrieved_at": "2026-09-05T10:00:00+00:00",
    "symbols": [
        {"symbol": "RELIANCE", "statement_scope": "consolidated", "status": "success",
         "last_checked_at": "2026-09-05T09:59:00+00:00", "last_success_at": "2026-09-05T09:59:00+00:00",
         "last_error": None},
        {"symbol": "TCS", "statement_scope": "consolidated", "status": "failed",
         "last_checked_at": "2026-09-05T09:59:00+00:00", "last_success_at": None,
         "last_error": "RuntimeError: boom"},
    ],
    "missing_symbols": ["INFY"],
    "recent_runs": [{"scope_type": "index", "scope_value": "Nifty50", "mode": "incremental", "status": "completed"}],
}

STATEMENTS_PAYLOAD = {
    "schema_version": 1,
    "source": "screener",
    "retrieved_at": "2026-09-05T10:00:00+00:00",
    "symbol": "RELIANCE",
    "statement_scope": "consolidated",
    "dataset": "quarterly",
    "rows": [
        {"dataset": "quarterly", "period_end": "2026-03-01", "metric_key": "sales",
         "metric_name": "Sales", "value_text": "140", "numeric_value": 140.0,
         "scraped_at": "2026-09-05T09:59:00+00:00"},
    ],
}

SYNC_RUN_PAYLOAD = {
    "run_id": "0f0e0d0c-1111-2222-3333-444455556666",
    "scope": {"scope_type": "symbols", "scope_value": "RELIANCE"},
    "mode": "full",
    "symbols_requested": 1,
    "symbols_changed": 1,
    "symbols_unchanged": 0,
    "symbols_failed": 0,
    "symbols_skipped": 0,
    "failed_symbols": [],
}


def _sync_client():
    from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient

    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_api_root(client, payload):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params"), "json": kwargs.get("json")})
        import copy

        return copy.deepcopy(payload)

    client._request = fake_request
    return calls


def _async_client():
    from kite_algo_worker import AlgoWorkerConfig
    from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient

    return AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_async_api_root(client, payload):
    import copy

    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "params": kwargs.get("params"), "json": kwargs.get("json")})
        return copy.deepcopy(payload)

    object.__setattr__(client, "_request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


def test_scope_validation_rejects_missing_and_ambiguous_scopes():
    client = _sync_client()
    for call in (
        lambda: client.get_fundamentals_features(),
        lambda: client.get_fundamentals_features(symbols=["RELIANCE"], index="Nifty50"),
        lambda: client.get_fundamentals_status(),
        lambda: client.get_fundamentals_statements("RELIANCE", dataset=""),
        lambda: client.refresh_fundamentals(),
        lambda: client.refresh_fundamentals(symbols=["RELIANCE"], index="Nifty50"),
        lambda: client.refresh_fundamentals(symbols=["   "]),
    ):
        with pytest.raises(ValueError):
            call()


# ---------------------------------------------------------------------------
# Typed features
# ---------------------------------------------------------------------------


def test_get_fundamentals_features_parses_typed_response():
    from kite_algo_worker import FundamentalFeatureRow, FundamentalFeatures

    client = _sync_client()
    calls = _capture_api_root(client, FEATURES_PAYLOAD)

    result = client.get_fundamentals_features(symbols=["reliance", "TCS"])
    assert isinstance(result, FundamentalFeatures)
    assert result.schema_version == 1
    assert result.source == "screener"
    row = result.for_symbol("reliance")  # case-insensitive lookup
    assert isinstance(row, FundamentalFeatureRow)
    assert row.ttm_revenue == 420.0
    assert row.market_cap_cr == 1234.0
    assert row.company_name == "Reliance Industries"
    # Unknown additive server fields are preserved in raw and round-trip.
    assert row.raw["brand_new_server_field"] == {"note": "additive"}
    assert row.model_dump()["brand_new_server_field"] == {"note": "additive"}
    assert result.for_symbol("MISSING") is None
    assert result.missing_symbols == ["TCS"]
    # Symbol scopes travel as repeated query params (FastAPI list encoding).
    assert calls == [
        {"method": "GET", "path": "/worker/fundamentals/features",
         "params": {"symbols": ["RELIANCE", "TCS"], "schema_version": 1}, "json": None}
    ]


def test_get_fundamentals_features_by_index_scope():
    client = _sync_client()
    calls = _capture_api_root(client, FEATURES_PAYLOAD)
    client.get_fundamentals_features(index="Nifty500")
    assert calls[0]["params"] == {"index": "Nifty500", "schema_version": 1}


def test_envelope_rejects_invalid_schema_version():
    from kite_algo_worker import FundamentalFeatures

    payload = {**FEATURES_PAYLOAD, "schema_version": 0}
    with pytest.raises(ValueError):
        FundamentalFeatures.model_validate(payload)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_get_fundamentals_status_and_freshness_helper():
    from kite_algo_worker import FundamentalsStatus

    client = _sync_client()
    calls = _capture_api_root(client, STATUS_PAYLOAD)

    result = client.get_fundamentals_status(index="Nifty50")
    assert isinstance(result, FundamentalsStatus)
    assert {row.symbol for row in result.symbols} == {"RELIANCE", "TCS"}
    assert result.recent_runs[0]["status"] == "completed"
    assert result.missing_symbols == ["INFY"]

    now = datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)
    assert result.fresh_within("RELIANCE", 2.0, now=now) is True  # ~1h old
    assert result.fresh_within("RELIANCE", 0.5, now=now) is False
    assert result.fresh_within("TCS", 24.0, now=now) is False  # failed, never succeeded
    assert result.fresh_within("INFY", 24.0, now=now) is False  # missing
    assert calls[0]["path"] == "/worker/fundamentals/status"


# ---------------------------------------------------------------------------
# Statements and refresh
# ---------------------------------------------------------------------------


def test_get_fundamentals_statements_uses_query_params():
    from kite_algo_worker import FundamentalsStatements

    client = _sync_client()
    calls = _capture_api_root(client, STATEMENTS_PAYLOAD)

    result = client.get_fundamentals_statements("reliance", dataset="quarterly")
    assert isinstance(result, FundamentalsStatements)
    assert result.symbol == "RELIANCE"
    assert result.dataset == "quarterly"
    assert result.rows[0]["numeric_value"] == 140.0
    assert calls[0] == {
        "method": "GET", "path": "/worker/fundamentals/statements",
        "params": {"symbol": "RELIANCE", "dataset": "quarterly", "statement_scope": "consolidated", "schema_version": 1},
        "json": None,
    }


def test_refresh_fundamentals_sends_exclusive_scope_body():
    from kite_algo_worker import FundamentalsSyncRun

    client = _sync_client()
    calls = _capture_api_root(client, SYNC_RUN_PAYLOAD)

    run = client.refresh_fundamentals(symbols=["reliance"], mode="full")
    assert isinstance(run, FundamentalsSyncRun)
    assert run.run_id.startswith("0f0e0d0c")
    assert run.symbols_changed == 1
    assert run.scope == {"scope_type": "symbols", "scope_value": "RELIANCE"}
    assert calls[0] == {
        "method": "POST", "path": "/worker/fundamentals/sync",
        "params": None, "json": {"symbols": ["RELIANCE"], "mode": "full"},
    }

    calls2 = _capture_api_root(client, SYNC_RUN_PAYLOAD)
    client.refresh_fundamentals(index="Nifty50")
    assert calls2[0]["json"] == {"index": "Nifty50", "mode": "incremental"}


def test_refresh_conflict_surfaces_as_worker_error():
    from kite_algo_worker import KiteAlgoWorkerError
    from kite_algo_worker.exceptions import error_for_status

    client = _sync_client()

    def conflict(method, path, **kwargs):
        raise error_for_status(409, {"detail": "fundamentals sync already in progress"}, fallback="fb")

    client._request = conflict
    with pytest.raises(KiteAlgoWorkerError) as excinfo:
        client.refresh_fundamentals(symbols=["RELIANCE"])
    assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# Async twins
# ---------------------------------------------------------------------------


def test_async_fundamentals_methods_use_server_contract():
    from kite_algo_worker import FundamentalFeatures, FundamentalsStatements, FundamentalsStatus, FundamentalsSyncRun

    async def main():
        client = _async_client()

        calls = _capture_async_api_root(client, FEATURES_PAYLOAD)
        features = await client.get_fundamentals_features(index="Nifty500")
        assert isinstance(features, FundamentalFeatures)
        assert calls[0] == {"method": "GET", "path": "/worker/fundamentals/features", "params": {"index": "Nifty500", "schema_version": 1}, "json": None}

        calls = _capture_async_api_root(client, STATUS_PAYLOAD)
        status = await client.get_fundamentals_status(symbols=["RELIANCE"])
        assert isinstance(status, FundamentalsStatus)
        assert calls[0]["params"] == {"symbols": ["RELIANCE"], "schema_version": 1}

        calls = _capture_async_api_root(client, STATEMENTS_PAYLOAD)
        statements = await client.get_fundamentals_statements("RELIANCE", dataset="quarterly")
        assert isinstance(statements, FundamentalsStatements)
        assert calls[0]["path"] == "/worker/fundamentals/statements"

        calls = _capture_async_api_root(client, SYNC_RUN_PAYLOAD)
        run = await client.refresh_fundamentals(symbols=["RELIANCE"], mode="full")
        assert isinstance(run, FundamentalsSyncRun)
        assert calls[0]["json"] == {"symbols": ["RELIANCE"], "mode": "full"}

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Contract fixture parity (mirrors the sanitized server envelope)
# ---------------------------------------------------------------------------


def test_contract_fixture_round_trips_through_typed_model():
    import json

    from kite_algo_worker import FundamentalFeatures

    payload = json.loads((FIXTURES / "fundamentals_features.json").read_text())
    model = FundamentalFeatures.model_validate(payload)
    assert model.schema_version == 1
    assert model.source == "screener"
    row = model.for_symbol("reliance")
    assert row.ttm_revenue == 420.0
    assert row.as_of_date == "2026-06-30"
    assert row.scraped_at is not None
    assert model.missing_symbols == ["SANITIZED2"]
    dumped = model.model_dump()
    assert dumped["schema_version"] == 1
    assert dumped["source"] == "screener"
