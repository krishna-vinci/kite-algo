from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.shared.serialization import _hash_token
from backend.api.routers.worker_shared import require_worker_token
from backend.options.api.execution_router import router as execution_router
from backend.options.api.market_router import get_options_session_manager, router as market_router
from backend.options.api.protection_router import router as protection_router
from backend.options.api.worker_options_router import router as worker_options_router
from backend.options.execution.store import OptionRunStore, get_option_run_store


class _FakeInstrumentRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), None


class _FakeManager:
    def __init__(self):
        self.instrument_repo = _FakeInstrumentRepo()
        self._snapshot = {
            "underlying": "NIFTY",
            "spot_ltp": 22520.0,
            "updated_at": "2026-04-29T10:00:00Z",
            "expiries": ["2026-05-07"],
            "per_expiry": {"2026-05-07": {"atm_strike": 22500, "rows": []}},
        }

    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper()

    def get_snapshot(self, _underlying: str):
        return self._snapshot


class _FakeWorkerRepo:
    def __init__(self, *, raw_token: str | None = None):
        self._tokens = {}
        if raw_token:
            token_hash = _hash_token(raw_token)
            self._tokens[token_hash] = SimpleNamespace(
                token_id="tok-1",
                status="active",
                expires_at=None,
            )

    async def get_token_by_hash(self, token_hash: str):
        return self._tokens.get(token_hash)

    async def touch_token(self, _token_id: str):
        return datetime.now(timezone.utc)


def _app(worker_repo: _FakeWorkerRepo) -> FastAPI:
    app = FastAPI()
    app.state.algo_worker_repository = worker_repo
    store = OptionRunStore()
    app.state.test_option_run_store = store
    app.include_router(market_router)
    app.include_router(execution_router)
    app.include_router(protection_router)
    app.include_router(worker_options_router)
    app.dependency_overrides[get_options_session_manager] = lambda: _FakeManager()
    app.dependency_overrides[get_option_run_store] = lambda: store
    return app


def _has_route(app: FastAPI, path: str, method: str) -> bool:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method.upper() in route.methods:
            return True
    return False


def test_worker_options_routes_are_registered() -> None:
    app = _app(_FakeWorkerRepo(raw_token="kwa_valid"))
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/session", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/expiries", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/chain", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/mini-chain", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/greeks", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/selection/resolve", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/analytics/pcr", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/underlyings/{underlying}/analytics/max-pain", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/strategies/preview", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-entry", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/enter", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-exit", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/exit", "POST")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/state", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/protection", "PUT")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/state", "GET")
    assert _has_route(app, "/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/replay", "POST")


def test_worker_options_route_rejects_missing_worker_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    response = client.get("/api/algo-workers/worker/options/underlyings/NIFTY/session")
    assert response.status_code == 401
    assert response.json()["detail"] == "Worker bearer token required"


def test_worker_options_route_accepts_valid_worker_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    response = client.get(
        "/api/algo-workers/worker/options/underlyings/NIFTY/session",
        headers={"Authorization": "Bearer kwa_valid"},
    )
    assert response.status_code == 200
    assert response.json()["underlying"] == "NIFTY"


def test_worker_options_run_proxy_accepts_valid_worker_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    response = client.post(
        "/api/algo-workers/worker/options/runs",
        headers={"Authorization": "Bearer kwa_valid"},
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [{"leg_id": "buy_1", "transaction_type": "BUY", "tradingsymbol": "NIFTY26MAY25000CE", "quantity": 75}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["strategy_run_id"].startswith("opt_run_")
    assert body["product"] == "MIS"


def test_worker_market_run_and_protection_routes_reject_missing_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))

    market_response = client.get("/api/algo-workers/worker/options/underlyings/NIFTY/session")
    run_response = client.post(
        "/api/algo-workers/worker/options/runs",
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                }
            ],
        },
    )
    protection_response = client.put(
        "/api/algo-workers/worker/options/runs/opt_run_missing/protection",
        json={"rules": []},
    )

    assert market_response.status_code == 401
    assert run_response.status_code == 401
    assert protection_response.status_code == 401
    assert market_response.json()["detail"] == "Worker bearer token required"
    assert run_response.json()["detail"] == "Worker bearer token required"
    assert protection_response.json()["detail"] == "Worker bearer token required"


def test_worker_options_route_rejects_invalid_worker_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    response = client.get(
        "/api/algo-workers/worker/options/underlyings/NIFTY/session",
        headers={"Authorization": "Bearer kwa_invalid"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid worker token"


def test_worker_market_run_and_protection_routes_reject_invalid_token() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    headers = {"Authorization": "Bearer kwa_invalid"}

    market_response = client.get("/api/algo-workers/worker/options/underlyings/NIFTY/session", headers=headers)
    run_response = client.post(
        "/api/algo-workers/worker/options/runs",
        headers=headers,
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                }
            ],
        },
    )
    protection_response = client.put(
        "/api/algo-workers/worker/options/runs/opt_run_invalid/protection",
        headers=headers,
        json={"rules": []},
    )

    assert market_response.status_code == 401
    assert run_response.status_code == 401
    assert protection_response.status_code == 401
    assert market_response.json()["detail"] == "Invalid worker token"
    assert run_response.json()["detail"] == "Invalid worker token"
    assert protection_response.json()["detail"] == "Invalid worker token"


def test_valid_worker_token_can_create_run_and_update_protection() -> None:
    client = TestClient(_app(_FakeWorkerRepo(raw_token="kwa_valid")))
    headers = {"Authorization": "Bearer kwa_valid"}

    create_response = client.post(
        "/api/algo-workers/worker/options/runs",
        headers=headers,
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                }
            ],
        },
    )
    assert create_response.status_code == 200
    strategy_run_id = create_response.json()["strategy_run_id"]

    update_protection_response = client.put(
        f"/api/algo-workers/worker/options/runs/{strategy_run_id}/protection",
        headers=headers,
        json={
            "rules": [
                {
                    "metric": "combined_premium",
                    "operator": "gte",
                    "threshold": 120.0,
                }
            ],
            "precedence": [],
        },
    )
    assert update_protection_response.status_code == 200
    assert update_protection_response.json()["strategy_run_id"] == strategy_run_id

    protection_state_response = client.get(
        f"/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/state",
        headers=headers,
    )
    assert protection_state_response.status_code == 200
    assert protection_state_response.json()["strategy_run_id"] == strategy_run_id


def test_canonical_options_market_route_does_not_require_worker_dependency_override() -> None:
    # Deterministic route-level boundary check: full app-user cookie auth is intentionally
    # out of scope for this isolated test app, so we assert worker-token auth never runs.
    app = _app(_FakeWorkerRepo(raw_token=None))
    app.dependency_overrides[require_worker_token] = lambda: (_ for _ in ()).throw(AssertionError("worker auth should not run"))
    client = TestClient(app)
    response = client.get("/api/options/underlyings/NIFTY/session")
    assert response.status_code == 200
    assert response.json()["underlying"] == "NIFTY"


def test_canonical_options_run_route_does_not_require_worker_dependency_override() -> None:
    app = _app(_FakeWorkerRepo(raw_token=None))
    app.dependency_overrides[require_worker_token] = lambda: (_ for _ in ()).throw(AssertionError("worker auth should not run"))
    client = TestClient(app)
    response = client.post(
        "/api/options/runs",
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["product"] == "MIS"
