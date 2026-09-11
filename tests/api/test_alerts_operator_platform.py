"""Phase 6 6A.3/6A.4 backend: operator universes, screeners and producers.

The recurring properties, asserted per surface rather than once:

- every route requires a cookie session (401) and is owner-scoped (404 for a
  foreign id, never 403-with-existence-leak);
- every mutation asserts same origin;
- the operator surface does not expose the routes that were deliberately
  excluded — producer VALUE submission (a credential-only operation) and
  `signals/health?purge` (a GET that deletes).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

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

from backend.api.routers import alerts_operator_platform as platform_router  # noqa: E402
from backend.api.services import alerts_operator as operator_service  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    Base,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
)
from backend.workflows.service import EvaluationService  # noqa: E402
from backend.workflows.universes import UniverseService  # noqa: E402
from backend.broker_api.instruments.catalog import (  # noqa: E402
    CatalogUnavailableError,
    InstrumentNotFoundError,
)


class _FakeCatalog:
    """Identity-only stand-in for InstrumentCatalog.resolve_public_key."""

    def __init__(self, keys=("NSE:RELIANCE", "NSE:TCS"), generation="generation-1"):
        self._keys = {str(key).upper() for key in keys}
        self.generation = generation

    def resolve_public_key(self, key):
        cleaned = str(key).upper()
        if cleaned not in self._keys:
            raise InstrumentNotFoundError(f"instrument not found: {key}")
        return SimpleNamespace(
            public_key=cleaned,
            lifecycle_status="active",
            catalog_generation=self.generation,
        )

    def health(self):
        return {"status": "published", "generation": self.generation}


def _unavailable_catalog():
    class _Broken:
        def resolve_public_key(self, key):
            raise CatalogUnavailableError("catalog database down")

        def health(self):
            return {"status": "unavailable"}

    return _Broken()

OPERATOR_SCOPE = "app:admin"
FOREIGN_SCOPE = "kite:someone-else"
BASE = "/api/alerts"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

SCREENER_DOCUMENT = {
    "version": 1,
    "name": "momentum-screen",
    "session": "nse_equity",
    "instruments": ["NSE:RELIANCE", "NSE:TCS"],
    "stages": [{
        "id": "f1", "type": "filter", "clock": "candle_close", "timeframe": "day",
        "conditions": {"all": [
            {"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}
        ]},
    }],
    "alerts": [],
    # Attachments live INSIDE the screener block, matching the document schema.
    "screener": {
        "rank": {"by": {"field": "close"}, "direction": "desc"},
        "top_n": 10,
        "schedule": {"every": "1d"},
        "attachments": [{
            "id": "entry",
            "trigger": "entry",
            "entry_rank": 10,
            "exit_rank": 25,
            "exit_after": 3,
            "channels": ["ops-ntfy"],
        }],
    },
}

PLAIN_DOCUMENT = {
    "version": 1, "name": "plain", "session": "nse_equity",
    "instruments": ["NSE:RELIANCE"],
    "stages": [{
        "id": "px", "type": "signal", "clock": "ltp",
        "conditions": {"all": [
            {"left": {"field": "ltp"}, "op": "crosses_above", "right": {"value": 1}}
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
    yield


def _app(session_factory, monkeypatch, *, authenticated=True, catalog=None):
    from backend.app import auth as auth_module

    user = AppUser(username="admin", role="admin") if authenticated else None
    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(platform_router.router, prefix="/api")
    app.dependency_overrides[platform_router._alerts_db] = lambda: session_factory
    # The screener helpers resolve their sessionmaker from app.state (not via
    # Depends), so it must be set here too or they fall back to the real database.
    app.state.alerts_session_factory = session_factory
    app.state.universe_service = UniverseService(
        session_factory, catalog=catalog if catalog is not None else _FakeCatalog()
    )
    return TestClient(app)


def _seed_workflow(session_factory, document, owner=OPERATOR_SCOPE, name=None):
    repository = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(document))
    workflow, revision = repository.create_workflow(
        owner, name or document["name"], compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    activated = repository.activate_revision(workflow.id, revision.id)
    EvaluationService(repository, session_factory).ensure_subscriptions(activated)
    return workflow, revision


# ---------------------------------------------------------------------------
# authentication and origin, swept over the whole surface
# ---------------------------------------------------------------------------

MUTATIONS = [
    ("POST", f"{BASE}/universes", {"name": "u", "kind": "explicit", "source_config": {"members": ["NSE:RELIANCE"]}}),
    ("POST", f"{BASE}/universes/preview", {"kind": "explicit", "source_config": {}}),
    ("POST", f"{BASE}/universes/u/resolve", None),
    ("POST", f"{BASE}/screeners/w/runs", None),
    ("POST", f"{BASE}/screeners/preview", {"document": PLAIN_DOCUMENT}),
    ("POST", f"{BASE}/signals/producers", {"name": "p"}),
    ("POST", f"{BASE}/signals/producers/p/revoke", None),
    ("POST", f"{BASE}/signals/producers/p/credentials", None),
    ("POST", f"{BASE}/signals/producers/p/credentials/t/revoke", None),
]

READS = [
    f"{BASE}/universes",
    f"{BASE}/universes/u",
    f"{BASE}/universes/u/revisions",
    f"{BASE}/screeners/w/runs",
    f"{BASE}/screeners/w/events",
    f"{BASE}/screeners/w/attachments",
    f"{BASE}/screener-runs/r",
    f"{BASE}/signals/producers",
    f"{BASE}/signals/producers/p",
    f"{BASE}/signals/values?producer=p",
    f"{BASE}/signals/health",
]


@pytest.mark.parametrize("method,url,body", MUTATIONS)
def test_mutations_require_a_session(session_factory, monkeypatch, method, url, body):
    client = _app(session_factory, monkeypatch, authenticated=False)
    assert client.request(method, url, json=body).status_code == 401


@pytest.mark.parametrize("url", READS)
def test_reads_require_a_session(session_factory, monkeypatch, url):
    client = _app(session_factory, monkeypatch, authenticated=False)
    assert client.get(url).status_code == 401


@pytest.mark.parametrize("method,url,body", MUTATIONS)
def test_mutations_refuse_a_cross_origin_request(session_factory, monkeypatch, method, url, body):
    """Cookie-authenticated mutations must assert same origin (SameSite=None)."""
    client = _app(session_factory, monkeypatch)
    response = client.request(
        method, url, json=body, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403, f"{method} {url} allowed a foreign origin"


def test_the_operator_surface_does_not_offer_value_submission(session_factory, monkeypatch):
    """Submitting a value needs a producer credential, not a browser session."""
    client = _app(session_factory, monkeypatch)
    assert client.post(f"{BASE}/signals/values", json={}).status_code in (404, 405)


def test_signals_health_never_purges(session_factory, monkeypatch):
    """A GET that deletes rows must not exist on the browser surface."""
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/signals/health").json()
    assert body["purge_available"] is False
    # A purge parameter is accepted-and-ignored at worst; it must not delete.
    assert client.get(f"{BASE}/signals/health?purge=true").json()["purge_available"] is False


# ---------------------------------------------------------------------------
# universes
# ---------------------------------------------------------------------------


def test_universe_lifecycle_is_owner_scoped(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    created = client.post(
        f"{BASE}/universes",
        json={
            "name": "majors",
            "kind": "explicit",
            "source_config": {"members": ["NSE:RELIANCE", "NSE:TCS"]},
        },
    ).json()
    assert created["name"] == "majors"
    assert created["kind"] == "explicit"

    listed = client.get(f"{BASE}/universes").json()["universes"]
    assert [u["name"] for u in listed] == ["majors"]

    detail = client.get(f"{BASE}/universes/majors").json()
    assert sorted(detail["source_config"]["members"]) == ["NSE:RELIANCE", "NSE:TCS"]

    resolved = client.post(f"{BASE}/universes/majors/resolve").json()
    assert sorted(resolved["members"]) == ["NSE:RELIANCE", "NSE:TCS"]
    assert resolved["revision"] >= 1

    revisions = client.get(f"{BASE}/universes/majors/revisions").json()
    assert len(revisions["revisions"]) >= 1


def test_a_foreign_universe_is_404_even_though_it_exists(session_factory, monkeypatch):
    """The universe belongs to another scope; the operator must not see it."""
    with session_factory() as session:
        pass
    UniverseService(session_factory).create_universe(
        FOREIGN_SCOPE, "foreign", "explicit", {"members": ["NSE:INFY"]}
    )
    client = _app(session_factory, monkeypatch)
    for url in (f"{BASE}/universes/foreign", f"{BASE}/universes/foreign/revisions"):
        assert client.get(url).status_code == 404, url
    assert client.post(f"{BASE}/universes/foreign/resolve").status_code == 404
    assert client.get(f"{BASE}/universes").json()["universes"] == []


def test_a_duplicate_universe_is_409(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    payload = {"name": "dup", "kind": "explicit", "source_config": {"members": ["NSE:RELIANCE"]}}
    assert client.post(f"{BASE}/universes", json=payload).status_code == 201
    assert client.post(f"{BASE}/universes", json=payload).status_code == 409


def test_an_unsupported_universe_kind_is_422(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    response = client.post(
        f"{BASE}/universes",
        json={"name": "bad", "kind": "magic", "source_config": {"members": ["NSE:RELIANCE"]}},
    )
    assert response.status_code == 422


def test_universe_preview_persists_no_universe(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    body = client.post(
        f"{BASE}/universes/preview",
        json={"kind": "explicit", "source_config": {"members": ["NSE:RELIANCE"]}},
    ).json()
    assert body["members"] == ["NSE:RELIANCE"]
    assert client.get(f"{BASE}/universes").json()["universes"] == []


# ---------------------------------------------------------------------------
# screeners
# ---------------------------------------------------------------------------


def test_a_non_screener_is_409_not_404_for_the_screener_routes(session_factory, monkeypatch):
    """It is readable, so 'not found' would be a lie; it is simply not a screener."""
    workflow, _revision = _seed_workflow(session_factory, PLAIN_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    for url in (
        f"{BASE}/screeners/{workflow.id}/runs",
        f"{BASE}/screeners/{workflow.id}/events",
        f"{BASE}/screeners/{workflow.id}/attachments",
    ):
        assert client.get(url).status_code == 409, url


def test_a_foreign_screener_is_404(session_factory, monkeypatch):
    workflow, _revision = _seed_workflow(
        session_factory, SCREENER_DOCUMENT, owner=FOREIGN_SCOPE
    )
    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/screeners/{workflow.id}/runs").status_code == 404
    assert client.get(f"{BASE}/screeners/{workflow.id}/attachments").status_code == 404


def test_a_screener_with_no_runs_reports_empty_not_error(session_factory, monkeypatch):
    workflow, _revision = _seed_workflow(session_factory, SCREENER_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/screeners/{workflow.id}/runs").json()
    assert body["runs"] == []
    assert "partial" in body["note"]


def test_attachment_baselines_are_routed_and_explain_hysteresis(session_factory, monkeypatch):
    """Previously computed and stored, but never reachable over HTTP.

    This is the inspection value: for each attachment and instrument, whether it
    is currently present, its last rank, and how many consecutive complete runs
    it has been absent — i.e. how close an exit trigger is to firing.
    """
    from backend.workflows.screener_repository import ScreenerAttachmentState

    workflow, revision = _seed_workflow(session_factory, SCREENER_DOCUMENT)
    with session_factory() as session:
        session.add_all([
            ScreenerAttachmentState(
                owner_id=OPERATOR_SCOPE, workflow_id=workflow.id,
                workflow_revision_id=revision.id, attachment_id="entry",
                instrument_key="NSE:RELIANCE", present=True, last_rank=3,
                consecutive_absent=0, updated_at=NOW,
            ),
            ScreenerAttachmentState(
                owner_id=OPERATOR_SCOPE, workflow_id=workflow.id,
                workflow_revision_id=revision.id, attachment_id="entry",
                instrument_key="NSE:TCS", present=False, last_rank=12,
                consecutive_absent=2, updated_at=NOW,
            ),
        ])
        session.commit()

    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/screeners/{workflow.id}/attachments").json()
    assert body["attachments"][0]["attachment_id"] == "entry"
    assert body["attachments"][0]["hysteresis"]["exit_after"] == 3
    members = {
        member["instrument_key"]: member
        for member in body["attachments"][0]["members"]
    }
    assert members["NSE:RELIANCE"]["present"] is True
    assert members["NSE:RELIANCE"]["last_rank"] == 3
    assert members["NSE:TCS"]["present"] is False
    assert members["NSE:TCS"]["consecutive_absent"] == 2
    # The honesty field: which run the baseline came from.
    assert "last_complete_run_id" in members["NSE:TCS"]
    assert "COMPLETE run" in body["note"]


def test_attachment_baselines_are_owner_scoped(session_factory, monkeypatch):
    """A baseline row belonging to another owner must not be readable."""
    from backend.workflows.screener_repository import ScreenerAttachmentState

    workflow, revision = _seed_workflow(session_factory, SCREENER_DOCUMENT)
    with session_factory() as session:
        session.add(
            ScreenerAttachmentState(
                owner_id=FOREIGN_SCOPE, workflow_id=workflow.id,
                workflow_revision_id=revision.id, attachment_id="entry",
                instrument_key="NSE:HIDDEN", present=True, last_rank=1,
                consecutive_absent=0, updated_at=NOW,
            )
        )
        session.commit()
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/screeners/{workflow.id}/attachments").json()
    assert body["attachments"][0]["members"] == []


def test_attachments_can_address_a_specific_revision(session_factory, monkeypatch):
    workflow, revision = _seed_workflow(session_factory, SCREENER_DOCUMENT)
    client = _app(session_factory, monkeypatch)
    assert client.get(
        f"{BASE}/screeners/{workflow.id}/attachments?revision=1"
    ).json()["revision"] == 1
    assert client.get(
        f"{BASE}/screeners/{workflow.id}/attachments?revision=99"
    ).status_code == 404


def test_a_foreign_screener_run_is_404(session_factory, monkeypatch):
    """A run id from another owner must not confirm its existence."""
    from backend.workflows.screener_repository import ScreenerRun

    workflow, revision = _seed_workflow(
        session_factory, SCREENER_DOCUMENT, owner=FOREIGN_SCOPE
    )
    with session_factory() as session:
        session.add(ScreenerRun(
            id="run-foreign", owner_id=FOREIGN_SCOPE, workflow_id=workflow.id,
            workflow_revision_id=revision.id, occurrence_key="foreign:1",
            scheduled_for=NOW, status="complete", created_at=NOW, updated_at=NOW,
        ))
        session.commit()
    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/screener-runs/run-foreign").status_code == 404


def test_run_members_carry_exclusion_reasons(session_factory, monkeypatch):
    """The operator sees WHY a symbol did not qualify, not just its absence."""
    from backend.workflows.screener_repository import ScreenerRun, ScreenerRunMember

    workflow, revision = _seed_workflow(session_factory, SCREENER_DOCUMENT)
    with session_factory() as session:
        session.add(ScreenerRun(
            id="run-1", owner_id=OPERATOR_SCOPE, workflow_id=workflow.id,
            workflow_revision_id=revision.id, occurrence_key="ops:1",
            scheduled_for=NOW, status="complete", created_at=NOW, updated_at=NOW,
        ))
        session.add_all([
            ScreenerRunMember(
                run_id="run-1", instrument_key="NSE:RELIANCE", passed=True,
                rank=1, score=99.5, values={"close": 3000.0},
            ),
            ScreenerRunMember(
                run_id="run-1", instrument_key="NSE:TCS", passed=False,
                exclusion_reason="insufficient_history", rank=None, score=None,
                values={},
            ),
        ])
        session.commit()
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/screener-runs/run-1").json()
    assert body["member_count"] == 2
    by_key = {member["instrument_key"]: member for member in body["members"]}
    assert by_key["NSE:RELIANCE"]["rank"] == 1
    assert by_key["NSE:TCS"]["exclusion_reason"] == "insufficient_history"


# ---------------------------------------------------------------------------
# producers
# ---------------------------------------------------------------------------


def test_producer_lifecycle_and_one_time_credential_reveal(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    created = client.post(
        f"{BASE}/signals/producers",
        json={"name": "my-scanner", "value_schema": {"fields": {"score": "number"}},
              "default_ttl_s": 600},
    ).json()
    assert created["producer"]["name"] == "my-scanner"
    # The producer view must never carry a secret or a hash.
    assert "secret" not in created["producer"]
    assert "token_hash" not in created["producer"]

    issued = client.post(f"{BASE}/signals/producers/my-scanner/credentials").json()
    assert issued["reveal_once"] is True
    assert issued["secret"]
    assert "cannot be retrieved" in issued["note"]
    assert "PRODUCER credential, not a worker token" in issued["note"]

    # The list must not carry the secret back.
    listed = client.get(f"{BASE}/signals/producers").json()
    assert "secret" not in str(listed["producers"])

    revoked = client.post(
        f"{BASE}/signals/producers/my-scanner/credentials/{issued['token_id']}/revoke"
    ).json()
    assert revoked["revoked"] is True

    assert client.post(f"{BASE}/signals/producers/my-scanner/revoke").json()["producer"]["revoked"] is True


def test_a_foreign_producer_is_404(session_factory, monkeypatch):
    from backend.workflows import external_signals as signals

    session = session_factory()
    try:
        signals.register_producer(
            session, owner_id=FOREIGN_SCOPE, name="foreign-producer",
            value_schema={"fields": {}}, default_ttl_s=600,
        )
        session.commit()
    finally:
        session.close()

    client = _app(session_factory, monkeypatch)
    assert client.get(f"{BASE}/signals/producers/foreign-producer").status_code == 404
    assert client.post(f"{BASE}/signals/producers/foreign-producer/revoke").status_code == 404
    assert client.post(
        f"{BASE}/signals/producers/foreign-producer/credentials"
    ).status_code == 404
    assert client.get(
        f"{BASE}/signals/values?producer=foreign-producer"
    ).status_code == 404
    assert client.get(f"{BASE}/signals/producers").json()["producers"] == []


def test_producer_credential_revoke_of_an_unknown_token_is_404(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    client.post(f"{BASE}/signals/producers", json={"name": "p1"})
    response = client.post(
        f"{BASE}/signals/producers/p1/credentials/does-not-exist/revoke"
    )
    assert response.status_code == 404


def test_values_carry_expiry_and_status(session_factory, monkeypatch):
    """A retained value may already be unusable; the response must say which."""
    from backend.workflows import external_signals as signals

    client = _app(session_factory, monkeypatch)
    client.post(f"{BASE}/signals/producers", json={"name": "feed", "default_ttl_s": 600})
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=OPERATOR_SCOPE, name="feed")
        session.add(signals.ExternalSignalValue(
            id="v-1", producer_id=producer.id, owner_id=OPERATOR_SCOPE,
            instrument_key="NSE:RELIANCE", event_time=NOW, received_at=NOW,
            expires_at=NOW.replace(year=NOW.year + 1), status="accepted",
            value={"score": 7}, content_hash="abc", idempotency_key="k1",
        ))
        session.commit()
    finally:
        session.close()

    body = client.get(f"{BASE}/signals/values?producer=feed").json()
    assert body["total"] == 1
    value = body["values"][0]
    assert value["status"] == "accepted"
    assert value["expires_at"] is not None
    assert value["value"] == {"score": 7}


def test_signals_health_explains_sampling_and_no_fallback(session_factory, monkeypatch):
    client = _app(session_factory, monkeypatch)
    body = client.get(f"{BASE}/signals/health").json()
    assert body["limits"]["max_future_skew_s"] > 0
    assert body["limits"]["max_lateness_s"] > 0
    assert "SAMPLED" in body["note"]
    assert "UNKNOWN rather than false" in body["note"]
