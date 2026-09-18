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


class _OwnerApiHarness(unittest.IsolatedAsyncioTestCase):
    """Shared harness: an app mounting only the strategies router, with the
    attribution store and worker-token fake wired into app state."""


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
                CREATE TABLE public.algo_worker_runs (
                    strategy_run_id TEXT PRIMARY KEY, token_id TEXT, template_id TEXT,
                    account_scope TEXT, execution_mode TEXT, status TEXT NOT NULL DEFAULT 'open'
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.account_positions (
                    account_id TEXT NOT NULL, instrument_token BIGINT NOT NULL,
                    product TEXT NOT NULL, exchange TEXT, tradingsymbol TEXT,
                    net_quantity INT NOT NULL DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (account_id, instrument_token, product)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.strategy_jobs (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, owner_id TEXT,
                    account_scope TEXT, execution_mode TEXT, status TEXT NOT NULL DEFAULT 'queued'
                )
                """
            )
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


# ---------------------------------------------------------------------------
# Task 5 (G5): worker submission authority and owner read endpoints
# ---------------------------------------------------------------------------

PROPOSALS_BASE = "/api/algo-workers/worker/proposals"

#: The raw bearer the fake token repository hashes on lookup.
RAW_WORKER_TOKEN = "secret-token"


class _FakeProposalWorkerRepo:
    """Worker repository surface the submission route needs: token + run."""

    def __init__(self, tokens=(), runs=()):
        self.tokens = {t["token_id"]: dict(t) for t in tokens}
        self.runs = {r["strategy_run_id"]: dict(r) for r in runs}
        self.touched = []

    async def get_token_by_hash(self, token_hash):
        for token in self.tokens.values():
            if token.get("token_hash") == token_hash:
                return WorkerToken(**{k: v for k, v in token.items() if k != "token_hash"})
        return None

    async def touch_token(self, token_id):
        self.touched.append(token_id)

    async def get_run(self, strategy_run_id):
        run = self.runs.get(strategy_run_id)
        return dict(run) if run else None


class _ProposalApiHarness(_OwnerApiHarness):
    """Owner + worker routers over one SQLite app, with catalog rows seeded."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from backend.strategies.attribution_models import (
            Strategy,
            StrategyPlan,
            StrategyProposal,
            StrategyProposalJournal,
        )

        Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyProposalJournal.__table__,
            ],
        )
        with self.factory() as session:
            session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS public.instrument_catalog_records ("
                    " instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,"
                    " lifecycle_status TEXT NOT NULL DEFAULT 'active', current_generation_id TEXT,"
                    " instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL,"
                    " underlying TEXT)"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES ('gen-2', 'published', '2026-09-10T00:00:00+00:00')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                    "VALUES ('inst-REL', 'NSE', 'RELIANCE', 'active', 'gen-2')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                    " valid_from_generation, is_current) "
                    "VALUES ('map-REL', 'inst-REL', 'kite', 'NSE', 'RELIANCE', 100, 'gen-2', 1)"
                )
            )
            session.commit()

    def _worker_repo(self):
        from backend.shared.serialization import _hash_token

        # The route hashes the bearer before lookup, so the fake stores the hash
        # while the test presents the raw token.
        token_hash = _hash_token(RAW_WORKER_TOKEN)
        repo = _FakeProposalWorkerRepo(
            tokens=[
                {
                    "token_id": "worker-ok",
                    "name": "ok",
                    "account_scope": "kite:paper",
                    "allowed_modes": ["paper", "live"],
                    "allowed_actions": ["proposals:submit", "intents:submit"],
                    "allowed_templates": [],
                    "status": "active",
                    "token_hash": token_hash,
                }
            ],
            runs=[
                {
                    "strategy_run_id": "run-bound",
                    "token_id": "worker-ok",
                    "template_id": "hosted:stg-A",
                    "account_scope": "kite:paper",
                    "execution_mode": "paper",
                    "status": "open",
                },
                {
                    "strategy_run_id": "run-other-strategy",
                    "token_id": "worker-ok",
                    "template_id": "hosted:stg-A",
                    "account_scope": "kite:paper",
                    "execution_mode": "paper",
                    "status": "open",
                },
                {
                    "strategy_run_id": "run-unbound",
                    "token_id": "worker-ok",
                    "template_id": "hosted:stg-A",
                    "account_scope": "kite:paper",
                    "execution_mode": "paper",
                    "status": "open",
                },
            ],
        )
        return repo, RAW_WORKER_TOKEN

    def _wire_extra_state(self, app):
        """Hook for subclasses that need more wired into app.state."""
        return None

    def _stop_patches(self):
        for patcher in (getattr(self, "_margin", None), getattr(self, "_patch", None), getattr(self, "_env", None)):
            if patcher is not None:
                patcher.stop()

    def _proposal_client(self, *, repo, username="admin"):
        from unittest.mock import patch as _patch

        from backend.app import auth as auth_module
        from backend.api.routers import strategies as strategies_module
        from backend.api.routers import worker_proposals as worker_proposals_module

        user = AppUser(username=username, role="admin") if username else None
        app = FastAPI()
        app.include_router(strategies_module.router, prefix="/api")
        app.include_router(worker_proposals_module.router, prefix="/api")
        app.dependency_overrides[strategies_module._strategies_db] = lambda: self.factory
        app.state.attribution_store = self.store
        app.state.algo_worker_repository = repo
        app.state.proposal_store = _proposal_store(self.factory)
        self._wire_extra_state(app)
        from datetime import datetime, timezone

        self._margin = _patch.object(
            strategies_module,
            "_live_margin_evidence",
            lambda _scope, _plan: {"usable": 10_000_000.0, "as_of": datetime.now(timezone.utc)},
        )
        self._margin.start()
        self._patch = _patch.object(auth_module, "get_optional_app_user", lambda _request: user)
        self._patch.start()
        self._env = _patch.dict(
            "os.environ", {"HOSTED_STRATEGY_ACCOUNT_SCOPES": "kite:paper"}
        )
        self._env.start()
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _bind_run(self, strategy_id, *, run_id, account="kite:paper"):
        self.store.bind_run(
            strategy_run_id=run_id,
            strategy_id=strategy_id,
            owner_id="app:admin",
            account_id=account,
            execution_environment="paper",
            bound_by="test",
            binding_source="audited_mapping",
        )

    def _payload(self, **overrides):
        body = {
            "evaluation_id": "eval-1",
            "evaluation_kind": "run_now",
            "strategy_run_id": "run-bound",
            "strategy_id": "stg-A",
            "account_scope": "kite:paper",
            "target_kind": "single_instrument",
            "payload": {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": 10,
                # Admission's allocation arithmetic needs a price from the plan.
                "reference_price": 100.0,
            },
        }
        body.update(overrides)
        return body


