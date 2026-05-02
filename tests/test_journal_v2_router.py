from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers import journal as journal_router  # noqa: E402


class _FakeJournalService:
    def __init__(self) -> None:
        self.resolve_calls: list[dict[str, Any]] = []
        self.episode_calls: list[dict[str, Any]] = []
        self.timeline_calls: list[dict[str, Any]] = []
        self.note_create_calls: list[dict[str, Any]] = []
        self.note_update_calls: list[dict[str, Any]] = []
        self.note_list_calls: list[dict[str, Any]] = []
        self.attachment_calls: list[dict[str, Any]] = []
        self.notes: dict[str, dict[str, Any]] = {}
        self.episode_owners = {"known-episode": "env-1", "ep-1": "env-1"}

    def resolve_v2_environment_id(
        self,
        *,
        environment_id: str | None = None,
        mode: str | None = None,
        account_scope: str | None = None,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int | None = None,
    ) -> str:
        self.resolve_calls.append(
            {
                "environment_id": environment_id,
                "mode": mode,
                "account_scope": account_scope,
                "broker_user_id": broker_user_id,
                "paper_account_key": paper_account_key,
                "environment_epoch": environment_epoch,
            }
        )
        if environment_id:
            return environment_id
        if mode and account_scope:
            return f"env::{mode}::{account_scope}"
        raise ValueError("environment context requires either environment_id or mode + account_scope")

    def list_v2_episodes(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if execution_context_id == "bad-context":
            raise ValueError("execution_context_id must be a valid UUID")
        self.episode_calls.append(
            {
                "environment_id": environment_id,
                "execution_context_id": execution_context_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            }
        )
        return [{"id": "ep-1", "environment_id": environment_id, "status": status or "open"}]

    def get_v2_episode_detail(self, episode_id: str, *, environment_id: str) -> dict[str, Any] | None:
        if episode_id == "known-episode":
            if self.episode_owners[episode_id] != environment_id:
                raise ValueError("episode_id does not belong to environment_id")
            return {"id": episode_id, "status": "open", "environment_id": environment_id}
        return None

    def list_v2_unresolved(self, *, environment_id: str) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "unres-1",
                    "environment_id": environment_id,
                    "execution_context_id": None,
                    "source_system": "algo_worker",
                    "reason": "missing_template_id_strategy_name_only",
                    "raw_identity": {"strategy_name": "Legacy Name"},
                    "candidate_mappings": [{"template_id": "legacy-name:legacy-name"}],
                    "metadata": {"resolution_confidence": "0.50"},
                    "status": "open",
                    "created_at": "2026-05-01T10:00:00+00:00",
                    "resolved_at": None,
                }
            ],
            "count": 1,
            "environment_id": environment_id,
        }

    def compute_v2_environment_metrics(self, *, environment_id: str):
        return {
            "environment_id": environment_id,
            "closed_episode_count": 1,
            "metrics": {"closed_episode_count": 1, "net_pnl": "10", "total_charges": "1"},
        }

    def compute_v2_environment_strategy_metrics(self, *, environment_id: str):
        return {
            "environment_id": environment_id,
            "items": [
                {
                    "template_id": "tmpl-1",
                    "strategy_family": "indicator_strategy",
                    "display_name": "Template 1",
                    "metrics": {"closed_episode_count": 1, "net_pnl": "10", "total_charges": "1"},
                }
            ],
            "count": 1,
        }

    def list_v2_strategies(self, *, environment_id: str):
        return {
            "environment_id": environment_id,
            "items": [{"template_id": "tmpl-1", "template_key": "tmpl-key", "display_name": "Template 1"}],
            "count": 1,
        }

    def compare_v2_paper_live_for_template(self, *, template_id: str, paper_environment_id: str, live_environment_id: str):
        return {
            "template_id": template_id,
            "paper_environment_id": paper_environment_id,
            "live_environment_id": live_environment_id,
            "paper": {"closed_episode_count": 2, "net_pnl": "20"},
            "live": {"closed_episode_count": 1, "net_pnl": "5"},
            "combined": None,
        }

    def list_v2_environments(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "env-1", "mode": mode or "paper", "account_scope": "kite:paper-e2e"}]

    def list_v2_timeline(self, *, episode_id: str, environment_id: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        if self.episode_owners.get(episode_id) not in {None, environment_id}:
            raise ValueError("episode_id does not belong to environment_id")
        self.timeline_calls.append({"episode_id": episode_id, "environment_id": environment_id, "limit": limit, "offset": offset})
        return [
            {
                "id": "evt-1",
                "episode_id": episode_id,
                "environment_id": environment_id,
                "event_type": "episode_opened",
                "occurred_at": "2026-05-01T10:00:00+00:00",
            }
        ]

    def create_v2_note(self, **kwargs) -> str:
        self.note_create_calls.append(dict(kwargs))
        note_id = f"note-{len(self.note_create_calls)}"
        self.notes[note_id] = {
            "id": note_id,
            "environment_id": kwargs["environment_id"],
            "subject_type": kwargs["subject_type"],
            "subject_id": kwargs["subject_id"],
            "episode_id": kwargs.get("episode_id"),
            "note_type": kwargs["note_type"],
            "title": kwargs["title"],
            "body_markdown": kwargs["body_markdown"],
            "tags": kwargs.get("tags") or [],
            "updated_at": "2026-05-01T10:00:00+00:00",
        }
        return note_id

    def get_v2_note(self, note_id: str, *, environment_id: str):
        note = self.notes.get(note_id)
        if note is not None and note.get("environment_id") != environment_id:
            raise ValueError("note_id does not belong to environment_id")
        return note

    def update_v2_note(self, note_id: str, **kwargs) -> None:
        if note_id not in self.notes:
            raise LookupError(f"Unknown note_id: {note_id}")
        self.note_update_calls.append({"note_id": note_id, **dict(kwargs)})
        if kwargs.get("environment_id") != self.notes[note_id]["environment_id"]:
            raise ValueError("environment_id mismatch for note update")
        if kwargs.get("subject_type") != self.notes[note_id]["subject_type"]:
            raise ValueError("subject_type mismatch for note update")
        if kwargs.get("subject_id") != self.notes[note_id]["subject_id"]:
            raise ValueError("subject_id mismatch for note update")
        if kwargs.get("title") is not None:
            self.notes[note_id]["title"] = kwargs["title"]
        if kwargs.get("body_markdown") is not None:
            self.notes[note_id]["body_markdown"] = kwargs["body_markdown"]
        self.notes[note_id]["updated_at"] = "2026-05-01T11:00:00+00:00"

    def list_v2_notes(self, environment_id: str, **kwargs):
        self.note_list_calls.append({"environment_id": environment_id, **dict(kwargs)})
        items = [item for item in self.notes.values() if item["environment_id"] == environment_id]
        if kwargs.get("subject_type") is not None:
            items = [item for item in items if item["subject_type"] == kwargs["subject_type"]]
        if kwargs.get("subject_id") is not None:
            items = [item for item in items if item["subject_id"] == kwargs["subject_id"]]
        if kwargs.get("episode_id") is not None:
            items = [item for item in items if item.get("episode_id") == kwargs["episode_id"]]
        return items

    def list_v2_note_revisions(self, note_id: str, *, environment_id: str):
        if note_id not in self.notes:
            raise LookupError(f"Unknown note_id: {note_id}")
        if self.notes[note_id].get("environment_id") != environment_id:
            raise ValueError("note_id does not belong to environment_id")
        return [{"note_id": note_id, "revision_no": 1, "body_markdown": "old"}]

    def attach_v2_file_metadata(self, **kwargs) -> str:
        self.attachment_calls.append(dict(kwargs))
        return "attachment-1"


def _client(service: _FakeJournalService) -> TestClient:
    app = FastAPI()
    app.include_router(journal_router.router, prefix="/api")
    app.state.journal_service = service
    return TestClient(app)


def test_v2_episodes_requires_environment_context(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/episodes")

    assert response.status_code == 400
    assert "environment context" in response.json()["detail"]


def test_v2_episodes_with_environment_id_calls_service(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get(
        "/api/journal/v2/episodes",
        params={"environment_id": "env-123", "status": "open", "limit": 10, "offset": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["environment_id"] == "env-123"
    assert service.episode_calls[-1]["environment_id"] == "env-123"
    assert service.episode_calls[-1]["status"] == "open"


def test_v2_episodes_with_mode_and_account_scope_resolves_environment(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get(
        "/api/journal/v2/episodes",
        params={"mode": "paper", "account_scope": "kite:paper-e2e"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["environment_id"] == "env::paper::kite:paper-e2e"
    assert service.resolve_calls[-1]["mode"] == "paper"
    assert service.resolve_calls[-1]["account_scope"] == "kite:paper-e2e"


def test_v2_episodes_rejects_invalid_execution_context_id(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get(
        "/api/journal/v2/episodes",
        params={"environment_id": "env-123", "execution_context_id": "bad-context"},
    )

    assert response.status_code == 400
    assert "execution_context_id" in response.json()["detail"]


def test_v2_episode_detail_returns_404_for_unknown_episode(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/episodes/missing-episode", params={"environment_id": "env-1"})

    assert response.status_code == 404
    assert "Unknown episode_id" in response.json()["detail"]


def test_v2_episode_detail_requires_environment_id_and_checks_owner(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)

    missing = client.get("/api/journal/v2/episodes/known-episode")
    wrong = client.get("/api/journal/v2/episodes/known-episode", params={"environment_id": "env-2"})
    correct = client.get("/api/journal/v2/episodes/known-episode", params={"environment_id": "env-1"})

    assert missing.status_code == 422
    assert wrong.status_code == 400
    assert "environment_id" in wrong.json()["detail"]
    assert correct.status_code == 200
    assert correct.json()["environment_id"] == "env-1"


def test_v2_unresolved_returns_queue_items(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/unresolved", params={"environment_id": "env-xyz"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["reason"] == "missing_template_id_strategy_name_only"


def test_v2_strategies_and_unresolved_resolve_mode_account_scope(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)

    strategies = client.get("/api/journal/v2/strategies", params={"mode": "paper", "account_scope": "kite:paper-e2e"})
    unresolved = client.get("/api/journal/v2/unresolved", params={"mode": "live", "account_scope": "kite:AB1234"})

    assert strategies.status_code == 200
    assert strategies.json()["environment_id"] == "env::paper::kite:paper-e2e"
    assert unresolved.status_code == 200
    assert unresolved.json()["environment_id"] == "env::live::kite:AB1234"
    assert service.resolve_calls[-2]["mode"] == "paper"
    assert service.resolve_calls[-1]["mode"] == "live"


def test_v2_analytics_summary_requires_environment_id(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/analytics/summary")

    assert response.status_code == 422


def test_v2_analytics_strategy_requires_environment_id(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/analytics/strategies")

    assert response.status_code == 422


def test_v2_compare_paper_live_has_no_combined_totals(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get(
        "/api/journal/v2/analytics/compare-paper-live",
        params={
            "template_id": "tmpl-1",
            "paper_environment_id": "00000000-0000-4000-8000-000000000001",
            "live_environment_id": "00000000-0000-4000-8000-000000000002",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["template_id"] == "tmpl-1"
    assert body["paper_environment_id"] == "00000000-0000-4000-8000-000000000001"
    assert body["live_environment_id"] == "00000000-0000-4000-8000-000000000002"
    assert body["paper"]["net_pnl"] == "20"
    assert body["live"]["net_pnl"] == "5"
    assert body["combined"] is None


def test_v2_strategy_analytics_are_environment_scoped(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)

    summary = client.get("/api/journal/v2/analytics/summary", params={"environment_id": "env-paper"})
    strategies = client.get("/api/journal/v2/analytics/strategies", params={"environment_id": "env-live"})

    assert summary.status_code == 200
    assert summary.json()["environment_id"] == "env-paper"
    assert summary.json()["metrics"]["closed_episode_count"] == 1
    assert strategies.status_code == 200
    assert strategies.json()["environment_id"] == "env-live"
    assert strategies.json()["count"] == 1


def test_v2_environments_returns_items(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/environments", params={"mode": "paper"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["mode"] == "paper"


def test_v1_journal_routes_still_exist():
    paths = {getattr(route, "path") for route in journal_router.router.routes if getattr(route, "path", None)}
    assert "/journal/runs" in paths
    assert "/journal/summary" in paths
    assert "/journal/benchmark" in paths


def test_v2_episode_timeline_route_returns_events(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).get("/api/journal/v2/episodes/ep-1/timeline", params={"environment_id": "env-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["event_type"] == "episode_opened"
    assert service.timeline_calls[-1]["episode_id"] == "ep-1"
    assert service.timeline_calls[-1]["environment_id"] == "env-1"


def test_v2_episode_timeline_requires_environment_id_and_checks_owner(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)

    missing = client.get("/api/journal/v2/episodes/ep-1/timeline")
    wrong = client.get("/api/journal/v2/episodes/ep-1/timeline", params={"environment_id": "env-2"})
    correct = client.get("/api/journal/v2/episodes/ep-1/timeline", params={"environment_id": "env-1"})

    assert missing.status_code == 422
    assert wrong.status_code == 400
    assert "environment_id" in wrong.json()["detail"]
    assert correct.status_code == 200


def test_v2_notes_create_list_update_and_revisions(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)

    create_response = client.post(
        "/api/journal/v2/notes",
        json={
            "environment_id": "env-1",
            "subject_type": "episode",
            "subject_id": "ep-11",
            "episode_id": "ep-11",
            "note_type": "thesis",
            "title": "Entry thesis",
            "body_markdown": "# idea",
            "tags": ["breakout"],
        },
    )
    assert create_response.status_code == 200
    note_id = create_response.json()["id"]

    list_episode = client.get(
        "/api/journal/v2/notes",
        params={"environment_id": "env-1", "subject_type": "episode", "subject_id": "ep-11"},
    )
    assert list_episode.status_code == 200
    assert list_episode.json()["count"] == 1

    list_template = client.get(
        "/api/journal/v2/notes",
        params={
            "environment_id": "env-1",
            "subject_type": "strategy_template",
            "subject_id": "tpl-22",
        },
    )
    assert list_template.status_code == 200
    assert list_template.json()["count"] == 0

    create_template = client.post(
        "/api/journal/v2/notes",
        json={
            "environment_id": "env-1",
            "subject_type": "strategy_template",
            "subject_id": "tpl-22",
            "note_type": "lesson",
            "title": "Template note",
            "body_markdown": "## learn",
        },
    )
    assert create_template.status_code == 200

    list_template = client.get(
        "/api/journal/v2/notes",
        params={
            "environment_id": "env-1",
            "subject_type": "strategy_template",
            "subject_id": "tpl-22",
        },
    )
    assert list_template.status_code == 200
    assert list_template.json()["count"] == 1

    patch_response = client.patch(
        f"/api/journal/v2/notes/{note_id}",
        json={
            "environment_id": "env-1",
            "subject_type": "episode",
            "subject_id": "ep-11",
            "title": "Updated title",
        },
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["title"] == "Updated title"

    revisions_response = client.get(f"/api/journal/v2/notes/{note_id}/revisions", params={"environment_id": "env-1"})
    assert revisions_response.status_code == 200
    assert revisions_response.json()["count"] == 1

    missing_get = client.get(f"/api/journal/v2/notes/{note_id}")
    wrong_get = client.get(f"/api/journal/v2/notes/{note_id}", params={"environment_id": "env-2"})
    correct_get = client.get(f"/api/journal/v2/notes/{note_id}", params={"environment_id": "env-1"})
    missing_revisions = client.get(f"/api/journal/v2/notes/{note_id}/revisions")
    wrong_revisions = client.get(f"/api/journal/v2/notes/{note_id}/revisions", params={"environment_id": "env-2"})

    assert missing_get.status_code == 422
    assert wrong_get.status_code == 400
    assert correct_get.status_code == 200
    assert missing_revisions.status_code == 422
    assert wrong_revisions.status_code == 400


def test_v2_note_patch_rejects_subject_boundary_mismatch(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()
    client = _client(service)
    note_id = service.create_v2_note(
        environment_id="env-1",
        subject_type="episode",
        subject_id="ep-1",
        note_type="thesis",
        title="x",
        body_markdown="y",
    )

    response = client.patch(
        f"/api/journal/v2/notes/{note_id}",
        json={
            "environment_id": "env-2",
            "subject_type": "episode",
            "subject_id": "ep-1",
            "title": "invalid",
        },
    )
    assert response.status_code == 400
    assert "environment_id mismatch" in response.json()["detail"]


def test_v2_attachments_create(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = _FakeJournalService()

    response = _client(service).post(
        "/api/journal/v2/attachments",
        json={
            "environment_id": "env-1",
            "subject_type": "episode",
            "subject_id": "ep-11",
            "storage_key": "attachments/abc.png",
            "mime_type": "image/png",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"id": "attachment-1"}
