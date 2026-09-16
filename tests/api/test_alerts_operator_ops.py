"""Phase 6 6A.4/6B backend: operator health, scope-aligned tokens, revision YAML.

These cover the surfaces an operator page needs beyond the workflow CRUD covered
in ``test_alerts_operator.py``:

- **health**: lifecycle and data-freshness reported as SEPARATE facts, tick age
  derived durably from the checkpoint so it ages with no tick arriving, and an
  explicitly UNKNOWN runtime section rather than fabricated zeros;
- **tokens**: the scope is taken from the authorization result and never from
  the request body, execution actions are refused, one-time reveal;
- **yaml**: revision-addressed and round-trip lossless;
- **layout**: cosmetic writes create no revision.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.routers import alerts_operator as operator_router  # noqa: E402
from backend.api.services import alerts_operator as operator_service  # noqa: E402
from backend.api.services import alerts_runtime_health as runtime_health  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.workflows.canvas_layout import CanvasLayoutRepository  # noqa: E402
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.service import EvaluationService  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    AlertSubscription,
    Base,
    EvaluationCheckpoint,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
    WorkflowRevision,
)

OPERATOR_SCOPE = "app:admin"
BASE = "/api/alerts"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

LTP_DOCUMENT = {
    "version": 1, "name": "ltp-watch", "session": "nse_equity",
    "instruments": ["NSE:RELIANCE"],
    "stages": [{
        "id": "px", "type": "signal", "clock": "ltp",
        "conditions": {"all": [
            {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 3000}}
        ]},
    }],
    "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition"}],
}

CANDLE_DOCUMENT = {
    "version": 1, "name": "candle-watch", "session": "nse_equity",
    "instruments": ["NSE:TCS"],
    "stages": [{
        "id": "px", "type": "signal", "clock": "candle_close", "timeframe": "5minute",
        "conditions": {"all": [
            {"left": {"field": "close"}, "op": "crosses_above", "right": {"value": 4000}}
        ]},
    }],
    "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition"}],
}


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def operator_scope_env(monkeypatch):
    monkeypatch.setenv(operator_service.ALERTS_OPERATOR_SCOPES_ENV, OPERATOR_SCOPE)
    monkeypatch.delenv(operator_service.ALERTS_OPERATOR_OWNER_ENV, raising=False)
    # No health file by default: the runtime section must be honestly unknown.
    monkeypatch.setenv(runtime_health.ALERTS_WORKER_HEALTH_FILE_ENV, "/nonexistent/health.json")
    yield


def _app(session_factory, monkeypatch, *, authenticated=True):
    from backend.app import auth as auth_module

    user = AppUser(username="admin", role="admin") if authenticated else None
    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(operator_router.router, prefix="/api")
    app.dependency_overrides[operator_router._alerts_db] = lambda: session_factory
    app.state.workflow_repository = SqlAlchemyWorkflowRepository(session_factory)
    app.state.canvas_layout_repository = CanvasLayoutRepository(session_factory)
    return TestClient(app)


def _activate(session_factory, document, name="wf"):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(document))
    workflow, revision = repository.create_workflow(
        OPERATOR_SCOPE, name, compiled.document.to_document_dict(), compiled.canonical_hash
    )
    activated = repository.activate_revision(workflow.id, revision.id)
    # Materialize subscriptions the same way the activate route does, so the
    # health view has real subscription rows to report on.
    EvaluationService(repository, session_factory).ensure_subscriptions(activated)
    return workflow, revision


def _subscription_for(session, workflow_id):
    """AlertSubscription links to a workflow through its revision."""
    return (
        session.query(AlertSubscription)
        .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
        .filter(WorkflowRevision.workflow_id == workflow_id)
        .first()
    )


def _write_checkpoint(session_factory, workflow_id, *, state, updated_at=NOW):
    with session_factory() as session:
        subscription = _subscription_for(session, workflow_id)
        session.add(
            EvaluationCheckpoint(
                subscription_id=subscription.id,
                instrument_key=subscription.instrument_key,
                epoch_id="epoch-1",
                state=state,
                owner_epoch=1,
                updated_at=updated_at,
            )
        )
        session.commit()
        return subscription


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_reports_lifecycle_and_freshness_separately(session_factory, monkeypatch):
    """An active workflow with no data must not read as "fine".

    Lifecycle ('switched on') and freshness ('receiving data') are different
    questions, and a single merged status is how a stale alert looks healthy.
    """
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    _write_checkpoint(
        session_factory, workflow.id,
        state={"last_tick_received_at": (NOW - timedelta(seconds=900)).isoformat()},
    )
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}/health").json()

    assert body["lifecycle"]["active"] is True, "lifecycle says it is switched on"
    row = body["subscriptions"][0]
    assert row["stale"] is True, "freshness says data is stale"
    assert row["stale_reason"] == "tick_age_exceeded"
    assert row["tick_age_s"] >= 800


def test_tick_age_is_derived_from_the_stored_receipt_not_wall_clock_ticks(session_factory, monkeypatch):
    """The age must come from a durable timestamp, so silence ages on its own."""
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    received = datetime.now(timezone.utc) - timedelta(seconds=42)
    _write_checkpoint(
        session_factory, workflow.id,
        state={"last_tick_received_at": received.isoformat()},
    )
    client = _app(session_factory, monkeypatch)
    row = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["subscriptions"][0]
    # No tick arrived during this test; the age exists purely because the
    # receipt timestamp was stored earlier.
    assert 40 <= row["tick_age_s"] <= 90
    assert row["stale"] is False
    assert row["received_at_is_receipt_not_event_time"] is True


def test_a_subscription_that_never_ticked_says_so(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    row = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["subscriptions"][0]
    assert row["stale_reason"] == "no_accepted_tick"
    assert row["tick_age_s"] is None
    assert row["last_tick_received_at"] is None


def test_a_continuity_invalidation_is_surfaced_with_its_reason(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    stamp = datetime.now(timezone.utc) - timedelta(seconds=30)
    _write_checkpoint(
        session_factory, workflow.id,
        state={
            "last_tick_received_at": stamp.isoformat(),
            "continuity_invalidated_at": stamp.isoformat(),
            "continuity_invalidation_reason": "ltp_gap",
        },
    )
    client = _app(session_factory, monkeypatch)
    row = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["subscriptions"][0]
    assert row["stale"] is False, "data is flowing again"
    assert row["stale_reason"] == "continuity_invalidated"
    assert row["continuity_invalidation_reason"] == "ltp_gap"
    assert row["continuity_invalidated_at"] is not None


def test_a_candle_subscription_reports_no_tick_age(session_factory, monkeypatch):
    """Candle freshness is not a tick question, so no tick age is invented."""
    workflow, _revision = _activate(session_factory, CANDLE_DOCUMENT, name="candles")
    _write_checkpoint(session_factory, workflow.id, state={"last_bar_ts": NOW.isoformat()})
    client = _app(session_factory, monkeypatch)
    row = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["subscriptions"][0]
    assert row["stale_reason"] == "not_an_ltp_subscription"
    assert row["tick_age_s"] is None


def test_evaluation_age_comes_from_the_checkpoint(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    stale_eval = datetime.now(timezone.utc) - timedelta(seconds=600)
    _write_checkpoint(
        session_factory, workflow.id,
        state={"last_tick_received_at": stale_eval.isoformat()},
        updated_at=stale_eval,
    )
    client = _app(session_factory, monkeypatch)
    row = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["subscriptions"][0]
    assert row["evaluation_age_s"] >= 500
    assert row["last_evaluated_at"] is not None


def test_unreadable_runtime_health_is_unknown_not_zero(session_factory, monkeypatch):
    """An unreachable worker must not look like a healthy one."""
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    runtime = client.get(f"{BASE}/workflows/{workflow.id}/health").json()["runtime"]
    assert runtime["available"] is False
    assert runtime["reason"] == "health_file_absent"
    assert "UNKNOWN" in runtime["note"]
    assert "not a report that they are zero" in runtime["note"]


def test_runtime_health_is_merged_when_the_file_is_readable(session_factory, monkeypatch, tmp_path):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    subscription = _write_checkpoint(
        session_factory, workflow.id,
        state={"last_tick_received_at": NOW.isoformat()},
    )
    health_file = tmp_path / "alerts-health.json"
    health_file.write_text(json.dumps({
        "last_health_at": NOW.isoformat(),
        "quarantined": {subscription.id: "2026-09-11T12:05:00+00:00"},
        "subscription_failures": {subscription.id: {
            "failures": 3, "last_error": "boom", "last_failure_at": NOW.isoformat(),
        }},
        "tasks": {"evaluation": {"alive": False, "restarts": 2}},
    }))
    monkeypatch.setenv(runtime_health.ALERTS_WORKER_HEALTH_FILE_ENV, str(health_file))

    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}/health").json()
    assert body["runtime"]["available"] is True
    assert body["runtime"]["tasks"]["evaluation"]["alive"] is False
    row = body["subscriptions"][0]
    assert row["quarantined_until"] == "2026-09-11T12:05:00+00:00"
    assert row["failures"] == 3
    assert row["last_error"] == "boom"


def test_platform_health_rejects_an_invalid_health_file(tmp_path, monkeypatch):
    """Malformed JSON is 'unavailable', never a crash or a partial read."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(runtime_health.ALERTS_WORKER_HEALTH_FILE_ENV, str(bad))
    view = runtime_health.runtime_health_view()
    assert view["available"] is False
    assert view["reason"] == "health_file_invalid"


