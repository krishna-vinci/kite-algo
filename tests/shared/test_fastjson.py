from __future__ import annotations

import importlib
import json
import math
import sys
from datetime import date, datetime, timezone

from backend.shared import fastjson


def test_loads_accepts_str_and_bytes() -> None:
    assert fastjson.loads('{"value": 1}') == {"value": 1}
    assert fastjson.loads(b'{"value": 1}') == {"value": 1}


def test_dumps_str_returns_json_string_for_snapshot_shapes() -> None:
    snapshot = {
        "updated_at": datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
        "expiries": [date(2026, 5, 7)],
        "per_expiry": {2026: {"rows": [{"iv": math.nan}]}},
    }

    raw = fastjson.dumps_str(snapshot)

    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert decoded == {
        "updated_at": "2026-04-29 10:00:00+00:00",
        "expiries": ["2026-05-07"],
        "per_expiry": {"2026": {"rows": [{"iv": None}]}},
    }
    assert fastjson.loads(raw) == decoded


def test_stdlib_fallback_when_orjson_import_fails() -> None:
    original_orjson = sys.modules.get("orjson")
    sys.modules["orjson"] = None
    try:
        module = importlib.reload(fastjson)
        raw = module.dumps_str({"value": math.nan})
        assert json.loads(raw) == {"value": None}
        assert module.loads(raw) == {"value": None}
    finally:
        if original_orjson is None:
            sys.modules.pop("orjson", None)
        else:
            sys.modules["orjson"] = original_orjson
        importlib.reload(fastjson)
