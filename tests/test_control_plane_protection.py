import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.control_plane_protection import ControlPlaneProtectionService, _json_loads


class _FakeOptionStrategyStore:
    def get_strategy_run(self, run_id):
        if run_id != "option-run-1":
            return None
        return {
            "id": "option-run-1",
            "algo_instance_id": "option-strategy:option-run-1",
            "canonical_strategy": {
                "protection_preferences": {
                    "combined_premium_target": 35,
                    "combined_premium_stoploss": 18,
                },
                "rules": [
                    {"id": "premium-target", "metric": "combined_premium", "operator": "lte", "value": 35},
                    {"id": "premium-stop", "metric": "combined_premium", "operator": "gte", "value": 18},
                ],
            },
        }


class _FakeAlgoRuntimeService:
    async def status(self):
        return {
            "started": True,
            "instances": [
                {
                    "instance_id": "option-strategy:option-run-1",
                    "algo_type": "runtime_option_strategy",
                    "lifecycle_state": "running",
                    "last_evaluated_at": "2026-04-25T12:00:00+00:00",
                    "last_action": {"action_type": "noop", "reason": "threshold_not_hit"},
                    "last_error": None,
                }
            ],
        }


class _FakeInvestingProtectionRepository:
    async def summarize_strategy(self, strategy_name):
        if strategy_name != "Nifty50 Momentum":
            return None
        return {
            "strategy_name": "Nifty50 Momentum",
            "active_holding_count": 5,
            "pending_exit_count": 1,
            "total_pnl": 1250.0,
            "worst_pnl_percent": -2.5,
            "last_checked_at": "2026-04-25T12:01:00+00:00",
        }


class ControlPlaneProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_option_strategy_protection_uses_option_store_and_algo_runtime(self):
        service = ControlPlaneProtectionService(
            option_strategy_store=_FakeOptionStrategyStore(),
            algo_runtime_service=_FakeAlgoRuntimeService(),
            investing_repository=_FakeInvestingProtectionRepository(),
        )

        protection = await service.for_strategy(
            {
                "strategy_run_id": "option-run-1",
                "source": "paper_runtime",
                "mode": "paper",
                "metadata": {"strategy_family": "options_strategy"},
            }
        )

        self.assertEqual(protection["source"], "option_runtime")
        self.assertEqual(protection["status"], "active")
        self.assertIn("2 rule", protection["summary"])
        self.assertEqual(protection["last_checked_at"], "2026-04-25T12:00:00+00:00")
        self.assertEqual(protection["details"]["algo_instance_id"], "option-strategy:option-run-1")

    async def test_investing_strategy_protection_uses_investing_holdings_summary(self):
        service = ControlPlaneProtectionService(
            option_strategy_store=_FakeOptionStrategyStore(),
            algo_runtime_service=_FakeAlgoRuntimeService(),
            investing_repository=_FakeInvestingProtectionRepository(),
        )

        protection = await service.for_strategy(
            {
                "strategy_run_id": "investing-run-1",
                "display_name": "Nifty50 Momentum",
                "metadata": {"strategy_family": "investment_strategy", "strategy_name": "Nifty50 Momentum"},
            }
        )

        self.assertEqual(protection["source"], "investing_runtime")
        self.assertEqual(protection["status"], "pending_exit")
        self.assertIn("5 active holdings", protection["summary"])
        self.assertEqual(protection["details"]["pending_exit_count"], 1)

    async def test_metadata_fallback_is_used_when_no_adapter_matches(self):
        service = ControlPlaneProtectionService(
            option_strategy_store=_FakeOptionStrategyStore(),
            algo_runtime_service=_FakeAlgoRuntimeService(),
            investing_repository=_FakeInvestingProtectionRepository(),
        )

        protection = await service.for_strategy(
            {
                "strategy_run_id": "generic-run-1",
                "protection": {
                    "source": "worker_metadata",
                    "status": "active",
                    "summary": "Worker supplied risk status",
                    "last_checked_at": "2026-04-25T12:02:00+00:00",
                },
            }
        )

        self.assertEqual(protection["source"], "worker_metadata")
        self.assertEqual(protection["status"], "active")
        self.assertEqual(protection["summary"], "Worker supplied risk status")

    async def test_backend_worker_protection_state_is_preferred_for_worker_runs(self):
        service = ControlPlaneProtectionService()

        protection = await service.for_strategy(
            {
                "strategy_run_id": "run-1",
                "source": "algo_worker",
                "metadata": {},
                "backend_protection": {"enabled": True, "positions": [{"symbol": "NSE:INFY"}]},
                "backend_protection_state": {"status": "active", "generation": 2, "current_basket_pnl_pct": 1.2, "last_checked_at": "2026-04-25T12:00:00+00:00"},
            }
        )

        self.assertEqual(protection["source"], "backend_worker_protection")
        self.assertEqual(protection["status"], "active")
        self.assertEqual(protection["details"]["generation"], 2)

    async def test_unknown_none_state_is_returned_when_no_adapter_matches(self):
        service = ControlPlaneProtectionService(
            option_strategy_store=_FakeOptionStrategyStore(),
            algo_runtime_service=_FakeAlgoRuntimeService(),
            investing_repository=_FakeInvestingProtectionRepository(),
        )

        protection = await service.for_strategy(
            {
                "strategy_run_id": "generic-run-2",
                "display_name": "Generic Strategy",
                "metadata": {},
            }
        )

        self.assertEqual(protection["source"], "none")
        self.assertEqual(protection["status"], "unknown")
        self.assertEqual(protection["summary"], "No protection runtime attached")
        self.assertIsNone(protection["last_checked_at"])
        self.assertEqual(protection["details"], {})


class ControlPlaneProtectionHelperTests(unittest.TestCase):
    def test_json_loads_returns_fallback_on_malformed_json(self):
        fallback = {"rules": []}

        value = _json_loads("{not-json", fallback)

        self.assertEqual(value, fallback)


if __name__ == "__main__":
    unittest.main()
