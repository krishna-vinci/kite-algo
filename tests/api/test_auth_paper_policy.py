import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import types

from fastapi import HTTPException

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

if "broker_api.broker_api" not in sys.modules:
    broker_api_stub = types.ModuleType("broker_api.broker_api")
    broker_api_stub.headless_login = lambda *args, **kwargs: {"ok": True}
    broker_api_stub.logout = lambda *args, **kwargs: {"ok": True}
    broker_api_stub.profile = lambda *args, **kwargs: {"ok": True}
    broker_api_stub.holdings = lambda *args, **kwargs: {"ok": True}
    broker_api_stub.get_margins = lambda *args, **kwargs: {"ok": True}
    sys.modules["broker_api.broker_api"] = broker_api_stub

from backend.algo_runtime.models import AlgoInstance, AlgoLifecycleState, DependencySpec, ExecutionMode  # noqa: E402
from backend.api.routers.auth import (  # noqa: E402
    PaperAccountResetRequest,
    PaperAccountUpsertRequest,
    PaperStrategyExitRequest,
    exit_paper_strategy,
    list_paper_strategies,
    reset_paper_account,
    upsert_paper_account,
)


class _FakeAlgoRepository:
    def __init__(self, instances):
        self.instances = list(instances)

    async def list_active_instances(self):
        return list(self.instances)


class _FakePaperRuntimeService:
    def __init__(self):
        self.reset_calls = []
        self.ensure_calls = []
        self.exit_calls = []
        self.summary = {"account": {"account_scope": "default"}, "strategies": []}

    async def reset_account(self, account_scope, *, starting_balance=None):
        self.reset_calls.append({"account_scope": account_scope, "starting_balance": starting_balance})
        return {"ok": True, "account_scope": account_scope}

    async def ensure_account(self, account_scope, *, starting_balance=None):
        self.ensure_calls.append({"account_scope": account_scope, "starting_balance": starting_balance})
        return SimpleNamespace(model_dump=lambda mode=None: {"account_scope": account_scope, "starting_balance": starting_balance})

    async def get_strategy_summary(self, account_scope):
        return self.summary

    async def exit_strategy(self, *, account_scope: str, strategy_id: str):
        self.exit_calls.append({"account_scope": account_scope, "strategy_id": strategy_id})
        return {"status": "success", "strategy_id": strategy_id, "results": []}


class _FakePaperMarketEngine:
    def __init__(self):
        self.sync_calls = 0

    async def sync_subscriptions(self):
        self.sync_calls += 1


class AuthPaperPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_blocked_when_active_paper_instance_uses_scope(self):
        paper_service = _FakePaperRuntimeService()
        active_instances = [
            AlgoInstance(
                instance_id="paper-1",
                algo_type="demo",
                status=AlgoLifecycleState.ENABLED,
                execution_mode=ExecutionMode.PAPER,
                dependency_spec=DependencySpec(account_scope="kite:paper-a"),
            ),
            AlgoInstance(
                instance_id="live-1",
                algo_type="demo",
                status=AlgoLifecycleState.ENABLED,
                execution_mode=ExecutionMode.LIVE,
                dependency_spec=DependencySpec(account_scope="kite:paper-a"),
            ),
        ]
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    paper_runtime_service=paper_service,
                    algo_runtime_service=SimpleNamespace(kernel=SimpleNamespace(repository=_FakeAlgoRepository(active_instances))),
                    paper_market_engine=None,
                )
            )
        )

        with patch("api.routers.auth.require_app_user", return_value=SimpleNamespace(username="admin", role="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await reset_paper_account(request, "kite:paper-a", PaperAccountResetRequest(starting_balance=250000, force=False))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["account_scope"], "kite:paper-a")
        self.assertEqual(ctx.exception.detail["active_instance_ids"], ["paper-1"])
        self.assertEqual(paper_service.reset_calls, [])

    async def test_reset_allows_force_even_with_active_paper_instance(self):
        paper_service = _FakePaperRuntimeService()
        market_engine = _FakePaperMarketEngine()
        active_instances = [
            AlgoInstance(
                instance_id="paper-1",
                algo_type="demo",
                status=AlgoLifecycleState.RUNNING,
                execution_mode=ExecutionMode.PAPER,
                dependency_spec=DependencySpec(account_scope="kite:paper-a"),
            )
        ]
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    paper_runtime_service=paper_service,
                    algo_runtime_service=SimpleNamespace(kernel=SimpleNamespace(repository=_FakeAlgoRepository(active_instances))),
                    paper_market_engine=market_engine,
                )
            )
        )

        with patch("api.routers.auth.require_app_user", return_value=SimpleNamespace(username="admin", role="admin")):
            result = await reset_paper_account(request, "kite:paper-a", PaperAccountResetRequest(starting_balance=500000, force=True))

        self.assertEqual(result["ok"], True)
        self.assertEqual(len(paper_service.reset_calls), 1)
        self.assertEqual(paper_service.reset_calls[0]["starting_balance"], 500000)
        self.assertEqual(market_engine.sync_calls, 1)

    async def test_upsert_account_requires_auth(self):
        paper_service = _FakePaperRuntimeService()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(paper_runtime_service=paper_service)))

        with patch("api.routers.auth.require_app_user", side_effect=HTTPException(status_code=401, detail="Unauthorized")):
            with self.assertRaises(HTTPException) as ctx:
                await upsert_paper_account(request, "kite:paper-a", PaperAccountUpsertRequest(starting_balance=150000))

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(paper_service.ensure_calls, [])

    async def test_reset_blocked_when_runtime_visibility_is_unavailable(self):
        paper_service = _FakePaperRuntimeService()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(paper_runtime_service=paper_service, algo_runtime_service=None, paper_market_engine=None)))

        with patch("api.routers.auth.require_app_user", return_value=SimpleNamespace(username="admin", role="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await reset_paper_account(request, "kite:paper-a", PaperAccountResetRequest(starting_balance=250000, force=False))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(paper_service.reset_calls, [])

    async def test_list_paper_strategies_exposes_exit_capabilities_for_manual_and_tracked_groups(self):
        paper_service = _FakePaperRuntimeService()
        paper_service.summary = {
            "account": {"account_scope": "default"},
            "strategies": [
                {
                    "strategy_id": "run-1",
                    "is_open": True,
                    "mode": "paper",
                    "summary_fields": [],
                    "capabilities": {"can_exit_strategy": False, "exit_reason": "pending"},
                },
                {
                    "strategy_id": "manual:256265:MIS",
                    "is_open": True,
                    "mode": "paper",
                    "summary_fields": [],
                    "capabilities": {"can_exit_strategy": False, "exit_reason": "manual"},
                },
            ],
        }
        store = SimpleNamespace(get_run=lambda run_id: {"execution_mode": "paper", "canonical_strategy": {}, "algo_instance_id": "algo-1"} if run_id == "run-1" else None)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(paper_runtime_service=paper_service, option_strategy_store=store, journal_service=None)))

        with patch("api.routers.auth.require_app_user", return_value=SimpleNamespace(username="admin", role="admin")):
            result = await list_paper_strategies(request, account_scope="default")

        tracked = next(item for item in result["strategies"] if item["strategy_id"] == "run-1")
        manual = next(item for item in result["strategies"] if item["strategy_id"].startswith("manual:"))
        self.assertEqual(tracked["capabilities"]["can_exit_strategy"], True)
        self.assertIsNone(tracked["capabilities"]["exit_reason"])
        self.assertIn("exit_strategy", tracked["capabilities"]["allowed_actions"])
        self.assertEqual(tracked["capabilities"]["risk_schema"], [])
        self.assertEqual(manual["capabilities"]["can_exit_strategy"], False)
        self.assertIn("manual paper activity", manual["capabilities"]["exit_reason"].lower())
        self.assertEqual(manual["capabilities"]["allowed_actions"], [])
        self.assertEqual(manual["capabilities"]["risk_schema"], [])

    async def test_exit_strategy_rejects_manual_groups_before_store_lookup(self):
        paper_service = _FakePaperRuntimeService()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(paper_runtime_service=paper_service, option_strategy_store=SimpleNamespace(get_run=lambda _run_id: None), journal_service=None)))

        with patch("api.routers.auth.require_app_user", return_value=SimpleNamespace(username="admin", role="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await exit_paper_strategy(request, "default", PaperStrategyExitRequest(strategy_id="manual:256265:MIS"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(paper_service.exit_calls, [])
