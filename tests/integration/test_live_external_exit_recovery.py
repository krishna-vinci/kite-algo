from unittest.mock import Mock
import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.journaling.live_projector import resolve_external_fill_run


class LiveExternalExitRecoveryTests(unittest.TestCase):
    def test_untagged_exit_maps_to_single_matching_open_strategy(self):
        repository = Mock()
        repository.find_open_live_runs_for_instrument.return_value = [
            {"run_id": "11111111-1111-4111-8111-111111111111", "net_quantity": 1}
        ]
        fill = {
            "account_id": "kite:AB1234",
            "instrument_token": 123,
            "product": "CNC",
            "transaction_type": "SELL",
            "quantity": 1,
        }

        result = resolve_external_fill_run(repository=repository, fill=fill)

        self.assertEqual(result["run_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(result["resolution"], "external_exit")

    def test_ambiguous_untagged_exit_imports_to_broker_bucket(self):
        repository = Mock()
        repository.find_open_live_runs_for_instrument.return_value = [
            {"run_id": "run-a", "net_quantity": 1},
            {"run_id": "run-b", "net_quantity": 1},
        ]
        fill = {"account_id": "kite:AB1234", "instrument_token": 123, "product": "CNC", "transaction_type": "SELL", "quantity": 1}

        result = resolve_external_fill_run(repository=repository, fill=fill)

        self.assertEqual(result["resolution"], "broker_import")


if __name__ == "__main__":
    unittest.main()
