import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from broker_api.kite_orders import (
    ConvertPositionRequest,
    Exchange,
    OrdersService,
    PositionType,
    Product,
    TransactionType,
)


class ConvertPositionTests(unittest.IsolatedAsyncioTestCase):
    def test_convert_position_request_rejects_same_product(self):
        with self.assertRaises(ValueError):
            ConvertPositionRequest(
                exchange=Exchange.NSE,
                tradingsymbol="SBIN",
                transaction_type=TransactionType.BUY,
                position_type=PositionType.DAY,
                quantity=1,
                old_product=Product.MIS,
                new_product=Product.MIS,
            )

    async def test_service_convert_position_uses_write_throttler(self):
        service = OrdersService()
        kite = MagicMock()
        req = ConvertPositionRequest(
            exchange=Exchange.NSE,
            tradingsymbol="SBIN",
            transaction_type=TransactionType.BUY,
            position_type=PositionType.DAY,
            quantity=3,
            old_product=Product.MIS,
            new_product=Product.NRML,
        )

        with patch(
            "broker_api.kite_orders.run_kite_write_action",
            AsyncMock(return_value=True),
        ) as run_action:
            response = await service.convert_position(kite, req, corr_id="corr-1")

        self.assertEqual(response.status, "success")
        self.assertTrue(response.data)
        run_action.assert_awaited_once()
        self.assertFalse(kite.convert_position.called)

        action_name, corr_id, callback = run_action.await_args.args[:3]
        self.assertEqual(action_name, "convert_position")
        self.assertEqual(corr_id, "corr-1")
        callback()
        kite.convert_position.assert_called_once_with(
            exchange=Exchange.NSE,
            tradingsymbol="SBIN",
            transaction_type=TransactionType.BUY,
            position_type=PositionType.DAY,
            quantity=3,
            old_product=Product.MIS,
            new_product=Product.NRML,
        )