def test_health_is_owner_scoped(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    with session_factory() as session:
        row = session.get(WorkflowModel, workflow.id)
        row.owner_id = "kite:someone-else"
        session.commit()
    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/workflows/{workflow.id}/health").status_code == 404


# ---------------------------------------------------------------------------
# scope-aligned tokens
# ---------------------------------------------------------------------------


class _StubTokenRepo:
    def __init__(self):
        self.created = []
        self.tokens = []

    async def create_token(self, payload, *, raw_token, token_id):
        record = {
            "token_id": token_id,
            "name": payload.name,
            "account_scope": payload.account_scope,
            "allowed_actions": list(payload.allowed_actions),
            "allowed_modes": list(payload.allowed_modes),
            "status": "active",
            "created_at": NOW.isoformat(),
            "last_used_at": None,
            "expires_at": None,
        }
        self.created.append(payload)
        self.tokens.append(record)
        return record

    async def list_tokens(self):
        return list(self.tokens)

    async def revoke_token(self, token_id):
        for token in self.tokens:
            if token["token_id"] == token_id:
                token["status"] = "revoked"
                return token
        return None


def _app_with_tokens(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    repo = _StubTokenRepo()
    client.app.state.algo_worker_repository = repo
    return client, repo


def test_presets_are_served_with_their_exact_actions(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    body = client.get(f"{BASE}/tokens/presets").json()
    presets = {item["id"]: item["actions"] for item in body["presets"]}
    assert presets["alerts_authoring"] == ["workflows:read", "workflows:write", "workflows:activate"]
    assert presets["alerts_read_only"] == ["workflows:read"]
    assert presets["external_producers"] == ["signals:read", "signals:admin"]
    assert body["account_scope"] == OPERATOR_SCOPE
    assert body["modes"] == ["paper", "dry_run"]


def test_no_preset_grants_execution_actions(session_factory, monkeypatch):
    """The alerts surface must not be a route to an order-placing credential."""
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    body = client.get(f"{BASE}/tokens/presets").json()
    forbidden = {"intents:submit", "risk:update", "runs:create", "runs:exit"}
    for preset in body["presets"]:
        assert not (set(preset["actions"]) & forbidden), preset["id"]
    assert not (set(body["all_actions"]) & forbidden)
    assert "live" not in body["modes"]


def test_the_body_scope_is_ignored_and_the_authorized_scope_is_used(session_factory, monkeypatch):
    """A client cannot mint a token for another owner's alerts."""
    client, repo = _app_with_tokens(session_factory, monkeypatch)
    response = client.post(
        f"{BASE}/tokens",
        json={
            "label": "sneaky",
            "preset": "alerts_read_only",
            # Both spellings a client might try; neither may be honoured.
            "account_scope": "kite:someone-else",
            "scope": "kite:someone-else",
        },
    )
    assert response.status_code == 200
    assert response.json()["account_scope"] == OPERATOR_SCOPE
    assert repo.created[0].account_scope == OPERATOR_SCOPE


def test_an_execution_action_is_refused(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    response = client.post(
        f"{BASE}/tokens",
        json={"label": "orders", "allowed_actions": ["workflows:read", "intents:submit"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "unsupported_actions"
    assert detail["unsupported"] == ["intents:submit"]


def test_a_live_mode_token_is_refused(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    response = client.post(
        f"{BASE}/tokens",
        json={"label": "live-y", "preset": "alerts_read_only", "allowed_modes": ["live"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unsupported_modes"


def test_an_unknown_preset_is_refused(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    response = client.post(f"{BASE}/tokens", json={"label": "x", "preset": "everything"})
    assert response.status_code == 422


def test_a_label_is_required(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    response = client.post(f"{BASE}/tokens", json={"preset": "alerts_read_only"})
    assert response.status_code == 422


def test_token_creation_reveals_the_secret_once(session_factory, monkeypatch):
    client, _repo = _app_with_tokens(session_factory, monkeypatch)
    body = client.post(
        f"{BASE}/tokens", json={"label": "ops", "preset": "alerts_read_only"}
    ).json()
    assert body["token"].startswith("kwa_")
    assert body["reveal_once"] is True
    assert "cannot be shown again" in body["note"]
    # The list must not carry the secret back.
    listed = client.get(f"{BASE}/tokens").json()
    assert "token" not in listed["tokens"][0]


def test_token_list_flags_a_scope_mismatch(session_factory, monkeypatch):
    """A token pointed at another scope reads an empty view, so say so."""
    client, repo = _app_with_tokens(session_factory, monkeypatch)
    repo.tokens.append({
        "token_id": "worker_other", "name": "legacy", "account_scope": "kite:elsewhere",
        "allowed_actions": ["workflows:read"], "allowed_modes": ["paper"],
        "status": "active", "created_at": None, "last_used_at": None, "expires_at": None,
    })
    body = client.get(f"{BASE}/tokens").json()
    assert body["tokens"][0]["scope_matches_operator"] is False
    assert body["authorized_scopes"] == [OPERATOR_SCOPE]


def test_token_routes_require_a_session_and_same_origin(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch, authenticated=False)
    assert client.get(f"{BASE}/tokens").status_code == 401
    assert client.post(f"{BASE}/tokens", json={"label": "x", "preset": "alerts_read_only"}).status_code == 401

    authed = _app(session_factory, monkeypatch)
    refused = authed.post(
        f"{BASE}/tokens",
        json={"label": "x", "preset": "alerts_read_only"},
        headers={"Origin": "https://evil.example"},
    )
    assert refused.status_code == 403


# ---------------------------------------------------------------------------
# revision-addressed YAML
# ---------------------------------------------------------------------------


def test_yaml_can_address_a_specific_revision(session_factory, monkeypatch):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(LTP_DOCUMENT))
    workflow, first = repository.create_workflow(
        OPERATOR_SCOPE, "yaml-hist", compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    changed = compile_document(parse_workflow_dict({**LTP_DOCUMENT, "name": "ltp-watch-2"}))
    repository.add_draft_revision(
        workflow.id, changed.document.to_document_dict(), changed.canonical_hash
    )
    client = _app(session_factory, monkeypatch)

    rev1 = client.get(f"{BASE}/workflows/{workflow.id}/yaml?revision=1").json()
    rev2 = client.get(f"{BASE}/workflows/{workflow.id}/yaml?revision=2").json()
    assert rev1["revision"] == 1
    assert rev2["revision"] == 2
    assert rev1["canonical_hash"] != rev2["canonical_hash"]

    latest = client.get(f"{BASE}/workflows/{workflow.id}/yaml").json()
    assert latest["revision"] == 2, "the default is the newest revision"
    assert "name: ltp-watch-2" in latest["yaml"]


def test_yaml_round_trips_to_the_advertised_hash(session_factory, monkeypatch):
    from backend.workflows.parser import parse_workflow_yaml

    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}/yaml").json()
    assert (
        compile_document(parse_workflow_yaml(body["yaml"])).canonical_hash
        == body["canonical_hash"]
    )
    assert "one definition" in body["round_trip"]


def test_yaml_of_a_missing_revision_is_404(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/workflows/{workflow.id}/yaml?revision=99").status_code == 404
    assert client.get(f"{BASE}/workflows/nope/yaml").status_code == 404


# ---------------------------------------------------------------------------
# canvas layout over HTTP
# ---------------------------------------------------------------------------


def test_layout_round_trips_and_reports_the_contract(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)

    empty = client.get(f"{BASE}/workflows/{workflow.id}/layout").json()
    assert empty["nodes"] == []
    assert empty["contract"]["namespaces"] == ["stage", "alert", "channel"]
    assert empty["contract"]["node_id_format"] == "<namespace>:<id>"

    saved = client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [
            {"node_id": "stage:px", "x": 100.5, "y": 200.25},
            {"node_id": "alert:a1", "x": 300.0, "y": 200.25, "collapsed": True},
        ]},
    ).json()
    assert saved["ok"] is True
    assert saved["saved"] == 2
    assert "no revision was created" in saved["note"]

    listed = client.get(f"{BASE}/workflows/{workflow.id}/layout").json()["nodes"]
    by_id = {node["node_id"]: node for node in listed}
    assert by_id["stage:px"]["x"] == 100.5
    assert by_id["alert:a1"]["collapsed"] is True


def test_a_layout_write_creates_no_revision(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    before = client.get(f"{BASE}/workflows/{workflow.id}").json()["latest_revision"]
    client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [{"node_id": "stage:px", "x": 1.0, "y": 2.0}]},
    )
    after = client.get(f"{BASE}/workflows/{workflow.id}").json()["latest_revision"]
    assert after == before, "a cosmetic move must not touch the definition"


def test_a_bare_layout_node_id_is_refused_with_the_reason(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    response = client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [{"node_id": "px", "x": 1.0, "y": 2.0}]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_node"
    assert "namespaced" in detail["message"]


def test_an_empty_layout_write_is_refused(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    assert client.put(
        f"{BASE}/workflows/{workflow.id}/layout", json={"nodes": []}
    ).status_code == 422
    assert client.put(f"{BASE}/workflows/{workflow.id}/layout", json={}).status_code == 422


def test_layout_delete_is_explicit(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [
            {"node_id": "stage:px", "x": 1.0, "y": 1.0},
            {"node_id": "stage:gone", "x": 2.0, "y": 2.0},
        ]},
    )
    removed = client.post(
        f"{BASE}/workflows/{workflow.id}/layout/delete",
        json={"node_ids": ["stage:gone"]},
    ).json()
    assert removed["removed"] == 1
    remaining = [n["node_id"] for n in client.get(f"{BASE}/workflows/{workflow.id}/layout").json()["nodes"]]
    assert remaining == ["stage:px"]


def test_layout_is_owner_scoped_over_http(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    with session_factory() as session:
        row = session.get(WorkflowModel, workflow.id)
        row.owner_id = "kite:someone-else"
        session.commit()
    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/workflows/{workflow.id}/layout").status_code == 404
    assert client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [{"node_id": "stage:px", "x": 1.0, "y": 1.0}]},
    ).status_code == 404


def test_layout_mutations_enforce_same_origin(session_factory, monkeypatch):
    workflow, _revision = _activate(session_factory, LTP_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    response = client.put(
        f"{BASE}/workflows/{workflow.id}/layout",
        json={"nodes": [{"node_id": "stage:px", "x": 1.0, "y": 1.0}]},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
