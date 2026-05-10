from __future__ import annotations

import os
import sys
import types

from fastapi import FastAPI
from starlette.routing import Route, WebSocketRoute

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

from backend.api.routers.worker_auth import router as worker_auth_router  # noqa: E402
from backend.api.routers.worker_execution import router as worker_execution_router  # noqa: E402
from backend.api.routers.worker_market import router as worker_market_router  # noqa: E402
from backend.api.routers.worker_protection import router as worker_protection_router  # noqa: E402


def test_generic_algo_worker_routes_are_mounted() -> None:
    app = FastAPI()
    app.include_router(worker_auth_router, prefix="/api")
    app.include_router(worker_market_router, prefix="/api")
    app.include_router(worker_execution_router, prefix="/api")
    app.include_router(worker_protection_router, prefix="/api")

    expected_http = {
        ("POST", "/api/algo-workers/tokens"),
        ("GET", "/api/algo-workers/tokens"),
        ("POST", "/api/algo-workers/tokens/{token_id}/revoke"),
        ("GET", "/api/algo-workers/worker/health"),
        ("POST", "/api/algo-workers/worker/heartbeat"),
        ("POST", "/api/algo-workers/worker/gtt/triggers"),
        ("GET", "/api/algo-workers/worker/gtt/triggers"),
        ("GET", "/api/algo-workers/worker/gtt/triggers/{trigger_id}"),
        ("PUT", "/api/algo-workers/worker/gtt/triggers/{trigger_id}"),
        ("DELETE", "/api/algo-workers/worker/gtt/triggers/{trigger_id}"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/claim-session"),
        ("DELETE", "/api/algo-workers/worker/runs/{strategy_run_id}/claim-session"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/heartbeat"),
        ("POST", "/api/algo-workers/worker/runs"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/safety-check"),
        ("GET", "/api/algo-workers/worker/market/instruments/resolve"),
        ("GET", "/api/algo-workers/worker/market/instruments/search"),
        ("POST", "/api/algo-workers/worker/market/instruments/resolve"),
        ("POST", "/api/algo-workers/worker/market/quotes"),
        ("GET", "/api/algo-workers/worker/market/ticks/stream"),
        ("GET", "/api/algo-workers/worker/market/candles"),
        ("GET", "/api/algo-workers/worker/market/history"),
        ("GET", "/api/algo-workers/worker/market/candles/stream"),
        ("POST", "/api/algo-workers/worker/market/snapshot"),
        ("GET", "/api/algo-workers/worker/funds"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/funds"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/pnl"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/pnl/stream"),
        ("PATCH", "/api/algo-workers/worker/runs/{strategy_run_id}/risk"),
        ("PATCH", "/api/algo-workers/worker/runs/{strategy_run_id}/protection"),
        ("GET", "/api/algo-workers/worker/orders"),
        ("GET", "/api/algo-workers/worker/trades"),
        ("GET", "/api/algo-workers/worker/orders/{order_id}"),
        ("GET", "/api/algo-workers/worker/orders/{order_id}/history"),
        ("POST", "/api/algo-workers/worker/orders/{order_id}/cancel"),
        ("POST", "/api/algo-workers/worker/orders/{order_id}/modify"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/preview/order"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/preview/basket"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/brackets"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/brackets"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}/cancel"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/intents"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/baskets"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/baskets/{basket_execution_id}"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/execution-events"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/execution-events/stream"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/timeline"),
        ("GET", "/api/algo-workers/worker/runs/{strategy_run_id}/timeline/stream"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/decision-events"),
        ("POST", "/api/algo-workers/worker/runs/{strategy_run_id}/exit"),
    }
    expected_ws = {
        "/api/algo-workers/worker/ws/market/ticks",
        "/api/algo-workers/worker/ws/market/candles",
        "/api/algo-workers/worker/ws/runs/{strategy_run_id}/pnl",
    }

    mounted_http: set[tuple[str, str]] = set()
    mounted_ws: set[str] = set()

    for route in app.router.routes:
        if isinstance(route, Route):
            for method in route.methods or set():
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    mounted_http.add((method, route.path))
        elif isinstance(route, WebSocketRoute):
            mounted_ws.add(route.path)

    missing_http = sorted(expected_http - mounted_http)
    missing_ws = sorted(expected_ws - mounted_ws)

    assert not missing_http, f"Missing generic worker HTTP routes: {missing_http}"
    assert not missing_ws, f"Missing generic worker WebSocket routes: {missing_ws}"
