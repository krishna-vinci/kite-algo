from datetime import date

import pytest

from backend.broker_api.instruments.catalog import (
    RefreshFailure,
    RefreshResult,
    RefreshValidationError,
    normalize_broker_record,
    normalize_exchange_rows,
)


def _record(**overrides):
    value = {
        "instrument_token": 100,
        "exchange_token": 10,
        "tradingsymbol": "GOLD26OCTFUT",
        "name": "GOLD",
        "expiry": date(2026, 10, 30),
        "strike": None,
        "tick_size": 1,
        "lot_size": 100,
        "instrument_type": "FUT",
        "segment": "MCX-FUT",
        "exchange": "MCX",
    }
    value.update(overrides)
    return value


def test_normalize_broker_record_builds_public_and_identity_keys():
    normalized = normalize_broker_record(_record(), source_exchange="MCX")
    assert normalized["public_key"] == "MCX:GOLD26OCTFUT"
    assert normalized["underlying"] == "GOLD"
    assert normalized["broker_token"] == 100
    assert normalized["identity_key"]


def test_exchange_validation_rejects_duplicate_token_for_different_contracts():
    with pytest.raises(RefreshValidationError, match="reuses token"):
        normalize_exchange_rows(
            "MCX",
            [_record(), _record(instrument_token=100, tradingsymbol="GOLD26NOVFUT", expiry=date(2026, 11, 30))],
        )


def test_exchange_validation_rejects_empty_or_short_payload():
    with pytest.raises(RefreshValidationError, match="minimum"):
        normalize_exchange_rows("MCX", [], minimum_count=1)


def test_exchange_validation_accepts_distinct_contracts_and_preserves_raw_values():
    rows = normalize_exchange_rows(
        "MCX",
        [_record(), _record(instrument_token=101, tradingsymbol="GOLD26NOVFUT", expiry=date(2026, 11, 30))],
    )
    assert len(rows) == 2
    assert rows[0]["raw_record"]["instrument_token"] == 100


def test_refresh_result_is_serializable():
    result = RefreshResult("generation", "degraded", ["MCX", "NSE"], ["NSE"], ["MCX"], 2, {"MCX": "timeout"})
    payload = result.to_dict()
    assert payload["status"] == "degraded"
    assert payload["retained_exchanges"] == ["MCX"]
    assert RefreshFailure("MCX", "timeout").exchange == "MCX"
