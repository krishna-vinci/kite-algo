"""Fundamentals route contract tests: scope validation and envelope shape."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers.fundamentals import router as fundamentals_router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(fundamentals_router, prefix="/api")
    return TestClient(app)


class _FakeCursor:
    def __init__(self, results, rows):
        self._results = results
        self._rows = rows
        self._current = ([], None)

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        for fragment, rows in self._results:
            if fragment in normalized:
                self._current = (rows, None)
                return
        self._current = ([], [desc[0] for desc in []])

    def fetchall(self):
        return self._current[0]

    @property
    def description(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sync_rejects_ambiguous_and_missing_scope(client):
    resp = client.post("/api/fundamentals/sync", json={"symbols": ["RELIANCE"], "index": "Nifty50"})
    assert resp.status_code == 422
    resp = client.post("/api/fundamentals/sync", json={})
    assert resp.status_code == 422
    resp = client.post("/api/fundamentals/sync", json={"symbols": ["RELIANCE"], "mode": "bogus"})
    assert resp.status_code == 422


def test_sync_rejects_unknown_index_and_oversized_symbol_list(client):
    resp = client.post("/api/fundamentals/sync", json={"index": "NiftyMidcap"})
    assert resp.status_code == 400
    resp = client.post("/api/fundamentals/sync", json={"symbols": [f"S{i:03d}" for i in range(51)]})
    assert resp.status_code == 400
    assert "50" in resp.json()["detail"]


def test_features_rejects_missing_scope(client):
    assert client.get("/api/fundamentals/features").status_code == 400
    assert client.get("/api/fundamentals/features", params={"symbols": ["RELIANCE"], "index": "Nifty50"}).status_code == 400
    assert client.get("/api/fundamentals/features", params={"index": "Unknown"}).status_code == 400


def test_features_returns_envelope_with_missing_symbols(client, monkeypatch):
    from backend.api.routers import fundamentals as fundamentals_module

    monkeypatch.setattr(fundamentals_module, "resolve_scope_symbols", lambda scope: ["RELIANCE", "TCS"])
    columns = [("symbol",), ("statement_scope",), ("ttm_revenue",), ("as_of_date",), ("scraped_at",)]

    class _Cursor:
        def __init__(self):
            self.description = columns

        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return [("RELIANCE", "consolidated", 420.0, None, None)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    monkeypatch.setattr(fundamentals_module, "get_db_connection", lambda: _Conn())

    resp = client.get("/api/fundamentals/features", params={"symbols": ["reliance", "TCS", "MISSING"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["schema_version"] == 1
    assert payload["source"] == "screener"
    assert payload["features"][0]["symbol"] == "RELIANCE"
    assert payload["features"][0]["ttm_revenue"] == 420.0
    assert payload["missing_symbols"] == ["TCS", "MISSING"]


def test_statements_rejects_unknown_dataset_and_404s_when_empty(client, monkeypatch):
    from backend.api.routers import fundamentals as fundamentals_module

    resp = client.get("/api/fundamentals/statements", params={"symbol": "RELIANCE", "dataset": "not_a_dataset"})
    assert resp.status_code == 400

    class _EmptyCursor:
        description = None

        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _EmptyCursor()

        def close(self):
            return None

    monkeypatch.setattr(fundamentals_module, "get_db_connection", lambda: _Conn())
    resp = client.get("/api/fundamentals/statements", params={"symbol": "RELIANCE", "dataset": "quarterly"})
    assert resp.status_code == 404


def test_sync_conflict_maps_to_409(client, monkeypatch):
    from backend.api.routers import fundamentals as fundamentals_module

    async def already_running(config):
        raise RuntimeError("fundamentals sync already in progress")

    monkeypatch.setattr(fundamentals_module, "run_fundamentals_sync", already_running)
    resp = client.post("/api/fundamentals/sync", json={"symbols": ["RELIANCE"]})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


def test_all_routers_registration_includes_fundamentals():
    from backend.api.routers import ALL_ROUTERS

    assert any(getattr(router, "prefix", "") == "" and router is fundamentals_router for router, _ in ALL_ROUTERS)
