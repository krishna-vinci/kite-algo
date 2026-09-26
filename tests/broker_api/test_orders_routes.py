from __future__ import annotations

import os

from fastapi import FastAPI
import pytest
from starlette.routing import Route

from tests.support.test_support import install_dependency_stubs, iter_mounted_routes

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

install_dependency_stubs()

from backend.api.routers import orders_router  # noqa: E402
from backend.app.auth import auth_exempt_path  # noqa: E402

# Read-only routes restored after the c598871 split dropped their decorators.
READ_ROUTES = {
    ("GET", "/api/orders"),
    ("GET", "/api/orders/{order_id}"),
    ("GET", "/api/orders/{order_id}/history"),
    ("GET", "/api/orders/{order_id}/trades"),
    ("GET", "/api/trades"),
    ("GET", "/api/positions"),
    ("POST", "/api/margins/orders"),
    ("POST", "/api/margins/basket"),
    ("POST", "/api/charges/orders"),
    ("GET", "/api/trigger-range"),
    ("POST", "/api/positions/initialize"),
    ("GET", "/api/positions/realtime"),
    ("GET", "/api/positions/stream"),
    ("POST", "/api/positions/reconcile"),
    ("GET", "/api/order-runtime/status"),
    ("GET", "/api/gtt/triggers"),
    ("GET", "/api/gtt/triggers/{trigger_id}"),
    ("GET", "/api/webhooks/orders/events"),
    ("GET", "/api/ws/orders/updates/status"),
    ("GET", "/api/ws/orders/events"),
    ("GET", "/api/order-events/stream"),
}

# Broker write / state-changing routes deliberately left unregistered.
WRITE_ROUTES = {
    ("POST", "/api/orders"),
    ("POST", "/api/orders/basket"),
    ("PUT", "/api/orders/{variety}/{order_id}"),
    ("DELETE", "/api/orders/{variety}/{order_id}"),
    ("POST", "/api/positions/convert"),
    ("POST", "/api/order-runtime/process-now"),
    ("POST", "/api/gtt/triggers"),
    ("PUT", "/api/gtt/triggers/{trigger_id}"),
    ("DELETE", "/api/gtt/triggers/{trigger_id}"),
    ("POST", "/api/webhooks/orders/postback"),
    ("POST", "/api/ws/orders/updates/enable"),
    ("POST", "/api/ws/orders/updates/disable"),
}


def _mounted_http(app: FastAPI) -> set[tuple[str, str]]:
    mounted: set[tuple[str, str]] = set()
    for path, route in iter_mounted_routes(app.router):
        if isinstance(route, Route):
            for method in route.methods or set():
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    mounted.add((method, path))
    return mounted


def test_orders_router_registers_read_only_routes_only() -> None:
    app = FastAPI()
    app.include_router(orders_router, prefix="/api")
    mounted = _mounted_http(app)

    missing = sorted(READ_ROUTES - mounted)
    assert not missing, f"Missing restored read-only routes: {missing}"

    registered_write_routes = sorted(WRITE_ROUTES & mounted)
    assert not registered_write_routes, (
        f"Broker write routes must stay unregistered: {registered_write_routes}"
    )


def test_restored_routes_are_cookie_protected() -> None:
    for _method, path in READ_ROUTES:
        assert not auth_exempt_path(path), f"{path} must not be cookie-exempt"


@pytest.mark.asyncio
async def test_kite_place_payload_preserves_autoslice(monkeypatch) -> None:
    from backend.broker_api.orders.models import PlaceOrderRequest
    from backend.broker_api.orders.service import OrdersService

    calls = []

    class _Kite:
        access_token = "test-token"

        def _post(self, path, *, url_args, params):
            calls.append((path, dict(url_args), dict(params)))
            return {"order_id": "PARENT-1"}

        def place_order(self, **params):
            calls.append(("place_order", {}, dict(params)))
            return "EQUITY-1"

    async def _immediate(_action, _corr_id, func, **_kwargs):
        return func()

    monkeypatch.setattr(
        "backend.broker_api.orders.service.run_kite_write_action", _immediate
    )
    fno = PlaceOrderRequest.model_validate(
        {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26OCTFUT",
            "transaction_type": "BUY",
            "variety": "regular",
            "product": "NRML",
            "order_type": "MARKET",
            "quantity": 100,
            "autoslice": True,
        }
    )
    result = await OrdersService().place_order(_Kite(), fno, "corr")
    assert result.order_id == "PARENT-1"
    assert calls[0][0] == "order.place"
    assert calls[0][2]["autoslice"] == "true"

    calls.clear()
    equity = PlaceOrderRequest.model_validate(
        {
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "transaction_type": "BUY",
            "variety": "regular",
            "product": "CNC",
            "order_type": "MARKET",
            "quantity": 1,
        }
    )
    await OrdersService().place_order(_Kite(), equity, "corr")
    assert calls[0][0] == "place_order"
    assert "autoslice" not in calls[0][2]