def _proposal_store(session_factory):
    from backend.strategies.proposals import ProposalStore

    return ProposalStore(session_factory=session_factory)


class OwnerStrategyApiTests(_OwnerApiHarness):
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


# ---------------------------------------------------------------------------
# G2 — closure correctness from the strategy book
# ---------------------------------------------------------------------------


class _BookRepo:
    """Repo surface consumed by flatness: the strategy book plus broker legs."""

    def __init__(self, *, book, run_legs=(), broker_positions=()):
        self._book = book
        self._run_legs = list(run_legs)
        self._broker_positions = list(broker_positions)

    async def get_strategy_book_for_run(self, *, strategy_run_id):
        return self._book

    async def list_live_strategy_open_legs(self, *, strategy_run_id, account_id):
        return [dict(leg) for leg in self._run_legs]

    async def list_live_strategy_broker_positions(self, *, strategy_run_id, account_id):
        return [dict(pos) for pos in self._broker_positions]


def _leg(*, token=738561, product="CNC", qty, net=0):
    return {
        "journal_run_id": None, "account_id": "kite:A", "instrument_token": token,
        "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": product,
        "net_quantity": qty, "broker_net_quantity": net,
    }


class TestStrategyBookClosure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from backend.strategies.attribution import SqlAttributionStore

        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.store = SqlAttributionStore(session_factory=self.factory)

    def tearDown(self):
        self.engine.dispose()

    def _seed(self, *, sid="stg-A", account="kite:A", run_id="run-A", env="live"):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope) "
                    "VALUES (:sid, 'app:o', :name, :account)"
                ),
                {"sid": sid, "name": f"Strategy {sid}", "account": account},
            )
            session.commit()
        self.store.bind_run(
            strategy_run_id=run_id, strategy_id=sid, owner_id="app:o", account_id=account,
            execution_environment=env, bound_by="t", binding_source="hosted_job",
        )

    def _project(self, *, sid="stg-A", account="kite:A", env="live", rows=()):
        self.store.recompute_publish(
            account_id=account, strategy_id=sid, execution_environment=env,
            resolve_and_fold=lambda db, bound: (list(rows), f"sha-{len(rows)}"),
        )

    def _row(self, *, identity="uuid-a", qty=100):
        return {
            "identity_kind": "canonical", "identity_key": identity,
            "canonical_instrument_id": identity, "instrument_token": 738561,
            "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
            "net_quantity": qty, "unresolved_reason": None,
        }

    # -- the helper ----------------------------------------------------------

    def test_open_positions_for_run_resolves_binding_to_the_strategy_book(self):
        self._seed()
        self._project(rows=[self._row(qty=100)])
        book = self.store.open_positions_for_run(strategy_run_id="run-A")
        self.assertEqual(book["strategy_id"], "stg-A")
        self.assertEqual(book["execution_environment"], "live")
        self.assertEqual([leg["net_quantity"] for leg in book["legs"]], [100])
        self.assertIn("broker_net_quantity", book["legs"][0])

    def test_open_positions_for_run_returns_none_for_unbound_run(self):
        # An unbound (legacy) run has no strategy book: callers must keep
        # today's run-scoped behavior rather than silently re-scoping it.
        self.assertEqual(self.store.open_positions_for_run(strategy_run_id="run-legacy"), None)

    # -- flatness ------------------------------------------------------------

    async def test_flatness_uses_strategy_book_when_bound(self):
        """G2 acceptance: A flat while B holds the same broker line."""
        from backend.api.services.runtime_recovery import load_live_run_flatness

        self._seed()
        # A's own book is flat (nothing published). The ACCOUNT still holds
        # quantity, and that quantity belongs to strategy B.
        repo = _BookRepo(
            book={"strategy_id": "stg-A", "account_id": "kite:A", "execution_environment": "live", "legs": []},
            broker_positions=[{"instrument_token": 738561, "product": "CNC", "quantity": 40}],
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        result = await load_live_run_flatness(request, {"strategy_run_id": "run-A", "account_scope": "kite:A"})
        self.assertTrue(result["is_flat"], result)
        self.assertEqual(result["remaining_legs"], [])
        # The account net is reported for the one-sided exit guard, never as a
        # flatness gate for a bound run.
        self.assertIn("broker_positions", result)

    async def test_flatness_strategy_book_not_flat_reports_remaining(self):
        from backend.api.services.runtime_recovery import load_live_run_flatness

        self._seed()
        repo = _BookRepo(
            book={
                "strategy_id": "stg-A", "account_id": "kite:A", "execution_environment": "live",
                "legs": [_leg(qty=100), _leg(token=738562, qty=-10)],
            },
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        result = await load_live_run_flatness(request, {"strategy_run_id": "run-A", "account_scope": "kite:A"})
        self.assertFalse(result["is_flat"])
        self.assertEqual([leg["net_quantity"] for leg in result["remaining_legs"]], [100, -10])
        self.assertEqual(result["reason"], "strategy book exposure remains")

    async def test_unbound_run_keeps_run_scoped_flatness(self):
        """Legacy pin: an unbound run behaves exactly as before."""
        from backend.api.services.runtime_recovery import load_live_run_flatness

        repo = _BookRepo(book=None, run_legs=[], broker_positions=[])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        flat = await load_live_run_flatness(request, {"strategy_run_id": "run-legacy", "account_scope": "kite:A"})
        self.assertTrue(flat["is_flat"])

        repo = _BookRepo(book=None, run_legs=[_leg(qty=5)])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        not_flat = await load_live_run_flatness(request, {"strategy_run_id": "run-legacy", "account_scope": "kite:A"})
        self.assertFalse(not_flat["is_flat"])
        self.assertEqual(not_flat["reason"], "broker exposure remains")

    # -- exit sizing ---------------------------------------------------------

    async def test_exit_sizing_uses_strategy_book_across_runs(self):
        """A strategy's earlier run opened the position: sizing must see it."""
        from backend.api.routers.worker_shared import _live_run_legs

        self._seed()
        self._project(rows=[self._row(qty=100)])
        # run-B is a *different* run of the same strategy with no links of its
        # own; sizing from run-scoped links would size zero.
        self.store.bind_run(
            strategy_run_id="run-B", strategy_id="stg-A", owner_id="app:o", account_id="kite:A",
            execution_environment="live", bound_by="t", binding_source="hosted_job",
        )
        repo = _BookRepo(book=self.store.open_positions_for_run(strategy_run_id="run-B"), run_legs=[])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        legs = await _live_run_legs(request, {"strategy_run_id": "run-B", "account_scope": "kite:A"})
        self.assertEqual([leg["net_quantity"] for leg in legs], [100])

        # An unbound run still sizes from its own links.
        repo = _BookRepo(book=None, run_legs=[_leg(qty=7)])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo)))
        legs = await _live_run_legs(request, {"strategy_run_id": "run-legacy", "account_scope": "kite:A"})
        self.assertEqual([leg["net_quantity"] for leg in legs], [7])


