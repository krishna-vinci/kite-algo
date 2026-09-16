"""Phase 6 6A.1 acceptance: the app-authenticated alerts operator API.

The browser has no worker token and must never be given one, so the operator
surface is a separate cookie-authenticated router. These tests pin the security
properties that make that safe, plus the read models the UI needs:

- cookie auth is required and a worker token is NOT accepted;
- the requested scope is a SELECTION, never an authority: the server allowlist
  decides, anything outside it is 403 even when it holds data;
- ``/scopes`` returns only authorized scopes;
- cross-owner ids are 404 (never 403-with-existence-leak);
- unsafe methods enforce a same-origin assertion (the SameSite=None gap);
- delivery history exposes attempt outcomes and provider acknowledgements;
- YAML round-trips to the same canonical hash;
- instrument search returns the catalog identity workflows need.
"""

from __future__ import annotations

import json

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test"
)

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.routers import alerts_operator as operator_router  # noqa: E402
from backend.api.services import alerts_operator as operator_service  # noqa: E402
from backend.api.services.csrf import enforce_same_origin  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.notifications.repository import (  # noqa: E402
    ChannelReference,
    Delivery,
    DeliveryAttempt,
)
from backend.workflows import advanced_repository as _advanced_repository  # noqa: E402,F401
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import (  # noqa: E402
    document_to_yaml,
    parse_workflow_dict,
    parse_workflow_yaml,
)
from backend.workflows.repository import (  # noqa: E402
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
    WorkflowRevision,
)

OPERATOR_SCOPE = "app:admin"
OTHER_SCOPE = "kite:paper-a"
BASE = "/api/alerts"
#: A scope that exists and may hold data, but is NOT in the allowlist.
OTHERS_NOT_AUTHORIZED = "kite:live-account"
T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

