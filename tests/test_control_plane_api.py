# pyright: reportArgumentType=false
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.control_plane import (  # noqa: E402
    build_empty_snapshot,
    build_strategy_positions_snapshot,
    cancel_control_strategy_orders,
    compute_worker_health,
    exit_control_strategy,
)
from api.routers.control import router as control_router  # noqa: E402


class _FakeWorkerRepository:
    async def list_runs_for_control_plane(self):
        return [
            {
                "strategy_run_id": "run-live-1",
                "template_id": "mean-reversion",
                "account_scope": "kite:AB1234",
                "execution_mode": "live",
                "status": "open",
                "summary_fields": [],
                "risk_schema": [],
                "allowed_actions": ["exit_strategy"],
                "runtime_state": {},
                "metadata": {"strategy_name": "Mean Reversion"},
                "token_id": "worker-token-1",
                "worker_name": "ml-box-worker",
                "last_heartbeat_at": datetime(2026, 4, 25, 11, 59, 30, tzinfo=timezone.utc),
                "token_last_heartbeat_at": datetime(2026, 4, 25, 11, 59, 29, tzinfo=timezone.utc),
                "worker_session_nonce": "nonce-1",
                "heartbeat_json": {"worker_id": "w-1", "metrics": {"machine_id": "ml-box-01"}},
            }
        ]

    async def get_run(self, strategy_run_id):
        for run in await self.list_runs_for_control_plane():
            if run["strategy_run_id"] == strategy_run_id:
                return dict(run)
        return None


class _FakePaperRuntime:
    async def get_strategy_summary(self, account_scope):
        return {
            "account": {"account_scope": account_scope},
            "strategies": [
                {
                    "strategy_run_id": "paper-1",
                    "display_name": "Paper Straddle",
                    "mode": "paper",
                    "status": "open",
                    "is_open": True,
                    "open_leg_count": 2,
                    "realized_pnl": 100.0,
                    "unrealized_pnl": -20.0,
                    "positions": [{"instrument_token": 1, "quantity": 50}],
                    "orders": [],
                    "trades": [],
                    "capabilities": {"can_exit_strategy": True, "allowed_actions": ["exit_strategy"]},
                    "summary_fields": [],
                }
            ],
        }


class _FakeRealtimePositionsService:
    async def get_positions(self, account_id, corr_id):
        _ = (account_id, corr_id)
        return {
            "NFO:MANUAL": SimpleNamespace(
                position_key="NFO:MANUAL",
                tradingsymbol="MANUAL",
                exchange="NFO",
                product="MIS",
                quantity=25,
                average_price=100.0,
                last_price=110.0,
                pnl=250.0,
                realized_pnl=0.0,
                unrealized_pnl=250.0,
                model_dump=lambda: {
                    "position_key": "NFO:MANUAL",
                    "tradingsymbol": "MANUAL",
                    "exchange": "NFO",
                    "product": "MIS",
                    "quantity": 25,
                    "average_price": 100.0,
                    "last_price": 110.0,
                    "pnl": 250.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 250.0,
                },
            )
        }


class _FakeRealtimePositionsServiceWithOverlap:
    async def get_positions(self, account_id, corr_id):
        _ = (account_id, corr_id)
        return {
            "NFO:KNOWN": SimpleNamespace(
                instrument_token=12345,
                tradingsymbol="KNOWNLEG",
                exchange="NFO",
                product="MIS",
                quantity=50,
                realized_pnl=10.0,
                unrealized_pnl=15.0,
                model_dump=lambda: {
                    "instrument_token": 12345,
                    "tradingsymbol": "KNOWNLEG",
                    "exchange": "NFO",
                    "product": "MIS",
                    "quantity": 50,
                    "realized_pnl": 10.0,
                    "unrealized_pnl": 15.0,
                },
            ),
            "NFO:MANUAL": SimpleNamespace(
                instrument_token=98765,
                tradingsymbol="MANUALONLY",
                exchange="NFO",
                product="MIS",
                quantity=25,
                realized_pnl=0.0,
                unrealized_pnl=250.0,
                model_dump=lambda: {
                    "instrument_token": 98765,
                    "tradingsymbol": "MANUALONLY",
                    "exchange": "NFO",
                    "product": "MIS",
                    "quantity": 25,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 250.0,
                },
            ),
        }


