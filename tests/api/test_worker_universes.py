# pyright: reportArgumentType=false
"""API tests for the worker universe endpoints (alerts platform).

Bootstrapping mirrors tests/api/test_worker_workflows.py: dependency stubs are
installed first, a fresh in-memory SQLite engine is created with the shared
alerts-platform ``Base.metadata.create_all`` (from
``backend.workflows.repository`` so the universe tables register too), and the
FastAPI app carries only the new router mounted at ``/api/worker`` with the
auth repository injected via ``app.state`` (the ``require_worker_token``
mechanism) and the alerts sessionmaker injected via
``app.dependency_overrides``. Catalog resolution is faked through
``app.state.universe_service`` (a fully wired UniverseService), which is the
override hook the router supports for the API layer.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# A Postgres-style DATABASE_URL keeps backend.app.database's module-level
# engine constructible (psycopg2 is stubbed; nothing ever connects) — same
# pattern as tests/api/test_worker_workflows.py.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers import worker_universes as worker_universes_router  # noqa: E402
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.broker_api.instruments.catalog import (  # noqa: E402
    CatalogUnavailableError,
    InstrumentNotFoundError,
)
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from backend.workflows.universes import (  # noqa: E402
    Universe,
    UniverseRevision,
    UniverseService,
)

RAW_TOKEN = "worker-secret-token"
HEADERS = {"Authorization": f"Bearer {RAW_TOKEN}"}
UV = "/api/worker/universes"


class _StubWorkerTokenRepository:
    """Minimal stand-in for SqlAlchemyAlgoWorkerRepository (auth only)."""

    def __init__(self, token: WorkerToken, *, raw_token: str = RAW_TOKEN):
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


def _token(actions=None, *, account_scope="kite:paper-a") -> WorkerToken:
    return WorkerToken(
        token_id="worker-1",
        name="test-worker",
        account_scope=account_scope,
        allowed_modes=["paper", "dry_run"],
        allowed_actions=sorted(DEFAULT_WORKER_ACTIONS if actions is None else actions),
        allowed_templates=[],
    )


class _FakeCatalog:
    """Stand-in for InstrumentCatalog.resolve_public_key (identity only)."""

    def __init__(self, descriptors=(), generation="generation-1"):
        self._descriptors = {d.public_key: d for d in descriptors}
        self.generation = generation

    def resolve_public_key(self, key):
        descriptor = self._descriptors.get(str(key).upper())
        if descriptor is None:
            raise InstrumentNotFoundError(f"instrument not found: {key}")
        return descriptor

    def health(self):
        return {"status": "published", "generation": self.generation}


def _descriptor(public_key, lifecycle_status="active", catalog_generation="generation-1"):
    return SimpleNamespace(
        public_key=public_key,
        lifecycle_status=lifecycle_status,
        catalog_generation=catalog_generation,
    )


def _unavailable_catalog():
    class _Broken:
        def resolve_public_key(self, key):
            raise CatalogUnavailableError("catalog database down")

    return _Broken()


def _client(actions=None, *, catalog=None):
    """TestClient over the universe router with an in-memory alerts database."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(worker_universes_router.router, prefix="/api")
    app.dependency_overrides[worker_universes_router._universes_db] = lambda: factory
    app.state.algo_worker_repository = _StubWorkerTokenRepository(_token(actions))
    app.state.universe_service = UniverseService(factory, catalog=catalog)
    return TestClient(app), factory


def _create_universe(client, *, name="nifty-core", kind="explicit", source_config=None):
    if source_config is None:
        source_config = {"members": ["NSE:RELIANCE"]} if kind == "explicit" else {}
    payload = {"name": name, "kind": kind, "source_config": source_config}
    response = client.post(UV, json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# authorization (same mechanism as test_worker_workflows.py)
# ---------------------------------------------------------------------------


def test_requires_bearer_token():
    client, _ = _client()
    assert client.get(UV).status_code == 401
    assert client.post(UV, json={"name": "x", "kind": "explicit", "source_config": {"members": ["NSE:X"]}}).status_code == 401


def test_create_without_workflows_write_action_is_403():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"workflows:write"})
    response = client.post(
        UV,
        json={"name": "nifty-core", "kind": "explicit", "source_config": {"members": ["NSE:X"]}},
        headers=HEADERS,
    )
    assert response.status_code == 403, response.text


def test_list_without_workflows_read_action_is_403():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"workflows:read"})
    response = client.get(UV, headers=HEADERS)
    assert response.status_code == 403, response.text


