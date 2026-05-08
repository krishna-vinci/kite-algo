import unittest
import sys

from fastapi import HTTPException

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

from broker_api.orders import Exchange, OrderType, PlaceOrderRequest, Product, TransactionType, Validity, Variety
from broker_api.orders.live_order_intents import validate_live_order_attribution


class LiveOrderAttributionGateTests(unittest.TestCase):
    def test_missing_strategy_attribution_is_rejected_before_kite(self):
        req = PlaceOrderRequest(
            exchange=Exchange.NSE,
            tradingsymbol="INFY",
            transaction_type=TransactionType.BUY,
            variety=Variety.REGULAR,
            product=Product.CNC,
            order_type=OrderType.MARKET,
            quantity=1,
            validity=Validity.DAY,
        )

        with self.assertRaises(HTTPException) as ctx:
            validate_live_order_attribution(req.model_dump(mode="json"))

        self.assertEqual(ctx.exception.status_code, 422)

    def test_valid_strategy_attribution_creates_compact_broker_tag(self):
        payload = {
            "strategy_run_id": "run-live-1",
            "strategy_family": "indicator_strategy",
            "strategy_name": "Mean Reversion",
            "execution_mode": "live",
            "account_ref": "kite:AB1234",
            "entry_surface": "quick_trade",
        }

        attribution = validate_live_order_attribution(payload)

        self.assertEqual(attribution.strategy_run_id, "run-live-1")
        self.assertTrue(attribution.client_order_ref.startswith("KA"))
        self.assertLessEqual(len(attribution.client_order_ref), 20)

    def test_empty_optional_journal_run_id_is_normalized(self):
        attribution = validate_live_order_attribution(
            {
                "strategy_run_id": "run-live-1",
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion",
                "execution_mode": "live",
                "account_ref": "kite:AB1234",
                "entry_surface": "quick_trade",
                "journal_run_id": "",
            }
        )

        self.assertIsNone(attribution.journal_run_id)


if __name__ == "__main__":
    unittest.main()
