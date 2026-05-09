import unittest
from datetime import datetime, timezone

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.services.protection import BackendProtectionConfig, evaluate_backend_protection, validate_backend_protection_payload


class WorkerProtectionContractTests(unittest.TestCase):
    def test_valid_position_and_basket_protection_normalizes(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "positions": [
                    {
                        "symbol": "nse:reliance",
                        "product": "cnc",
                        "side": "buy",
                        "quantity": 10,
                        "entry_price": 2800,
                        "stoploss_pct": 5,
                        "target_pct": 12,
                        "trailing_stoploss_pct": 4,
                    }
                ],
                "basket": {"stoploss_pct": 8, "target_pct": 15, "trailing_activate_pct": 10, "trailing_drawdown_pct": 4},
                "operations": {"exit_on_worker_stale": True, "worker_stale_sec": 180, "mis_squareoff_buffer_sec": 60},
            },
            live=True,
        )

        self.assertIsInstance(config, BackendProtectionConfig)
        self.assertEqual(config.positions[0].symbol, "NSE:RELIANCE")
        self.assertEqual(config.positions[0].product, "CNC")
        self.assertEqual(config.positions[0].side, "BUY")
        self.assertEqual(config.operations.worker_stale_sec, 180)

    def test_enabled_protection_requires_at_least_one_rule(self):
        with self.assertRaises(ValueError) as ctx:
            validate_backend_protection_payload({"enabled": True}, live=True)

        self.assertIn("at least one protection rule", str(ctx.exception))

    def test_position_rule_requires_positive_quantity_and_entry_price(self):
        with self.assertRaises(ValueError) as ctx:
            validate_backend_protection_payload(
                {
                    "enabled": True,
                    "positions": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 0, "entry_price": 1500, "stoploss_pct": 3}],
                },
                live=True,
            )

        self.assertIn("quantity", str(ctx.exception))

    def test_invalid_percent_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_backend_protection_payload({"enabled": True, "basket": {"stoploss_pct": -1}}, live=True)

        self.assertIn("percent", str(ctx.exception).lower())

    def test_disabled_protection_allows_empty_config(self):
        config = validate_backend_protection_payload({"enabled": False}, live=True)

        self.assertFalse(config.enabled)
        self.assertEqual(config.positions, [])


class WorkerProtectionEvaluationTests(unittest.TestCase):
    def test_position_stoploss_triggers_from_long_leg(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "positions": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "entry_price": 100, "stoploss_pct": 5}],
            },
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={"generation": 2},
            positions=[{"symbol": "nse:infy", "product": "cnc", "side": "LONG", "net_quantity": 1, "average_price": 100, "last_price": 94}],
            heartbeat_age_sec=10,
            now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        self.assertEqual(result["status"], "triggered")
        self.assertEqual(result["triggered_rule"], "position_stoploss")
        self.assertEqual(result["action"], "exit_strategy")
        self.assertEqual(result["generation"], 2)

    def test_basket_trailing_drawdown_triggers(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "basket": {"trailing_activate_pct": 10, "trailing_drawdown_pct": 4},
            },
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={"best_basket_pnl_pct": 12},
            positions=[{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "net_quantity": 10, "average_price": 100, "last_price": 107}],
            heartbeat_age_sec=10,
            now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        self.assertEqual(result["triggered_rule"], "basket_trailing_drawdown")

    def test_worker_stale_triggers(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "operations": {"exit_on_worker_stale": True, "worker_stale_sec": 180},
            },
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={},
            positions=[],
            heartbeat_age_sec=181,
            now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        self.assertEqual(result["triggered_rule"], "worker_stale")

    def test_mis_squareoff_buffer_triggers(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "operations": {"mis_squareoff_buffer_sec": 60},
            },
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={},
            positions=[{"exchange": "NSE", "tradingsymbol": "INFY", "product": "MIS", "side": "BUY", "net_quantity": 1, "average_price": 100, "last_price": 100}],
            heartbeat_age_sec=10,
            now=datetime(2026, 4, 25, 9, 49, 30, tzinfo=timezone.utc),
            squareoff_schedule={"NSE:MIS": "15:20:00"},
        )

        self.assertEqual(result["triggered_rule"], "mis_squareoff_buffer")
        self.assertEqual(result["action"], "exit_strategy")

    def test_mis_squareoff_buffer_does_not_use_utc_wall_clock(self):
        config = validate_backend_protection_payload(
            {
                "enabled": True,
                "operations": {"mis_squareoff_buffer_sec": 60},
            },
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={},
            positions=[{"exchange": "NSE", "tradingsymbol": "INFY", "product": "MIS", "side": "BUY", "net_quantity": 1, "average_price": 100, "last_price": 100}],
            heartbeat_age_sec=10,
            now=datetime(2026, 4, 25, 9, 0, 0, tzinfo=timezone.utc),
            squareoff_schedule={"NSE:MIS": "15:20:00"},
        )

        self.assertEqual(result["status"], "active")

    def test_exit_submitted_state_is_preserved(self):
        config = validate_backend_protection_payload(
            {"enabled": True, "basket": {"stoploss_pct": 5}},
            live=True,
        )

        result = evaluate_backend_protection(
            config,
            state={"status": "triggered", "exit_submitted": True, "generation": 3, "triggered_rule": "basket_stoploss"},
            positions=[],
            heartbeat_age_sec=10,
            now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        self.assertTrue(result["exit_submitted"])
        self.assertEqual(result["generation"], 3)


if __name__ == "__main__":
    unittest.main()
