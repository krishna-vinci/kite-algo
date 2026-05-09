import unittest
from decimal import Decimal
from unittest.mock import patch

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.paper_runtime.run_state import PaperRunStateService  # noqa: E402
from backend.paper_runtime.service import PaperTradingService  # noqa: E402
from tests.algo_runtime.test_paper_executor import (  # noqa: E402
    FakeInstrumentsRepository,
    FakeMarketRuntime,
    FakePaperRepository,
    _inline_to_thread,
)


class PaperRunStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = FakePaperRepository()
        self.market_runtime = FakeMarketRuntime(
            {
                256265: {
                    "instrument_token": 256265,
                    "last_price": 150.0,
                    "depth": {"buy": [{"price": 149.9}], "sell": [{"price": 150.1}]},
                }
            }
        )
        self.service = PaperTradingService(
            repository=self.repository,
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=self.market_runtime,
            default_starting_balance=Decimal("100000.00"),
        )
        self.run_state_service = PaperRunStateService(self.repository)
        self.to_thread_patch = patch("paper_runtime.service.asyncio.to_thread", new=_inline_to_thread)
        self.publish_patch = patch("paper_runtime.service.publish_event", autospec=True)
        self.to_thread_patch.start()
        self.publish_patch.start()

    async def asyncTearDown(self):
        self.publish_patch.stop()
        self.to_thread_patch.stop()

    async def test_same_scope_same_symbol_runs_keep_distinct_pnl(self):
        scope = "kite:paper-a"
        await self.service.place_order(
            account_scope=scope,
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "LIMIT",
                "quantity": 1,
                "price": 151.0,
            },
            attribution={"strategy_run_id": "run-a", "strategy_family": "indicator_strategy", "strategy_name": "Run A"},
        )
        await self.service.place_order(
            account_scope=scope,
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "LIMIT",
                "quantity": 1,
                "price": 152.0,
            },
            attribution={"strategy_run_id": "run-b", "strategy_family": "indicator_strategy", "strategy_name": "Run B"},
        )

        state_a = self.run_state_service.get_run_state(scope, "run-a")
        state_b = self.run_state_service.get_run_state(scope, "run-b")

        self.assertIsNotNone(state_a)
        self.assertIsNotNone(state_b)
        state_a = state_a or {}
        state_b = state_b or {}
        self.assertEqual(state_a["positions"][0]["net_quantity"], 1)
        self.assertEqual(state_b["positions"][0]["net_quantity"], 1)
        self.assertEqual(state_a["positions"][0]["average_price"], 151.0)
        self.assertEqual(state_b["positions"][0]["average_price"], 152.0)
        self.assertNotEqual(state_a["positions"][0]["average_price"], state_b["positions"][0]["average_price"])


if __name__ == "__main__":
    unittest.main()
