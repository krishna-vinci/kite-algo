from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from shared.serialization import _hash_token
from app.auth import ACCESS_COOKIE_NAME, AppUser, auth_exempt_path, create_access_token, get_optional_app_user
from options.api.execution_router import router as execution_router
from options.api.market_router import get_options_session_manager, router as market_router
from options.api.protection_router import router as protection_router
from options.api.worker_options_router import router as worker_options_router
from options.execution.store import OptionRunStore, get_option_run_store


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


def _build_app(*, worker_token: str | None = None) -> FastAPI:
    app = FastAPI()
    app.state.algo_worker_repository = _FakeWorkerRepo(raw_token=worker_token)

    @app.middleware("http")
    async def app_auth_guard(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith("/api") or auth_exempt_path(path):
            return await call_next(request)

        user = get_optional_app_user(request)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "App authentication required"})

        request.state.app_user = user
        return await call_next(request)

    run_store = OptionRunStore()
    app.include_router(market_router)
    app.include_router(execution_router)
    app.include_router(protection_router)
    app.include_router(worker_options_router)
    app.dependency_overrides[get_options_session_manager] = lambda: _FakeManager()
    app.dependency_overrides[get_option_run_store] = lambda: run_store
    return app


def _app_cookie_for(username: str = "admin") -> dict[str, str]:
    token = create_access_token(AppUser(username=username, role="admin"))
    return {ACCESS_COOKIE_NAME: token}


def test_unauthenticated_canonical_option_session_requires_app_cookie() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.get("/api/options/underlyings/NIFTY/session")
    assert response.status_code == 401
    assert response.json()["detail"] == "App authentication required"


def test_authenticated_canonical_market_route_returns_200() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.get(
        "/api/options/underlyings/NIFTY/session",
        cookies=_app_cookie_for(),
    )
    assert response.status_code == 200
    assert response.json()["underlying"] == "NIFTY"


def test_unauthenticated_canonical_runs_list_requires_app_cookie() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.get("/api/options/runs")
    assert response.status_code == 401
    assert response.json()["detail"] == "App authentication required"


def test_authenticated_canonical_runs_create_returns_200() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.post(
        "/api/options/runs",
        cookies=_app_cookie_for(),
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


def test_worker_options_route_is_app_auth_exempt_but_needs_worker_bearer() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.get("/api/algo-workers/worker/options/underlyings/NIFTY/session")
    assert response.status_code == 401
    assert response.json()["detail"] == "Worker bearer token required"


def test_valid_worker_bearer_can_access_worker_options_without_app_cookie() -> None:
    client = TestClient(_build_app(worker_token="kwa_valid"))
    response = client.get(
        "/api/algo-workers/worker/options/underlyings/NIFTY/session",
        headers={"Authorization": "Bearer kwa_valid"},
    )
    assert response.status_code == 200
    assert response.json()["underlying"] == "NIFTY"
