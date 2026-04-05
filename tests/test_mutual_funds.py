import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.kite_mutual_funds import PlaceMFOrderRequest, mf_service


class FakeMutualFundKite:
    def __init__(self):
        self.mf_orders = lambda *args, **kwargs: []
        self.mf_sips = lambda *args, **kwargs: []


class MutualFundsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_orders_accepts_provider_extra_fields(self):
        kite = FakeMutualFundKite()
        kite.mf_orders = lambda: [
            {
                "order_id": "MF123",
                "tradingsymbol": "INF000000001",
                "transaction_type": "BUY",
                "status": "COMPLETE",
                "amount": 2500.0,
                "provider_only_field": "ignored-but-accepted",
            }
        ]

        orders = await mf_service.list_orders(kite)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].order_id, "MF123")
        self.assertEqual(orders[0].amount, 2500.0)

    async def test_place_order_uses_shared_write_wrapper(self):
        kite = FakeMutualFundKite()
        request = PlaceMFOrderRequest(
            tradingsymbol="INF000000001",
            transaction_type="BUY",
            amount=5000.0,
            tag="sip-topup",
        )

        with patch(
            "broker_api.kite_mutual_funds.run_kite_write_action",
            AsyncMock(return_value="MF-ORDER-1"),
        ) as run_action:
            response = await mf_service.place_order(kite, request, corr_id="corr-1")

        self.assertEqual(response.order_id, "MF-ORDER-1")
        run_action.assert_awaited_once()

    async def test_provider_errors_are_mapped_to_bad_gateway(self):
        kite = FakeMutualFundKite()

        def blow_up():
            raise RuntimeError("provider down")

        kite.mf_sips = blow_up

        with self.assertRaises(HTTPException) as ctx:
            await mf_service.list_sips(kite)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("provider down", ctx.exception.detail)
