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


if __name__ == "__main__":
    unittest.main()
