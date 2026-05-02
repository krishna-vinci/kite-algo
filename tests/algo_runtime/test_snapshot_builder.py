import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import (  # noqa: E402
    AlgoInstance,
    CandleSeriesSpec,
    DependencySpec,
    IndicatorSpec,
    OptionExpiryMode,
    OptionReadSpec,
    OrderScope,
    PositionFilter,
    TriggerEvent,
)
from algo_runtime.snapshot_builder import DependencyFilteredSnapshotBuilder, OptionsSnapshotReader, OrderProjectionReader  # noqa: E402


class FakeMarketReader:
    async def get_tick(self, token):
        return {
            "instrument_token": token,
            "last_price": 24210.55,
            "exchange_timestamp": "2026-04-07T12:34:56+00:00",
            "received_at": "2026-04-07T12:34:57+00:00",
        }

    async def get_last_price(self, token):
        return 24210.55

    def get_runtime_status(self):
        return "CONNECTED"


class FakeCandleReader:
    async def get_latest_closed(self, token, timeframe):
        return {"ts": "2026-04-07T12:30:00+00:00", "close": 24180.0}

    async def get_forming(self, token, timeframe):
        return {"ts": "2026-04-07T12:35:00+00:00", "close": 24205.0}

    async def get_history(self, spec):
        return [{"ts": "2026-04-07T12:25:00+00:00", "close": 24150.0}] * spec.lookback


class FakeIndicatorReader:
    async def get_indicator(self, spec, candles=None):
        return {"value": 24188.2, "ready": True, "history_len": len((candles or {}).get("history", []))}


class FakeOptionsReader:
    async def read(self, spec):
        return {"underlying": spec.underlying, "spot_ltp": 24210.55}


class FakePositionsReader:
    async def get_positions(self, account_id):
        return {
            "NSE:NIFTY:MIS": {
                "instrument_token": 256265,
                "exchange": "NSE",
                "product": "MIS",
                "tradingsymbol": "NIFTY",
                "pnl": 123.45,
            },
            "NSE:BANKNIFTY:NRML": {
                "instrument_token": 260105,
                "exchange": "NSE",
                "product": "NRML",
                "tradingsymbol": "BANKNIFTY",
                "pnl": 50.0,
            },
        }


class FakeOrdersReader:
    async def get_orders(self, account_id, order_scope, limit=20):
        return [{"order_id": "OID-1", "latest_status": "COMPLETE"}]


class SnapshotBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_for_instance_shapes_requested_sections_only(self):
        builder = DependencyFilteredSnapshotBuilder(
            market_reader=FakeMarketReader(),
            candle_reader=FakeCandleReader(),
            indicator_reader=FakeIndicatorReader(),
            options_reader=FakeOptionsReader(),
            positions_reader=FakePositionsReader(),
            orders_reader=FakeOrdersReader(),
        )
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="ema_monitor",
            dependency_spec=DependencySpec(
                market_tokens={256265: "full"},
                candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=20, include_forming=True)],
                indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 20})],
                option_reads=[OptionReadSpec(underlying="NIFTY")],
                account_scope="kite:AB1234",
                position_filters=[PositionFilter(product="MIS")],
                order_scope=OrderScope.ACCOUNT_RELEVANT,
                triggers={"tick", "candle_close"},
            ),
        )

        snapshot = await builder.build_for_instance(instance, TriggerEvent(type="tick", token=256265))

        self.assertEqual(snapshot.meta["runtime_status"], "CONNECTED")
        self.assertEqual(snapshot.market["ltp"]["256265"], 24210.55)
        candle_key = "256265:5minute:20:1"
        self.assertIn(candle_key, snapshot.candles)
        self.assertIsNotNone(snapshot.candles[candle_key]["forming"])
        indicator_key = "ema:256265:5minute:length=20"
        self.assertEqual(snapshot.indicators[indicator_key]["value"], 24188.2)
        self.assertEqual(snapshot.options["NIFTY:nearest:snapshot:5:" ]["underlying"], "NIFTY")
        self.assertEqual(snapshot.positions["totals"]["position_count"], 1)
        self.assertEqual(snapshot.positions["totals"]["total_pnl"], 123.45)
        self.assertEqual(snapshot.orders["relevant"][0]["order_id"], "OID-1")

    async def test_indicator_section_prefers_larger_matching_lookback(self):
        builder = DependencyFilteredSnapshotBuilder(
            candle_reader=FakeCandleReader(),
            indicator_reader=FakeIndicatorReader(),
        )
        instance = AlgoInstance(
            instance_id="algo-3",
            algo_type="ema_monitor",
            dependency_spec=DependencySpec(
                candle_series=[
                    CandleSeriesSpec(token=256265, timeframe="5minute", lookback=5),
                    CandleSeriesSpec(token=256265, timeframe="5minute", lookback=50),
                ],
                indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 20})],
                triggers={"candle_close"},
            ),
        )

        snapshot = await builder.build_for_instance(instance, TriggerEvent(type="candle_close", token=256265, timeframe="5minute"))

        indicator_key = "ema:256265:5minute:length=20"
        self.assertEqual(snapshot.indicators[indicator_key]["history_len"], 50)

    async def test_build_for_instance_leaves_unrequested_sections_empty(self):
        builder = DependencyFilteredSnapshotBuilder(market_reader=FakeMarketReader())
        instance = AlgoInstance(
            instance_id="algo-2",
            algo_type="simple",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={"tick"}),
        )

        snapshot = await builder.build_for_instance(instance, TriggerEvent(type="tick", token=256265))

        self.assertTrue(snapshot.market)
        self.assertEqual(snapshot.candles, {})
        self.assertEqual(snapshot.indicators, {})
        self.assertEqual(snapshot.options, {})
        self.assertEqual(snapshot.positions, {})
        self.assertEqual(snapshot.orders, {})


class OptionsSnapshotReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_expiry_falls_back_to_raw_snapshot(self):
        class FakeOptionsManager:
            def get_snapshot(self, underlying):
                return {"underlying": underlying, "expiries": ["not-a-date"], "spot_ltp": 1}

        reader = OptionsSnapshotReader(FakeOptionsManager(), strike_selector=object())

        payload = await reader.read(
            OptionReadSpec(underlying="NIFTY", view="mini_chain", expiry_mode=OptionExpiryMode.NEAREST)
        )

        self.assertEqual(payload["underlying"], "NIFTY")


class OrderProjectionReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_instance_relevant_scope_fails_fast(self):
        reader = OrderProjectionReader(session_factory=lambda: None)

        with self.assertRaises(ValueError):
            await reader.get_orders("kite:AB1234", OrderScope.INSTANCE_RELEVANT)
