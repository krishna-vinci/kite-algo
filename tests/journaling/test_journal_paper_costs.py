from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.journaling.models import JournalRun  # noqa: E402
from backend.journaling.service import JournalService  # noqa: E402


class JournalPaperCostTests(unittest.TestCase):
    def test_record_paper_trade_stores_gross_cash_flow_and_costs(self):
        repository = Mock()
        repository.get_run.return_value = JournalRun(
            id="11111111-1111-4111-8111-111111111111",
            strategy_family="indicator_strategy",
            strategy_name="Mean Reversion",
            execution_mode="paper",
            status="open",
            capital_basis_type="notional",
        )
        repository.link_source.return_value = 1
        repository.list_source_links.return_value = []
        service = JournalService(repository=repository)

        service.record_paper_trade(
            run_id="11111111-1111-4111-8111-111111111111",
            trade_id="PTRD-1",
            order_id="PAPER-1",
            trade_timestamp=datetime(2026, 4, 24, tzinfo=timezone.utc),
            side="buy",
            quantity=2,
            price=Decimal("100"),
            gross_cash_flow=Decimal("-200"),
            fees_amount=Decimal("1.25"),
            taxes_amount=Decimal("0.75"),
            payload={"charges_status": "estimated"},
        )

        fact = repository.insert_execution_fact.call_args.args[0]
        self.assertEqual(fact.gross_cash_flow, Decimal("-200"))
        self.assertEqual(fact.fees_amount, Decimal("1.25"))
        self.assertEqual(fact.taxes_amount, Decimal("0.75"))
        self.assertEqual(fact.payload["charges_status"], "estimated")

    def test_record_paper_trade_projects_v2_fill_when_account_and_strategy_exist(self):
        repository = Mock()
        repository.get_run.return_value = JournalRun(
            id="11111111-1111-4111-8111-111111111111",
            strategy_family="indicator_strategy",
            strategy_name="Mean Reversion",
            execution_mode="paper",
            status="open",
            capital_basis_type="notional",
            account_ref="kite:paper-e2e",
        )
        repository.link_source.return_value = 1
        repository.list_source_links.return_value = []
        service = JournalService(repository=repository)
        service.record_v2_execution_fill = Mock()  # type: ignore[method-assign]

        service.record_paper_trade(
            run_id="11111111-1111-4111-8111-111111111111",
            trade_id="PTRD-3",
            order_id="PAPER-3",
            trade_timestamp=datetime(2026, 4, 24, tzinfo=timezone.utc),
            side="buy",
            quantity=1,
            price=Decimal("100"),
            gross_cash_flow=Decimal("-100"),
            fees_amount=Decimal("1.00"),
            taxes_amount=Decimal("0.40"),
            payload={"strategy_run_id": "paper-run-1", "account_ref": "kite:paper-e2e", "source": "paper_runtime"},
        )

        service.record_v2_execution_fill.assert_called_once()
        kwargs = service.record_v2_execution_fill.call_args.kwargs
        self.assertEqual(kwargs["mode"], "paper")
        self.assertEqual(kwargs["source_fact_key"], "PTRD-3")
        self.assertEqual(kwargs["fees_amount"], Decimal("1.00"))
        self.assertEqual(kwargs["taxes_amount"], Decimal("0.40"))

    def test_record_paper_trade_computes_gross_cash_flow_when_missing(self):
        repository = Mock()
        repository.get_run.return_value = JournalRun(
            id="11111111-1111-4111-8111-111111111111",
            strategy_family="indicator_strategy",
            strategy_name="Mean Reversion",
            execution_mode="paper",
            status="open",
            capital_basis_type="notional",
        )
        repository.link_source.return_value = 1
        repository.list_source_links.return_value = []
        service = JournalService(repository=repository)

        service.record_paper_trade(
            run_id="11111111-1111-4111-8111-111111111111",
            trade_id="PTRD-2",
            order_id="PAPER-2",
            trade_timestamp=datetime(2026, 4, 24, tzinfo=timezone.utc),
            side="sell",
            quantity=3,
            price=Decimal("125"),
        )

        fact = repository.insert_execution_fact.call_args.args[0]
        self.assertEqual(fact.gross_cash_flow, Decimal("375"))
        self.assertEqual(fact.fees_amount, Decimal("0"))
        self.assertEqual(fact.taxes_amount, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
