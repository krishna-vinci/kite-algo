# pyright: reportArgumentType=false
"""API tests for the worker workflow endpoints (Alerts Platform Task 8).

Bootstrapping mirrors tests/api/test_algo_worker_api.py: dependency stubs are
installed first, a fresh in-memory SQLite engine is created with the shared
alerts-platform ``Base.metadata.create_all`` (from
``backend.workflows.repository`` so BOTH repos' tables register), and the
FastAPI app carries only the new routers with the auth repository injected via
``app.state`` (the ``require_worker_token`` mechanism) and the alerts
sessionmaker injected via ``app.dependency_overrides``.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# A Postgres-style DATABASE_URL keeps backend.app.database's module-level
# engine constructible (psycopg2 is stubbed; nothing ever connects) — same
# pattern as tests/api/test_worker_indicators.py.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers import worker_notifications as worker_notifications_router  # noqa: E402
from backend.api.routers import worker_workflows as worker_workflows_router  # noqa: E402
from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS  # noqa: E402
from backend.notifications.repository import Delivery  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    AlertSubscription,
    Base,
    SignalEvent,
    Workflow as WorkflowModel,
    WorkflowRevision,
)

RAW_TOKEN = "worker-secret-token"
HEADERS = {"Authorization": f"Bearer {RAW_TOKEN}"}
WF = "/api/worker/workflows"

VALID_YAML = """\
version: 1
name: reliance-breakout
instruments: ["NSE:RELIANCE"]
stages:
  - id: px
    type: signal
    clock: ltp
    conditions:
      all:
        - left: {field: ltp}
          op: crosses_above
          right: {value: 3000}
alerts:
  - id: breakout
    source: px
    trigger: once
    channels: ["telegram_primary"]
"""

VALID_YAML_V2 = VALID_YAML.replace("value: 3000", "value: 3100")
INVALID_YAML = "version: 1\nname: [unclosed\n"
BAD_OPERATOR_YAML = VALID_YAML.replace("crosses_above", "crosses_sideways")


class _StubWorkerTokenRepository:
    """Minimal stand-in for SqlAlchemyAlgoWorkerRepository (auth only)."""

    def __init__(self, token: WorkerToken, *, raw_token: str = RAW_TOKEN):
        self.token = token
        self.raw_token = raw_token

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        return None


def _token(actions=None) -> WorkerToken:
    return WorkerToken(
        token_id="worker-1",
        name="test-worker",
        account_scope="kite:paper-a",
        allowed_modes=["paper", "dry_run"],
        allowed_actions=sorted(DEFAULT_WORKER_ACTIONS if actions is None else actions),
        allowed_templates=[],
    )


def _client(actions=None):
    """TestClient over both new routers with an in-memory alerts database."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(worker_workflows_router.router, prefix="/api")
    app.include_router(worker_notifications_router.router, prefix="/api")
    app.dependency_overrides[worker_workflows_router._alerts_db] = lambda: factory
    app.state.algo_worker_repository = _StubWorkerTokenRepository(_token(actions))
    return TestClient(app), factory


