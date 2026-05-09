import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.services.protection_runtime import WorkerProtectionRuntime, submit_worker_protection_exit


class _Repo:
    def __init__(self):
        self.saved = []
        self.runs = [
            {
                "strategy_run_id": "run-1",
                "account_scope": "kite:paper-a",
                "execution_mode": "paper",
                "status": "open",
                "runtime_state": {
                    "backend_protection": {
                        "enabled": True,
                        "positions": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "entry_price": 100, "stoploss_pct": 5}],
                    },
                    "backend_protection_state": {"generation": 1},
                },
                "last_heartbeat_at": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            }
        ]

    async def list_protection_enabled_runs(self):
        return [dict(item) for item in self.runs]

    async def update_run_runtime_state(self, strategy_run_id, runtime_state):
        self.saved.append((strategy_run_id, runtime_state))
        self.runs[0]["runtime_state"] = runtime_state
        return dict(self.runs[0])

    async def update_run_backend_protection_state(self, strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
        current = dict(self.runs[0].get("runtime_state", {}).get("backend_protection_state") or {})
        if expected_generation is not None and int(current.get("generation") or 0) != int(expected_generation):
            return None
        if expected_triggered_rule is not None and str(current.get("triggered_rule") or "") != str(expected_triggered_rule):
            return None
        if expected_exit_claim_id is not None and str(current.get("exit_claim_id") or "") != str(expected_exit_claim_id):
            return None
        runtime_state = dict(self.runs[0]["runtime_state"])
        runtime_state["backend_protection_state"] = dict(protection_state)
        self.saved.append((strategy_run_id, runtime_state))
        self.runs[0]["runtime_state"] = runtime_state
        return dict(self.runs[0])


class WorkerProtectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_submits_exit_and_persists_state_when_triggered(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(repo.saved[-1][1]["backend_protection_state"]["triggered_rule"], "position_stoploss")
        self.assertEqual(repo.saved[-1][1]["backend_protection_state"]["action"], "exit_strategy")
        self.assertTrue(repo.saved[-1][1]["backend_protection_state"]["exit_submitted"])

    async def test_runtime_persists_error_without_breaking_loop(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(side_effect=RuntimeError("pnl broken")),
            exit_submitter=AsyncMock(),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(repo.saved[0][1]["backend_protection_state"]["status"], "error")

    async def test_already_exit_submitted_does_not_duplicate_exit(self):
        repo = _Repo()
        repo.runs[0]["runtime_state"]["backend_protection_state"] = {"generation": 1, "exit_submitted": True, "status": "triggered"}
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_generation_conflict_skips_stale_exit_submission(self):
        repo = _Repo()

        async def stale_generation_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            return None

        repo.update_run_backend_protection_state = stale_generation_update
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_recent_exit_claim_prevents_duplicate_exit_submission(self):
        repo = _Repo()
        repo.runs[0]["runtime_state"]["backend_protection_state"] = {
            "generation": 1,
            "status": "triggered",
            "triggered_rule": "position_stoploss",
            "exit_claim_id": "claim-1",
            "exit_claimed_at": "2026-04-25T12:00:45+00:00",
            "exit_submitted": False,
        }
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_submit_exception_records_unknown_terminal_state_with_claim(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(side_effect=RuntimeError("broker timeout")),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertEqual(state["status"], "error")
        self.assertTrue(state["exit_submitted"])
        self.assertEqual(state["exit_submission_status"], "unknown")
        self.assertTrue(state["exit_claim_id"])

    async def test_deferred_exit_result_keeps_claim_for_retry_without_marking_exit_submitted(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "deferred", "deferred": True, "message": "attribution pending"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertEqual(state["status"], "triggered")
        self.assertFalse(state["exit_submitted"])
        self.assertEqual(state["exit_submission_status"], "deferred")
        self.assertEqual(state["exit_result"]["status"], "deferred")
        self.assertTrue(state["exit_claim_id"])

    async def test_successful_exit_forces_terminal_state_if_final_cas_fails(self):
        repo = _Repo()
        calls = {"count": 0}

        async def flaky_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            calls["count"] += 1
            if calls["count"] == 2:
                return None
            runtime_state = dict(repo.runs[0]["runtime_state"])
            runtime_state["backend_protection_state"] = dict(protection_state)
            repo.saved.append((strategy_run_id, runtime_state))
            repo.runs[0]["runtime_state"] = runtime_state
            return dict(repo.runs[0])

        repo.update_run_backend_protection_state = flaky_update
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 1)
        self.assertTrue(repo.saved[-1][1]["backend_protection_state"]["exit_submitted"])
        self.assertEqual(calls["count"], 3)

    async def test_non_trigger_update_does_not_overwrite_concurrent_claim(self):
        repo = _Repo()

        async def claimed_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            self.assertEqual(expected_triggered_rule, "")
            self.assertEqual(expected_exit_claim_id, "")
            return None

        repo.update_run_backend_protection_state = claimed_update
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 100}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)

    async def test_submit_worker_protection_exit_forwards_claimed_idempotency_key(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), headers={})
        run = {"strategy_run_id": "run-1", "account_scope": "kite:paper-a"}
        state = {"triggered_rule": "basket_stoploss", "exit_idempotency_key": "backend-protection:run-1:g1:basket_stoploss:abc"}

        with patch("api.control_plane.exit_control_strategy", new=AsyncMock(return_value={"status": "closed"})) as exit_mock:
            result = await submit_worker_protection_exit(request, run, state)

        self.assertEqual(result["status"], "closed")
        self.assertIsNotNone(exit_mock.await_args)
        kwargs = getattr(exit_mock.await_args, "kwargs", {})
        self.assertEqual(kwargs["idempotency_key"], "backend-protection:run-1:g1:basket_stoploss:abc")


if __name__ == "__main__":
    unittest.main()
