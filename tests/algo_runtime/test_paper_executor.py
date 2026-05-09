import unittest
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.paper_runtime.models import PaperOrderStatus  # noqa: E402
from backend.paper_runtime.service import PaperTradingService  # noqa: E402


async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


class FakePaperRepository:
    def __init__(self):
        self.accounts = {}
        self.orders = {}
        self.trades = {}
        self.positions = {}
        self.position_lots = {}
        self.fund_ledger = []

    def get_account(self, account_scope):
        return self.accounts.get(account_scope)

    def upsert_account(self, account):
        self.accounts[account.account_scope] = account
        return account

    def insert_order(self, order):
        self.orders[(order.account_scope, order.order_id)] = order
        return order

    def update_order(self, order):
        self.orders[(order.account_scope, order.order_id)] = order
        return order

    def get_position(self, account_scope, instrument_token, product):
        return self.positions.get((account_scope, instrument_token, product))

    def upsert_position(self, position):
        self.positions[(position.account_scope, position.instrument_token, position.product)] = position
        return position

    def insert_trade(self, trade):
        self.trades[(trade.account_scope, trade.trade_id)] = trade
        return trade

    def upsert_position_lot(self, lot):
        self.position_lots[(lot.account_scope, lot.lot_id)] = lot
        return lot

    def list_open_position_lots(self, account_scope, instrument_token=None, product=None):
        lots = [lot for lot in self.position_lots.values() if lot.account_scope == account_scope and lot.remaining_quantity > 0]
        if instrument_token is not None:
            lots = [lot for lot in lots if lot.instrument_token == instrument_token]
        if product is not None:
            lots = [lot for lot in lots if lot.product == product]
        return sorted(lots, key=lambda lot: lot.opened_at)

    def list_pending_orders_for_instrument(self, instrument_token):
        return [
            order
            for order in self.orders.values()
            if order.instrument_token == instrument_token and order.status in {PaperOrderStatus.PENDING, PaperOrderStatus.OPEN, PaperOrderStatus.PARTIALLY_FILLED}
        ]

    def list_open_positions_for_instrument(self, instrument_token):
        return [position for position in self.positions.values() if position.instrument_token == instrument_token and position.net_quantity != 0]

    def list_orders(self, account_scope, limit=200, **kwargs):
        return [order for order in self.orders.values() if order.account_scope == account_scope][:limit]

    def list_trades(self, account_scope, limit=500, **kwargs):
        return [trade for trade in self.trades.values() if trade.account_scope == account_scope][:limit]

    def list_positions(self, account_scope, only_open=False, **kwargs):
        items = [position for position in self.positions.values() if position.account_scope == account_scope]
        if only_open:
            items = [position for position in items if position.net_quantity != 0]
        return items

    def list_active_market_tokens(self):
        return sorted({order.instrument_token for order in self.orders.values()} | {position.instrument_token for position in self.positions.values() if position.net_quantity != 0})

    def append_fund_ledger_entry(self, entry):
        self.fund_ledger.append(entry)
        return entry

    def clear_account_scope(self, account_scope):
        self.orders = {key: value for key, value in self.orders.items() if key[0] != account_scope}
        self.trades = {key: value for key, value in self.trades.items() if key[0] != account_scope}
        self.positions = {key: value for key, value in self.positions.items() if key[0] != account_scope}
        self.fund_ledger = [entry for entry in self.fund_ledger if entry.account_scope != account_scope]

    @contextmanager
    def unit_of_work(self):
        snapshot = {
            "accounts": deepcopy(self.accounts),
            "orders": deepcopy(self.orders),
            "trades": deepcopy(self.trades),
            "positions": deepcopy(self.positions),
            "position_lots": deepcopy(self.position_lots),
            "fund_ledger": deepcopy(self.fund_ledger),
        }
        try:
            yield self
        except Exception:
            self.accounts = snapshot["accounts"]
            self.orders = snapshot["orders"]
            self.trades = snapshot["trades"]
            self.positions = snapshot["positions"]
            self.position_lots = snapshot["position_lots"]
            self.fund_ledger = snapshot["fund_ledger"]
            raise


class FakeInstrumentsRepository:
    def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
        if tradingsymbol == "MISSING":
            return None
        if tradingsymbol == "NIFTY24APR22000CE":
            return {
                "instrument_token": 5001,
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "lot_size": 50,
                "instrument_type": "CE",
                "last_price": 102.5,
            }
        return {
            "instrument_token": 256265,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "lot_size": 1,
            "instrument_type": "EQ",
            "last_price": 150.0,
        }