class AdjustmentApiTests(_OwnerApiHarness):
    """Task 5 API: reclassification is account-owner-only and append-only."""

    def _line(self, delta=-10):
        return {
            "instrument_token": 738561, "exchange": "NSE", "tradingsymbol": "RELIANCE",
            "product": "CNC", "quantity_delta": delta,
        }

    def _body(self, **overrides):
        body = {"reason_code": "owner_claimed_manual_exit", "lines": [self._line()]}
        body.update(overrides)
        return body

    async def test_owner_can_record_an_adjustment(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            response = await client.post(f"{BASE}/{sid}/adjustments", json=self._body())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["strategy_id"], sid)
            self.assertEqual(body["adjustment_kind"], "owner_reclassification")
            self.assertEqual(body["created_by"], "app:admin")
            self.assertEqual([line["quantity_delta"] for line in body["lines"]], [-10])
        finally:
            self._stop_patches()

    async def test_reclassification_is_owner_only(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
        finally:
            self._stop_patches()

        # Cross-owner: non-disclosing 404, never a 403 with existence leak.
        client = self._client(username="other")
        try:
            response = await client.post(f"{BASE}/{sid}/adjustments", json=self._body())
            self.assertEqual(response.status_code, 404)
        finally:
            self._stop_patches()

        # A worker bearer token cannot reach the owner surface at all.
        client = self._client(username=None)
        try:
            response = await client.post(
                f"{BASE}/{sid}/adjustments", json=self._body(),
                headers={"Authorization": "Bearer kwa_something"},
            )
            self.assertEqual(response.status_code, 401)
        finally:
            self._stop_patches()

    async def test_unknown_and_archived_strategy_refused(self):
        client = self._client()
        try:
            response = await client.post(f"{BASE}/stg-missing/adjustments", json=self._body())
            self.assertEqual(response.status_code, 404)

            created = await self._create(client)
            sid = created["strategy_id"]
            await client.patch(f"{BASE}/{sid}/status", json={"status": "archived"})
            archived = await client.post(f"{BASE}/{sid}/adjustments", json=self._body())
            self.assertEqual(archived.status_code, 409)
            self.assertEqual(archived.json()["detail"]["rejection_reason"], "STRATEGY_ARCHIVED")
        finally:
            self._stop_patches()

    async def test_unbalanced_and_unknown_fields_are_refused(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            # A zero-delta line moves nothing: refused before any row is written.
            zero = await client.post(
                f"{BASE}/{sid}/adjustments", json=self._body(lines=[self._line(0)])
            )
            self.assertEqual(zero.status_code, 422)
            self.assertEqual(
                zero.json()["detail"]["rejection_reason"], "ADJUSTMENT_LINE_ZERO_DELTA"
            )

            # extra="forbid": the two contracts cannot blur.
            extra = await client.post(
                f"{BASE}/{sid}/adjustments", json={**self._body(), "quantity_delta": 5}
            )
            self.assertEqual(extra.status_code, 422)
            empty = await client.post(f"{BASE}/{sid}/adjustments", json=self._body(lines=[]))
            self.assertEqual(empty.status_code, 422)
        finally:
            self._stop_patches()

        with self.factory() as session:
            rows = session.execute(text("SELECT COUNT(*) FROM strategy_attribution_adjustments")).scalar()
        self.assertEqual(rows, 0)


class WorkerProposalSubmissionTests(_ProposalApiHarness):
    """D-8: submission authority fails closed on the G1 binding."""

    async def _setup_strategy(self, client):
        created = await self._create(client)
        return created["strategy_id"]

    async def test_worker_submission_requires_bound_matching_run(self):
        client = self._client()
        try:
            sid = await self._setup_strategy(client)
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")
        # A run bound to a DIFFERENT strategy, and a run bound to nothing at all.
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-B', 'app:admin', 'B', 'kite:paper', 'active')"
                )
            )
            session.commit()
        await self._bind_run("stg-B", run_id="run-other-strategy")

        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        headers = {"Authorization": f"Bearer {raw_token}"}
        try:
            ok = await client.post(
                PROPOSALS_BASE, json=self._payload(strategy_id=sid), headers=headers
            )
            self.assertEqual(ok.status_code, 201, ok.text)
            body = ok.json()
            self.assertEqual(body["status"], "validated")
            self.assertTrue(body["plan"]["plan_id"])
            self.assertFalse(body["idempotent"])

            # Bound to another strategy: refuse, never silently rebind.
            mismatch = await client.post(
                PROPOSALS_BASE, json=self._payload(strategy_id=sid, strategy_run_id="run-other-strategy",
                                                   evaluation_id="eval-mismatch"),
                headers=headers,
            )
            self.assertEqual(mismatch.status_code, 403)
            self.assertEqual(mismatch.json()["detail"]["rejection_reason"], "AUTHORITY_MISMATCH")

            # Unbound run: legacy runs cannot open authority.
            unbound = await client.post(
                PROPOSALS_BASE,
                json=self._payload(strategy_id=sid, strategy_run_id="run-unbound",
                                   evaluation_id="eval-unbound"),
                headers=headers,
            )
            self.assertEqual(unbound.status_code, 403)
            self.assertEqual(unbound.json()["detail"]["rejection_reason"], "AUTHORITY_MISMATCH")

            # Missing / unknown run is a 404 from the shared run loader.
            missing = await client.post(
                PROPOSALS_BASE,
                json=self._payload(strategy_id=sid, strategy_run_id="run-nope",
                                   evaluation_id="eval-missing"),
                headers=headers,
            )
            self.assertEqual(missing.status_code, 404)
        finally:
            self._stop_patches()

        # A refused submission writes no envelope.
        with self.factory() as session:
            count = session.execute(text("SELECT COUNT(*) FROM strategy_proposals")).scalar()
        self.assertEqual(count, 1)

    async def test_worker_submission_rejects_unscoped_action_and_payload_drift(self):
        client = self._client()
        try:
            sid = await self._setup_strategy(client)
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")

        repo, raw_token = self._worker_repo()
        # Token without the action, and a token scoped to another account.
        repo.tokens["worker-ok"]["allowed_actions"] = ["intents:submit"]
        client = self._proposal_client(repo=repo)
        try:
            refused = await client.post(
                PROPOSALS_BASE, json=self._payload(strategy_id=sid),
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(refused.status_code, 403)
        finally:
            self._stop_patches()

        # extra="forbid": an unknown field is a 422, not a silent ignore.
        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            drifted = await client.post(
                PROPOSALS_BASE,
                json={**self._payload(strategy_id=sid), "surprise": 1},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(drifted.status_code, 422)
            no_auth = await client.post(
                PROPOSALS_BASE, json=self._payload(strategy_id=sid)
            )
            self.assertEqual(no_auth.status_code, 401)
        finally:
            self._stop_patches()

    async def test_owner_read_endpoints(self):
        client = self._client()
        try:
            sid = await self._setup_strategy(client)
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")

        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            submitted = await client.post(
                PROPOSALS_BASE, json=self._payload(strategy_id=sid),
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(submitted.status_code, 201, submitted.text)
            proposal_id = submitted.json()["proposal_id"]

            listed = await client.get(f"{BASE}/{sid}/proposals")
            self.assertEqual(listed.status_code, 200, listed.text)
            body = listed.json()
            self.assertEqual(len(body["proposals"]), 1)
            self.assertEqual(body["proposals"][0]["proposal_id"], proposal_id)
            self.assertEqual(body["proposals"][0]["status"], "validated")
            # The journal trail is readable as evidence.
            self.assertEqual(
                [row["event"] for row in body["journal"]], ["received", "plan_created"]
            )

            plan = await client.get(f"{BASE}/{sid}/plans/{proposal_id}")
            self.assertEqual(plan.status_code, 200, plan.text)
            plan_body = plan.json()
            self.assertEqual(plan_body["plan_kind"], "single_instrument")
            # Derived, never stored: a plan read always reports current validity.
            self.assertEqual(plan_body["invalidation_state"]["state"], "valid")
            self.assertEqual(plan_body["invalidation_state"]["reason"], "CATALOG_GENERATION_CURRENT")

            missing = await client.get(f"{BASE}/{sid}/plans/no-such-proposal")
            self.assertEqual(missing.status_code, 404)
        finally:
            self._stop_patches()

    async def test_worker_cannot_read_owner_endpoints(self):
        client = self._client()
        try:
            sid = await self._setup_strategy(client)
        finally:
            self._stop_patches()
        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo, username=None)
        try:
            for path in (f"{BASE}/{sid}/proposals", f"{BASE}/{sid}/plans/x"):
                response = await client.get(
                    path, headers={"Authorization": f"Bearer {raw_token}"}
                )
                self.assertEqual(response.status_code, 401)
        finally:
            self._stop_patches()


class AdmissionOwnerApiTests(_ProposalApiHarness):
    """Task 5 (G9+G10+G6): owner surfaces, read-only previews, workers refused."""

    async def _submitted(self):
        """A validated live plan plus its ids, reached through the worker route."""
        client = self._client()
        try:
            sid = (await self._create(client))["strategy_id"]
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")
        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            response = await client.post(
                PROPOSALS_BASE,
                json=self._payload(strategy_id=sid),
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(response.status_code, 201, response.text)
        finally:
            self._stop_patches()
        return sid, response.json()["plan"]["plan_id"]

    def _with_env(self, client):
        return client

    async def test_admission_preview_does_not_reserve(self):
        sid, plan_id = await self._submitted()
        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            # No policy yet: the preview reports the named refusal.
            preview = await client.post(f"{BASE}/{sid}/plans/{plan_id}/admission")
            self.assertEqual(preview.status_code, 200, preview.text)
            body = preview.json()
            self.assertFalse(body["admitted"])
            self.assertEqual(body["rejection_reason"], "ADMISSION_POLICY_MISSING")

            # A preview is NOT a reservation: nothing was written.
            with self.factory() as session:
                reservations = session.execute(
                    text("SELECT COUNT(*) FROM strategy_reservations")
                ).scalar()
            self.assertEqual(reservations, 0)

            # Record a policy, preview again: admitted, and still nothing reserved.
            policy = await client.put(
                f"{BASE}/{sid}/admission-policy", json={"allocation_inr": 100000.0}
            )
            self.assertEqual(policy.status_code, 200, policy.text)
            self.assertEqual(policy.json()["allocation_inr"], 100000.0)

            admitted = await client.post(f"{BASE}/{sid}/plans/{plan_id}/admission")
            self.assertEqual(admitted.status_code, 200, admitted.text)
            self.assertTrue(admitted.json()["admitted"], admitted.text)
            with self.factory() as session:
                reservations = session.execute(
                    text("SELECT COUNT(*) FROM strategy_reservations")
                ).scalar()
            self.assertEqual(reservations, 0)
        finally:
            self._stop_patches()

    async def test_reserve_then_approve_then_revoke(self):
        sid, plan_id = await self._submitted()
        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            await client.put(f"{BASE}/{sid}/admission-policy", json={"allocation_inr": 100000.0})
            reserved = await client.post(f"{BASE}/{sid}/plans/{plan_id}/reserve")
            self.assertEqual(reserved.status_code, 200, reserved.text)
            reservation = reserved.json()
            self.assertEqual(reservation["status"], "active")

            # One plan claims capacity once: a second reserve is idempotent.
            again = await client.post(f"{BASE}/{sid}/plans/{plan_id}/reserve")
            self.assertEqual(again.json()["reservation_id"], reservation["reservation_id"])

            listed = await client.get(f"{BASE}/{sid}/reservations")
            self.assertEqual(len(listed.json()["reservations"]), 1)

            approved = await client.post(
                f"{BASE}/{sid}/plans/{plan_id}/approval",
                json={"reservation_id": reservation["reservation_id"], "validity_seconds": 600},
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            approval = approved.json()
            self.assertTrue(approval["structural_validity"]["valid"], approval["structural_validity"])

            # A duplicate approval with identical pins adds nothing.
            duplicate = await client.post(
                f"{BASE}/{sid}/plans/{plan_id}/approval",
                json={"reservation_id": reservation["reservation_id"]},
            )
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(
                duplicate.json()["detail"]["rejection_reason"], "APPROVAL_ALREADY_ACTIVE"
            )

            history = await client.get(f"{BASE}/{sid}/approvals")
            self.assertEqual(len(history.json()["approvals"]), 1)

            revoked = await client.post(f"{BASE}/{sid}/plans/{plan_id}/approval/revoke")
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(revoked.json()["status"], "revoked")
        finally:
            self._stop_patches()

    async def test_reserve_refuses_when_capacity_is_committed(self):
        sid, plan_id = await self._submitted()
        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            # A tiny allocation that the plan cannot fit inside.
            await client.put(f"{BASE}/{sid}/admission-policy", json={"allocation_inr": 1.0})
            refused = await client.post(f"{BASE}/{sid}/plans/{plan_id}/reserve")
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()["detail"]["rejection_reason"], "ALLOCATION_EXCEEDED")
        finally:
            self._stop_patches()

    async def test_workers_and_foreign_owners_are_refused(self):
        sid, plan_id = await self._submitted()
        repo, raw_token = self._worker_repo()

        # A worker token cannot reach the owner surface at all.
        client = self._proposal_client(repo=repo, username=None)
        try:
            for path in (
                f"{BASE}/{sid}/plans/{plan_id}/admission",
                f"{BASE}/{sid}/plans/{plan_id}/reserve",
            ):
                response = await client.post(
                    path, headers={"Authorization": f"Bearer {raw_token}"},
                    json={"reservation_id": "x"},
                )
                self.assertEqual(response.status_code, 401, path)
        finally:
            self._stop_patches()

        # A different app user gets a non-disclosing 404 from the owner check.
        client = self._proposal_client(repo=repo, username="someone-else")
        try:
            response = await client.post(f"{BASE}/{sid}/plans/{plan_id}/reserve")
            self.assertEqual(response.status_code, 404)
            listed = await client.get(f"{BASE}/{sid}/reservations")
            self.assertEqual(listed.status_code, 404)
        finally:
            self._stop_patches()

    async def test_unlisted_account_is_refused(self):
        """A plan on an account outside the operator allowlist writes nothing."""
        from unittest.mock import patch as _patch

        sid, plan_id = await self._submitted()
        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            # The harness authorizes kite:paper; deny everything for this call.
            with _patch.dict("os.environ", {"HOSTED_STRATEGY_ACCOUNT_SCOPES": "kite:elsewhere"}):
                for path in (
                    f"{BASE}/{sid}/plans/{plan_id}/admission",
                    f"{BASE}/{sid}/plans/{plan_id}/reserve",
                ):
                    response = await client.post(path)
                    self.assertEqual(response.status_code, 403, path)
        finally:
            self._stop_patches()

    async def test_request_models_forbid_extra_fields(self):
        sid, _ = await self._submitted()
        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            drifted = await client.put(
                f"{BASE}/{sid}/admission-policy",
                json={"allocation_inr": 10.0, "surprise": 1},
            )
            self.assertEqual(drifted.status_code, 422)
            negative = await client.put(
                f"{BASE}/{sid}/admission-policy", json={"allocation_inr": -5.0}
            )
            self.assertEqual(negative.status_code, 422)
        finally:
            self._stop_patches()


class SquareoffReadApiTests(_ProposalApiHarness):
    """D-6: the owner read is owner-scoped, worker-proof and validated."""

    async def test_owner_reads_square_off_evidence(self):
        client = self._client()
        try:
            sid = (await self._create(client))["strategy_id"]
        finally:
            self._stop_patches()

        from datetime import date, datetime, timezone

        from backend.strategies.mis_squareoff import (
            MisSquareoffEvidenceStore,
            SquareoffRecord,
        )

        MisSquareoffEvidenceStore(session_factory=self.factory).record(
            SquareoffRecord(
                account_id="kite:paper", strategy_id=sid, strategy_run_id="run-1",
                product="MIS", session_date=date(2026, 10, 15), exchange="NSE",
                scheduled_at=datetime(2026, 10, 15, 9, 50, tzinfo=timezone.utc),
                outcome="squared_off", exit_claim_id="claim-1", detail={"quantity": -40},
            )
        )

        repo, _ = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            listed = await client.get(f"{BASE}/{sid}/squareoffs")
            self.assertEqual(listed.status_code, 200, listed.text)
            rows = listed.json()["squareoffs"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "squared_off")
            self.assertEqual(rows[0]["exit_claim_id"], "claim-1")
            self.assertEqual(rows[0]["product"], "MIS")

            # An unrecognised environment is a mistake, not "all".
            bad = await client.get(f"{BASE}/{sid}/squareoffs?environment=nope")
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(bad.json()["detail"]["rejection_reason"], "ENVIRONMENT_INVALID")

            # extra="forbid" on the response model is the contract; the query
            # parameter surface stays closed to unknown values by validation.
            for scope in ("all", "paper", "dry_run", "live"):
                ok = await client.get(f"{BASE}/{sid}/squareoffs?environment={scope}")
                self.assertEqual(ok.status_code, 200, scope)
        finally:
            self._stop_patches()

    async def test_workers_and_foreign_owners_cannot_read_square_offs(self):
        client = self._client()
        try:
            sid = (await self._create(client))["strategy_id"]
        finally:
            self._stop_patches()
        repo, raw_token = self._worker_repo()

        client = self._proposal_client(repo=repo, username=None)
        try:
            response = await client.get(
                f"{BASE}/{sid}/squareoffs",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(response.status_code, 401)
        finally:
            self._stop_patches()

        client = self._proposal_client(repo=repo, username="someone-else")
        try:
            response = await client.get(f"{BASE}/{sid}/squareoffs")
            self.assertEqual(response.status_code, 404)
        finally:
            self._stop_patches()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Task 5 (G7): owner settlement evidence surfaces
# ---------------------------------------------------------------------------


class SettlementApiTests(_OwnerApiHarness):
    """GET /settlement and POST /settlement/assess: owner-only, account-authorized,
    worker-proof, and honest about staleness (a settled snapshot is not a state)."""

    def _barrier(self):
        from backend.strategies.settlement import ExecutionBarrier

        return ExecutionBarrier(session_factory=self.factory)

    async def _assess(self, client, sid, **body):
        return await client.post(f"{BASE}/{sid}/settlement/assess", json=body)

    async def test_owner_can_assess_and_read_the_snapshot(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            # No proof yet: the snapshot is honest (quiescence unknown ⇒ unknown),
            # and it is persisted so the read surface has something to return.
            assessed = await self._assess(client, sid)
            self.assertEqual(assessed.status_code, 200, assessed.text)
            body = assessed.json()
            self.assertEqual(body["overall"], "unknown")
            self.assertEqual(body["strategy_id"], sid)
            self.assertEqual(body["account_id"], "kite:paper")
            self.assertEqual(body["execution_environment"], "live")
            self.assertFalse(body["stale"])
            for axis in (
                "quiescence",
                "attribution_scoped_flatness",
                "terminal_domain_state",
                "no_live_evaluation_authority",
            ):
                self.assertIn(axis, body["axes"])
                self.assertIn("state", body["axes"][axis])
                self.assertIn("evidence_digest", body["axes"][axis])
            self.assertEqual(body["axes"]["quiescence"]["state"], "unknown")

            # Read back the latest snapshot.
            fetched = await client.get(f"{BASE}/{sid}/settlement")
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertEqual(fetched.json()["assessment_id"], body["assessment_id"])
            self.assertEqual(fetched.json()["overall"], "unknown")
        finally:
            self._stop_patches()

    async def test_valid_proof_settles_and_a_later_work_event_marks_it_stale(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]

            barrier = self._barrier()
            barrier.record_proof(
                account_id="kite:paper", strategy_id=sid, execution_environment="live"
            )
            settled = await self._assess(client, sid)
            self.assertEqual(settled.status_code, 200, settled.text)
            self.assertEqual(settled.json()["overall"], "settled")
            self.assertFalse(settled.json()["stale"])

            # A late fill (work_created) invalidates: the snapshot is detectably
            # stale on read — it is never rewritten into a fresh-looking state.
            barrier.record_work_event(
                account_id="kite:paper", strategy_id=sid, execution_environment="live",
                event="work_created", ref="fill:late-1",
            )
            fetched = await client.get(f"{BASE}/{sid}/settlement")
            self.assertEqual(fetched.status_code, 200)
            self.assertTrue(fetched.json()["stale"])
            self.assertEqual(fetched.json()["overall"], "settled")  # snapshot, not state
        finally:
            self._stop_patches()

    async def test_read_without_any_assessment_is_404(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            response = await client.get(f"{BASE}/{sid}/settlement")
            self.assertEqual(response.status_code, 404, response.text)
        finally:
            self._stop_patches()

    async def test_surfaces_are_owner_scoped_and_cross_owner_is_404(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            await self._assess(client, sid)
        finally:
            self._stop_patches()

        client = self._client(username="other")
        try:
            self.assertEqual(
                (await client.get(f"{BASE}/{sid}/settlement")).status_code, 404
            )
            self.assertEqual(
                (await self._assess(client, sid)).status_code, 404
            )
        finally:
            self._stop_patches()

    async def test_worker_bearer_token_cannot_reach_settlement_surfaces(self):
        client = self._client(username=None)
        try:
            headers = {"Authorization": "Bearer kwa_something"}
            self.assertEqual(
                (await client.get(f"{BASE}/stg-1/settlement", headers=headers)).status_code, 401
            )
            self.assertEqual(
                (await client.post(f"{BASE}/stg-1/settlement/assess", json={}, headers=headers)).status_code,
                401,
            )
        finally:
            self._stop_patches()

    async def test_unauthorized_account_is_403(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            # Move the canonical strategy's account out of the authorized scopes:
            # ownership holds, account authorization does not.
            with self.factory() as session:
                session.execute(
                    text("UPDATE strategies SET account_scope = 'kite:OTHER' WHERE id = :sid"),
                    {"sid": sid},
                )
                session.commit()
            self.assertEqual(
                (await client.get(f"{BASE}/{sid}/settlement")).status_code, 403
            )
            self.assertEqual((await self._assess(client, sid)).status_code, 403)
        finally:
            self._stop_patches()

    async def test_environment_is_validated_and_unknown_fields_are_refused(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            self.assertEqual(
                (await client.get(f"{BASE}/{sid}/settlement?environment=banana")).status_code, 422
            )
            self.assertEqual(
                (await self._assess(client, sid, environment="banana")).status_code, 422
            )
            self.assertEqual(
                (await self._assess(client, sid, environment="paper", bogus=True)).status_code, 422
            )
            paper = await self._assess(client, sid, environment="paper")
            self.assertEqual(paper.status_code, 200, paper.text)
            self.assertEqual(paper.json()["execution_environment"], "paper")
        finally:
            self._stop_patches()

    async def test_assess_enforces_same_origin(self):
        client = self._client()
        try:
            created = await self._create(client)
            sid = created["strategy_id"]
            response = await client.post(
                f"{BASE}/{sid}/settlement/assess",
                json={},
                headers={"Origin": "http://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self._stop_patches()


# ---------------------------------------------------------------------------
# Phase 6 (Project 6): owner execute + execution-trail surfaces (D-7)
# ---------------------------------------------------------------------------


class ExecutionOwnerApiTests(_ProposalApiHarness):
    """POST .../plans/{plan_id}/execute and GET .../plans/{plan_id}/executions.

    Paper-only by construction (the executor refuses anything else by name),
    owner + account-authorized, worker-proof, and the trail is read-only.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from backend.strategies.attribution_models import (
            StrategyExecutionBarrier,
            StrategyExecutionBarrierEvent,
            StrategyPlanExecutionEvent,
            StrategyPositionProjection,
            StrategyReservation,
            StrategyReservationEvent,
            StrategyRunBinding,
        )

        Base.metadata.create_all(
            self.engine,
            tables=[
                StrategyRunBinding.__table__,
                StrategyReservation.__table__,
                StrategyReservationEvent.__table__,
                StrategyPlanExecutionEvent.__table__,
                StrategyPositionProjection.__table__,
                StrategyExecutionBarrier.__table__,
                StrategyExecutionBarrierEvent.__table__,
            ],
        )
        # The shared catalog fixture predates the lot/instrument-type columns;
        # widen it additively (fresh in-memory table per test, so always clean).
        with self.factory() as session:
            for ddl in (
                "ALTER TABLE public.instrument_catalog_records ADD COLUMN lot_size INTEGER",
                "ALTER TABLE public.instrument_catalog_records ADD COLUMN instrument_type TEXT",
            ):
                try:
                    session.execute(text(ddl))
                except Exception:  # noqa: BLE001 - column already present
                    pass
            session.commit()

    def _stop_patches(self):
        for patcher in (
            getattr(self, "_margin", None),
            getattr(self, "_patch", None),
            getattr(self, "_env", None),
            getattr(self, "_paper_patchers", None),
        ):
            if patcher is not None:
                if isinstance(patcher, (list, tuple)):
                    for one in patcher:
                        one.stop()
                else:
                    patcher.stop()

    def _wire_extra_state(self, app):
        from unittest.mock import patch as _patch

        from backend.strategies.execution import PaperPlanExecutor
        from tests.strategies.test_execution import (
            FakeInstrumentsRepository,
            FakeMarketRuntime,
            _FakePaperRepository,
        )
        from decimal import Decimal

        from backend.paper_runtime.service import PaperTradingService

        async def _inline_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        # The paper runtime must not spawn threads or reach Redis in tests.
        patchers = [
            _patch("backend.paper_runtime.service.asyncio.to_thread", new=_inline_to_thread),
            _patch("backend.paper_runtime.service.publish_event", autospec=True),
        ]
        for patcher in patchers:
            patcher.start()
        self._paper_patchers = patchers

        paper_service = PaperTradingService(
            repository=_FakePaperRepository(),
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=FakeMarketRuntime(),
            default_starting_balance=Decimal("100000"),
        )
        app.state.paper_plan_executor = PaperPlanExecutor(
            session_factory=self.factory, paper_service=paper_service
        )

    async def _admitted_plan(self, *, environment="paper"):
        """A validated plan, a policy, and an active reservation on the account."""
        client = self._client()
        try:
            sid = (await self._create(client))["strategy_id"]
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")
        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            submitted = await client.post(
                PROPOSALS_BASE,
                json=self._payload(strategy_id=sid),
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(submitted.status_code, 201, submitted.text)
            plan_id = submitted.json()["plan"]["plan_id"]
            policy = await client.put(
                f"{BASE}/{sid}/admission-policy", json={"allocation_inr": 100000.0}
            )
            self.assertEqual(policy.status_code, 200, policy.text)
            reserved = await client.post(
                f"{BASE}/{sid}/plans/{plan_id}/reserve?execution_environment={environment}"
            )
            self.assertEqual(reserved.status_code, 200, reserved.text)
        finally:
            self._stop_patches()
        return repo, sid, plan_id

    async def test_owner_executes_an_admitted_paper_plan_and_reads_the_trail(self):
        repo, sid, plan_id = await self._admitted_plan(environment="paper")
        client = self._proposal_client(repo=repo)
        try:
            executed = await client.post(f"{BASE}/{sid}/plans/{plan_id}/execute")
            self.assertEqual(executed.status_code, 200, executed.text)
            body = executed.json()
            self.assertEqual(body["plan_id"], plan_id)
            self.assertEqual(body["status"], "filled")
            (step,) = body["steps"]
            self.assertEqual(step["event"], "filled")
            self.assertEqual(step["filled_quantity"], 10)
            self.assertTrue(step["paper_order_id"].startswith("PAPER-"))

            # The event trail surface returns the append-only facts in order.
            trail = await client.get(f"{BASE}/{sid}/plans/{plan_id}/executions")
            self.assertEqual(trail.status_code, 200, trail.text)
            events = trail.json()["events"]
            self.assertEqual([row["event"] for row in events], ["submitted", "filled"])
            self.assertEqual(events[1]["paper_order_id"], step["paper_order_id"])
            self.assertEqual(events[1]["filled_quantity"], 10)

            # The fill CONSUMED the reservation (D-4), visible on the owner surface.
            listed = await client.get(f"{BASE}/{sid}/reservations")
            self.assertEqual(listed.json()["reservations"][0]["status"], "consumed")

            # A plan executes once, ever.
            again = await client.post(f"{BASE}/{sid}/plans/{plan_id}/execute")
            self.assertEqual(again.status_code, 409, again.text)
            self.assertEqual(
                again.json()["detail"]["rejection_reason"], "PLAN_ALREADY_EXECUTED"
            )
        finally:
            self._stop_patches()

    async def test_execute_without_a_reservation_is_a_named_409(self):
        client = self._client()
        try:
            sid = (await self._create(client))["strategy_id"]
        finally:
            self._stop_patches()
        await self._bind_run(sid, run_id="run-bound")
        repo, raw_token = self._worker_repo()
        client = self._proposal_client(repo=repo)
        try:
            submitted = await client.post(
                PROPOSALS_BASE,
                json=self._payload(strategy_id=sid),
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            self.assertEqual(submitted.status_code, 201, submitted.text)
            plan_id = submitted.json()["plan"]["plan_id"]

            refused = await client.post(f"{BASE}/{sid}/plans/{plan_id}/execute")
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"]["rejection_reason"], "RESERVATION_REQUIRED"
            )
            # The named refusal is itself an event in the trail.
            trail = await client.get(f"{BASE}/{sid}/plans/{plan_id}/executions")
            self.assertEqual(
                [row["refusal_reason"] for row in trail.json()["events"]],
                ["RESERVATION_REQUIRED"],
            )
        finally:
            self._stop_patches()

    async def test_execute_of_a_live_reservation_refuses_paper_only(self):
        repo, sid, plan_id = await self._admitted_plan(environment="live")
        client = self._proposal_client(repo=repo)
        try:
            refused = await client.post(f"{BASE}/{sid}/plans/{plan_id}/execute")
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"]["rejection_reason"], "PAPER_ONLY_EXECUTION"
            )
        finally:
            self._stop_patches()

    async def test_execute_and_trail_are_worker_proof_and_cross_owner_404(self):
        repo, sid, plan_id = await self._admitted_plan(environment="paper")
        _, raw_token = self._worker_repo()

        client = self._proposal_client(repo=repo, username=None)
        try:
            for method, path in (
                ("post", f"{BASE}/{sid}/plans/{plan_id}/execute"),
                ("get", f"{BASE}/{sid}/plans/{plan_id}/executions"),
            ):
                response = await getattr(client, method)(
                    path, headers={"Authorization": f"Bearer {raw_token}"}
                )
                self.assertEqual(response.status_code, 401, path)
        finally:
            self._stop_patches()

        client = self._proposal_client(repo=repo, username="someone-else")
        try:
            foreign_execute = await client.post(f"{BASE}/{sid}/plans/{plan_id}/execute")
            self.assertEqual(foreign_execute.status_code, 404)
            foreign_trail = await client.get(f"{BASE}/{sid}/plans/{plan_id}/executions")
            self.assertEqual(foreign_trail.status_code, 404)
        finally:
            self._stop_patches()

    async def test_trail_for_an_unknown_plan_is_404(self):
        repo, sid, plan_id = await self._admitted_plan(environment="paper")
        client = self._proposal_client(repo=repo)
        try:
            response = await client.get(f"{BASE}/{sid}/plans/no-such-plan/executions")
            self.assertEqual(response.status_code, 404)
        finally:
            self._stop_patches()

    async def test_execute_enforces_same_origin(self):
        repo, sid, plan_id = await self._admitted_plan(environment="paper")
        client = self._proposal_client(repo=repo)
        try:
            response = await client.post(
                f"{BASE}/{sid}/plans/{plan_id}/execute",
                headers={"Origin": "http://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self._stop_patches()
