from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute

from options.api.execution_router import router as options_execution_router
from options.api.market_router import router as options_market_router
from options.api.protection_router import router as options_protection_router
from options.api.strategy_router import router as options_strategy_router
from options.api.worker_options_router import router as worker_options_router

app = FastAPI()
app.include_router(options_market_router)
app.include_router(options_strategy_router)
app.include_router(options_execution_router)
app.include_router(options_protection_router)
app.include_router(worker_options_router)


def _has_route(path: str, method: str) -> bool:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method.upper() in route.methods:
            return True
    return False


def test_options_market_expiries_route_is_registered() -> None:
    assert _has_route("/api/options/underlyings/{underlying}/expiries", "GET")


def test_options_strategy_preview_route_is_registered() -> None:
    assert _has_route("/api/options/strategies/preview", "POST")


def test_options_execution_route_family_scaffolding_is_registered() -> None:
    assert _has_route("/api/options/runs", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/preview-entry", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/enter", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/preview-exit", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/exit", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/orders", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/trades", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/state", "GET")


def test_options_protection_route_family_scaffolding_is_registered() -> None:
    assert _has_route("/api/options/runs/{strategy_run_id}/protection", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/protection/state", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/protection/replay", "POST")


def test_main_registers_canonical_options_routers() -> None:
    main_source = Path("/home/krishna/kite-algo/main.py").read_text(encoding="utf-8")
    assert "app.include_router(options_market_router)" in main_source
    assert "app.include_router(options_strategy_router)" in main_source
    assert "app.include_router(options_execution_router)" in main_source
    assert "app.include_router(options_protection_router)" in main_source
    assert "app.include_router(worker_options_router)" in main_source


def test_worker_options_market_routes_are_registered() -> None:
    assert _has_route("/api/algo-workers/worker/options/underlyings/{underlying}/session", "GET")
    assert _has_route("/api/algo-workers/worker/options/underlyings/{underlying}/expiries", "GET")
    assert _has_route("/api/algo-workers/worker/options/strategies/preview", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-entry", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/enter", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-exit", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/exit", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/state", "GET")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection", "PUT")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/state", "GET")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/replay", "POST")
