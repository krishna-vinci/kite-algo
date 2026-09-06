from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.support.test_support import install_dependency_stubs

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_ALLOW_INSECURE_DEV_AUTH", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")
install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from backend.main import app  # noqa: E402
from kite_algo_worker import AsyncKiteAlgoWorkerClient, KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.endpoint_manifest import (  # noqa: E402
    WORKER_HTTP_ENDPOINTS,
    WORKER_WEBSOCKET_PATHS,
)
from kite_algo_worker.options import AsyncOptionWorkerClient, OptionWorkerClient  # noqa: E402
from scripts.sdk_worker_certification import collect_endpoint_coverage  # noqa: E402


def _mounted_http_operations() -> set[tuple[str, str]]:
    verbs = {"get", "post", "put", "patch", "delete"}
    return {
        (method.upper(), path.removeprefix("/api/algo-workers"))
        for path, path_item in app.openapi()["paths"].items()
        if path.startswith("/api/algo-workers/worker/")
        for method in path_item
        if method in verbs
    }


def test_worker_http_manifest_matches_mounted_openapi() -> None:
    mounted = _mounted_http_operations()
    declared = {(item.method, item.path) for item in WORKER_HTTP_ENDPOINTS}

    assert len(mounted) == len(WORKER_HTTP_ENDPOINTS)
    assert declared == mounted


def test_worker_websocket_manifest_matches_mounted_routes() -> None:
    mounted = {
        route.path.removeprefix("/api/algo-workers")
        for route in app.routes
        if route.__class__.__name__ == "APIWebSocketRoute"
        and route.path.startswith("/api/algo-workers/worker/")
    }

    assert mounted == set(WORKER_WEBSOCKET_PATHS)


def _has_public_method(client_class: type, options_class: type, dotted_name: str) -> bool:
    if dotted_name.startswith("options."):
        return callable(getattr(options_class, dotted_name.split(".", 1)[1], None))
    return callable(getattr(client_class, dotted_name, None))


def test_every_manifest_operation_has_sync_and_async_public_methods() -> None:
    missing_sync = [
        item
        for item in WORKER_HTTP_ENDPOINTS
        if not _has_public_method(KiteAlgoWorkerClient, OptionWorkerClient, item.public_method)
    ]
    missing_async = [
        item
        for item in WORKER_HTTP_ENDPOINTS
        if not _has_public_method(AsyncKiteAlgoWorkerClient, AsyncOptionWorkerClient, item.resolved_async_method)
    ]

    assert not missing_sync
    assert not missing_async


def test_manifest_classifies_non_json_and_mutating_operations() -> None:
    by_key = {(item.method, item.path): item for item in WORKER_HTTP_ENDPOINTS}
    assert by_key[("GET", "/worker/fundamentals/export.csv")].response_kind == "text"
    assert by_key[("GET", "/worker/runs/{strategy_run_id}/execution-events/stream")].response_kind == "sse"
    assert by_key[("POST", "/worker/runs/{strategy_run_id}/preview/order")].mutates is False
    assert by_key[("POST", "/worker/runs/{strategy_run_id}/intents")].mutates is True


def test_certification_reports_measured_sync_and_async_coverage() -> None:
    coverage = collect_endpoint_coverage()

    expected_http = len(WORKER_HTTP_ENDPOINTS)
    assert coverage == {
        "worker_http_operations": expected_http,
        "sync_http_operations": expected_http,
        "async_http_operations": expected_http,
        "worker_websocket_routes": len(WORKER_WEBSOCKET_PATHS),
    }