def _create_workflow(client, *, name="reliance-breakout", yaml_text=VALID_YAML, idempotency_key=None):
    payload = {"name": name, "yaml_text": yaml_text}
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    response = client.post(WF, json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _activate(client, workflow_id):
    response = client.post(f"{WF}/{workflow_id}/activate", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _workflow_subscription_id(factory, workflow_id):
    with factory() as session:
        subscription = session.execute(
            select(AlertSubscription)
            .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
            .where(WorkflowRevision.workflow_id == workflow_id)
            .order_by(AlertSubscription.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        assert subscription is not None, "activation must materialize a subscription"
        return subscription.id


def test_validate_invalid_yaml_returns_issues_with_200():
    client, _ = _client()
    response = client.post(f"{WF}/validate", json={"yaml_text": INVALID_YAML}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["issues"], "parse failures must surface as issues"
    assert body["issues"][0]["code"] == "parse_error"
    assert body["issues"][0]["message"]


def _raw_document_with_unknown_operator():
    import yaml

    raw = yaml.safe_load(VALID_YAML)
    raw["stages"][0]["conditions"]["all"][0]["op"] = "crosses_sideways"
    return raw


def test_validate_valid_document_is_ok_and_unknown_operator_is_flagged():
    client, _ = _client()
    ok = client.post(f"{WF}/validate", json={"yaml_text": VALID_YAML}, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert ok.json()["issues"] == []

    bad = client.post(
        f"{WF}/validate",
        json={"document": _raw_document_with_unknown_operator()},
        headers=HEADERS,
    )
    assert bad.status_code == 200
    body = bad.json()
    assert body["ok"] is False
    assert any(issue["code"] == "unknown_operator" for issue in body["issues"])


def test_validate_requires_exactly_one_transport():
    client, _ = _client()
    neither = client.post(f"{WF}/validate", json={}, headers=HEADERS)
    assert neither.status_code == 422
    both = client.post(
        f"{WF}/validate",
        json={"yaml_text": VALID_YAML, "document": {}},
        headers=HEADERS,
    )
    assert both.status_code == 422


def test_create_then_get_shows_draft_revision_1():
    client, _ = _client()
    created = _create_workflow(client)
    assert created["workflow_id"]
    assert created["created"] is True
    assert created["revision"] == 1
    assert created["revision_status"] == "draft"
    assert created["canonical_hash"]

    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS)
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "reliance-breakout"
    assert body["latest_revision"]["revision"] == 1
    assert body["latest_revision"]["status"] == "draft"
    assert body["active_revision"] is None

    listing = client.get(WF, headers=HEADERS).json()["workflows"]
    assert [workflow["workflow_id"] for workflow in listing] == [created["workflow_id"]]


def test_activate_without_workflows_activate_action_is_403():
    client, _ = _client(actions=DEFAULT_WORKER_ACTIONS - {"workflows:activate"})
    created = _create_workflow(client)
    response = client.post(f"{WF}/{created['workflow_id']}/activate", headers=HEADERS)
    assert response.status_code == 403, response.text


def test_activate_happy_path_materializes_subscriptions_and_health():
    client, factory = _client()
    created = _create_workflow(client)
    activated = _activate(client, created["workflow_id"])
    assert activated["revision"] == 1
    assert activated["subscriptions_created"] == 1

    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["active_revision"] == 1
    assert health["stale_after_seconds"] == 300
    subscriptions = {
        (subscription["alert_id"], subscription["instrument_key"]): subscription["state"]
        for subscription in health["subscriptions"]
    }
    assert subscriptions == {("breakout", "NSE:RELIANCE"): "active"}

    with factory() as session:
        rows = session.execute(select(AlertSubscription)).scalars().all()
        assert len(rows) == 1
        assert rows[0].alert_id == "breakout"
        assert rows[0].stage_id == "px"
        assert rows[0].trigger == "once"
        assert rows[0].config["channels"] == ["telegram_primary"]

    # activating again without a new draft replays cleanly (idempotent rows)
    again = client.post(f"{WF}/{created['workflow_id']}/activate", headers=HEADERS)
    assert again.status_code == 409  # an already-active revision cannot re-activate


def test_activate_explicit_revision_rolls_back():
    """POST /{id}/activate {"revision": N} activates that revision (rollback)."""
    client, _ = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])
    patched = client.patch(
        f"{WF}/{created['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 1},
        headers=HEADERS,
    )
    assert patched.status_code == 200, patched.text
    activated2 = client.post(
        f"{WF}/{created['workflow_id']}/activate",
        json={"revision": 2},
        headers=HEADERS,
    )
    assert activated2.status_code == 200, activated2.text
    assert activated2.json()["revision"] == 2

    rolled = client.post(
        f"{WF}/{created['workflow_id']}/activate",
        json={"revision": 1},
        headers=HEADERS,
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["revision"] == 1

    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS).json()
    assert got["active_revision"]["revision"] == 1
    assert got["latest_revision"]["revision"] == 2
    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["active_revision"] == 1


def test_activate_archived_workflow_reactivates_it():
    client, _ = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])

    archived = client.post(f"{WF}/{created['workflow_id']}/archive", headers=HEADERS)
    assert archived.status_code == 200, archived.text
    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS).json()
    assert got["archived"] is True

    # activating the archived workflow's revision un-archives the workflow
    reactivated = client.post(
        f"{WF}/{created['workflow_id']}/activate",
        json={"revision": 1},
        headers=HEADERS,
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["revision"] == 1

    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS).json()
    assert got["archived"] is False
    assert got["active_revision"]["revision"] == 1

    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["active_revision"] == 1