class FakeMarketRuntime:
    def __init__(self, ticks=None):
        self.ticks = ticks or {}

    async def get_tick(self, token):
        return self.ticks.get(token)

    async def get_last_price(self, token):
        tick = self.ticks.get(token) or {}
        return tick.get("last_price")


class PaperExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = FakePaperRepository()
        self.market_runtime = FakeMarketRuntime(
            {
                256265: {"instrument_token": 256265, "last_price": 150.0, "depth": {"buy": [{"price": 149.9}], "sell": [{"price": 150.1}]}},
                5001: {"instrument_token": 5001, "last_price": 102.5, "depth": {"buy": [{"price": 102.0}], "sell": [{"price": 102.5}]}},
            }
        )
        self.service = PaperTradingService(
            repository=self.repository,
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=self.market_runtime,
            default_starting_balance=Decimal("100000.00"),
        )
        self.to_thread_patch = patch("paper_runtime.service.asyncio.to_thread", new=_inline_to_thread)
        self.to_thread_patch.start()
        self.publish_patch = patch("paper_runtime.service.publish_event", autospec=True)
        self.publish_patch.start()

    async def asyncTearDown(self):
        self.publish_patch.stop()
        self.to_thread_patch.stop()

    async def test_market_order_fills_and_creates_position(self):
        result = await self.service.place_order(
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

        self.assertEqual(result["status"], "filled")
        self.assertEqual(len(self.repository.trades), 1)
        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.metadata["cost_contract"]["charges_status"], "estimated")
        self.assertEqual(order.metadata["estimated_charges"], order.metadata["cost_contract"]["total_charges"])
        position = next(iter(self.repository.positions.values()))
        self.assertEqual(position.net_quantity, 2)
        self.assertEqual(position.metadata["last_price"], "150.1")

    async def test_rejects_when_quantity_violates_lot_size(self):
        result = await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NFO",
                "tradingsymbol": "NIFTY24APR22000CE",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "NRML",
                "order_type": "MARKET",
                "quantity": 5,
            },
            attribution={"algo_instance_id": "algo-opt", "strategy_tag": "combined_premium_stoploss"},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("lot size", result["reason"].lower())

    async def test_limit_order_stays_open_until_market_cross(self):
        result = await self.service.place_order(
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

        self.assertEqual(result["status"], "accepted")
        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, PaperOrderStatus.OPEN)

    async def test_shared_account_trade_listing_filters_by_strategy(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "index_stoploss"},
        )
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"algo_instance_id": "algo-2", "strategy_tag": "ema_monitor"},
        )

        trades = await self.service.list_trades("kite:test-paper", strategy_tag="index_stoploss")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].metadata["strategy_tag"], "index_stoploss")

    async def test_strategy_exit_closes_all_linked_legs(self):
        attribution = {
            "algo_instance_id": "option-strategy:run-1",
            "strategy_tag": "short_straddle",
            "option_strategy_id": "run-1",
        }
        await self.service.place_basket(
            account_scope="kite:test-paper",
            basket_payload={
                "all_or_none": True,
                "orders": [
                    {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APR22000CE",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "NRML",
                        "order_type": "MARKET",
                        "quantity": 50,
                    },
                    {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APR22000CE",
                        "transaction_type": "SELL",
                        "variety": "regular",
                        "product": "NRML",
                        "order_type": "MARKET",
                        "quantity": 50,
                    },
                ],
            },
            attribution=attribution,
        )

        # Build an actually open two-leg strategy manually.
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NFO",
                "tradingsymbol": "NIFTY24APR22000CE",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "NRML",
                "order_type": "MARKET",
                "quantity": 50,
            },
            attribution=attribution,
        )
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NFO",
                "tradingsymbol": "NIFTY24APR22000CE",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "NRML",
                "order_type": "MARKET",
                "quantity": 50,
            },
            attribution={**attribution, "option_strategy_id": "run-1-adjustment"},
        )

        # force the open lots and position snapshot to still belong to the same strategy family
        for position in self.repository.positions.values():
            position.metadata["option_strategy_id"] = "run-1"
        for lot in self.repository.position_lots.values():
            if int(lot.remaining_quantity) > 0:
                lot.metadata["option_strategy_id"] = "run-1"

        result = await self.service.exit_strategy(account_scope="kite:test-paper", strategy_id="run-1")

        self.assertEqual(result["status"], "success")
        open_positions = await self.service.list_positions("kite:test-paper", only_open=True)
        self.assertEqual(len(open_positions), 0)

    async def test_strategy_summary_groups_positions_by_strategy(self):
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
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "bull_call", "option_strategy_id": "run-bull"},
        )

        summary = await self.service.get_strategy_summary("kite:test-paper")

        self.assertEqual(summary["account"]["account_scope"], "kite:test-paper")
        self.assertGreaterEqual(len(summary["strategies"]), 1)
        grouped = next(item for item in summary["strategies"] if item["strategy_id"] == "run-bull")
        self.assertEqual(grouped["strategy_tag"], "bull_call")
        self.assertEqual(grouped["leg_count"], 1)

    async def test_strategy_summary_prefers_strategy_run_id_for_grouping(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"strategy_run_id": "run-canonical", "strategy_tag": "bull_call"},
        )

        summary = await self.service.get_strategy_summary("kite:test-paper")

        grouped = next(item for item in summary["strategies"] if item["strategy_id"] == "run-canonical")
        self.assertEqual(grouped["strategy_run_id"], "run-canonical")
        self.assertEqual(grouped["strategy_tag"], "bull_call")

    async def test_strategy_summary_gates_manual_activity_from_strategy_exit(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"strategy_tag": "manual_flow"},
        )

        summary = await self.service.get_strategy_summary("kite:test-paper")

        grouped = next(item for item in summary["strategies"] if item["strategy_id"].startswith("manual:"))
        self.assertEqual(grouped["capabilities"]["can_exit_strategy"], False)
        self.assertIn("manual paper activity", grouped["capabilities"]["exit_reason"].lower())

    async def test_strategy_summary_marks_shared_open_position_as_unsupported(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"option_strategy_id": "run-a", "strategy_tag": "alpha"},
        )
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"option_strategy_id": "run-b", "strategy_tag": "beta"},
        )

        summary = await self.service.get_strategy_summary("kite:test-paper")

        grouped = next(item for item in summary["strategies"] if item["strategy_id"].startswith("unsupported:"))
        self.assertEqual(grouped["status"], "open")
        self.assertEqual(grouped["capabilities"]["can_exit_strategy"], False)
        self.assertIn("shared across multiple strategy ids", grouped["capabilities"]["exit_reason"].lower())

    async def test_strategy_exit_does_not_shadow_order_snapshot_collections(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"option_strategy_id": "run-shadow", "strategy_tag": "single_leg"},
        )
        result = await self.service.exit_strategy(account_scope="kite:test-paper", strategy_id="run-shadow")
        self.assertIn(result["status"], {"success", "partial", "failed", "noop"})

    async def test_account_summary_reconciles_realized_and_unrealized_after_partial_close(self):
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
            attribution={"option_strategy_id": "run-pnl", "strategy_tag": "swing"},
        )

        self.market_runtime.ticks[256265] = {
            "instrument_token": 256265,
            "last_price": 155.0,
            "depth": {"buy": [{"price": 154.9}], "sell": [{"price": 155.1}]},
        }
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"option_strategy_id": "run-pnl", "strategy_tag": "swing"},
        )

        summary = await self.service.get_account_summary("kite:test-paper")
        positions = await self.service.list_positions("kite:test-paper", only_open=True)
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(float(positions[0].realized_pnl), summary["realized_pnl"], places=6)
        self.assertAlmostEqual(float(positions[0].unrealized_pnl), summary["unrealized_pnl"], places=6)
        self.assertGreater(summary["realized_pnl"], 0)
        self.assertGreater(summary["unrealized_pnl"], 0)

    async def test_all_or_none_basket_rejects_without_partial_mutation(self):
        result = await self.service.place_basket(
            account_scope="kite:test-paper",
            basket_payload={
                "all_or_none": True,
                "orders": [
                    {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 1,
                    },
                    {
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY24APR22000CE",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "NRML",
                        "order_type": "MARKET",
                        "quantity": 5,
                    },
                ],
            },
            attribution={"algo_instance_id": "algo-basket", "strategy_tag": "index_stoploss"},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.repository.orders), 0)
        self.assertEqual(len(self.repository.trades), 0)

    async def test_sl_order_uses_limit_price_when_triggered(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "SL",
                "quantity": 1,
                "price": 149.0,
                "trigger_price": 149.5,
            },
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "bracket_stoploss"},
        )

        await self.service.process_tick({"instrument_token": 256265, "last_price": 149.4, "depth": {"buy": [{"price": 149.3}], "sell": [{"price": 149.5}]}})

        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.average_price, Decimal("149.0"))

    async def test_sl_order_stays_open_after_trigger_until_limit_is_executable(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "SL",
                "quantity": 1,
                "price": 150.0,
                "trigger_price": 150.5,
            },
            attribution={"algo_instance_id": "algo-1", "strategy_tag": "bracket_stoploss"},
        )

        await self.service.process_tick({"instrument_token": 256265, "last_price": 150.4, "depth": {"buy": [{"price": 149.8}], "sell": [{"price": 150.5}]}})
        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, PaperOrderStatus.OPEN)
        self.assertTrue(order.metadata["stop_triggered"])

        await self.service.process_tick({"instrument_token": 256265, "last_price": 150.1, "depth": {"buy": [{"price": 150.0}], "sell": [{"price": 150.2}]}})
        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.average_price, Decimal("150.0"))

    async def test_fill_rejects_when_charges_would_overdraw_account(self):
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

        account = self.repository.accounts["kite:test-paper"]
        self.repository.accounts["kite:test-paper"] = account.model_copy(update={"available_funds": Decimal("0.00")})

        await self.service.process_tick({"instrument_token": 256265, "last_price": 147.5, "depth": {"buy": [{"price": 147.4}], "sell": [{"price": 147.5}]}})

        order = next(iter(self.repository.orders.values()))
        self.assertEqual(order.status, PaperOrderStatus.REJECTED)
        self.assertEqual(len(self.repository.trades), 0)

    async def test_blocked_funds_clears_after_full_position_exit(self):
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
            attribution={"strategy_run_id": "run-blocked-clear", "strategy_tag": "blocked_clear"},
        )
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 2,
            },
            attribution={"strategy_run_id": "run-blocked-clear", "strategy_tag": "blocked_clear"},
        )

        account = await self.service.get_account_summary("kite:test-paper")
        self.assertEqual(account["open_position_count"], 0)
        self.assertAlmostEqual(account["blocked_funds"], 0.0, places=6)

    async def test_all_or_none_rolls_back_when_later_leg_fails_after_prior_fill(self):
        service = PaperTradingService(
            repository=self.repository,
            instruments_repository=FakeInstrumentsRepository(),
            market_data_runtime=self.market_runtime,
            default_starting_balance=Decimal("30000.00"),
        )

        result = await service.place_basket(
            account_scope="kite:test-paper",
            basket_payload={
                "all_or_none": True,
                "orders": [
                    {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 500,
                    },
                    {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 500,
                    },
                ],
            },
            attribution={"algo_instance_id": "algo-basket", "strategy_tag": "index_stoploss"},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.repository.orders), 0)
        self.assertEqual(len(self.repository.trades), 0)
        self.assertEqual(len(self.repository.positions), 0)
        self.assertEqual(len(self.repository.fund_ledger), 0)

    async def test_strategy_exit_prefers_attributed_lots_for_same_symbol(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"strategy_run_id": "run-a", "strategy_tag": "alpha"},
        )
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"strategy_run_id": "run-b", "strategy_tag": "beta"},
        )

        result = await self.service.exit_strategy(account_scope="kite:test-paper", strategy_id="run-a")

        self.assertEqual(result["status"], "success")
        positions = await self.service.list_positions("kite:test-paper", only_open=True)
        self.assertEqual(len(positions), 1)
        self.assertEqual(int(positions[0].net_quantity), 1)
        open_lots = self.repository.list_open_position_lots("kite:test-paper", instrument_token=256265, product="MIS")
        self.assertEqual(len(open_lots), 1)
        self.assertEqual(str(open_lots[0].metadata.get("strategy_run_id")), "run-b")

    async def test_strategy_exit_blocks_on_legacy_unresolved_lots(self):
        await self.service.place_order(
            account_scope="kite:test-paper",
            order_payload={
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
            },
            attribution={"strategy_run_id": "run-legacy", "strategy_tag": "legacy"},
        )
        for lot in self.repository.position_lots.values():
            lot.metadata = {}

        result = await self.service.exit_strategy(account_scope="kite:test-paper", strategy_id="run-legacy")

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("unresolved_lots" in reason for reason in result.get("stale_reasons", [])))
