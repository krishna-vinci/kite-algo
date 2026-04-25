import unittest
import sys
from decimal import Decimal
from unittest.mock import Mock

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.kite_orders", None)

from broker_api.kite_orders import Exchange, OrderType, Product, TransactionType, Variety
from execution_accounting.kite_costs import build_live_order_cost_contract


class LiveCostContractTests(unittest.TestCase):
    def test_live_contract_uses_broker_margin_and_charges(self):
        orders_service = Mock()
        orders_service.order_margins.return_value = [
            Mock(total=1500.0, model_dump=lambda mode="json": {"total": 1500.0})
        ]
        orders_service.charges_orders.return_value = [
            Mock(
                charges={
                    "brokerage": 20,
                    "transaction_tax": 5,
                    "exchange_turnover_charge": 2,
                    "sebi_turnover_charge": 0.1,
                    "stamp_duty": 1,
                    "gst": 4.14,
                    "total": 32.24,
                },
                model_dump=lambda mode="json": {"charges": {"total": 32.24}},
            )
        ]
        order = {
            "exchange": Exchange.NSE,
            "tradingsymbol": "INFY",
            "transaction_type": TransactionType.BUY,
            "variety": Variety.REGULAR,
            "product": Product.CNC,
            "order_type": OrderType.MARKET,
            "quantity": 1,
            "price": 1500,
        }

        contract = build_live_order_cost_contract(
            kite=Mock(),
            orders_service=orders_service,
            order=order,
            corr_id="test",
        )

        self.assertEqual(contract.margin_required, Decimal("1500.0"))
        self.assertEqual(contract.brokerage, Decimal("20"))
        self.assertEqual(contract.exchange_txn_charge, Decimal("2"))
        self.assertEqual(contract.stt, Decimal("5"))
        self.assertEqual(contract.stamp_duty, Decimal("1"))
        self.assertEqual(contract.sebi_charge, Decimal("0.1"))
        self.assertEqual(contract.gst, Decimal("4.14"))
        self.assertEqual(contract.total_charges, Decimal("32.24"))
        self.assertEqual(contract.charges_status, "broker_quoted")

    def test_live_contract_returns_unavailable_when_broker_cost_quote_fails(self):
        orders_service = Mock()
        orders_service.order_margins.side_effect = RuntimeError("kite unavailable")

        contract = build_live_order_cost_contract(
            kite=Mock(),
            orders_service=orders_service,
            order={
                "exchange": Exchange.NSE,
                "tradingsymbol": "INFY",
                "transaction_type": TransactionType.BUY,
                "variety": Variety.REGULAR,
                "product": Product.CNC,
                "order_type": OrderType.MARKET,
                "quantity": 1,
                "price": 1500,
            },
            corr_id="test",
        )

        self.assertEqual(contract.charges_status, "unavailable")
        self.assertEqual(contract.raw, {"error": "kite unavailable"})


if __name__ == "__main__":
    unittest.main()