def test_activate_unknown_explicit_revision_is_404():
    client, _ = _client()
    created = _create_workflow(client)
    response = client.post(
        f"{WF}/{created['workflow_id']}/activate",
        json={"revision": 99},
        headers=HEADERS,
    )
    assert response.status_code == 404, response.text


def test_patch_stale_expected_revision_conflicts_409():
    client, _ = _client()
    created = _create_workflow(client)
    response = client.patch(
        f"{WF}/{created['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 2},
        headers=HEADERS,
    )
    assert response.status_code == 409, response.text


def test_patch_with_current_revision_creates_draft_2():
    client, factory = _client()
    created = _create_workflow(client)
    response = client.patch(
        f"{WF}/{created['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["revision"] == 2
    assert body["revision_status"] == "draft"

    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS).json()
    assert got["latest_revision"]["revision"] == 2
    assert got["active_revision"] is None

    with factory() as session:
        revisions = session.execute(
            select(WorkflowRevision).where(WorkflowRevision.workflow_id == created["workflow_id"])
        ).scalars().all()
        assert sorted(revision.revision for revision in revisions) == [1, 2]


def test_patch_noop_same_hash_returns_current_without_new_revision():
    client, factory = _client()
    created = _create_workflow(client)
    first = client.patch(
        f"{WF}/{created['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 1},
        headers=HEADERS,
    ).json()
    second = client.patch(
        f"{WF}/{created['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 2},
        headers=HEADERS,
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["changed"] is False
    assert body["revision"] == first["revision"] == 2

    with factory() as session:
        count = session.execute(
            select(func.count()).select_from(WorkflowRevision).where(
                WorkflowRevision.workflow_id == created["workflow_id"]
            )
        ).scalar()
        assert count == 2


def test_idempotent_create_replays_same_workflow_id():
    client, _ = _client()
    first = _create_workflow(client, idempotency_key="idem-key-1")
    second = _create_workflow(client, idempotency_key="idem-key-1")
    assert second["workflow_id"] == first["workflow_id"]
    assert first["created"] is True
    assert second["created"] is False


def test_create_with_invalid_document_returns_422_with_issues():
    client, _ = _client()
    response = client.post(WF, json={"name": "broken", "yaml_text": BAD_OPERATOR_YAML}, headers=HEADERS)
    assert response.status_code == 422
    issues = response.json()["detail"]["issues"]
    assert any(item["code"] == "unknown_operator" for item in issues)


def test_import_creates_workflow_from_yaml_and_export_round_trips_hash():
    client, _ = _client()
    imported = client.post(
        f"{WF}/import",
        json={"yaml_text": VALID_YAML, "idempotency_key": "import-1"},
        headers=HEADERS,
    )
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["name"] == "reliance-breakout"
    assert body["revision"] == 1

    _activate(client, body["workflow_id"])
    exported = client.get(f"{WF}/{body['workflow_id']}/export", headers=HEADERS)
    assert exported.status_code == 200
    export = exported.json()
    assert export["revision"] == 1
    assert export["canonical_hash"] == body["canonical_hash"]

    reparsed = compile_document(parse_workflow_dict(export["document"]))
    assert reparsed.canonical_hash == export["canonical_hash"]

    # requested (non-active) revision export
    client.patch(
        f"{WF}/{body['workflow_id']}",
        json={"yaml_text": VALID_YAML_V2, "expected_revision": 1},
        headers=HEADERS,
    )
    draft_export = client.get(
        f"{WF}/{body['workflow_id']}/export",
        params={"revision": 2},
        headers=HEADERS,
    )
    assert draft_export.status_code == 200
    assert draft_export.json()["revision"] == 2


def test_preview_returns_warmup_report_and_writes_nothing():
    client, factory = _client()
    response = client.post(f"{WF}/preview", json={"yaml_text": VALID_YAML}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["issues"] == []
    assert body["instruments"] == ["NSE:RELIANCE"]
    assert body["stages"] == ["px"]
    assert body["alerts"] == ["breakout"]
    assert body["evaluation"] == "dry_run_no_data"
    assert body["evaluated_observations"] == 0

    with factory() as session:
        assert session.execute(select(func.count()).select_from(Delivery)).scalar() == 0
        assert session.execute(select(func.count()).select_from(SignalEvent)).scalar() == 0
        assert session.execute(select(func.count()).select_from(WorkflowModel)).scalar() == 0
        assert session.execute(select(func.count()).select_from(AlertSubscription)).scalar() == 0


def test_preview_dry_run_reports_would_fire_without_persisting():
    client, factory = _client()
    observations = [
        {
            "instrument_key": "NSE:RELIANCE",
            "epoch_id": "preview-epoch",
            "ts": "2026-09-08T09:15:00Z",
            "ltp": 2990,
        },
        {
            "instrument_key": "NSE:RELIANCE",
            "epoch_id": "preview-epoch",
            "ts": "2026-09-08T09:16:00Z",
            "ltp": 3010,
        },
    ]
    response = client.post(
        f"{WF}/preview",
        json={"yaml_text": VALID_YAML, "observations": observations},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evaluation"] == "dry_run"
    assert body["evaluated_observations"] == 2
    assert len(body["would_fire"]) == 1
    assert body["would_fire"][0]["alert_id"] == "breakout"
    with factory() as session:
        assert session.execute(select(func.count()).select_from(SignalEvent)).scalar() == 0


def test_events_endpoint_returns_seeded_events_paginated_newest_first():
    client, factory = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])
    subscription_id = _workflow_subscription_id(factory, created["workflow_id"])

    base = datetime(2026, 9, 8, 9, 15, tzinfo=timezone.utc)
    with factory() as session:
        for index in range(3):
            session.add(
                SignalEvent(
                    id=str(uuid.uuid4()),
                    subscription_id=subscription_id,
                    occurrence_key=f"occ-{index}",
                    fired_at=base + timedelta(minutes=index),
                    evidence={"ltp": 3000.0 + index},
                )
            )
        session.commit()

    page1 = client.get(
        f"{WF}/{created['workflow_id']}/events",
        params={"limit": 2, "offset": 0},
        headers=HEADERS,
    )
    assert page1.status_code == 200, page1.text
    page1_body = page1.json()
    assert page1_body["total"] == 3
    assert [event["occurrence_key"] for event in page1_body["events"]] == ["occ-2", "occ-1"]
    assert all(event["alert_id"] == "breakout" for event in page1_body["events"])

    page2 = client.get(
        f"{WF}/{created['workflow_id']}/events",
        params={"limit": 2, "offset": 1},
        headers=HEADERS,
    ).json()
    assert [event["occurrence_key"] for event in page2["events"]] == ["occ-1", "occ-0"]


def test_health_reports_delivery_counts_and_last_event_at():
    client, factory = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])
    subscription_id = _workflow_subscription_id(factory, created["workflow_id"])

    base = datetime(2026, 9, 8, 9, 15, tzinfo=timezone.utc)
    with factory() as session:
        event = SignalEvent(
            id=str(uuid.uuid4()),
            subscription_id=subscription_id,
            occurrence_key="occ-0",
            fired_at=base,
            evidence={"ltp": 3000.0},
        )
        session.add(event)
        session.flush()
        session.add(Delivery(id=str(uuid.uuid4()), event_id=event.id, channel_id=str(uuid.uuid4()), status="delivered"))
        session.add(Delivery(id=str(uuid.uuid4()), event_id=event.id, channel_id=str(uuid.uuid4()), status="pending"))
        session.commit()

    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["delivery_counts"] == {"delivered": 1, "pending": 1}
    assert health["last_event_at"] is not None


