import asyncio
import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.dependencies import DependencyAggregator  # noqa: E402
from algo_runtime.live import AlgoRuntimeLiveWorker  # noqa: E402
from algo_runtime.models import (  # noqa: E402
    AlgoInstance,
    CandleSeriesSpec,
    DependencySpec,
    IndicatorSpec,
    OrderScope,
    TriggerType,
)


class FakeMarketRuntime:
    def __init__(self):
        self.set_calls = []
        self.delete_calls = []

    async def set_owner_subscriptions(self, owner_id, subscriptions):
        self.set_calls.append((owner_id, dict(subscriptions)))
        return {"status": "ok"}

    async def delete_owner(self, owner_id):
        self.delete_calls.append(owner_id)
        return {"status": "ok"}


class FakeCandleAggregator:
    def __init__(self):
        self.calls = []

    async def set_external_tokens(self, owner_id, tokens):
        self.calls.append((owner_id, sorted(tokens)))


class FakeKernel:
    def __init__(self, instances):
        self.instances = list(instances)
        self.dependency_aggregator = DependencyAggregator()

    async def list_instances(self):
        return list(self.instances)


class FakeService:
    def __init__(self, instances):
        self.kernel = FakeKernel(instances)
        self.dispatched = []

    async def dispatch_trigger(self, trigger):
        self.dispatched.append(trigger)
        return [{"instance_id": "demo", "action_count": 0}]


class AlgoRuntimeLiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instances = [
            AlgoInstance(
                instance_id="tick-algo",
                algo_type="index_stoploss",
                dependency_spec=DependencySpec(
                    market_tokens={256265: "full"},
                    triggers={TriggerType.TICK},
                ),
            ),
            AlgoInstance(
                instance_id="candle-algo",
                algo_type="ema_monitor",
                dependency_spec=DependencySpec(
                    candle_series=[CandleSeriesSpec(token=256265, timeframe="5minute", lookback=50)],
                    indicators=[IndicatorSpec(kind="ema", token=256265, timeframe="5minute", params={"length": 9})],
                    triggers={TriggerType.CANDLE_CLOSE},
                ),
            ),
            AlgoInstance(
                instance_id="account-algo",
                algo_type="bracket_stoploss",
                dependency_spec=DependencySpec(
                    account_scope="kite:AB1234",
                    order_scope=OrderScope.ACCOUNT_RELEVANT,
                    triggers={TriggerType.ORDER_UPDATE, TriggerType.FILL_UPDATE, TriggerType.POSITION_UPDATE},
                ),
            ),
        ]
        self.service = FakeService(self.instances)
        self.market_runtime = FakeMarketRuntime()
        self.candle_aggregator = FakeCandleAggregator()
        self.worker = AlgoRuntimeLiveWorker(
            service=self.service,
            market_data_runtime=self.market_runtime,
            candle_aggregator=self.candle_aggregator,
            redis_client=object(),
        )

    async def test_sync_dependencies_updates_market_and_candle_routing(self):
        status = await self.worker.sync_dependencies()

        self.assertEqual(len(self.market_runtime.set_calls), 1)
        self.assertEqual(self.market_runtime.set_calls[0][1], {256265: "full"})
        self.assertEqual(self.candle_aggregator.calls[-1][1], [256265])
        self.assertEqual(status["routing"]["market_tokens"], [256265])
        self.assertEqual(status["routing"]["candle_pairs"], ["256265:5minute"])
        self.assertEqual(status["routing"]["account_scopes"], ["kite:AB1234"])

    async def test_handlers_enqueue_and_dispatch_matching_live_triggers(self):
        await self.worker.sync_dependencies()
        self.worker._running = True
        dispatch_task = asyncio.create_task(self.worker._dispatch_loop())
        try:
            await self.worker._handle_tick_message({"instrument_token": 256265, "last_price": 24000.0}, {})
            await self.worker._handle_candle_message(
                {
                    "instrument_token": 256265,
                    "interval": "5minute",
                    "candle": ["2026-04-07T09:20:00+00:00", 1, 2, 0.5, 1.5, 100],
                },
                {},
            )
            await self.worker._handle_order_message(
                {
                    "user_id": "AB1234",
                    "order_id": "OID-1",
                    "status": "COMPLETE",
                    "filled_quantity": 10,
                    "event_timestamp": "2026-04-07T09:21:00+00:00",
                },
                {},
            )
            await self.worker._handle_position_message(
                {
                    "account_id": "kite:AB1234",
                    "timestamp": "2026-04-07T09:22:00+00:00",
                    "positions": {},
                },
                {},
            )

            await asyncio.wait_for(self.worker._queue.join(), timeout=1.0)
        finally:
            self.worker._running = False
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)

        trigger_types = [trigger.trigger_type.value for trigger in self.service.dispatched]
        self.assertEqual(trigger_types, ["tick", "candle_close", "order_update", "fill_update", "position_update"])
        self.assertEqual(self.worker.status()["stats"]["processed"], 5)

    async def test_handlers_ignore_unrouted_events(self):
        await self.worker.sync_dependencies()

        await self.worker._handle_tick_message({"instrument_token": 999, "last_price": 1.0}, {})
        await self.worker._handle_candle_message({"instrument_token": 256265, "interval": "day"}, {})
        await self.worker._handle_order_message({"user_id": "OTHER", "filled_quantity": 3}, {})
        await self.worker._handle_position_message({"account_id": "kite:OTHER"}, {})

        self.assertEqual(self.worker._queue.qsize(), 0)
        self.assertEqual(self.service.dispatched, [])

    async def test_fill_updates_are_only_emitted_on_incremental_fill_progress(self):
        await self.worker.sync_dependencies()
        self.worker._running = True
        dispatch_task = asyncio.create_task(self.worker._dispatch_loop())
        try:
            payload = {
                "user_id": "AB1234",
                "order_id": "OID-1",
                "status": "UPDATE",
                "filled_quantity": 5,
                "event_timestamp": "2026-04-07T09:21:00+00:00",
            }
            await self.worker._handle_order_message(payload, {})
            await self.worker._handle_order_message(payload, {})
            await asyncio.wait_for(self.worker._queue.join(), timeout=1.0)
        finally:
            self.worker._running = False
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)

        trigger_types = [trigger.trigger_type.value for trigger in self.service.dispatched]
        self.assertEqual(trigger_types, ["order_update", "fill_update", "order_update"])

    async def test_unscoped_order_event_routing_is_permitted_when_runtime_has_unscoped_order_algo(self):
        service = FakeService(
            [
                AlgoInstance(
                    instance_id="unscoped-order-algo",
                    algo_type="demo",
                    dependency_spec=DependencySpec(
                        order_scope=OrderScope.ACCOUNT_RELEVANT,
                        triggers={TriggerType.ORDER_UPDATE},
                    ),
                )
            ]
        )
        worker = AlgoRuntimeLiveWorker(
            service=service,
            market_data_runtime=FakeMarketRuntime(),
            candle_aggregator=FakeCandleAggregator(),
            redis_client=object(),
        )
        await worker.sync_dependencies()
        worker._running = True
        dispatch_task = asyncio.create_task(worker._dispatch_loop())
        try:
            await worker._handle_order_message({"user_id": "OTHER", "order_id": "OID-2", "filled_quantity": 0}, {})
            await asyncio.wait_for(worker._queue.join(), timeout=1.0)
        finally:
            worker._running = False
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)

        self.assertEqual([trigger.trigger_type.value for trigger in service.dispatched], ["order_update"])
