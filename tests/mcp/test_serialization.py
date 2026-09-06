from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kite_algo_mcp.serialization import SerializationLimitError, ok_result, serialize_json


def test_serialization_redacts_credentials_and_non_finite_values() -> None:
    result = ok_result({
        "when": datetime(2026, 9, 6, tzinfo=timezone.utc),
        "worker_token": "secret",
        "instrument_token": 123,
        "value": float("nan"),
    })
    assert result.data["worker_token"] == "[REDACTED]"
    assert result.data["instrument_token"] == 123
    assert result.data["value"] is None
    assert "redacted sensitive field" in result.warnings[0]
    assert any("non-finite" in item for item in result.warnings)
    assert result.data["when"].endswith("+00:00")


def test_serialization_enforces_result_bound() -> None:
    with pytest.raises(SerializationLimitError):
        serialize_json({"items": ["x" * 100] * 100}, max_bytes=128)