DOCUMENT = {
    "version": 1,
    "name": "operator-alert",
    "session": "nse_equity",
    "instruments": ["NSE:RELIANCE"],
    "stages": [
        {
            "id": "px",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "5minute",
            "conditions": {
                "all": [
                    {"left": {"field": "close"}, "op": "crosses_above",
                     "right": {"value": 3000}}
                ]
            },
        }
    ],
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
    """Authorize the operator scope plus one token scope for the picker tests."""
    monkeypatch.setenv(
        operator_service.ALERTS_OPERATOR_SCOPES_ENV,
        f"{OPERATOR_SCOPE},{OTHER_SCOPE}",
    )
    monkeypatch.delenv(operator_service.ALERTS_OPERATOR_OWNER_ENV, raising=False)
    yield


def _app(session_factory, *, authenticated=True, monkeypatch=None):
    """A TestClient over the real operator router and the REAL auth chain.

    ``require_app_user`` is called inside ``require_operator_scope`` rather than
    injected with ``Depends``, so a ``dependency_overrides`` entry would never
    take effect. The cookie-decoding seam is patched instead, which keeps the
    real ``require_app_user`` -> ``authorize_scope`` path under test — including
    the 401s and the 403s.
    """
    from backend.app import auth as auth_module

    user = AppUser(username="admin", role="admin") if authenticated else None
    if monkeypatch is not None:
        monkeypatch.setattr(
            auth_module, "get_optional_app_user", lambda _request: user
        )
    app = FastAPI()
    app.include_router(operator_router.router, prefix="/api")
    app.dependency_overrides[operator_router._alerts_db] = lambda: session_factory
    return TestClient(app)


def _activate(session_factory, workflow, revision):
    """Activate AND materialize subscriptions, as the activate route does."""
    from backend.workflows.service import EvaluationService

    repository = SqlAlchemyWorkflowRepository(session_factory)
    activated = repository.activate_revision(workflow.id, revision.id)
    EvaluationService(repository, session_factory).ensure_subscriptions(activated)
    return activated


def _seed_workflow(session_factory, owner, name="wf-1", document=None):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(
        parse_workflow_yaml(document) if isinstance(document, str)
        else parse_workflow_dict(document or DOCUMENT)
    )
    workflow, revision = repository.create_workflow(
        owner, name, compiled.document.to_document_dict(), compiled.canonical_hash
    )
    return workflow, revision


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


def test_every_route_requires_app_authentication(session_factory, monkeypatch):
    """No route may be reachable without a session."""
    client = _app(session_factory, authenticated=False, monkeypatch=monkeypatch)
    _seed_workflow(session_factory, OPERATOR_SCOPE)
    cases = [
        ("GET", f"{BASE}/scopes", None),
        ("GET", f"{BASE}/capabilities", None),
        ("GET", f"{BASE}/workflows", None),
        ("GET", f"{BASE}/channels", None),
        ("GET", f"{BASE}/instruments/search?q=REL", None),
        ("POST", f"{BASE}/workflows/validate", {"document": DOCUMENT}),
        ("POST", f"{BASE}/workflows", {"document": DOCUMENT}),
        ("POST", f"{BASE}/workflows/wf/activate", None),
        ("POST", f"{BASE}/workflows/wf/archive", None),
        ("POST", f"{BASE}/workflows/wf/pause", None),
        ("POST", f"{BASE}/workflows/wf/resume", None),
        ("GET", f"{BASE}/deliveries/x/attempts", None),
    ]
    for method, url, body in cases:
        response = client.request(method, url, json=body)
        assert response.status_code == 401, (
            f"{method} {url} returned {response.status_code} without a session"
        )


def test_capabilities_render_for_an_authorized_operator(session_factory, monkeypatch):
    """The authoring form renders from these capabilities, so the operator route
    must serve them to a BROWSER session.

    Regression: the route delegated to the worker handler, whose
    ``workflows:read`` dependency requires a worker bearer token, so an
    authenticated operator got 401 "Worker bearer token required" and the whole
    new/edit alert form showed "Could not load capabilities".
    """
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.get(f"{BASE}/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    capabilities = body["capabilities"]
    # The fields the form cannot work without.
    for key in ("operators", "timeframes", "limits"):
        assert key in capabilities, key
    assert "crosses_above" in capabilities["operators"]


def test_health_reports_the_newest_checkpoint_state(session_factory, monkeypatch):
    """The per-subscription freshness must come from the newest checkpoint.

    Regression: the route aggregated with ``func.max(state)``. ``state`` is
    JSONB, so PostgreSQL has no ``max(jsonb)`` and every workflow with
    subscriptions returned 500 in the deployed stack; on SQLite the aggregate
    "worked" but returned the lexically greatest JSON, i.e. not the newest row.
    """
    from backend.workflows.repository import AlertSubscription, EvaluationCheckpoint

    client = _app(session_factory, monkeypatch=monkeypatch)
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE)
    _activate(session_factory, workflow, revision)

    session = session_factory()
    subscription_id = session.execute(select(AlertSubscription.id)).scalars().first()
    session.add_all(
        [
            EvaluationCheckpoint(
                subscription_id=subscription_id,
                instrument_key="NSE:RELIANCE",
                epoch_id="epoch-old",
                # lexically GREATER than the newer row on purpose
                state={"last_tick_received_at": "2026-09-15T09:00:00+00:00", "z": "zzz"},
                owner_epoch=1,
                updated_at=T0,
            ),
            EvaluationCheckpoint(
                subscription_id=subscription_id,
                instrument_key="NSE:RELIANCE",
                epoch_id="epoch-new",
                state={"last_tick_received_at": "2026-09-15T09:30:00+00:00"},
                owner_epoch=2,
                updated_at=T0 + timedelta(minutes=30),
            ),
        ]
    )
    session.commit()
    session.close()

    body = client.get(f"{BASE}/workflows/{workflow.id}/health").json()
    assert body["ok"] is True
    rows = body["subscriptions"]
    assert len(rows) == 1
    assert rows[0]["last_tick_received_at"] == "2026-09-15T09:30:00+00:00"


def test_a_worker_token_is_not_accepted(session_factory, monkeypatch):
    """The operator surface is cookie-only; a bearer token must not open it."""
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.get(
        f"{BASE}/workflows", headers={"Authorization": "Bearer kwa_something"}
    )
    # The override authenticates regardless of headers, so assert the ROUTE does
    # not depend on the worker dependency at all.
    assert "require_worker_token" not in {
        dependency.call.__name__
        for route in operator_router.router.routes
        for dependency in getattr(route, "dependencies", []) or []
        if hasattr(dependency, "call")
    }
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# scope authorization
# ---------------------------------------------------------------------------


def test_scopes_lists_only_authorized_scopes(session_factory, monkeypatch):
    _seed_workflow(session_factory, OPERATOR_SCOPE)
    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/scopes").json()
    scopes = {item["scope"] for item in body["scopes"]}
    assert scopes == {OPERATOR_SCOPE, OTHER_SCOPE}
    assert OTHERS_NOT_AUTHORIZED not in scopes
    default = next(item for item in body["scopes"] if item["is_default"])
    assert default["scope"] == OPERATOR_SCOPE
    data_holders = {item["scope"] for item in body["scopes"] if item["has_data"]}
    assert data_holders == {OPERATOR_SCOPE}


def test_a_scope_outside_the_allowlist_is_403_even_with_data(session_factory, monkeypatch):
    """The client's scope is a preference; the allowlist is the authority."""
    _seed_workflow(session_factory, OTHERS_NOT_AUTHORIZED, name="secret-wf")
    client = _app(session_factory, monkeypatch=monkeypatch)

    response = client.get(f"{BASE}/workflows?scope={OTHERS_NOT_AUTHORIZED}")
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "not authorized" in detail
    # The refusal must not confirm that the scope holds anything.
    assert "secret-wf" not in response.text


def test_an_authorized_scope_is_honoured(session_factory, monkeypatch):
    _seed_workflow(session_factory, OTHER_SCOPE, name="token-wf")
    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/workflows?scope={OTHER_SCOPE}").json()
    assert [row["name"] for row in body["workflows"]] == ["token-wf"]
    assert body["scope"] == OTHER_SCOPE


def test_the_default_scope_is_used_when_none_is_requested(session_factory, monkeypatch):
    _seed_workflow(session_factory, OPERATOR_SCOPE, name="op-wf")
    _seed_workflow(session_factory, OTHER_SCOPE, name="token-wf")
    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/workflows").json()
    assert [row["name"] for row in body["workflows"]] == ["op-wf"]


def test_a_configured_default_must_be_authorized(session_factory, monkeypatch):
    """A misconfigured default must not silently widen access."""
    monkeypatch.setenv(operator_service.ALERTS_OPERATOR_OWNER_ENV, "kite:not-allowed")
    user = AppUser(username="admin", role="admin")
    assert operator_service.default_scope(user) == OPERATOR_SCOPE


def test_writes_use_the_authorized_scope_not_a_client_claim(session_factory, monkeypatch):
    """A body cannot smuggle a different owner."""
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.post(
        f"{BASE}/workflows?scope={OTHER_SCOPE}",
        json={"document": DOCUMENT, "name": "created-under-token-scope"},
    )
    assert response.status_code == 200
    with session_factory() as session:
        row = session.execute(
            select(WorkflowModel).where(WorkflowModel.name == "created-under-token-scope")
        ).scalar_one()
    assert row.owner_id == OTHER_SCOPE


# ---------------------------------------------------------------------------
# cross-owner isolation
# ---------------------------------------------------------------------------


def test_cross_owner_reads_are_404_not_403(session_factory, monkeypatch):
    """A foreign id must not confirm the row exists."""
    foreign, _revision = _seed_workflow(
        session_factory, OTHERS_NOT_AUTHORIZED, name="foreign-wf"
    )
    client = _app(session_factory, monkeypatch=monkeypatch)
    for url in (
        f"{BASE}/workflows/{foreign.id}",
        f"{BASE}/workflows/{foreign.id}/events",
        f"{BASE}/workflows/{foreign.id}/deliveries",
        f"{BASE}/workflows/{foreign.id}/export",
    ):
        response = client.get(url)
        assert response.status_code == 404, f"{url} leaked {response.status_code}"


def test_cross_owner_mutations_are_404(session_factory, monkeypatch):
    foreign, _revision = _seed_workflow(
        session_factory, OTHERS_NOT_AUTHORIZED, name="foreign-wf"
    )
    client = _app(session_factory, monkeypatch=monkeypatch)
    for url in (
        f"{BASE}/workflows/{foreign.id}/activate",
        f"{BASE}/workflows/{foreign.id}/archive",
        f"{BASE}/workflows/{foreign.id}/pause",
        f"{BASE}/workflows/{foreign.id}/resume",
    ):
        response = client.post(url)
        assert response.status_code == 404, f"{url} leaked {response.status_code}"


# ---------------------------------------------------------------------------
# CSRF / origin
# ---------------------------------------------------------------------------


def test_cross_origin_mutations_are_refused(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.post(
        f"{BASE}/workflows",
        json={"document": DOCUMENT},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"]


def test_same_origin_and_scripted_mutations_are_allowed(session_factory, monkeypatch):
    """The allowlist is configuration: pin it here rather than inheriting .env.

    (The deployed .env sets APP_ALLOWED_ORIGINS explicitly, and these tests load
    it through the app's dotenv import, so relying on the defaults would make the
    suite depend on the developer's machine.)
    """
    monkeypatch.setenv("APP_ALLOWED_ORIGINS", "http://localhost:3000")
    client = _app(session_factory, monkeypatch=monkeypatch)
    allowed = client.post(
        f"{BASE}/workflows",
        json={"document": DOCUMENT, "name": "same-origin"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert allowed.status_code == 200
    scripted = client.post(
        f"{BASE}/workflows",
        json={"document": DOCUMENT, "name": "no-origin"},
    )
    assert scripted.status_code == 200, "a scripted client sends no Origin"


def test_cors_allowlist_has_no_wildcard():
    from backend.app.config import get_allowed_cors_origins

    assert "*" not in get_allowed_cors_origins()


def test_origin_assertion_ignores_safe_methods(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.get(
        f"{BASE}/workflows", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 200, "a read is not a CSRF target here"


# ---------------------------------------------------------------------------
# enriched list / definition
# ---------------------------------------------------------------------------


def test_workflow_list_is_enriched(session_factory, monkeypatch):
    _seed_workflow(session_factory, OPERATOR_SCOPE, name="enriched")
    client = _app(session_factory, monkeypatch=monkeypatch)
    row = client.get(f"{BASE}/workflows").json()["workflows"][0]
    assert row["name"] == "enriched"
    assert row["kind"] == "alert"
    assert row["instruments"] == ["NSE:RELIANCE"]
    assert row["instrument_summary"] == "NSE:RELIANCE"
    assert row["alerts"] == [
        {"id": "a1", "source": "px", "trigger": "on_transition"}
    ]
    assert row["latest_revision"]["revision"] == 1
    assert row["warnings"] == []


def test_the_list_surfaces_a_never_fires_warning(session_factory, monkeypatch):
    """The operator sees the authoring mistake without opening the workflow."""
    level_only = {
        **DOCUMENT,
        "name": "level-only",
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [
                        {"left": {"field": "ltp"}, "op": "gte",
                         "right": {"value": 100}}
                    ]
                },
            }
        ],
    }
    _seed_workflow(session_factory, OPERATOR_SCOPE, name="level-only", document=level_only)
    client = _app(session_factory, monkeypatch=monkeypatch)
    row = client.get(f"{BASE}/workflows").json()["workflows"][0]
    codes = [warning["code"] for warning in row["warnings"]]
    assert codes == ["level_only_never_fires"]
    assert row["warnings"][0]["severity"] == "warning"


def test_detail_returns_a_readable_yaml_definition(session_factory, monkeypatch):
    workflow, _revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="yaml-wf")
    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}").json()
    assert body["yaml"]
    # It is the authoring shorthand, not the internal normalized operand form.
    assert "field: close" in body["yaml"]
    assert "kind: field" not in body["yaml"]
    # And it compiles back to the SAME canonical hash.
    assert (
        compile_document(parse_workflow_yaml(body["yaml"])).canonical_hash
        == body["latest_revision"]["canonical_hash"]
    )


# ---------------------------------------------------------------------------
# validate / preview are pure and carry warnings
# ---------------------------------------------------------------------------


def test_validate_reports_warnings_with_ok_true(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    level_only = {
        **DOCUMENT,
        "stages": [
            {
                "id": "px",
                "type": "signal",
                "clock": "ltp",
                "conditions": {
                    "all": [
                        {"left": {"field": "ltp"}, "op": "gte", "right": {"value": 1}}
                    ]
                },
            }
        ],
    }
    body = client.post(f"{BASE}/workflows/validate", json={"document": level_only}).json()
    assert body["ok"] is True
    assert body["issues"][0]["code"] == "level_only_never_fires"
    assert body["issues"][0]["severity"] == "warning"


def test_preview_persists_nothing(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.post(
        f"{BASE}/workflows/preview",
        json={
            "document": DOCUMENT,
            "observations": [
                {"instrument_key": "NSE:RELIANCE", "ts": T0.isoformat(), "close": 2900.0},
                {"instrument_key": "NSE:RELIANCE", "ts": (T0 + timedelta(minutes=5)).isoformat(), "close": 3100.0},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    with session_factory() as session:
        assert session.execute(select(WorkflowModel)).scalars().all() == []
        assert session.execute(select(SignalEvent)).scalars().all() == []
        assert session.execute(select(Delivery)).scalars().all() == []


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_create_then_activate_then_pause_resume_then_archive(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    created = client.post(
        f"{BASE}/workflows", json={"document": DOCUMENT, "name": "lifecycle"}
    ).json()
    workflow_id = created["workflow_id"]
    assert created["revision_status"] == "draft"

    activated = client.post(f"{BASE}/workflows/{workflow_id}/activate").json()
    assert activated["revision_status"] == "active"
    assert activated["subscriptions_created"] == 1
    assert "silent" in activated["note"]

    assert client.post(f"{BASE}/workflows/{workflow_id}/pause").json()["state"] == "paused"
    assert client.post(f"{BASE}/workflows/{workflow_id}/resume").json()["state"] == "active"

    archived = client.post(f"{BASE}/workflows/{workflow_id}/archive").json()
    assert archived["archived"] is True
    assert archived["revisions_archived"] >= 1
    # Archived rows leave the default list but remain retrievable by id.
    assert client.get(f"{BASE}/workflows").json()["workflows"] == []
    assert client.get(f"{BASE}/workflows?include_archived=true").json()["workflows"]
    assert client.get(f"{BASE}/workflows/{workflow_id}").status_code == 200


def test_pause_returns_the_revision_with_expiring_sessions(monkeypatch):
    """Pause/resume must work when the session expires attributes on commit.

    Regression (live only): the app's SessionLocal uses the default
    ``expire_on_commit=True`` while every test factory disables it. ``_set_state``
    read ``active.revision`` after the session block had closed, so Pause
    returned 500 with DetachedInstanceError in the deployed stack.
    """
    from backend.workflows.repository import AlertSubscription  # noqa: F401

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    expiring = sessionmaker(bind=engine, expire_on_commit=True)  # production shape

    client = _app(expiring, monkeypatch=monkeypatch)
    created = client.post(
        f"{BASE}/workflows", json={"document": DOCUMENT, "name": "expiring"}
    ).json()
    workflow_id = created["workflow_id"]
    client.post(f"{BASE}/workflows/{workflow_id}/activate")

    paused = client.post(f"{BASE}/workflows/{workflow_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "paused"
    assert paused.json()["revision"] == 1
    resumed = client.post(f"{BASE}/workflows/{workflow_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "active"
    engine.dispose()


def test_revision_conflict_is_409_with_a_recoverable_shape(session_factory, monkeypatch):
    workflow, _revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="conflict")
    client = _app(session_factory, monkeypatch=monkeypatch)
    changed = {**DOCUMENT, "name": "conflict-v2"}
    response = client.patch(
        f"{BASE}/workflows/{workflow.id}",
        json={"document": changed, "expected_revision": 99},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["rejection_reason"] == "REVISION_CONFLICT"


def test_activation_refuses_an_invalid_stored_revision(session_factory, monkeypatch):
    """The stored document is re-validated, so junk can never activate."""
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="bad-store")
    with session_factory() as session:
        row = session.get(WorkflowRevision, revision.id)
        # An alert whose `source` names a stage that does not exist: a structural
        # error the compiler rejects, so this revision is invalid as STORED.
        row.document = {
            "version": 1, "name": "broken", "session": "nse_equity",
            "instruments": ["NSE:RELIANCE"],
            "stages": [{
                "id": "px", "type": "signal", "clock": "ltp",
                "conditions": {"all": [
                    {"left": {"field": "ltp"}, "op": "crosses_above",
                     "right": {"value": 1}}
                ]},
            }],
            "alerts": [{"id": "a1", "source": "does-not-exist", "trigger": "once"}],
        }
        session.commit()
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.post(f"{BASE}/workflows/{workflow.id}/activate")
    assert response.status_code == 409
    assert response.json()["detail"]["ok"] is False
    with session_factory() as session:
        stored = session.get(WorkflowRevision, revision.id)
        assert stored.status == "draft", "an invalid revision must not activate"


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------


def _seed_delivery_with_attempts(session_factory, workflow_id, revision_id):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    subscriptions = repository.list_active_subscriptions()
    with session_factory() as session:
        channel = ChannelReference(
            id="chan-1", owner_id=OPERATOR_SCOPE, name="ops-ntfy", provider="ntfy",
            destination={"url_env": "NTFY_URL"}, enabled=True,
        )
        session.add(channel)
        event = SignalEvent(
            id="evt-1", subscription_id=subscriptions[0].id if subscriptions else None,
            workflow_id=workflow_id, occurrence_key="occ-1", fired_at=T0,
            evidence={}, created_at=T0,
        )
        session.add(event)
        delivery = Delivery(
            id="del-1", event_id="evt-1", channel_id="chan-1", status="failed",
            attempts=3, created_at=T0, updated_at=T0,
            last_error="max attempts exceeded",
        )
        session.add(delivery)
        session.add_all([
            DeliveryAttempt(
                delivery_id="del-1", attempt_no=1, outcome="retryable",
                detail="provider said slow", provider_id=None, created_at=T0,
            ),
            DeliveryAttempt(
                delivery_id="del-1", attempt_no=2, outcome="unknown",
                detail="timed out", provider_id=None, created_at=T0,
            ),
            DeliveryAttempt(
                delivery_id="del-1", attempt_no=3, outcome="permanent",
                detail="400 bad request", provider_id="msg-xyz", created_at=T0,
            ),
        ])
        session.commit()


def test_delivery_history_exposes_attempts_and_provider_outcomes(session_factory, monkeypatch):
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="deliveries")
    repository = SqlAlchemyWorkflowRepository(session_factory)
    repository.activate_revision(workflow.id, revision.id)
    _seed_delivery_with_attempts(session_factory, workflow.id, revision.id)

    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}/deliveries").json()
    assert len(body["deliveries"]) == 1
    delivery = body["deliveries"][0]
    assert delivery["status"] == "failed"
    assert delivery["attempts"] == 3
    assert delivery["last_error"] == "max attempts exceeded"
    assert delivery["channel_name"] == "ops-ntfy"
    assert [a["outcome"] for a in delivery["attempt_log"]] == [
        "retryable", "unknown", "permanent"
    ]
    assert delivery["attempt_log"][2]["provider_id"] == "msg-xyz"
    # Provider acceptance is explicitly NOT human receipt.
    assert "PROVIDER ACCEPTED" in body["note"]
    assert "not confirmation that a human read it" in body["note"]


def test_delivery_attempts_are_reachable_by_delivery_id(session_factory, monkeypatch):
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="attempts")
    repository = SqlAlchemyWorkflowRepository(session_factory)
    repository.activate_revision(workflow.id, revision.id)
    _seed_delivery_with_attempts(session_factory, workflow.id, revision.id)

    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/deliveries/del-1/attempts").json()
    assert [a["attempt_no"] for a in body["attempts"]] == [1, 2, 3]
    assert body["attempts"][2]["provider_id"] == "msg-xyz"


def test_delivery_status_filter(session_factory, monkeypatch):
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="filter")
    repository = SqlAlchemyWorkflowRepository(session_factory)
    repository.activate_revision(workflow.id, revision.id)
    _seed_delivery_with_attempts(session_factory, workflow.id, revision.id)
    client = _app(session_factory, monkeypatch=monkeypatch)
    assert client.get(f"{BASE}/workflows/{workflow.id}/deliveries?status=delivered").json()["deliveries"] == []
    assert len(client.get(f"{BASE}/workflows/{workflow.id}/deliveries?status=failed").json()["deliveries"]) == 1


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def test_channels_never_expose_a_secret_value(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    created = client.post(
        f"{BASE}/channels",
        json={
            "name": "ops-ntfy",
            "provider": "ntfy",
            "destination": {"url_env": "NTFY_PRIMARY_URL"},
            "secret_env": "NTFY_PRIMARY_URL",
        },
    ).json()
    assert created["ok"] is True
    listed = client.get(f"{BASE}/channels").json()
    channel = listed["channels"][0]
    # The env-var NAME is returned (it is not a secret); no VALUE ever is.
    assert channel["secret_env"] == "NTFY_PRIMARY_URL"
    assert "NTFY_URL_VALUE" not in listed.__str__()
    assert "secret VALUE is never" in listed["note"]


def test_channel_requires_a_supported_provider(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    response = client.post(
        f"{BASE}/channels",
        json={"name": "slack", "provider": "slack", "destination": {}},
    )
    assert response.status_code == 422
    assert "supported: telegram, ntfy" in response.json()["detail"]


def test_channel_test_send_is_explicit_and_reports_a_missing_secret(session_factory, monkeypatch):
    """A test send is a deliberate action; a missing env var names itself."""
    client = _app(session_factory, monkeypatch=monkeypatch)
    client.post(
        f"{BASE}/channels",
        json={
            "name": "ops-ntfy",
            "provider": "ntfy",
            "destination": {"url_env": "NTFY_PRIMARY_URL"},
            "secret_env": "DEFINITELY_UNSET_VAR_FOR_TEST",
        },
    )
    channel_id = client.get(f"{BASE}/channels").json()["channels"][0]["channel_id"]
    response = client.post(f"{BASE}/channels/{channel_id}/test")
    assert response.status_code == 400
    body = response.json()["detail"]
    assert body["error"] == "missing_env_secret"
    assert body["secret_env"] == "DEFINITELY_UNSET_VAR_FOR_TEST"


# ---------------------------------------------------------------------------
# instrument search
# ---------------------------------------------------------------------------


def test_instrument_search_returns_the_catalog_identity(session_factory, monkeypatch):
    """The UI needs public_key; the legacy fuzzy-search cannot provide it."""
    from backend.broker_api.instruments import catalog as catalog_module

    class _Descriptor:
        public_key = "MCX:GOLD26OCTFUT"
        tradingsymbol = "GOLD26OCTFUT"
        exchange = "MCX"
        segment = "MCX-FUT"
        name = "GOLD"
        instrument_type = "FUT"
        expiry = datetime(2026, 10, 26).date()
        strike = None
        option_type = None
        underlying = None
        lot_size = 100
        lifecycle_status = "active"

    class _Catalog:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, query, *, exchange=None, segment=None, limit=20):
            return [_Descriptor()]

    monkeypatch.setattr(catalog_module, "InstrumentCatalog", _Catalog)
    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/instruments/search?q=GOLD").json()
    result = body["results"][0]
    assert result["public_key"] == "MCX:GOLD26OCTFUT"
    assert result["expiry"] == "2026-10-26"
    assert result["lifecycle_status"] == "active"
    assert "broker_token" not in result
    assert "deliberately not exposed" in body["note"]


# ---------------------------------------------------------------------------
# YAML round trip (the contract the canvas will depend on)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        DOCUMENT,
        {
            "version": 1, "name": "rich", "session": "nse_equity",
            "instruments": ["NSE:A", "NSE:B"],
            "stages": [
                {
                    "id": "px", "type": "signal", "clock": "candle_close",
                    "timeframe": "15minute",
                    "conditions": {
                        "all": [
                            {"left": {"field": "close"}, "op": "crosses_above",
                             "right": {"value": 100}},
                            {"left": {"indicator": "rsi", "period": 14},
                             "op": "lt", "right": {"value": 70}},
                        ],
                        "any": [
                            {"left": {"field": "volume"}, "op": "gt",
                             "right": {"indicator": "sma", "period": 20,
                                       "source": "volume"}},
                        ],
                    },
                },
                {
                    "id": "seq", "type": "signal", "clock": "candle_close",
                    "timeframe": "15minute",
                    "sequence": {
                        "first": {"all": [{"left": {"field": "close"}, "op": "gt",
                                           "right": {"value": 1}}]},
                        "then": {"all": [{"left": {"field": "close"}, "op": "lt",
                                          "right": {"value": 1}}]},
                        "within_bars": 5,
                    },
                },
            ],
            "alerts": [
                {"id": "a1", "source": "px", "trigger": "reminder",
                 "reminder_interval_s": 60, "channels": ["c1", "c2"]},
                {"id": "a2", "source": "seq", "trigger": "on_transition",
                 "max_per_session": 3},
            ],
        },
    ],
)
def test_yaml_round_trips_to_the_same_canonical_hash(document):
    document_obj = parse_workflow_dict(document)
    text = document_to_yaml(document_obj)
    reparsed = parse_workflow_yaml(text)
    assert (
        compile_document(reparsed).canonical_hash
        == compile_document(document_obj).canonical_hash
    )


# ---------------------------------------------------------------------------
# list freshness
# ---------------------------------------------------------------------------


def test_the_list_always_carries_a_populated_freshness_block(session_factory, monkeypatch):
    """A null freshness is indistinguishable from 'not computed'.

    The list must never report null for freshness: an operator reading the list
    has to be able to tell "evaluated recently" from "we did not look".
    """
    _seed_workflow(session_factory, OPERATOR_SCOPE, name="no-subs")
    client = _app(session_factory, monkeypatch=monkeypatch)
    row = client.get(f"{BASE}/workflows").json()["workflows"][0]
    freshness = row["freshness"]
    assert freshness is not None
    assert set(freshness) >= {
        "last_evaluated_at",
        "evaluation_age_s",
        "subscription_count",
        "stale_subscriptions",
        "stale",
        "stale_after_seconds",
    }


def test_a_workflow_with_no_subscriptions_reports_unknown_not_fresh(session_factory, monkeypatch):
    """Nothing has been evaluated, so 'not stale' would be a claim about nothing."""
    _seed_workflow(session_factory, OPERATOR_SCOPE, name="draft-only")
    client = _app(session_factory, monkeypatch=monkeypatch)
    freshness = client.get(f"{BASE}/workflows").json()["workflows"][0]["freshness"]
    assert freshness["subscription_count"] == 0
    assert freshness["stale"] is None, "unknown must not read as 'fresh'"
    assert freshness["last_evaluated_at"] is None


def test_the_list_marks_a_workflow_stale_from_checkpoint_age(session_factory, monkeypatch):
    """Recency comes from the checkpoint, which advances on every evaluation."""
    from datetime import datetime, timedelta, timezone as _tz

    from sqlalchemy import select as _select

    from backend.workflows.repository import AlertSubscription, EvaluationCheckpoint

    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="stale-one")
    _activate(session_factory, workflow, revision)
    with session_factory() as session:
        subscription = session.execute(
            _select(AlertSubscription).where(AlertSubscription.revision_id == revision.id)
        ).scalars().first()
        old = datetime.now(_tz.utc) - timedelta(seconds=900)
        session.add(
            EvaluationCheckpoint(
                subscription_id=subscription.id,
                instrument_key=subscription.instrument_key,
                epoch_id="e1",
                state={"last_tick_received_at": old.isoformat()},
                owner_epoch=1,
                updated_at=old,
            )
        )
        session.commit()

    client = _app(session_factory, monkeypatch=monkeypatch)
    freshness = client.get(f"{BASE}/workflows").json()["workflows"][0]["freshness"]
    assert freshness["subscription_count"] == 1
    assert freshness["stale"] is True
    assert freshness["stale_subscriptions"] == 1
    assert freshness["evaluation_age_s"] >= 800
    assert freshness["last_evaluated_at"] is not None


def test_a_recent_evaluation_is_not_stale(session_factory, monkeypatch):
    from datetime import datetime, timezone as _tz

    from sqlalchemy import select as _select

    from backend.workflows.repository import AlertSubscription, EvaluationCheckpoint

    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="fresh-one")
    _activate(session_factory, workflow, revision)
    with session_factory() as session:
        subscription = session.execute(
            _select(AlertSubscription).where(AlertSubscription.revision_id == revision.id)
        ).scalars().first()
        session.add(
            EvaluationCheckpoint(
                subscription_id=subscription.id,
                instrument_key=subscription.instrument_key,
                epoch_id="e1",
                state={},
                owner_epoch=1,
                updated_at=datetime.now(_tz.utc),
            )
        )
        session.commit()

    client = _app(session_factory, monkeypatch=monkeypatch)
    freshness = client.get(f"{BASE}/workflows").json()["workflows"][0]["freshness"]
    assert freshness["stale"] is False
    assert freshness["stale_subscriptions"] == 0


def test_freshness_is_computed_per_workflow_not_shared(session_factory, monkeypatch):
    """One workflow's staleness must not leak onto another's row."""
    from datetime import datetime, timedelta, timezone as _tz

    from sqlalchemy import select as _select

    from backend.workflows.repository import AlertSubscription, EvaluationCheckpoint

    stale_wf, stale_rev = _seed_workflow(session_factory, OPERATOR_SCOPE, name="wf-stale")
    fresh_wf, fresh_rev = _seed_workflow(session_factory, OPERATOR_SCOPE, name="wf-fresh")
    _activate(session_factory, stale_wf, stale_rev)
    _activate(session_factory, fresh_wf, fresh_rev)
    with session_factory() as session:
        for revision, moment in (
            (stale_rev, datetime.now(_tz.utc) - timedelta(seconds=900)),
            (fresh_rev, datetime.now(_tz.utc)),
        ):
            subscription = session.execute(
                _select(AlertSubscription).where(AlertSubscription.revision_id == revision.id)
            ).scalars().first()
            session.add(
                EvaluationCheckpoint(
                    subscription_id=subscription.id,
                    instrument_key=subscription.instrument_key,
                    epoch_id="e1",
                    state={},
                    owner_epoch=1,
                    updated_at=moment,
                )
            )
        session.commit()

    client = _app(session_factory, monkeypatch=monkeypatch)
    by_name = {
        row["name"]: row["freshness"]
        for row in client.get(f"{BASE}/workflows").json()["workflows"]
    }
    assert by_name["wf-stale"]["stale"] is True
    assert by_name["wf-fresh"]["stale"] is False


def test_a_just_activated_workflow_reports_unknown_freshness(session_factory, monkeypatch):
    """Subscriptions exist but nothing has evaluated them yet.

    This is the realistic "I just activated it" state. It must read as UNKNOWN,
    not as fresh: reporting `stale: false` here would tell the operator the feed
    is fine when no observation has ever been processed.
    """
    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="just-activated")
    _activate(session_factory, workflow, revision)

    client = _app(session_factory, monkeypatch=monkeypatch)
    freshness = client.get(f"{BASE}/workflows").json()["workflows"][0]["freshness"]
    assert freshness["subscription_count"] == 1, "subscriptions were materialized"
    assert freshness["last_evaluated_at"] is None
    assert freshness["stale"] is None, "never evaluated must not read as fresh"


def test_workflow_health_exposes_durable_suppression_counters(session_factory, monkeypatch):
    """Suppressions are persisted, so they are visible without the worker file.

    The handoff recorded suppression reasons as runtime-only. The session cap is
    in fact a DURABLE per-reason counter, and the operator endpoint now exposes
    it: it must be readable even though no worker health file is mounted here.
    """
    from backend.workflows.advanced_repository import record_suppression

    workflow, revision = _seed_workflow(session_factory, OPERATOR_SCOPE, name="suppressed")
    session = session_factory()
    try:
        record_suppression(
            session,
            owner_id=OPERATOR_SCOPE,
            workflow_id=workflow.id,
            revision_id=revision.id,
            alert_id="a1",
            session_id="2026-09-11",
            reason="session_cap",
        )
        session.commit()
    finally:
        session.close()

    client = _app(session_factory, monkeypatch=monkeypatch)
    body = client.get(f"{BASE}/workflows/{workflow.id}/health").json()
    assert body["suppressions"] == {"session_cap": 1}
    assert body["runtime"]["available"] is False, "no worker health file in this environment"
    assert "DURABLE" in body["note"]


def test_paused_workflow_reports_its_effective_lifecycle(session_factory, monkeypatch):
    """Pause must be visible in the detail payload.

    Regression: Pause sets the SUBSCRIPTION state to paused while the revision
    stays active, so `active_revision.status` still read "active" and the UI
    badge said ACTIVE for a workflow the operator had just paused.
    """
    client = _app(session_factory, monkeypatch=monkeypatch)
    created = client.post(
        f"{BASE}/workflows", json={"document": DOCUMENT, "name": "lifecycle-visible"}
    ).json()
    workflow_id = created["workflow_id"]
    client.post(f"{BASE}/workflows/{workflow_id}/activate")

    active = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert active["lifecycle_state"] == "active"

    client.post(f"{BASE}/workflows/{workflow_id}/pause")
    paused = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert paused["lifecycle_state"] == "paused"
    # the revision itself is still active (that is what makes Resume cheap)
    assert (paused["active_revision"] or {}).get("status") == "active"

    client.post(f"{BASE}/workflows/{workflow_id}/resume")
    resumed = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert resumed["lifecycle_state"] == "active"

    client.post(f"{BASE}/workflows/{workflow_id}/archive")
    archived = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert archived["lifecycle_state"] == "archived"


def test_draft_workflow_reports_draft_lifecycle(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    created = client.post(
        f"{BASE}/workflows", json={"document": DOCUMENT, "name": "not-activated"}
    ).json()
    body = client.get(f"{BASE}/workflows/{created['workflow_id']}").json()
    assert body["lifecycle_state"] == "draft"


def test_list_row_carries_a_plain_rule_for_a_simple_alert(session_factory, monkeypatch):
    """The list needs the rule without opening each alert.

    Only a simple field-vs-constant comparison is described: a wrong one-line
    rule would be worse than none, so anything else reports null and the UI falls
    back to the instrument summary.
    """
    client = _app(session_factory, monkeypatch=monkeypatch)
    document = json.loads(json.dumps(DOCUMENT))
    document["name"] = "rule-probe"
    document["instruments"] = ["NSE:RELIANCE"]
    document["stages"][0]["clock"] = "ltp"
    document["stages"][0]["timeframe"] = "day"
    document["stages"][0]["conditions"]["all"] = [
        {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 2950.5}}
    ]
    created = client.post(f"{BASE}/workflows", json={"document": document}).json()

    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    row = next(item for item in rows if item["workflow_id"] == created["workflow_id"])
    assert row["rule"] == {
        "field": "ltp",
        "operator": "crosses_above",
        "value": 2950.5,
        "clock": "ltp",
        "timeframe": "day",
    }


def test_rule_summary_is_null_for_a_rule_it_cannot_describe(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch=monkeypatch)
    document = json.loads(json.dumps(DOCUMENT))
    document["name"] = "complex-probe"
    document["stages"][0]["conditions"]["all"] = [
        {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 1}},
        {"left": {"field": "ltp"}, "op": "lt", "right": {"value": 99}},
    ]
    created = client.post(f"{BASE}/workflows", json={"document": document}).json()

    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    row = next(item for item in rows if item["workflow_id"] == created["workflow_id"])
    assert row["rule"] is None