class _FakeRealtimePositionsServiceWithPartialOverlap:
    async def get_positions(self, account_id, corr_id):
        _ = (account_id, corr_id)
        return {
            "NFO:KNOWN": SimpleNamespace(
                instrument_token=12345,
                tradingsymbol="KNOWNLEG",
                exchange="NFO",
                product="MIS",
                quantity=75,
                pnl=150.0,
                realized_pnl=30.0,
                unrealized_pnl=120.0,
                model_dump=lambda: {
                    "instrument_token": 12345,
                    "tradingsymbol": "KNOWNLEG",
                    "exchange": "NFO",
                    "product": "MIS",
                    "quantity": 75,
                    "pnl": 150.0,
                    "realized_pnl": 30.0,
                    "unrealized_pnl": 120.0,
                },
            )
        }


class _FakeOverlapWorkerRepository(_FakeWorkerRepository):
    async def list_runs_for_control_plane(self):
        runs = await super().list_runs_for_control_plane()
        runs[0]["runtime_state"] = {"open_orders": [], "recent_trades": []}
        return runs


async def _fake_worker_pnl_snapshot(_request, _run):
    return {
        "totals": {"realized_pnl": 1.0, "unrealized_pnl": 2.0, "net_pnl": 3.0},
        "legs": [
            {
                "instrument_token": 12345,
                "exchange": "NFO",
                "tradingsymbol": "KNOWNLEG",
                "product": "MIS",
                "net_quantity": 50,
                "realized_pnl": 1.0,
                "unrealized_pnl": 2.0,
            }
        ],
        "updated_at": "2026-04-25T12:00:00+00:00",
    }


class _FakeExitPaperRuntime(_FakePaperRuntime):
    def __init__(self):
        self.exited = []

    async def exit_strategy(self, *, account_scope, strategy_id):
        self.exited.append((account_scope, strategy_id))
        return {"mode": "paper", "status": "closed", "strategy_id": strategy_id}


class _FakeExitWorkerRepository(_FakeWorkerRepository):
    async def get_run(self, strategy_run_id):
        if strategy_run_id == "run-live-1":
            return (await self.list_runs_for_control_plane())[0]
        if strategy_run_id == "paper-1":
            return None
        return None

    async def update_run_status(self, strategy_run_id, status, *, state_patch=None):
        run = await self.get_run(strategy_run_id)
        if run is None:
            raise KeyError(strategy_run_id)
        run["status"] = status
        run["runtime_state"] = {**dict(run.get("runtime_state") or {}), **dict(state_patch or {})}
        return run


