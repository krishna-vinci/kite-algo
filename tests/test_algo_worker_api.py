import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers.algo_workers import (  # noqa: E402
    DEFAULT_WORKER_ACTIONS,
    WorkerIntentRequest,
    WorkerRiskPatchRequest,
    WorkerRunCreateRequest,
    WorkerToken,
    WorkerTokenCreateRequest,
    create_worker_run,
    create_worker_token,
    patch_worker_run_risk,
    submit_worker_intent,
)
from api.routers.algo_workers import _hash_token  # noqa: E402


class _FakeWorkerRepository:
    def __init__(self, *, raw_token="secret-token", token=None):
        self.raw_token = raw_token
        self.token = token or WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:paper-a",
            allowed_modes=["paper", "dry_run"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        self.tokens = {}
        self.runs = {}
        self.intent_results = {}
        self.touched = []

    async def create_token(self, payload, *, raw_token, token_id):
        self.tokens[token_id] = {
            "token_id": token_id,
            "name": payload.name,
            "account_scope": payload.account_scope,
            "allowed_modes": payload.allowed_modes,
            "allowed_actions": payload.allowed_actions,
            "allowed_templates": payload.allowed_templates,
            "status": "active",
            "created_at": None,
            "expires_at": payload.expires_at,
            "last_used_at": None,
        }
        return dict(self.tokens[token_id])

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        self.touched.append(token_id)

    async def create_run(self, token, payload, *, strategy_run_id):
        run = {
            "strategy_run_id": strategy_run_id,
            "token_id": token.token_id,
            "template_id": payload.template_id,
            "account_scope": payload.account_scope,
            "execution_mode": payload.execution_mode,
            "status": "open",
            "summary_fields": payload.summary_fields,
            "risk_schema": payload.risk_schema,
            "allowed_actions": payload.allowed_actions,
            "runtime_state": payload.runtime_state,
            "metadata": payload.metadata,
        }
        self.runs[strategy_run_id] = run
        return dict(run)

    async def get_run(self, strategy_run_id):
        run = self.runs.get(strategy_run_id)
        return dict(run) if run else None

    async def update_run_risk(self, strategy_run_id, patch):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        risk = dict(state.get("risk") or {})
        risk.update(patch)
        state["risk"] = risk
        run["runtime_state"] = state
        run["risk_schema"] = [
            {**field, "value": patch.get(field.get("key"), field.get("value"))}
            for field in run.get("risk_schema", [])
        ]
        return dict(run)

    async def get_intent_result(self, strategy_run_id, idempotency_key):
        return self.intent_results.get((strategy_run_id, idempotency_key))

    async def save_intent_result(self, *, token_id, strategy_run_id, request, status, result):
        self.intent_results[(strategy_run_id, request.idempotency_key)] = result
        return result


class AlgoWorkerApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, repo, *, paper_runtime=None, raw_token="secret-token"):
        return SimpleNamespace(
            headers={"authorization": f"Bearer {raw_token}"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=paper_runtime)),
        )

    async def test_admin_token_creation_rejects_live_scope_in_v1(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", allowed_modes=["paper", "live"])

        with patch("api.routers.algo_workers.require_app_user", return_value=SimpleNamespace(username="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_token(request, payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(repo.tokens, {})

    async def test_worker_can_create_paper_run_and_submit_idempotent_basket_intent(self):
        repo = _FakeWorkerRepository()
        paper_runtime = SimpleNamespace(place_basket=AsyncMock(return_value={"mode": "paper", "status": "success", "results": []}))
        request = self._request(repo, paper_runtime=paper_runtime)

        run = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-worker-1",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
                risk_schema=[{"key": "stop_loss_pct", "label": "Stop loss", "type": "number", "value": 1.2}],
            ),
        )

        self.assertEqual(run["strategy_run_id"], "run-worker-1")

        payload = WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key="entry-0001",
            payload={"orders": [{"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1}]},
        )
        first = await submit_worker_intent(request, "run-worker-1", payload)
        second = await submit_worker_intent(request, "run-worker-1", payload)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "deduped")
        paper_runtime.place_basket.assert_awaited_once()
        call = paper_runtime.place_basket.await_args.kwargs
        self.assertEqual(call["account_scope"], "kite:paper-a")
        self.assertEqual(call["attribution"]["strategy_run_id"], "run-worker-1")
        self.assertEqual(call["attribution"]["source"], "algo_worker")

    async def test_worker_risk_patch_updates_runtime_state_and_schema_values(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-risk",
                template_id="momentum",
                account_scope="kite:paper-a",
                risk_schema=[{"key": "trailing_distance", "label": "Trail", "type": "number", "value": 3.0}],
            ),
        )

        updated = await patch_worker_run_risk(request, "run-risk", WorkerRiskPatchRequest(patch={"trailing_distance": 2.0}))

        self.assertEqual(updated["runtime_state"]["risk"]["trailing_distance"], 2.0)
        self.assertEqual(updated["risk_schema"][0]["value"], 2.0)

    async def test_worker_intent_rejects_live_run_in_v1(self):
        token = WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:paper-a",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "live",
            "status": "open",
        }
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await submit_worker_intent(
                request,
                "run-live",
                WorkerIntentRequest(intent_type="place_order", idempotency_key="live-0001", payload={"order": {}}),
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("paper and dry_run", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