def test_health_reports_per_subscription_last_evaluated_at_and_stale_after():
    """last_evaluated_at comes from evaluation_checkpoints.updated_at (max per
    subscription) and stale_after_seconds is a stable constant clients compute
    staleness against."""
    from backend.workflows.repository import EvaluationCheckpoint

    client, factory = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])
    subscription_id = _workflow_subscription_id(factory, created["workflow_id"])

    with factory() as session:
        session.add(
            EvaluationCheckpoint(
                subscription_id=subscription_id,
                instrument_key="NSE:RELIANCE",
                epoch_id="boot-1",
                state={"prev": 2999.0},
                owner_epoch=1,
                updated_at=datetime(2026, 9, 8, 9, 30, tzinfo=timezone.utc),
            )
        )
        session.commit()

    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["stale_after_seconds"] == 300
    subscriptions = health["subscriptions"]
    assert len(subscriptions) == 1
    evaluated = subscriptions[0]["last_evaluated_at"]
    assert evaluated is not None
    assert evaluated.startswith("2026-09-08T09:30:00")


def test_health_last_evaluated_at_is_none_without_checkpoints():
    client, _ = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])
    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["stale_after_seconds"] == 300
    assert all(sub["last_evaluated_at"] is None for sub in health["subscriptions"])


def test_pause_resume_and_archive():
    client, _ = _client()
    created = _create_workflow(client)
    _activate(client, created["workflow_id"])

    paused = client.post(f"{WF}/{created['workflow_id']}/pause", headers=HEADERS)
    assert paused.status_code == 200, paused.text
    assert paused.json()["updated"] == 1
    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert {subscription["state"] for subscription in health["subscriptions"]} == {"paused"}

    resumed = client.post(f"{WF}/{created['workflow_id']}/resume", headers=HEADERS)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["updated"] == 1
    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert {subscription["state"] for subscription in health["subscriptions"]} == {"active"}

    archived = client.post(f"{WF}/{created['workflow_id']}/archive", headers=HEADERS)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert archived.json()["revisions_archived"] == 1
    got = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS).json()
    assert got["archived"] is True
    assert got["latest_revision"]["status"] == "archived"
    health = client.get(f"{WF}/{created['workflow_id']}/health", headers=HEADERS).json()
    assert health["active_revision"] is None


def test_other_owner_cannot_read_workflow():
    client, _ = _client()
    created = _create_workflow(client)
    app = client.app
    # swap in a token for a different account scope
    app.state.algo_worker_repository = _StubWorkerTokenRepository(
        WorkerToken(
            token_id="worker-2",
            name="other-worker",
            account_scope="kite:paper-b",
            allowed_modes=["paper"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
    )
    response = client.get(f"{WF}/{created['workflow_id']}", headers=HEADERS)
    assert response.status_code == 404


def test_requires_bearer_token():
    client, _ = _client()
    response = client.get(WF)
    assert response.status_code == 401


@pytest.mark.parametrize("yaml_text", [INVALID_YAML])
def test_create_rejects_unparseable_yaml_with_422(yaml_text):
    client, _ = _client()
    response = client.post(WF, json={"name": "broken", "yaml_text": yaml_text}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"]["issues"][0]["code"] == "parse_error"
