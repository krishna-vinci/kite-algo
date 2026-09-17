"""Owner-facing canonical strategy API and trusted run binding (G1 Tasks 5-7).

Two halves:

* **Trusted run binding** — a hosted run binds from its *persisted job*, an
  external run binds only from an owner-issued *token grant*, and a run with
  neither is explicitly legacy/unattributed. Payload metadata is never identity.
* **Owner API** — canonical strategies, adapters and grants over the existing
  hosted-strategies router, backward compatible with what the hosted frontend
  already sends.

These are unit tests over fake/SQLite-backed stores; real composite-FK and
trigger enforcement is verified by the disposable-PostgreSQL suite.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.routers.worker_shared import create_worker_run_for_token  # noqa: E402
from backend.api.schemas.worker import WorkerRunCreateRequest  # noqa: E402
from backend.strategies.attribution import RunBindingFailed, RunBindingInput  # noqa: E402

#: A paper-shaped scope and a live-shaped scope: run creation enforces that a
#: paper run uses a paper account, so the two environments need distinct scopes.
PAPER_SCOPE = "kite:paper-a"
LIVE_SCOPE = "kite:AB1234"


class _FakeBindingRepository:
    """Fake for the run-create path.

    Models the store's single-transaction semantics: a binding that cannot be
    written leaves **no** run behind, and a run insert that fails never records a
    binding.
    """

    def __init__(self, *, owner_by_strategy=None):
        self.runs = {}
        self.bindings = {}
        self.grants = []
        self.created = []
        #: strategy_id -> owner_id. Presence means "the canonical row exists".
        self.owner_by_strategy = dict(owner_by_strategy or {})
        #: strategy_id -> account_scope
        self.account_by_strategy = {}

    async def create_run(self, token, payload, *, strategy_run_id):
        if not getattr(payload, "template_id", None):
            raise ValueError("template_id is required")
        run = {
            "strategy_run_id": strategy_run_id,
            "token_id": token.token_id,
            "template_id": payload.template_id,
            "account_scope": payload.account_scope,
            "execution_mode": payload.execution_mode,
            "status": "open",
            "metadata": dict(payload.metadata or {}),
        }
        self.runs[strategy_run_id] = run
        self.created.append(strategy_run_id)
        return dict(run)

    async def create_run_with_binding(self, token, payload, *, strategy_run_id, binding):
        run = await self.create_run(token, payload, strategy_run_id=strategy_run_id)
        if binding is not None:
            if binding.strategy_id not in self.owner_by_strategy:
                # Simulates the composite-FK refusal, then rolls the unit back.
                self.runs.pop(strategy_run_id, None)
                self.created.remove(strategy_run_id)
                raise RunBindingFailed("canonical strategy not found")
            self.bindings[strategy_run_id] = binding
        return run

    async def active_grants(self, *, token_id, account_id):
        return [
            {
                "strategy_id": grant["strategy_id"],
                "owner_id": self.owner_by_strategy.get(grant["strategy_id"], ""),
                "account_scope": grant["account_scope"],
            }
            for grant in self.grants
            if grant["token_id"] == token_id
            and grant.get("revoked_at") is None
            and grant["account_scope"] == account_id
        ]


def _request(repo):
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)),
    )


def _token(*, token_id="worker-1", account_scope=PAPER_SCOPE, allowed_templates=None):
    return WorkerToken(
        token_id=token_id,
        name="worker",
        account_scope=account_scope,
        allowed_modes=["paper", "live"],
        allowed_actions=["runs:write"],
        allowed_templates=list(allowed_templates or []),
    )


def _payload(**overrides):
    values = {
        "template_id": "mean_reversion",
        "account_scope": PAPER_SCOPE,
        "execution_mode": "paper",
    }
    values.update(overrides)
    return WorkerRunCreateRequest(**values)


class TrustedRunBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = _FakeBindingRepository(
            owner_by_strategy={"stg-REAL": "app:o", "stg-1": "app:o", "stg-2": "app:o"}
        )
        self.repo.account_by_strategy = {
            "stg-REAL": PAPER_SCOPE,
            "stg-1": PAPER_SCOPE,
            "stg-2": "kite:OTHER",
        }

    # ------------------------------------------------------------------ hosted

    async def test_hosted_run_binds_from_persisted_job_not_metadata(self):
        # The payload metadata claims another strategy; the persisted job wins.
        binding = RunBindingInput(
            strategy_id="stg-REAL", owner_id="app:o", account_id=PAPER_SCOPE,
            execution_environment="paper", bound_by="supervisor", binding_source="hosted_job",
        )
        payload = _payload(
            template_id="hosted:stg-REAL",
            metadata={"hosted_strategy_id": "stg-VICTIM", "hosted_job_id": "job-1"},
        )
        token = _token(allowed_templates=["hosted:stg-REAL"])

        await create_worker_run_for_token(
            _request(self.repo), token, payload, strategy_run_id="run-hosted", binding=binding,
        )

        recorded = self.repo.bindings["run-hosted"]
        self.assertEqual(recorded.strategy_id, "stg-REAL")
        self.assertEqual(recorded.binding_source, "hosted_job")
        self.assertEqual(recorded.owner_id, "app:o")
        self.assertEqual(recorded.execution_environment, "paper")
        # Job/attempt/run ids never become the strategy id.
        self.assertNotIn(recorded.strategy_id, {"job-1", "run-hosted"})

    async def test_hosted_binding_failure_rolls_back_run(self):
        binding = RunBindingInput(
            strategy_id="stg-MISSING", owner_id="app:o", account_id=PAPER_SCOPE,
            execution_environment="paper", bound_by="supervisor", binding_source="hosted_job",
        )
        token = _token(allowed_templates=["hosted:stg-MISSING"])
        payload = _payload(template_id="hosted:stg-MISSING")

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run_for_token(
                _request(self.repo), token, payload, strategy_run_id="run-bad", binding=binding,
            )

        self.assertEqual(ctx.exception.detail.get("rejection_reason"), "HOSTED_RUN_BINDING_FAILED")
        self.assertEqual(self.repo.runs, {})
        self.assertEqual(self.repo.bindings, {})

    async def test_hosted_run_without_a_descriptor_is_refused(self):
        # A hosted run must always bind: no descriptor is a server-side invariant
        # violation, never a silent legacy run.
        token = _token(allowed_templates=["hosted:stg-REAL"])
        payload = _payload(template_id="hosted:stg-REAL")

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run_for_token(_request(self.repo), token, payload, strategy_run_id="run-x")

        self.assertEqual(ctx.exception.detail.get("rejection_reason"), "HOSTED_RUN_BINDING_FAILED")
        self.assertEqual(self.repo.runs, {})

    # ---------------------------------------------------------------- external

    async def test_external_run_binds_only_from_token_grant(self):
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-1", "account_scope": PAPER_SCOPE}
        )
        response = await create_worker_run_for_token(
            _request(self.repo), _token(), _payload(strategy_id="stg-1"), strategy_run_id="run-g",
        )
        self.assertEqual(self.repo.bindings["run-g"].strategy_id, "stg-1")
        self.assertEqual(self.repo.bindings["run-g"].binding_source, "external_run_create")
        self.assertEqual(response["strategy_attribution"]["strategy_id"], "stg-1")

    async def test_external_run_ungranted_selection_is_refused_before_creation(self):
        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run_for_token(
                _request(self.repo), _token(), _payload(strategy_id="stg-9"), strategy_run_id="run-9",
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail.get("rejection_reason"), "STRATEGY_NOT_GRANTED")
        self.assertEqual(self.repo.runs, {})  # no orphan run

    async def test_external_run_single_grant_binds_without_selection(self):
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-1", "account_scope": PAPER_SCOPE}
        )
        await create_worker_run_for_token(
            _request(self.repo), _token(), _payload(), strategy_run_id="run-single",
        )
        self.assertEqual(self.repo.bindings["run-single"].strategy_id, "stg-1")

    async def test_external_run_no_grant_no_selection_is_legacy_unattributed(self):
        response = await create_worker_run_for_token(
            _request(self.repo), _token(), _payload(), strategy_run_id="run-legacy",
        )
        self.assertEqual(response["strategy_attribution"], "legacy_unattributed")
        self.assertNotIn("run-legacy", self.repo.bindings)
        self.assertIn("run-legacy", self.repo.runs)

    async def test_grant_scope_requires_account_match(self):
        # Grant exists for another account: treated as absent.
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-2", "account_scope": "kite:OTHER"}
        )
        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run_for_token(
                _request(self.repo), _token(), _payload(strategy_id="stg-2"), strategy_run_id="run-2",
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.repo.runs, {})

    async def test_revoked_grant_cannot_authorize_new_runs(self):
        self.repo.grants.append(
            {
                "token_id": "worker-1", "strategy_id": "stg-1",
                "account_scope": PAPER_SCOPE, "revoked_at": "2026-09-17T10:00:00+00:00",
            }
        )
        # No active grant and no selection: explicit legacy path, never bound.
        response = await create_worker_run_for_token(
            _request(self.repo), _token(), _payload(), strategy_run_id="run-revoked",
        )
        self.assertEqual(response["strategy_attribution"], "legacy_unattributed")
        self.assertNotIn("run-revoked", self.repo.bindings)

        # Explicitly selecting it is refused.
        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run_for_token(
                _request(self.repo), _token(), _payload(strategy_id="stg-1"), strategy_run_id="run-revoked-2",
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_token_rotation_preserves_strategy_identity(self):
        # Old token's grant revoked; a new token is granted the SAME strategy.
        self.repo.grants.append(
            {
                "token_id": "worker-old", "strategy_id": "stg-1",
                "account_scope": PAPER_SCOPE, "revoked_at": "2026-09-17T10:00:00+00:00",
            }
        )
        self.repo.grants.append(
            {"token_id": "worker-new", "strategy_id": "stg-1", "account_scope": PAPER_SCOPE}
        )
        await create_worker_run_for_token(
            _request(self.repo), _token(token_id="worker-new"), _payload(), strategy_run_id="run-new",
        )
        self.assertEqual(self.repo.bindings["run-new"].strategy_id, "stg-1")
        # Rotation is a credential change, never an identity change.
        self.assertEqual(
            {b.strategy_id for b in self.repo.bindings.values()}, {"stg-1"},
        )

    async def test_binding_environment_equals_run_mode(self):
        # The same granted strategy runs in both books; the two bindings coexist
        # and each carries the run's own mode.
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-1", "account_scope": PAPER_SCOPE}
        )
        self.repo.account_by_strategy["stg-live"] = LIVE_SCOPE
        self.repo.owner_by_strategy["stg-live"] = "app:o"
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-live", "account_scope": LIVE_SCOPE}
        )
        live_metadata = {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"}
        await create_worker_run_for_token(
            _request(self.repo), _token(),
            _payload(execution_mode="paper", strategy_id="stg-1"), strategy_run_id="run-paper",
        )
        await create_worker_run_for_token(
            _request(self.repo), _token(account_scope=LIVE_SCOPE),
            _payload(
                execution_mode="live", account_scope=LIVE_SCOPE,
                strategy_id="stg-live", metadata=live_metadata,
            ),
            strategy_run_id="run-live",
        )
        self.assertEqual(self.repo.bindings["run-paper"].execution_environment, "paper")
        self.assertEqual(self.repo.bindings["run-live"].execution_environment, "live")
        # Two books, one identity per book: neither binding overwrote the other.
        self.assertEqual(len(self.repo.bindings), 2)

    async def test_binding_environment_is_server_derived_from_run_mode(self):
        # A caller that lies about the environment cannot move the book: the
        # descriptor is re-stamped from the run's own execution_mode.
        self.repo.grants.append(
            {"token_id": "worker-1", "strategy_id": "stg-1", "account_scope": PAPER_SCOPE}
        )
        binding = RunBindingInput(
            strategy_id="stg-1", owner_id="app:o", account_id=PAPER_SCOPE,
            execution_environment="live", bound_by="t", binding_source="external_run_create",
        )
        await create_worker_run_for_token(
            _request(self.repo), _token(), _payload(execution_mode="paper"), strategy_run_id="run-stamp",
            binding=binding,
        )
        self.assertEqual(self.repo.bindings["run-stamp"].execution_environment, "paper")


# ---------------------------------------------------------------------------
# Owner-facing canonical strategy API (Task 6)
# ---------------------------------------------------------------------------

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.strategies.attribution import SqlAttributionStore  # noqa: E402
import backend.strategies.attribution_models  # noqa: E402,F401  registers the attribution tables
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/strategies"


class _FakeWorkerTokens:
    """Just enough of the worker repository for grant issuance."""

    def __init__(self, tokens=()):
        self.tokens = {t["token_id"]: dict(t) for t in tokens}

    async def list_tokens(self):
        return [dict(t) for t in self.tokens.values()]


def _hosted_payload(**overrides):
    body = {
        "name": "momentum",
        "description": "monthly",
        "execution_mode": "paper",
        "job_kind": "finite",
        "account_scope": "kite:paper",
        "max_duration_s": 21600,
        "progress_deadline_s": 600,
        "stale_exit_policy": "exit_on_worker_stale",
    }
    body.update(overrides)
    return body


class OwnerStrategyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _attach_public(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            # Empty fact tables: the attribution store reads them through
            # ``public.``-qualified SQL, and an empty book must rebuild cleanly.
            cursor.execute(
                """
                CREATE TABLE public.order_trade_fills (
                    account_id TEXT NOT NULL, order_id TEXT NOT NULL, trade_id TEXT NOT NULL,
                    instrument_token BIGINT, exchange TEXT, tradingsymbol TEXT, product TEXT,
                    transaction_type TEXT, quantity INTEGER, fill_timestamp TEXT, payload_json TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.worker_live_execution_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, broker_order_id TEXT NOT NULL, trade_id TEXT,
                    client_order_ref TEXT, basket_execution_id TEXT, basket_leg_index INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.live_order_intents (
                    intent_id TEXT PRIMARY KEY, client_order_ref TEXT NOT NULL,
                    account_id TEXT NOT NULL, strategy_run_id TEXT NOT NULL, broker_order_id TEXT,
                    basket_execution_id TEXT, basket_leg_index INTEGER, bracket_intent_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.paper_orders (
                    account_scope TEXT NOT NULL, order_id TEXT NOT NULL, instrument_token BIGINT,
                    exchange TEXT, tradingsymbol TEXT, product TEXT, metadata_json TEXT,
                    PRIMARY KEY (account_scope, order_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.paper_trades (
                    account_scope TEXT NOT NULL, trade_id TEXT NOT NULL, order_id TEXT NOT NULL,
                    transaction_type TEXT, quantity INTEGER, trade_timestamp TEXT,
                    PRIMARY KEY (account_scope, trade_id)
                )
                """
            )
            # No catalog generations/mappings: tokens resolve as explicit
            # unresolved raw identities, which is a real production state.
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_broker_mappings (
                    mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT, broker_exchange TEXT,
                    broker_symbol TEXT, broker_token TEXT, valid_from_generation TEXT,
                    valid_to_generation TEXT, is_current INTEGER
                )
                """
            )
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.store = SqlAttributionStore(session_factory=self.factory)
        self.tokens = _FakeWorkerTokens(
            [
                {"token_id": "worker-ok", "name": "ok", "account_scope": "kite:paper", "status": "active"},
                {"token_id": "worker-other", "name": "other", "account_scope": "kite:OTHER", "status": "active"},
                {"token_id": "worker-revoked", "name": "rev", "account_scope": "kite:paper", "status": "revoked"},
            ]
        )

    async def asyncTearDown(self):
        self.engine.dispose()

    def _client(self, *, username="admin", monkeypatch=None):
        from unittest.mock import patch as _patch

        from backend.app import auth as auth_module

        user = AppUser(username=username, role="admin") if username else None
        app = FastAPI()
        app.include_router(strategies_router.router, prefix="/api")
        app.dependency_overrides[strategies_router._strategies_db] = lambda: self.factory
        app.state.attribution_store = self.store
        app.state.algo_worker_repository = self.tokens
        self._patch = _patch.object(auth_module, "get_optional_app_user", lambda _request: user)
        self._patch.start()
        from backend.api.routers import strategies as router_module

        self._env = _patch.dict(
            "os.environ", {"HOSTED_STRATEGY_ACCOUNT_SCOPES": "kite:paper"}
        )
        self._env.start()
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def _stop_patches(self):
        self._patch.stop()
        self._env.stop()

    async def _create(self, client, **overrides):
        response = await client.post(BASE, json=_hosted_payload(**overrides))
        assert response.status_code == 200, response.text
        return response.json()

    # -- backward compatibility ----------------------------------------------

    async def test_legacy_hosted_create_keeps_working_and_writes_canonical_atomically(self):
        client = self._client()
        try:
            created = await self._create(client)
        finally:
            self._stop_patches()

        # Existing response shape is preserved...
        self.assertEqual(created["template_id"], f"hosted:{created['strategy_id']}")
        self.assertEqual(created["owner_id"], "app:admin")
        self.assertEqual(created["name"], "momentum")
        # ...and canonical fields are additive.
        self.assertEqual(created["product_status"], "active")

        with self.factory() as session:
            canonical = session.execute(
                text("SELECT id, owner_id, name, account_scope, status FROM strategies")
            ).fetchall()
            hosted = session.execute(
                text("SELECT id, owner_id, name, default_account_scope FROM hosted_strategies")
            ).fetchall()
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(hosted), 1)
        self.assertEqual(canonical[0][0], hosted[0][0])          # same id
        self.assertEqual(canonical[0][1], hosted[0][1])          # same owner
        self.assertEqual(canonical[0][2], hosted[0][2])          # same name
        self.assertEqual(canonical[0][3], hosted[0][3])          # same account

    async def test_get_and_list_include_canonical_fields_additively(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            fetched = (await client.get(f"{BASE}/{sid}")).json()
            listed = (await client.get(BASE)).json()["strategies"][0]
        finally:
            self._stop_patches()

        for body in (fetched, listed):
            # existing fields keep their names and shape
            for key in ("strategy_id", "owner_id", "name", "template_id", "status",
                        "default_execution_mode", "default_account_scope"):
                self.assertIn(key, body)
            # additive canonical fields
            self.assertIn("product_status", body)
            self.assertIn("adapter_kinds", body)
        self.assertEqual(listed["adapter_kinds"], ["hosted"])
        self.assertEqual(listed["status"], "active")
        self.assertEqual(listed["product_status"], "active")

    async def test_rename_updates_both_sides_in_one_transaction(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            response = await client.patch(f"{BASE}/{sid}", json={"name": "renamed", "description": "x"})
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            self._stop_patches()

        with self.factory() as session:
            canonical_name = session.execute(
                text("SELECT name FROM strategies WHERE id=:sid"), {"sid": sid}
            ).scalar()
            hosted_name = session.execute(
                text("SELECT name FROM hosted_strategies WHERE id=:sid"), {"sid": sid}
            ).scalar()
        self.assertEqual(canonical_name, "renamed")
        self.assertEqual(hosted_name, "renamed")

    async def test_name_drift_is_refused_by_the_repository_boundary(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
        finally:
            self._stop_patches()

        # A hosted adapter with no canonical identity is drift and is refused
        # rather than silently renamed on one side only.
        with self.factory() as session:
            session.execute(text("DELETE FROM strategies WHERE id=:sid"), {"sid": sid})
            session.commit()
        repo = strategies_router.SqlAlchemyStrategyRepository(self.factory)
        with self.assertRaises(Exception):
            repo.update_strategy("app:admin", sid, name="drifted")

    # -- status semantics -----------------------------------------------------

    async def test_product_status_independent_of_scheduling_status(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            # PATCH /{id} keeps its existing meaning: hosted SCHEDULING enablement.
            patched = (await client.patch(f"{BASE}/{sid}", json={"status": "disabled"})).json()
            self.assertEqual(patched["status"], "disabled")
            self.assertEqual(patched["product_status"], "active")

            # PATCH /{id}/status writes only the canonical PRODUCT status.
            archived = (await client.patch(f"{BASE}/{sid}/status", json={"status": "archived"})).json()
            self.assertEqual(archived["product_status"], "archived")
            self.assertEqual(archived["status"], "disabled")

            # Archive preserves history: the strategy is still readable.
            self.assertEqual((await client.get(f"{BASE}/{sid}")).status_code, 200)
            # There is no delete route.
            self.assertIn((await client.delete(f"{BASE}/{sid}")).status_code, (404, 405))
        finally:
            self._stop_patches()

    # -- discriminated create -------------------------------------------------

    async def test_create_schema_discrimination(self):
        client = self._client()
        try:
            # kind: "hosted" behaves exactly as the legacy payload.
            explicit = await client.post(BASE, json=_hosted_payload(name="explicit-hosted", kind="hosted"))
            self.assertEqual(explicit.status_code, 200, explicit.text)

            # External creation needs only name/account/config.
            external = await client.post(
                BASE,
                json={"kind": "external", "name": "ext-one", "account_scope": "kite:paper",
                      "external_config": {"endpoint": "https://worker.example"}},
            )
            self.assertEqual(external.status_code, 200, external.text)
            body = external.json()
            self.assertEqual(body["name"], "ext-one")

            # Hosted-only fields are rejected on an external request.
            mixed = await client.post(
                BASE,
                json={"kind": "external", "name": "ext-two", "account_scope": "kite:paper",
                      "job_kind": "finite"},
            )
            self.assertEqual(mixed.status_code, 422)

            # Unknown kind and unknown fields are 422.
            self.assertEqual(
                (await client.post(BASE, json=_hosted_payload(kind="banana"))).status_code, 422
            )
            self.assertEqual(
                (await client.post(BASE, json={**_hosted_payload(), "owner_id": "app:x"})).status_code, 422
            )
        finally:
            self._stop_patches()

        # The external strategy exists canonically with an external adapter and
        # no hosted adapter.
        with self.factory() as session:
            rows = session.execute(
                text("SELECT id, name FROM strategies WHERE name='ext-one'")
            ).fetchall()
            adapters = session.execute(
                text("SELECT strategy_id FROM external_strategy_adapters")
            ).fetchall()
            hosted = session.execute(text("SELECT id FROM hosted_strategies")).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual([r[0] for r in adapters], [rows[0][0]])
        self.assertNotIn(rows[0][0], [r[0] for r in hosted])

    # -- grants ---------------------------------------------------------------

    async def test_grant_issuance_owner_verification(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            # The token must exist, be active, and match the canonical account.
            mismatch = await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-other"})
            self.assertEqual(mismatch.status_code, 409)
            self.assertEqual(mismatch.json()["detail"]["rejection_reason"], "TOKEN_ACCOUNT_MISMATCH")

            revoked = await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-revoked"})
            self.assertEqual(revoked.status_code, 409)

            unknown = await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-nope"})
            self.assertEqual(unknown.status_code, 404)

            issued = await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-ok"})
            self.assertEqual(issued.status_code, 200, issued.text)
            self.assertEqual(issued.json()["token_id"], "worker-ok")
        finally:
            self._stop_patches()

        # The grant actually authorizes on the read path...
        grants = self.store.active_grants(token_id="worker-ok", account_id="kite:paper")
        self.assertEqual([g["strategy_id"] for g in grants], [sid])

        # ...and a cross-owner actor cannot issue one.
        client = self._client(username="other")
        try:
            self.assertEqual(
                (await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-ok"})).status_code, 404
            )
        finally:
            self._stop_patches()

    async def test_revoked_grant_and_revoked_token_authorize_nothing(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            issued = await client.post(f"{BASE}/{sid}/grants", json={"token_id": "worker-ok"})
            self.assertEqual(issued.status_code, 200, issued.text)

            revoked = await client.delete(f"{BASE}/{sid}/grants/worker-ok")
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertTrue(revoked.json()["revoked"])

            missing = await client.delete(f"{BASE}/{sid}/grants/worker-absent")
            self.assertEqual(missing.status_code, 404)
        finally:
            self._stop_patches()

        self.assertEqual(self.store.active_grants(token_id="worker-ok", account_id="kite:paper"), [])
        # Revocation is a stamp, never a delete: history survives.
        with self.factory() as session:
            rows = session.execute(
                text("SELECT revoked_at FROM worker_token_strategy_grants WHERE token_id='worker-ok'")
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][0])

    async def test_worker_bearer_token_cannot_reach_the_owner_surface(self):
        client = self._client(username=None)
        try:
            headers = {"Authorization": "Bearer kwa_something"}
            self.assertEqual((await client.post(BASE, json=_hosted_payload(), headers=headers)).status_code, 401)
            for path, method in (
                (f"{BASE}/stg-1/status", "patch"),
                (f"{BASE}/stg-1/grants", "post"),
                (f"{BASE}/stg-1/adapters/external", "post"),
                (f"{BASE}/stg-1/positions", "get"),
                (f"{BASE}/stg-1/positions/rebuild", "post"),
            ):
                if method == "get":
                    response = await client.get(path, headers=headers)
                else:
                    response = await getattr(client, method)(path, json={}, headers=headers)
                self.assertEqual(response.status_code, 401, f"{method} {path}")
        finally:
            self._stop_patches()

    # -- positions ------------------------------------------------------------

    async def test_positions_read_and_rebuild_are_owner_scoped(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            empty = await client.get(f"{BASE}/{sid}/positions")
            self.assertEqual(empty.status_code, 200, empty.text)
            self.assertEqual(empty.json()["positions"], [])
            self.assertEqual(empty.json()["environment"], "live")

            rebuilt = await client.post(f"{BASE}/{sid}/positions/rebuild?environment=paper")
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
            self.assertEqual(rebuilt.json()["execution_environment"], "paper")

            bad_env = await client.post(f"{BASE}/{sid}/positions/rebuild?environment=banana")
            self.assertEqual(bad_env.status_code, 422)
        finally:
            self._stop_patches()

        client = self._client(username="other")
        try:
            self.assertEqual((await client.get(f"{BASE}/{sid}/positions")).status_code, 404)
        finally:
            self._stop_patches()

    # -- app-state wiring -----------------------------------------------------

    async def test_wired_attribution_state_publishes_binding_into_positions_read(self):
        """After a run binds, publishing makes the book visible at the owner read.

        Exercises the app-state-wired store/service end to end: a bound run's
        fill is folded per fact, and because no catalog mapping exists for the
        token the exposure stays an explicit unresolved raw row rather than being
        silently attributed.
        """
        from backend.app.background import ensure_attribution_state

        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            # A run binds in the live book; its fill is a real durable fact.
            self.store.bind_run(
                strategy_run_id="run-wired", strategy_id=sid, owner_id="app:admin",
                account_id="kite:paper", execution_environment="live",
                bound_by="supervisor", binding_source="hosted_job",
            )
            with self.factory() as session:
                session.execute(
                    text(
                        "INSERT INTO public.worker_live_execution_links "
                        "(strategy_run_id, account_id, broker_order_id) "
                        "VALUES ('run-wired', 'kite:paper', 'OID-WIRED')"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO public.order_trade_fills "
                        "(account_id, order_id, trade_id, instrument_token, exchange, tradingsymbol, "
                        " product, transaction_type, quantity, fill_timestamp, payload_json) "
                        "VALUES ('kite:paper', 'OID-WIRED', 'T-1', 738561, 'NSE', 'RELIANCE', "
                        " 'CNC', 'BUY', 100, '2026-09-17T10:00:00+00:00', '{}')"
                    )
                )
                session.commit()

            # The app factory wires the store/service into app state.
            app = client._transport.app  # type: ignore[attr-defined]
            ensure_attribution_state(app)
            self.assertIsNotNone(getattr(app.state, "attribution_store", None))
            self.assertIsNotNone(getattr(app.state, "attribution_service", None))

            rebuilt = await client.post(f"{BASE}/{sid}/positions/rebuild?environment=live")
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
            self.assertEqual(rebuilt.json()["folded_facts"], 1)

            listed = await client.get(f"{BASE}/{sid}/positions?environment=live")
            self.assertEqual(listed.status_code, 200, listed.text)
            positions = listed.json()["positions"]
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0]["net_quantity"], 100)
            self.assertEqual(positions[0]["instrument_token"], 738561)
            # No catalog mapping exists for this token: the row must say so
            # rather than pretend to be resolved.
            self.assertEqual(positions[0]["identity_kind"], "raw")
            self.assertEqual(positions[0]["unresolved_reason"], "mapping_missing")
        finally:
            self._stop_patches()


if __name__ == "__main__":
    unittest.main()