class ControlPlaneHealthTests(unittest.TestCase):
    def test_compute_worker_health_prefers_run_heartbeat_and_flags_action_required_for_stale_live_unprotected(self):
        health = compute_worker_health(
            datetime.fromisoformat("2026-05-06T09:00:00+00:00"),
            now=datetime.fromisoformat("2026-05-06T09:10:00+00:00"),
        )

        self.assertEqual(health["heartbeat_age_sec"], 600)
        self.assertEqual(health["health_status"], "disconnected")

    def test_worker_health_unknown_without_heartbeat(self):
        health = compute_worker_health(None, now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(health["health_status"], "unknown")
        self.assertIsNone(health["heartbeat_age_sec"])

    def test_worker_health_healthy_stale_and_disconnected(self):
        now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

        healthy = compute_worker_health(now - timedelta(seconds=20), now=now)
        stale = compute_worker_health(now - timedelta(seconds=95), now=now)
        disconnected = compute_worker_health(now - timedelta(seconds=360), now=now)

        self.assertEqual(healthy["health_status"], "healthy")
        self.assertEqual(healthy["heartbeat_age_sec"], 20)
        self.assertEqual(stale["health_status"], "stale")
        self.assertEqual(disconnected["health_status"], "disconnected")


class ControlPlaneSnapshotContractTests(unittest.TestCase):
    def test_empty_snapshot_has_stable_top_level_contract(self):
        snapshot = build_empty_snapshot(now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(snapshot["generated_at"], "2026-04-25T12:00:00+00:00")
        self.assertEqual(snapshot["totals"]["strategy_count"], 0)
        self.assertEqual(snapshot["totals"]["open_strategy_count"], 0)
        self.assertEqual(snapshot["strategies"], [])
        self.assertEqual(snapshot["unattributed"]["display_name"], "Manual / unattributed broker exposure")


class ControlPlaneAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_merges_paper_worker_and_unattributed_positions(self):
        request = SimpleNamespace(
            headers={},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    algo_worker_repository=_FakeWorkerRepository(),
                    paper_runtime_service=_FakePaperRuntime(),
                    realtime_positions_service=_FakeRealtimePositionsService(),
                )
            ),
        )

        snapshot = await build_strategy_positions_snapshot(
            request,
            account_scope="default",
            broker_account_id="kite:AB1234",
            now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot["totals"]["strategy_count"], 2)
        self.assertEqual(snapshot["totals"]["open_strategy_count"], 2)
        self.assertEqual(snapshot["strategies"][0]["strategy_run_id"], "paper-1")
        self.assertEqual(snapshot["strategies"][1]["strategy_run_id"], "run-live-1")
        self.assertEqual(snapshot["strategies"][1]["health_status"], "healthy")
        self.assertEqual(snapshot["strategies"][1]["session_status"], "claimed")
        self.assertEqual(snapshot["unattributed"]["positions"][0]["tradingsymbol"], "MANUAL")
        self.assertEqual(snapshot["strategies"][1]["action_reasons"]["cancel_orders"], "Strategy-scoped cancel is disabled until a broker-safe open-order lookup is registered")

    async def test_unattributed_positions_exclude_live_strategy_overlap(self):
        request = SimpleNamespace(
            headers={},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    algo_worker_repository=_FakeOverlapWorkerRepository(),
                    paper_runtime_service=_FakePaperRuntime(),
                    realtime_positions_service=_FakeRealtimePositionsServiceWithOverlap(),
                )
            ),
        )

        with patch("api.control_plane._worker_pnl_or_empty", new=AsyncMock(side_effect=_fake_worker_pnl_snapshot)):
            snapshot = await build_strategy_positions_snapshot(
                request,
                account_scope="default",
                broker_account_id="kite:AB1234",
                now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual([item["tradingsymbol"] for item in snapshot["unattributed"]["positions"]], ["MANUALONLY"])
        self.assertEqual(snapshot["unattributed"]["net_pnl"], 250.0)

    async def test_unattributed_positions_keep_residual_quantity_for_partial_live_overlap(self):
        request = SimpleNamespace(
            headers={},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    algo_worker_repository=_FakeOverlapWorkerRepository(),
                    paper_runtime_service=_FakePaperRuntime(),
                    realtime_positions_service=_FakeRealtimePositionsServiceWithPartialOverlap(),
                )
            ),
        )

        with patch("api.control_plane._worker_pnl_or_empty", new=AsyncMock(side_effect=_fake_worker_pnl_snapshot)):
            snapshot = await build_strategy_positions_snapshot(
                request,
                account_scope="default",
                broker_account_id="kite:AB1234",
                now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(len(snapshot["unattributed"]["positions"]), 1)
        residual = snapshot["unattributed"]["positions"][0]
        self.assertEqual(residual["tradingsymbol"], "KNOWNLEG")
        self.assertEqual(residual["quantity"], 25)
        self.assertEqual(residual["attributed_quantity_removed"], 50)
        self.assertAlmostEqual(snapshot["unattributed"]["unrealized_pnl"], 40.0)

    async def test_snapshot_degrades_when_protection_adapter_fails(self):
        request = SimpleNamespace(
            headers={},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    algo_worker_repository=_FakeWorkerRepository(),
                    paper_runtime_service=_FakePaperRuntime(),
                    realtime_positions_service=_FakeRealtimePositionsService(),
                )
            ),
        )

        with patch("api.control_plane.ControlPlaneProtectionService.for_strategy", new=AsyncMock(side_effect=RuntimeError("runtime down"))):
            snapshot = await build_strategy_positions_snapshot(
                request,
                account_scope="default",
                broker_account_id="kite:AB1234",
                now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(snapshot["strategies"][0]["protection"]["status"], "error")
        self.assertIn("runtime down", snapshot["strategies"][0]["protection"]["details"]["error"])

    async def test_snapshot_marks_recovery_action_required_when_runtime_state_requests_it(self):
        repo = _FakeWorkerRepository()

        async def _runs():
            items = await _FakeWorkerRepository().list_runs_for_control_plane()
            items[0]["runtime_state"] = {"runtime_recovery": {"action_required": True, "recovery_status": "stalled"}}
            return items

        repo.list_runs_for_control_plane = _runs  # type: ignore[method-assign]
        request = SimpleNamespace(
            headers={},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    algo_worker_repository=repo,
                    paper_runtime_service=_FakePaperRuntime(),
                    realtime_positions_service=_FakeRealtimePositionsService(),
                )
            ),
        )

        snapshot = await build_strategy_positions_snapshot(
            request,
            account_scope="default",
            broker_account_id="kite:AB1234",
            now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc),
        )

        live_row = snapshot["strategies"][1]
        self.assertTrue(live_row["recovery_action_required"])
        self.assertEqual(live_row["recovery_status"], "stalled")


class ControlPlaneActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_paper_strategy_routes_to_paper_runtime(self):
        paper = _FakeExitPaperRuntime()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=_FakeExitWorkerRepository(), paper_runtime_service=paper))
        )

        result = await exit_control_strategy(request, "paper-1", account_scope="default", reason="operator_exit", dry_run=False)

        self.assertEqual(result["mode"], "paper")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(paper.exited, [("default", "paper-1")])

    async def test_cancel_orders_is_disabled_until_safe_path_exists(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

        with self.assertRaises(HTTPException) as ctx:
            await cancel_control_strategy_orders(request, "run-live-1", reason="operator_cancel")

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Strategy-scoped cancel is disabled", str(ctx.exception.detail))

    async def test_exit_live_worker_strategy_uses_existing_live_exit_helper(self):
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=_FakeExitWorkerRepository(), paper_runtime_service=_FakeExitPaperRuntime()))
        )

        with patch("api.routers.algo_workers._exit_live_worker_run", new=AsyncMock(return_value={"mode": "live", "status": "exiting"})) as exit_mock:
            result = await exit_control_strategy(request, "run-live-1", account_scope="default", reason="operator_exit", dry_run=False)

        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["status"], "exiting")
        exit_mock.assert_awaited_once()


class ControlPlaneRouterTests(unittest.TestCase):
    def test_reconcile_endpoint_passes_resolved_dependencies(self):
        app = FastAPI()
        app.include_router(control_router)

        fake_db = object()
        fake_kite = object()

        def _require_user(_request):
            return None

        async def _fake_reconcile(request, *, kite, db, corr_id):
            self.assertIs(request.app, app)
            self.assertIs(kite, fake_kite)
            self.assertIs(db, fake_db)
            self.assertEqual(corr_id, "corr-123")
            return {"status": "ok", "corr_id": corr_id}

        from api.routers import control as control_module

        app.dependency_overrides = {
            control_module.get_db: lambda: fake_db,
            control_module.get_kite: lambda: fake_kite,
            control_module.get_correlation_id: lambda: "corr-123",
        }

        with patch("api.routers.control.require_app_user", new=_require_user), patch(
            "broker_api.kite_orders.reconcile_realtime_positions", new=AsyncMock(side_effect=_fake_reconcile), create=True
        ) as reconcile_mock:
            client = TestClient(app)
            response = client.post("/control/reconcile")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["corr_id"], "corr-123")
        reconcile_mock.assert_awaited_once()

    def test_strategy_positions_infers_broker_account_from_session_when_missing(self):
        app = FastAPI()
        app.include_router(control_router)

        fake_db = object()

        def _require_user(_request):
            return None

        async def _fake_snapshot(request, *, account_scope, broker_account_id):
            self.assertIs(request.app, app)
            self.assertEqual(account_scope, "default")
            self.assertEqual(broker_account_id, "kite:AB1234")
            return {"generated_at": "x", "totals": {}, "strategies": [], "unattributed": {}}

        from api.routers import control as control_module

        app.dependency_overrides = {control_module.get_db: lambda: fake_db}

        with patch("api.routers.control.require_app_user", new=_require_user), patch(
            "api.routers.control.get_kite_session_id", return_value="session-1"
        ), patch("api.routers.control.get_session_account_id", return_value="kite:AB1234") as account_mock, patch(
            "api.routers.control.build_strategy_positions_snapshot", new=AsyncMock(side_effect=_fake_snapshot)
        ) as snapshot_mock:
            client = TestClient(app)
            response = client.get("/control/strategy-positions")

        self.assertEqual(response.status_code, 200)
        account_mock.assert_called_once_with(fake_db, "session-1")
        snapshot_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
