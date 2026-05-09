import unittest
from decimal import Decimal
from unittest.mock import patch

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.paper_runtime.market_engine import PaperMarketEngine  # noqa: E402
from backend.paper_runtime.service import PaperTradingService  # noqa: E402
from tests.algo_runtime.test_paper_executor import FakeInstrumentsRepository, FakeMarketRuntime, FakePaperRepository  # noqa: E402


class PaperMarketEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = FakePaperRepository()
        self.market_runtime = FakeMarketRuntime(
            {
                256265: {"instrument_token": 256265, "last_price": 150.0, "depth": {"buy": [{"price": 149.9}], "sell": [{"price": 150.1}]}}
            }
        )
        self.service = PaperTradingService(
            repository=self.repository,
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=self.market_runtime,
            default_starting_balance=Decimal("100000.00"),
        )
        self.publish_patch = patch("paper_runtime.service.publish_event", autospec=True)
        self.publish_patch.start()

    async def asyncTearDown(self):
        self.publish_patch.stop()

    async def test_process_tick_fills_open_limit_order(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "LIMIT",
                "quantity": 1,
                "price": 148.0,
            },
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "index_stoploss"},
        )
        engine = PaperMarketEngine(service=self.service, market_data_runtime=self.market_runtime, redis_client=object())

        await engine.process_tick({"instrument_token": 256265, "last_price": 147.5, "depth": {"buy": [{"price": 147.4}], "sell": [{"price": 147.5}]}})

        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, "filled")
        self.assertEqual(len(self.repository.trades), 1)

    async def test_process_tick_marks_position_to_market(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 2,
            },
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "index_stoploss"},
        )
        engine = PaperMarketEngine(service=self.service, market_data_runtime=self.market_runtime, redis_client=object())

        await engine.process_tick({"instrument_token": 256265, "last_price": 155.0})

        position = next(iter(self.repository.positions.values()))
        self.assertEqual(position.unrealized_pnl, Decimal("9.8"))