def test_list_rows_report_the_effective_lifecycle(session_factory, monkeypatch):
    """A paused workflow must not read as active in the list.

    The list drives Pause/Resume, so showing "active" (the revision status) for a
    workflow whose subscriptions are paused would offer the wrong action — the
    same defect the detail endpoint had.
    """
    client = _app(session_factory, monkeypatch=monkeypatch)
    document = json.loads(json.dumps(DOCUMENT))
    document["name"] = "list-lifecycle"
    created = client.post(f"{BASE}/workflows", json={"document": document}).json()
    workflow_id = created["workflow_id"]

    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    row = next(item for item in rows if item["workflow_id"] == workflow_id)
    assert row["lifecycle_state"] == "draft"

    client.post(f"{BASE}/workflows/{workflow_id}/activate")
    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    assert next(item for item in rows if item["workflow_id"] == workflow_id)["lifecycle_state"] == "active"

    client.post(f"{BASE}/workflows/{workflow_id}/pause")
    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    assert next(item for item in rows if item["workflow_id"] == workflow_id)["lifecycle_state"] == "paused"

    client.post(f"{BASE}/workflows/{workflow_id}/resume")
    rows = client.get(f"{BASE}/workflows").json()["workflows"]
    assert next(item for item in rows if item["workflow_id"] == workflow_id)["lifecycle_state"] == "active"

    client.post(f"{BASE}/workflows/{workflow_id}/archive")
    rows = client.get(f"{BASE}/workflows", params={"include_archived": "true"}).json()["workflows"]
    assert next(item for item in rows if item["workflow_id"] == workflow_id)["lifecycle_state"] == "archived"


def test_detail_reports_real_subscription_count_and_freshness(session_factory, monkeypatch):
    """The detail page's state depends on this.

    Regression: `_enrich_workflow` is a LIST placeholder and starts at zero, and
    the detail route never refined it, so an activated alert with a live
    subscription read as "never evaluated" forever on its own page.
    """
    client = _app(session_factory, monkeypatch=monkeypatch)
    document = json.loads(json.dumps(DOCUMENT))
    document["name"] = "detail-counts"
    created = client.post(f"{BASE}/workflows", json={"document": document}).json()
    workflow_id = created["workflow_id"]
    client.post(f"{BASE}/workflows/{workflow_id}/activate")

    detail = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert detail["subscription_count"] >= 1
    assert detail["freshness"]["subscription_count"] == detail["subscription_count"]
    # never evaluated yet is a real answer, not a placeholder zero
    assert detail["freshness"]["last_evaluated_at"] is None
