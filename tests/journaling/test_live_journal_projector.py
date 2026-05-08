from decimal import Decimal
import unittest
from unittest.mock import Mock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from journaling.live_projector import LiveJournalProjector


class LiveJournalProjectorTests(unittest.TestCase):
    def test_known_live_fill_uses_intent_strategy_run(self):
        repository = Mock()
        repository.list_unprojected_live_fills.return_value = [
            {
                "account_id": "kite:AB1234",
                "trade_id": "T1",
                "order_id": "O1",
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "instrument_token": 123,
                "product": "CNC",
                "transaction_type": "BUY",
                "quantity": 1,
                "price": Decimal("1500"),
                "fill_timestamp": "2026-04-24T10:00:00+00:00",
                "payload_json": {},
            }
        ]
        repository.find_live_order_intent.return_value = {
            "journal_run_id": "11111111-1111-4111-8111-111111111111",
            "cost_contract_json": {
                "brokerage": "20.00",
                "exchange_txn_charge": "2.50",
                "stt": "5.00",
                "stamp_duty": "1.00",
                "sebi_charge": "0.20",
                "gst": "2.50",
                "margin_required": "1500.00",
                "charges_status": "broker_quoted",
            },
        }
        repository.ensure_live_strategy_run_for_intent.return_value = "11111111-1111-4111-8111-111111111111"
        repository.insert_execution_fact.return_value = 1

        journal_service = Mock()
        result = LiveJournalProjector(repository=repository, journal_service=journal_service).project(batch_size=10)

        self.assertEqual(result["projected"], 1)
        fact = repository.insert_execution_fact.call_args.args[0]
        self.assertEqual(str(fact.run_id), "11111111-1111-4111-8111-111111111111")
        self.assertEqual(fact.source_type, "live_fill")
        self.assertEqual(fact.fees_amount, Decimal("22.50"))
        self.assertEqual(fact.taxes_amount, Decimal("8.70"))
        self.assertEqual(fact.brokerage, Decimal("20.00"))
        self.assertEqual(fact.exchange_txn_charge, Decimal("2.50"))
        self.assertEqual(fact.stt, Decimal("5.00"))
        self.assertEqual(fact.stamp_duty, Decimal("1.00"))
        self.assertEqual(fact.sebi_charge, Decimal("0.20"))
        self.assertEqual(fact.gst, Decimal("2.50"))
        self.assertEqual(fact.margin_required, Decimal("1500.00"))
        self.assertEqual(fact.charges_status, "broker_quoted")
        journal_service.record_v2_execution_fill.assert_called_once()
        v2_call = journal_service.record_v2_execution_fill.call_args.kwargs
        self.assertEqual(v2_call["mode"], "live")
        self.assertEqual(v2_call["external_run_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(v2_call["cost_contract"].brokerage, Decimal("20.00"))
        self.assertEqual(v2_call["payload"]["cost_contract"]["charges_status"], "broker_quoted")

    def test_unknown_live_fill_imports_to_broker_activity(self):
        repository = Mock()
        repository.list_unprojected_live_fills.return_value = [
            {
                "account_id": "kite:AB1234",
                "trade_id": "T2",
                "order_id": "O2",
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "instrument_token": 456,
                "product": "CNC",
                "transaction_type": "SELL",
                "quantity": 1,
                "price": Decimal("2800"),
                "fill_timestamp": "2026-04-24T10:00:00+00:00",
                "payload_json": {},
            }
        ]
        repository.find_live_order_intent.return_value = None
        repository.find_open_live_runs_for_instrument.return_value = []
        repository.ensure_imported_broker_run.return_value = "22222222-2222-4222-8222-222222222222"

        journal_service = Mock()
        result = LiveJournalProjector(repository=repository, journal_service=journal_service).project(batch_size=10)

        self.assertEqual(result["imported"], 1)
        repository.ensure_imported_broker_run.assert_called_once()
        journal_service.record_v2_execution_fill.assert_called_once()
        fact = repository.insert_execution_fact.call_args.args[0]
        self.assertEqual(fact.fees_amount, Decimal("0"))
        self.assertEqual(fact.taxes_amount, Decimal("0"))
        self.assertEqual(fact.charges_status, "unavailable")

    def test_intent_without_journal_run_still_projects_to_strategy_run(self):
        repository = Mock()
        repository.list_unprojected_live_fills.return_value = [
            {
                "account_id": "kite:AB1234",
                "trade_id": "T3",
                "order_id": "O3",
                "exchange": "NSE",
                "tradingsymbol": "SBIN",
                "instrument_token": 789,
                "product": "MIS",
                "transaction_type": "BUY",
                "quantity": 1,
                "price": Decimal("700"),
                "fill_timestamp": "2026-04-24T10:00:00+00:00",
                "payload_json": {},
            }
        ]
        repository.find_live_order_intent.return_value = {
            "client_order_ref": "KA12345678",
            "strategy_run_id": "strategy-run-1",
            "cost_contract_json": {},
        }
        repository.ensure_live_strategy_run_for_intent.return_value = "33333333-3333-4333-8333-333333333333"

        journal_service = Mock()
        result = LiveJournalProjector(repository=repository, journal_service=journal_service).project(batch_size=10)

        self.assertEqual(result["projected"], 1)
        repository.ensure_live_strategy_run_for_intent.assert_called_once()
        fact = repository.insert_execution_fact.call_args.args[0]
        self.assertEqual(str(fact.run_id), "33333333-3333-4333-8333-333333333333")
        self.assertEqual(fact.source_type, "live_fill")
        self.assertEqual(fact.charges_status, "unavailable")
        journal_service.record_v2_execution_fill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
