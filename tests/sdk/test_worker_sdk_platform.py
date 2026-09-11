"""SDK ↔ platform contract (Phase 4 F6/SDK).

Two things must hold, and neither is a blanket "wrap every route" rule:

1. the CURATED platform operation list names routes that are actually mounted
   (no phantom operations a caller could invoke into a 404), and
2. every listed operation has a method on BOTH transports — the sync
   ``KiteAlgoWorkerClient`` and the async ``AsyncKiteAlgoWorkerClient`` — so the
   two clients cannot drift.

The list itself is the scope decision: it covers what an operator or a Phase 4
example needs. Routes outside it are not SDK-wrapped and not asserted here.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "python"))

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.client import KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.endpoint_manifest import PLATFORM_OPERATIONS  # noqa: E402


def _platform_routes() -> set[tuple[str, str]]:
    """(METHOD, path) for the mounted alerts-platform surface.

    Paths are normalized relative to the platform mount so they compare with
    the manifest entries.
    """
    from backend.main import app

    verbs = {"get", "post", "put", "patch", "delete"}
    return {
        (method.upper(), path.removeprefix("/api"))
        for path, path_item in app.openapi()["paths"].items()
        if path.startswith("/api/worker/")
        for method in path_item
        if method in verbs
    }


def test_every_curated_operation_is_actually_mounted():
    """No phantom operations: a listed route must exist on the server."""
    mounted = _platform_routes()
    missing = [
        (entry.method, entry.path)
        for entry in PLATFORM_OPERATIONS
        if (entry.method, entry.path) not in mounted
    ]
    assert missing == [], f"manifest names unmounted platform routes: {missing}"


def test_every_curated_operation_has_sync_and_async_methods():
    missing_sync = []
    missing_async = []
    for entry in PLATFORM_OPERATIONS:
        if not callable(getattr(KiteAlgoWorkerClient, entry.public_method, None)):
            missing_sync.append(entry.public_method)
        if not callable(getattr(AsyncKiteAlgoWorkerClient, entry.resolved_async_method, None)):
            missing_async.append(entry.resolved_async_method)
    assert missing_sync == [], f"platform operations without a sync method: {missing_sync}"
    assert missing_async == [], f"platform operations without an async method: {missing_async}"


def test_mutating_operations_are_classified():
    """``mutates`` drives what a caller may safely retry — it must be right."""
    by_method = {
        (entry.method, entry.path): entry.mutates for entry in PLATFORM_OPERATIONS
    }
    read_only = [
        ("GET", "/worker/workflows/capabilities"),
        ("POST", "/worker/workflows/validate"),
        ("POST", "/worker/workflows/preview"),
        ("POST", "/worker/universes/preview"),
        ("POST", "/worker/screeners/preview"),
        ("GET", "/worker/workflows"),
        ("GET", "/worker/signals/health"),
    ]
    mutating = [
        ("POST", "/worker/workflows"),
        ("PATCH", "/worker/workflows/{workflow_id}"),
        ("POST", "/worker/workflows/{workflow_id}/activate"),
        ("POST", "/worker/signals/values"),
        ("POST", "/worker/signals/producers"),
        ("POST", "/worker/screeners/{workflow_id}/runs"),
    ]
    for key in read_only:
        assert by_method[key] is False, f"{key} must not be classified as mutating"
    for key in mutating:
        assert by_method[key] is True, f"{key} must be classified as mutating"


def test_platform_and_worker_prefixes_do_not_collide():
    """The two mounts are distinct, so neither family can 404 by accident."""
    from kite_algo_worker.client import AlgoWorkerConfig

    config = AlgoWorkerConfig(base_url="http://example.test", token="t")
    client = KiteAlgoWorkerClient(config)
    assert client._url("/worker/health") == (
        "http://example.test/api/algo-workers/worker/health"
    )
    assert client._platform_url("/worker/workflows") == (
        "http://example.test/api/worker/workflows"
    )


def test_document_payload_rejects_ambiguous_input():
    from kite_algo_worker._shared import document_payload

    with pytest.raises(ValueError):
        document_payload()
    with pytest.raises(ValueError):
        document_payload(yaml_text="a", document={})
    assert document_payload(yaml_text="a") == {"yaml_text": "a"}
    assert document_payload(document={"version": 1}) == {"document": {"version": 1}}


def test_pagination_bounds_are_enforced_client_side():
    from kite_algo_worker._shared import page_params

    with pytest.raises(ValueError):
        page_params(0, 0)
    with pytest.raises(ValueError):
        page_params(501, 0)
    with pytest.raises(ValueError):
        page_params(10, -1)
    assert page_params(50, 10) == {"limit": 50, "offset": 10}
