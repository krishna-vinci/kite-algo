from __future__ import annotations

import json
import math
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.test_support import install_dependency_stubs

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

if "pyotp" not in sys.modules:
    pyotp = types.ModuleType("pyotp")

    class _DummyTOTP:
        def __init__(self, *args, **kwargs):
            pass

        def now(self) -> str:
            return "000000"

    pyotp.TOTP = _DummyTOTP
    sys.modules["pyotp"] = pyotp

install_dependency_stubs()

from backend.api.routers import worker_market  # noqa: E402
from backend.api.services.indicators_service import compute_indicator  # noqa: E402
from backend.api.schemas.worker_indicators import WorkerIndicatorRequest  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "backend" / "fixtures" / "indicator_golden.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _golden_close(actual, expected, path: str = "$") -> None:
    # Fixture stores raw compute output (NaN warmups); responses carry the
    # serialized form where non-finite values become null.
    if expected is None and isinstance(actual, float) and math.isnan(actual):
        return
    if actual is None and isinstance(expected, float) and math.isnan(expected):
        return
    if isinstance(actual, float) and isinstance(expected, float) and math.isnan(actual) and math.isnan(expected):
        return
    if isinstance(actual, dict):
        assert set(actual) == set(expected), path
        for key in actual:
            _golden_close(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(actual, list):
        assert len(actual) == len(expected), path
        for index, (a, b) in enumerate(zip(actual, expected)):
            _golden_close(a, b, f"{path}[{index}]")
        return
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        # Fixture was recorded from the previous in-adapter pandas path; the
        # backend runtime may JIT the same kernels, so compare numerically.
        assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12), f"{path}: {actual} != {expected}"
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_service_matches_recorded_adapter_reference() -> None:
    fixture = _fixture()
    for name, expected in fixture["results"].items():
        payload = WorkerIndicatorRequest(name=name, bars=fixture["bars"])
        actual = compute_indicator(payload)
        _golden_close(actual, expected, path=f"$.{name}")


class _FakeToken:
    def __init__(self, actions: list[str]):
        self.allowed_actions = actions
        self.status = "active"


@pytest.fixture()
def client(monkeypatch):
    async def _require_token(request):
        return _FakeToken(["market:read"])

    monkeypatch.setattr(worker_market, "require_worker_token", _require_token)
    app = FastAPI()
    app.include_router(worker_market.router, prefix="/api")
    return TestClient(app)


def test_endpoint_requires_market_read_action(client, monkeypatch):
    async def _limited_token(request):
        return _FakeToken(["runs:read"])

    monkeypatch.setattr(worker_market, "require_worker_token", _limited_token)
    bars = [{"close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
    response = client.post("/api/algo-workers/worker/indicators", json={"name": "sma", "bars": bars})
    assert response.status_code == 403


def test_endpoint_validates_bounds_and_schema(client):
    good_bar = {"close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}
    response = client.post("/api/algo-workers/worker/indicators", json={"name": "sma", "bars": [good_bar] * 1001})
    assert response.status_code == 422
    response = client.post("/api/algo-workers/worker/indicators", json={"name": "sma", "bars": [good_bar], "expression": "x"})
    assert response.status_code == 422
    response = client.post("/api/algo-workers/worker/indicators", json={"name": "macd", "bars": [good_bar], "fast_period": 26, "slow_period": 12})
    assert response.status_code == 422


def test_endpoint_excludes_forming_bars_by_default(client):
    fixture = _fixture()
    bars = [dict(bar) for bar in fixture["bars"][:20]]
    bars[-1]["is_complete"] = False
    response = client.post("/api/algo-workers/worker/indicators", json={"name": "sma", "bars": bars, "period": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["included_forming"] is False
    assert len(body["timestamps"]) == 19
