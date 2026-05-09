import unittest
import sys
from decimal import Decimal
from unittest.mock import Mock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

from backend.broker_api.orders import Exchange, OrderType, Product, Trade, TransactionType, Variety
from backend.execution_accounting.kite_costs import build_live_basket_cost_contract, build_live_order_cost_contract


class LiveCostContractTests(unittest.TestCase):
    def test_live_basket_contract_uses_broker_initial_margin_and_summed_charges(self):
        orders_service = Mock()
        orders_service.basket_margins.return_value = Mock(
            initial=Mock(total=208605.8),
            final=Mock(total=184692.3),
            model_dump=lambda mode="json": {
                "initial": {"total": 208605.8},
                "final": {"total": 184692.3},
            },
        )
        orders_service.charges_orders.return_value = [
            Mock(charges={"total": 39.04, "brokerage": 11.23}, model_dump=lambda mode="json": {"charges": {"total": 39.04}}),
            Mock(charges={"total": 30.48, "brokerage": 8.77}, model_dump=lambda mode="json": {"charges": {"total": 30.48}}),
        ]

        contract = build_live_basket_cost_contract(
            kite=Mock(),
            orders_service=orders_service,
            orders=[
                {
                    "exchange": Exchange.NFO,
                    "tradingsymbol": "NIFTY2650524000CE",
                    "transaction_type": TransactionType.SELL,
                    "variety": Variety.REGULAR,
                    "product": Product.MIS,
                    "order_type": OrderType.MARKET,
                    "quantity": 65,
                    "price": 206.6,
                },
                {
                    "exchange": Exchange.NFO,
                    "tradingsymbol": "NIFTY2650524000PE",
                    "transaction_type": TransactionType.SELL,
                    "variety": Variety.REGULAR,
                    "product": Product.MIS,
                    "order_type": OrderType.MARKET,
                    "quantity": 65,
                    "price": 161.3,
                },
            ],
            corr_id="test",
        )

        self.assertEqual(contract.margin_required, Decimal("208605.8"))
        self.assertEqual(contract.total_charges, Decimal("69.52"))
        self.assertEqual(contract.brokerage, Decimal("20.00"))
        self.assertEqual(contract.charges_status, "broker_quoted")

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

    def test_live_contract_uses_quote_price_when_preview_has_no_average_price(self):
        orders_service = Mock()
        orders_service.order_margins.return_value = [
            Mock(total=1500.0, model_dump=lambda mode="json": {"total": 1500.0})
        ]
        orders_service.charges_orders.return_value = [
            Mock(charges={"total": 11.25}, model_dump=lambda mode="json": {"charges": {"total": 11.25}})
        ]
        kite = Mock()
        kite.quote.return_value = {"NSE:INFY": {"last_price": 1512.45}}

        build_live_order_cost_contract(
            kite=kite,
            orders_service=orders_service,
            order={
                "exchange": Exchange.NSE,
                "tradingsymbol": "INFY",
                "transaction_type": TransactionType.BUY,
                "variety": Variety.REGULAR,
                "product": Product.CNC,
                "order_type": OrderType.MARKET,
                "quantity": 1,
                "price": 0,
            },
            corr_id="test",
        )

        charges_input = orders_service.charges_orders.call_args.args[1][0]
        self.assertEqual(charges_input.average_price, 1512.45)

    def test_trade_accepts_time_only_timestamps_from_provider(self):
        trade = Trade.model_validate(
            {
                "trade_id": "t1",
                "order_id": "o1",
                "exchange": "NSE",
                "tradingsymbol": "IDEA",
                "instrument_token": 123,
                "transaction_type": "BUY",
                "product": "MIS",
                "average_price": 9.8,
                "quantity": 1,
                "order_timestamp": "09:31:10",
                "exchange_timestamp": "09:31:11",
                "fill_timestamp": "09:31:12",
            }
        )

        self.assertEqual(trade.order_timestamp, "09:31:10")
        self.assertEqual(trade.exchange_timestamp, "09:31:11")
        self.assertEqual(trade.fill_timestamp, "09:31:12")

    def test_live_contract_tolerates_blank_charge_fields(self):
        orders_service = Mock()
        orders_service.order_margins.return_value = [
            Mock(total=1500.0, model_dump=lambda mode="json": {"total": 1500.0})
        ]
        orders_service.charges_orders.return_value = [
            Mock(
                charges={
                    "brokerage": "",
                    "transaction_tax": None,
                    "exchange_turnover_charge": "2.5",
                    "sebi_turnover_charge": "",
                    "stamp_duty": "0",
                    "gst": "",
                    "total": "2.5",
                },
                model_dump=lambda mode="json": {"charges": {"total": "2.5"}},
            )
        ]

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

        self.assertEqual(contract.total_charges, Decimal("2.5"))
        self.assertEqual(contract.exchange_txn_charge, Decimal("2.5"))
        self.assertEqual(contract.gst, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
