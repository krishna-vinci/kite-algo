from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository  # noqa: E402
from backend.api.routers import worker_auth  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402


@pytest.fixture(autouse=True)
def _inline_repository_threads(monkeypatch):
    """Keep SQLite/TestClient tests independent of the sandbox thread runner."""

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("backend.api.repositories.algo_worker_repo.asyncio.to_thread", _inline)


def _repo() -> SqlAlchemyAlgoWorkerRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, connection_record):
        _ = connection_record
        dbapi_connection.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE public.algo_worker_tokens (
                    token_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    account_scope TEXT,
                    allowed_modes TEXT,
                    allowed_actions TEXT,
                    allowed_templates TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    last_used_at TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE public.algo_worker_runs (
                    strategy_run_id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    account_scope TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_fields_json TEXT,
                    risk_schema_json TEXT,
                    allowed_actions_json TEXT,
                    runtime_state_json TEXT,
                    metadata_json TEXT,
                    worker_session_nonce TEXT,
                    worker_session_claimed_at TEXT,
                    last_heartbeat_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    closed_at TEXT
                )
                """
            )
        )
    return SqlAlchemyAlgoWorkerRepository(sessionmaker(bind=engine))


def _insert_token(repo, *, token_id: str, raw_token: str, account_scope: str | None, modes, templates=()):
    with repo.session_factory() as connection:
        connection.execute(
            text(
                """
                INSERT INTO public.algo_worker_tokens (
                    token_id, name, token_hash, account_scope, allowed_modes,
                    allowed_actions, allowed_templates, status
                ) VALUES (
                    :token_id, :name, :token_hash, :account_scope, :allowed_modes,
                    :allowed_actions, :allowed_templates, 'active'
                )
                """
            ),
            {
                "token_id": token_id,
                "name": token_id,
                "token_hash": _hash_token(raw_token),
                "account_scope": account_scope,
                "allowed_modes": json.dumps(list(modes)),
                "allowed_actions": json.dumps(["runs:read"]),
                "allowed_templates": json.dumps(list(templates)),
            },
        )
        connection.commit()


def _insert_run(
    repo,
    *,
    run_id: str,
    token_id: str,
    template_id: str,
    account_scope: str,
    execution_mode: str,
    created_at: str,
    name: str | None = None,
    nonce: str | None = None,
):
    with repo.session_factory() as connection:
        connection.execute(
            text(
                """
                INSERT INTO public.algo_worker_runs (
                    strategy_run_id, token_id, template_id, account_scope,
                    execution_mode, status, metadata_json, worker_session_nonce,
                    created_at, updated_at
                ) VALUES (
                    :run_id, :token_id, :template_id, :account_scope,
                    :execution_mode, 'open', :metadata_json, :nonce,
                    :created_at, :created_at
                )
                """
            ),
            {
                "run_id": run_id,
                "token_id": token_id,
                "template_id": template_id,
                "account_scope": account_scope,
                "execution_mode": execution_mode,
                "metadata_json": json.dumps({"strategy_name": name}) if name else "{}",
                "nonce": nonce,
                "created_at": created_at,
            },
        )
        connection.commit()


class _DirectResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, default=str)

    def json(self):
        return self._payload


class _DirectClient:
    def __init__(self, repo):
        self._repo = repo

    def get(self, path: str, *, params=None, headers=None):
        parsed = urlsplit(path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        query.update({key: str(value) for key, value in (params or {}).items()})
        limit = query.get("limit", 25)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            pass
        request = SimpleNamespace(
            headers=dict(headers or {}),
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=self._repo)),
        )
        try:
            payload = __import__("asyncio").run(
                worker_auth.list_worker_runs(
                    request,
                    limit=limit,
                    cursor=query.get("cursor"),
                )
            )
        except HTTPException as exc:
            return _DirectResponse(exc.status_code, {"detail": exc.detail})
        return _DirectResponse(200, payload)


def _client(repo, raw_token: str) -> _DirectClient:
    _ = raw_token
    return _DirectClient(repo)


@pytest.fixture
def discovery_client():
    repo = _repo()
    _insert_token(
        repo,
        token_id="owner-1",
        raw_token="secret-1",
        account_scope="kite:paper-a",
        modes=("paper", "dry_run"),
        templates=("allowed-template",),
    )
    _insert_token(
        repo,
        token_id="owner-2",
        raw_token="secret-2",
        account_scope="kite:paper-a",
        modes=("paper",),
    )
    _insert_run(
        repo,
        run_id="run-new",
        token_id="owner-1",
        template_id="allowed-template",
        account_scope="kite:paper-a",
        execution_mode="paper",
        created_at="2026-09-06T09:00:00+00:00",
        name="Newest run",
        nonce="do-not-return",
    )
    _insert_run(
        repo,
        run_id="run-old",
        token_id="owner-1",
        template_id="allowed-template",
        account_scope="kite:paper-a",
        execution_mode="dry_run",
        created_at="2026-09-06T08:00:00+00:00",
        name="Older run",
    )
    _insert_run(
        repo,
        run_id="run-wrong-template",
        token_id="owner-1",
        template_id="blocked-template",
        account_scope="kite:paper-a",
        execution_mode="paper",
        created_at="2026-09-06T10:00:00+00:00",
    )
    _insert_run(
        repo,
        run_id="run-wrong-account",
        token_id="owner-1",
        template_id="allowed-template",
        account_scope="kite:paper-b",
        execution_mode="paper",
        created_at="2026-09-06T11:00:00+00:00",
    )
    _insert_run(
        repo,
        run_id="run-wrong-mode",
        token_id="owner-1",
        template_id="allowed-template",
        account_scope="kite:paper-a",
        execution_mode="live",
        created_at="2026-09-06T12:00:00+00:00",
    )
    _insert_run(
        repo,
        run_id="run-other-owner",
        token_id="owner-2",
        template_id="allowed-template",
        account_scope="kite:paper-a",
        execution_mode="paper",
        created_at="2026-09-06T13:00:00+00:00",
    )
    return _client(repo, "secret-1")


def test_list_runs_filters_owner_scope_template_mode_and_redacts_secrets(discovery_client):
    response = discovery_client.get(
        "/api/algo-workers/worker/runs",
        headers={"Authorization": "Bearer secret-1"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["strategy_run_id"] for item in payload["items"]] == ["run-new", "run-old"]
    assert payload["items"][0]["name"] == "Newest run"
    assert payload["next_cursor"] is None
    assert set(payload["items"][0]) == {
        "strategy_run_id",
        "name",
        "template_id",
        "account_scope",
        "execution_mode",
        "status",
        "created_at",
        "updated_at",
        "closed_at",
    }
    assert "token_id" not in payload["items"][0]
    assert "worker_session_nonce" not in payload["items"][0]


def test_list_runs_keyset_cursor_has_page_boundary(discovery_client):
    headers = {"Authorization": "Bearer secret-1"}
    first = discovery_client.get("/api/algo-workers/worker/runs?limit=1", headers=headers)
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert [item["strategy_run_id"] for item in first_payload["items"]] == ["run-new"]
    assert first_payload["next_cursor"]

    second = discovery_client.get(
        "/api/algo-workers/worker/runs",
        params={"limit": 1, "cursor": first_payload["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert [item["strategy_run_id"] for item in second_payload["items"]] == ["run-old"]
    assert second_payload["next_cursor"] is None


def test_list_runs_supports_live_token_paper_scope_semantics():
    repo = _repo()
    _insert_token(
        repo,
        token_id="live-owner",
        raw_token="live-secret",
        account_scope="kite:broker-user",
        modes=("paper", "live"),
    )
    _insert_run(
        repo,
        run_id="live-run",
        token_id="live-owner",
        template_id="template",
        account_scope="kite:broker-user",
        execution_mode="live",
        created_at="2026-09-06T09:00:00+00:00",
    )
    _insert_run(
        repo,
        run_id="permitted-paper-run",
        token_id="live-owner",
        template_id="template",
        account_scope="kite:paper-any",
        execution_mode="paper",
        created_at="2026-09-06T08:00:00+00:00",
    )
    _insert_run(
        repo,
        run_id="other-live-run",
        token_id="live-owner",
        template_id="template",
        account_scope="kite:other-broker",
        execution_mode="live",
        created_at="2026-09-06T07:00:00+00:00",
    )

    response = _client(repo, "live-secret").get(
        "/api/algo-workers/worker/runs",
        headers={"Authorization": "Bearer live-secret"},
    )
    assert response.status_code == 200, response.text
    assert [item["strategy_run_id"] for item in response.json()["items"]] == [
        "live-run",
        "permitted-paper-run",
    ]


def test_list_runs_empty_and_invalid_inputs(discovery_client):
    headers = {"Authorization": "Bearer secret-1"}
    assert discovery_client.get(
        "/api/algo-workers/worker/runs?limit=0", headers=headers
    ).status_code == 422
    assert discovery_client.get(
        "/api/algo-workers/worker/runs?limit=101", headers=headers
    ).status_code == 422
    assert discovery_client.get(
        "/api/algo-workers/worker/runs?cursor=not-a-valid-cursor", headers=headers
    ).status_code == 400

    repo = _repo()
    _insert_token(
        repo,
        token_id="empty-owner",
        raw_token="empty-secret",
        account_scope="kite:paper-empty",
        modes=("paper",),
    )
    empty = _client(repo, "empty-secret").get(
        "/api/algo-workers/worker/runs",
        headers={"Authorization": "Bearer empty-secret"},
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}


def test_list_runs_requires_runs_read(discovery_client):
    repo = _repo()
    _insert_token(
        repo,
        token_id="no-read",
        raw_token="no-read-secret",
        account_scope="kite:paper-a",
        modes=("paper",),
    )
    with repo.session_factory() as connection:
        connection.execute(
            text("UPDATE public.algo_worker_tokens SET allowed_actions = :actions WHERE token_id = 'no-read'"),
            {"actions": json.dumps([])},
        )
        connection.commit()
    response = _client(repo, "no-read-secret").get(
        "/api/algo-workers/worker/runs",
        headers={"Authorization": "Bearer no-read-secret"},
    )
    assert response.status_code == 403