def test_invalid_token_is_401():
    client, _ = _client()
    response = client.get(UV, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# CRUD + resolve
# ---------------------------------------------------------------------------


def test_create_then_get_and_list_shows_universe_without_revisions():
    client, _ = _client(catalog=_FakeCatalog())
    created = _create_universe(
        client,
        source_config={"members": ["NSE:RELIANCE", "NSE:TCS"]},
    )
    assert created["universe_id"]
    assert created["name"] == "nifty-core"
    assert created["kind"] == "explicit"
    assert created["source_config"]["members"] == ["NSE:RELIANCE", "NSE:TCS"]

    got = client.get(f"{UV}/nifty-core", headers=HEADERS)
    assert got.status_code == 200
    body = got.json()
    assert body["latest_revision"] is None
    assert body["latest_members"] is None

    listing = client.get(UV, headers=HEADERS).json()["universes"]
    assert [universe["name"] for universe in listing] == ["nifty-core"]


def test_duplicate_create_is_409():
    client, _ = _client(catalog=_FakeCatalog())
    _create_universe(client)
    response = client.post(
        UV,
        json={"name": "nifty-core", "kind": "explicit", "source_config": {"members": ["NSE:Y"]}},
        headers=HEADERS,
    )
    assert response.status_code == 409, response.text


def test_invalid_universe_payload_is_422():
    client, _ = _client(catalog=_FakeCatalog())
    response = client.post(
        UV,
        json={"name": "bad", "kind": "explicit", "source_config": {"members": ["RELIANCE"]}},
        headers=HEADERS,
    )
    assert response.status_code == 422
    response = client.post(
        UV,
        json={"name": "bad", "kind": "telepathic", "source_config": {}},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_resolve_persists_revision_and_returns_payload():
    client, factory = _client(
        catalog=_FakeCatalog([_descriptor("NSE:RELIANCE"), _descriptor("BSE:RELIANCE")])
    )
    _create_universe(client, source_config={"members": ["NSE:RELIANCE", "BSE:RELIANCE"]})

    resolved = client.post(f"{UV}/nifty-core/resolve", headers=HEADERS)
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["ok"] is True
    assert body["revision"] == 1
    assert body["members"] == ["BSE:RELIANCE", "NSE:RELIANCE"]
    assert body["source_generation"] == "generation-1"
    assert body["coverage"]["resolved"] == 2
    assert body["coverage"]["source"] == "explicit"

    with factory() as session:
        rows = session.execute(select(UniverseRevision)).scalars().all()
        assert len(rows) == 1
        assert rows[0].revision == 1
        assert rows[0].members == ["BSE:RELIANCE", "NSE:RELIANCE"]

    detail = client.get(f"{UV}/nifty-core", headers=HEADERS).json()
    assert detail["latest_revision"]["revision"] == 1
    assert detail["latest_revision"]["member_count"] == 2
    assert detail["latest_members"] == ["BSE:RELIANCE", "NSE:RELIANCE"]

    again = client.post(f"{UV}/nifty-core/resolve", headers=HEADERS).json()
    assert again["revision"] == 2  # revisions are strictly monotonic

    history = client.get(
        f"{UV}/nifty-core/revisions", params={"limit": 1}, headers=HEADERS
    )
    assert history.status_code == 200, history.text
    history_body = history.json()
    assert history_body["limit"] == 1
    assert [revision["revision"] for revision in history_body["revisions"]] == [2]


def test_resolve_records_rejected_members_in_coverage():
    client, _ = _client(
        catalog=_FakeCatalog([_descriptor("NSE:GOOD"), _descriptor("NSE:OLD", lifecycle_status="expired")])
    )
    _create_universe(client, source_config={"members": ["NSE:GOOD", "NSE:OLD", "NSE:GONE"]})
    body = client.post(f"{UV}/nifty-core/resolve", headers=HEADERS).json()
    assert body["members"] == ["NSE:GOOD"]
    # rejected is recorded in deterministic (sorted candidate) order
    assert body["rejected"] == [
        {"key": "NSE:GONE", "reason": "not_found"},
        {"key": "NSE:OLD", "reason": "expired"},
    ]
    assert body["coverage"]["rejected"] == 2


def test_unknown_universe_is_404():
    client, _ = _client(catalog=_FakeCatalog())
    assert client.get(f"{UV}/missing", headers=HEADERS).status_code == 404
    assert client.post(f"{UV}/missing/resolve", headers=HEADERS).status_code == 404
    assert client.get(f"{UV}/missing/revisions", headers=HEADERS).status_code == 404


def test_other_owner_cannot_see_universe():
    client, _ = _client(catalog=_FakeCatalog())
    _create_universe(client)
    client.app.state.algo_worker_repository = _StubWorkerTokenRepository(
        _token(account_scope="kite:paper-b")
    )
    assert client.get(f"{UV}/nifty-core", headers=HEADERS).status_code == 404
    assert client.post(f"{UV}/nifty-core/resolve", headers=HEADERS).status_code == 404
    assert client.get(UV, headers=HEADERS).json()["universes"] == []


def test_catalog_unavailable_is_503_and_persists_nothing():
    client, factory = _client(catalog=_unavailable_catalog())
    _create_universe(client)
    response = client.post(f"{UV}/nifty-core/resolve", headers=HEADERS)
    assert response.status_code == 503, response.text
    with factory() as session:
        assert session.execute(select(func.count()).select_from(UniverseRevision)).scalar() == 0


# ---------------------------------------------------------------------------
# preview: zero persistent side effects
# ---------------------------------------------------------------------------


def test_preview_resolves_membership_and_persists_nothing():
    client, factory = _client(
        catalog=_FakeCatalog([_descriptor("NSE:RELIANCE"), _descriptor("NSE:OLD", lifecycle_status="retired")])
    )
    _create_universe(client, source_config={"members": ["NSE:RELIANCE"]})

    response = client.post(
        f"{UV}/preview",
        json={"kind": "explicit", "source_config": {"members": ["NSE:RELIANCE", "NSE:OLD", "NSE:GONE"]}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["kind"] == "explicit"
    assert body["members"] == ["NSE:RELIANCE"]
    assert [item["key"] for item in body["rejected"]] == ["NSE:GONE", "NSE:OLD"]
    assert "preview only" in body["note"]

    # the universe created above is untouched; preview created nothing
    with factory() as session:
        assert session.execute(select(func.count()).select_from(Universe)).scalar() == 1
        assert session.execute(select(func.count()).select_from(UniverseRevision)).scalar() == 0


def test_preview_without_workflows_read_action_is_403():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"workflows:read"})
    response = client.post(
        f"{UV}/preview",
        json={"kind": "explicit", "source_config": {"members": ["NSE:X"]}},
        headers=HEADERS,
    )
    assert response.status_code == 403, response.text


def test_preview_unavailable_source_is_503():
    client, factory = _client(catalog=_FakeCatalog())

    class _EmptyLoader:
        def __call__(self, source_list):
            from backend.workflows.universes import UniverseSourceUnavailable

            raise UniverseSourceUnavailable("no constituents")

    service = client.app.state.universe_service
    client.app.state.universe_service = UniverseService(
        service.session_factory,
        catalog=service._catalog,
        index_constituents_loader=_EmptyLoader(),
    )

    # unsupported source list is a validation error (422), not a 503
    response = client.post(
        f"{UV}/preview",
        json={"kind": "index", "source_config": {"source_list": "Sensex30"}},
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text

    # a known list with an unavailable loader is a 503
    response = client.post(
        f"{UV}/preview",
        json={"kind": "index", "source_config": {"source_list": "Nifty50"}},
        headers=HEADERS,
    )
    assert response.status_code == 503, response.text
    with factory() as session:
        assert session.execute(select(func.count()).select_from(UniverseRevision)).scalar() == 0


# ---------------------------------------------------------------------------
# index universes via injected loader
# ---------------------------------------------------------------------------


def test_index_universe_via_app_state_service_loader():
    client, factory = _client(catalog=_FakeCatalog([_descriptor("NSE:RELIANCE"), _descriptor("NSE:TCS")]))

    def loader(source_list):
        assert source_list == "Nifty50"
        return ["RELIANCE", "TCS"]

    service = client.app.state.universe_service
    client.app.state.universe_service = UniverseService(
        service.session_factory,
        catalog=service._catalog,
        index_constituents_loader=loader,
    )

    _create_universe(client, kind="index", source_config={"source_list": "Nifty50"})
    body = client.post(f"{UV}/nifty-core/resolve", headers=HEADERS).json()
    assert body["members"] == ["NSE:RELIANCE", "NSE:TCS"]  # bare symbols NSE-qualified
    assert body["coverage"]["source"] == "index"

    with factory() as session:
        assert session.execute(select(func.count()).select_from(UniverseRevision)).scalar() == 1


def test_portfolio_universe_rejected_without_provider():
    client, _ = _client(catalog=_FakeCatalog())
    response = client.post(
        UV,
        json={"name": "holdings", "kind": "portfolio", "source_config": {}},
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    assert "provider" in response.json()["detail"].lower()


def test_production_mount_keeps_paths_under_worker_auth_exemption():
    """Production mounts this router at '/api' (ALL_ROUTERS). The app auth
    middleware only exempts '/api/worker/...' paths for worker tokens, so a
    production-style mount must still produce '/api/worker/universes' paths —
    otherwise the middleware 401s every worker-token call before the router
    runs (regression: router prefix was '/universes', unreachable in prod)."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(worker_universes_router.router, prefix="/api")
    universe_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/universes")
        or "/universes" in getattr(route, "path", "")
    }
    assert universe_paths, "universe routes not registered"
    assert all(
        path.startswith("/api/worker/universes") for path in universe_paths
    ), sorted(universe_paths)
