import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.admin import refresh_runtime, update_instance_status, upsert_instance  # noqa: E402
from algo_runtime.kernel import AlgoKernel  # noqa: E402
from algo_runtime.models import AlgoInstance, AlgoLifecycleState, DependencySpec, ExecutionMode, TriggerType  # noqa: E402
from algo_runtime.registry import AlgoRegistry  # noqa: E402
from algo_runtime.repository import InMemoryAlgoRepository  # noqa: E402
from algo_runtime.service import AlgoRuntimeService  # noqa: E402
from algo_runtime.state_store import InMemoryAlgoStateStore  # noqa: E402


class FakeSnapshotBuilder:
    async def build_for_instance(self, instance, trigger):
        return {"instance_id": instance.instance_id, "trigger": trigger.trigger_type.value}


class DemoAlgo:
    ALGO_TYPE = "demo"

    def __init__(self, instance, **kwargs):
        self.instance = instance

    async def initialize(self, context):
        return None

    async def evaluate(self, snapshot, state):
        return []


class FakeLiveWorker:
    def __init__(self):
        self.sync_calls = 0

    async def sync_dependencies(self):
        self.sync_calls += 1
        return {"synced": self.sync_calls}

    def status(self):
        return {"synced": self.sync_calls}


class AlgoRuntimeAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        self.service = AlgoRuntimeService(
            AlgoKernel(
                registry=registry,
                repository=InMemoryAlgoRepository(),
                state_store=InMemoryAlgoStateStore(),
                snapshot_builder=FakeSnapshotBuilder(),
            )
        )
        await self.service.start()
        self.live_worker = FakeLiveWorker()

    async def test_upsert_instance_persists_and_refreshes_runtime(self):
        result = await upsert_instance(
            self.service,
            AlgoInstance(
                instance_id="demo-1",
                algo_type="demo",
                execution_mode=ExecutionMode.PAPER,
                dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
            ),
            live_worker=self.live_worker,
        )

        self.assertEqual(result["instance"]["instance_id"], "demo-1")
        self.assertEqual(result["instance"]["execution_mode"], "paper")
        self.assertEqual(result["runtime"]["instance_ids"], ["demo-1"])
        self.assertEqual(result["runtime"]["instances"][0]["execution_mode"], "paper")
        self.assertEqual(result["live_worker"], {"synced": 1})

    async def test_update_instance_status_reloads_and_syncs(self):
        await upsert_instance(
            self.service,
            AlgoInstance(
                instance_id="demo-1",
                algo_type="demo",
                dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
            ),
            live_worker=self.live_worker,
        )

        result = await update_instance_status(
            self.service,
            instance_id="demo-1",
            status=AlgoLifecycleState.STOPPED,
            live_worker=self.live_worker,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["instance"]["status"], "stopped")
        self.assertEqual(result["runtime"]["instance_ids"], [])
        self.assertEqual(result["live_worker"], {"synced": 2})

    async def test_refresh_runtime_returns_status_even_without_live_worker(self):
        result = await refresh_runtime(self.service)
        self.assertTrue(result["runtime"]["started"])
        self.assertIsNone(result["live_worker"])
