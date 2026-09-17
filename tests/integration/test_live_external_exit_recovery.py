from unittest.mock import Mock
import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.journaling import live_projector
from backend.journaling.live_projector import resolve_external_fill_run


class LiveExternalExitRecoveryTests(unittest.TestCase):
    """The unique-candidate heuristic is REMOVED (R3 §17, G3).

    Previously an untagged reducing fill attached to the only matching open run.
    "Exactly one run happens to hold this instrument" is not evidence of
    ownership: guessing there credits a strategy with a human's trade and hides
    real manual exposure. An untagged fill is now imported, unattributed truth.
    """

    def test_untagged_exit_never_attaches_to_the_only_matching_open_run(self):
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

        self.assertEqual(result["resolution"], "broker_import")
        self.assertEqual(result["run_id"], "")
        # The heuristic must not even consult the candidate list any more.
        repository.find_open_live_runs_for_instrument.assert_not_called()

    def test_ambiguous_untagged_exit_imports_to_broker_bucket(self):
        repository = Mock()
        repository.find_open_live_runs_for_instrument.return_value = [
            {"run_id": "run-a", "net_quantity": 1},
            {"run_id": "run-b", "net_quantity": 1},
        ]
        fill = {"account_id": "kite:AB1234", "instrument_token": 123, "product": "CNC", "transaction_type": "SELL", "quantity": 1}

        result = resolve_external_fill_run(repository=repository, fill=fill)

        self.assertEqual(result["resolution"], "broker_import")

    def test_reducing_fill_helper_is_gone(self):
        self.assertFalse(hasattr(live_projector, "_is_reducing_fill"))
        self.assertNotIn("external_exit", live_projector.LiveJournalProjector().project.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
