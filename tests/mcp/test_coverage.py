from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.endpoint_manifest import WORKER_HTTP_ENDPOINTS, WORKER_WEBSOCKET_PATHS  # noqa: E402


COVERAGE_PATH = ROOT / "mcp" / "python" / "kite_algo_mcp" / "coverage.json"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
DISPOSITIONS = {"tool", "internal", "deferred"}


def _records() -> list[dict[str, object]]:
    records = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    return records


def test_every_http_operation_has_one_disposition() -> None:
    records = _records()
    expected = {(entry.method, entry.path) for entry in WORKER_HTTP_ENDPOINTS}
    http_records = [record for record in records if record["method"] in HTTP_METHODS]
    actual = [(record["method"], record["path"]) for record in http_records]

    assert len(actual) == len(set(actual))
    assert set(actual) == expected
    assert len(actual) == len(expected)
    assert all(record["disposition"] in DISPOSITIONS for record in records)
    assert all(str(record["reason"]).strip() for record in records)
    assert all(isinstance(record["tools"], list) for record in records)


def test_websocket_and_helper_families_are_explicitly_mapped() -> None:
    records = _records()
    websocket_records = [record for record in records if record["method"] == "WS"]
    helper_records = [record for record in records if record["method"] == "HELPER"]

    assert {record["path"] for record in websocket_records} == set(WORKER_WEBSOCKET_PATHS)
    assert helper_records
    assert all(record["path"] for record in helper_records)
    assert all(record["method"] in {"WS", "HELPER"} for record in websocket_records + helper_records)


def test_records_have_only_reviewed_record_shapes() -> None:
    records = _records()
    allowed_methods = HTTP_METHODS | {"WS", "HELPER"}
    required_keys = {"method", "path", "disposition", "tools", "reason"}

    assert all(set(record) == required_keys for record in records)
    assert all(record["method"] in allowed_methods for record in records)
    assert all(isinstance(record["path"], str) and record["path"] for record in records)
    assert all(isinstance(record["tools"], list) for record in records)
