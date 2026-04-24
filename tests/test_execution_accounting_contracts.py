from decimal import Decimal
import unittest

from execution_accounting.contracts import (
    ChargesStatus,
    ExecutionCostContract,
    OrderAttribution,
    signed_cash_flow,
)


class ExecutionAccountingContractTests(unittest.TestCase):
    def test_order_attribution_requires_strategy_for_app_order(self):
        with self.assertRaises(ValueError):
            OrderAttribution(
                strategy_run_id="",
                strategy_family="indicator_strategy",
                strategy_name="Mean Reversion",
                execution_mode="live",
                account_ref="kite:AB1234",
                entry_surface="quick_trade",
            )

    def test_cost_contract_totals_charges_and_taxes(self):
        contract = ExecutionCostContract(
            margin_required=Decimal("1200"),
            brokerage=Decimal("20"),
            exchange_txn_charge=Decimal("2.50"),
            stt=Decimal("4"),
            stamp_duty=Decimal("1"),
            sebi_charge=Decimal("0.10"),
            gst=Decimal("3.60"),
            charges_status=ChargesStatus.BROKER_QUOTED,
        )

        self.assertEqual(contract.total_taxes, Decimal("8.70"))
        self.assertEqual(contract.total_charges, Decimal("31.20"))
        self.assertEqual(contract.charges_status, ChargesStatus.BROKER_QUOTED)

    def test_signed_cash_flow_uses_one_convention(self):
        self.assertEqual(
            signed_cash_flow(side="BUY", price=Decimal("100"), quantity=3),
            Decimal("-300"),
        )
        self.assertEqual(
            signed_cash_flow(side="sell", price=Decimal("100"), quantity=3),
            Decimal("300"),
        )


if __name__ == "__main__":
    unittest.main()
