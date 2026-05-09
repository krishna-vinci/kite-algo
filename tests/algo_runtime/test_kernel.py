import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.algo_runtime.kernel import AlgoKernel  # noqa: E402
from backend.algo_runtime.models import (  # noqa: E402
    AlgoInstance,
    AlgoLifecycleState,
    DependencySpec,
    ExecutionMode,
    NotifyAction,
    StatePatchAction,
    TriggerEvent,
    TriggerType,
)
from backend.algo_runtime.registry import AlgoRegistry  # noqa: E402
from backend.algo_runtime.repository import InMemoryAlgoRepository  # noqa: E402
from backend.algo_runtime.service import AlgoRuntimeService  # noqa: E402
from backend.algo_runtime.state_store import InMemoryAlgoStateStore  # noqa: E402


class FakeSnapshotBuilder:
    async def build_for_instance(self, instance, trigger):
        return {
            "algo_instance_id": instance.instance_id,
            "trigger": trigger.trigger_type.value,
        }


class DemoAlgo:
    ALGO_TYPE = "demo"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.initialized = False

    async def initialize(self, context):
        self.initialized = True

    async def evaluate(self, snapshot, state):
        return [
            StatePatchAction(patch={"seen_trigger": snapshot["trigger"]}),
            NotifyAction(message=f"processed {self.instance.instance_id}"),
        ]


class BrokenAlgo(DemoAlgo):
    ALGO_TYPE = "broken"

    async def evaluate(self, snapshot, state):
        raise RuntimeError("boom")


class StopSelfAlgo(DemoAlgo):
    ALGO_TYPE = "stop-self"

    async def evaluate(self, snapshot, state):
        return [StatePatchAction(patch={"_instance_status": "stopped"})]


class FailingIntentBridge:
    async def execute(self, actions, **kwargs):
        raise RuntimeError("intent failed")


class KernelAndServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_loads_registered_instances_and_dispatches_trigger(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="demo",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)

        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        state_store = InMemoryAlgoStateStore()
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=state_store,
            snapshot_builder=FakeSnapshotBuilder(),
        )

        loaded = await kernel.load_active_instances()
        results = await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))
        checkpoint = await repository.get_checkpoint("algo-1")

        self.assertEqual(len(loaded), 1)
        self.assertEqual(results[0]["action_count"], 2)
        self.assertEqual(checkpoint.state["seen_trigger"], "tick")
        self.assertEqual(checkpoint.last_action["action_type"], "notify")

    async def test_service_start_and_status_include_dependency_summary(self):
        repository = InMemoryAlgoRepository()
        await repository.save_instance(
            AlgoInstance(
                instance_id="algo-1",
                algo_type="demo",
                execution_mode=ExecutionMode.PAPER,
                dependency_spec=DependencySpec(market_tokens={256265: "full"}, triggers={"tick"}),
            )
        )
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        service = AlgoRuntimeService(
            AlgoKernel(
                registry=registry,
                repository=repository,
                state_store=InMemoryAlgoStateStore(),
                snapshot_builder=FakeSnapshotBuilder(),
            )
        )

        await service.start()
        status = await service.status()

        self.assertTrue(status["started"])
        self.assertEqual(status["instance_count"], 1)
        self.assertEqual(status["instances"][0]["lifecycle_state"], "enabled")
        self.assertEqual(status["instances"][0]["execution_mode"], "paper")
        self.assertEqual(status["dependency_summary"]["market_tokens"]["256265"], "full")
        self.assertEqual(status["dependency_summary"]["triggers"], ["tick"])

    async def test_service_status_includes_last_evaluation_metadata(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="demo",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)
        service = AlgoRuntimeService(
            AlgoKernel(
                registry=AlgoRegistry(),
                repository=repository,
                state_store=InMemoryAlgoStateStore(),
                snapshot_builder=FakeSnapshotBuilder(),
            )
        )
        service.kernel.registry.register(DemoAlgo)

        await service.start()
        await service.dispatch_trigger(TriggerEvent(type="tick", token=256265))
        status = await service.status()

        self.assertIsNotNone(status["instances"][0]["last_evaluated_at"])
        self.assertEqual(status["instances"][0]["last_action"]["action_type"], "notify")
        self.assertEqual(status["instances"][0]["last_action_count"], 2)
        self.assertEqual(status["instances"][0]["last_trigger"]["type"], "tick")

    async def test_repeated_dispatch_keeps_state_stable(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="demo",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        state_store = InMemoryAlgoStateStore()
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=state_store,
            snapshot_builder=FakeSnapshotBuilder(),
        )

        await kernel.load_active_instances()
        await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))
        await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))
        hot_state = await state_store.get_hot_state("algo-1")

        self.assertEqual(hot_state, {"seen_trigger": "tick"})

    async def test_paused_instance_is_loaded_but_not_executed(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-paused",
            algo_type="demo",
            status=AlgoLifecycleState.PAUSED,
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        state_store = InMemoryAlgoStateStore()
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=state_store,
            snapshot_builder=FakeSnapshotBuilder(),
        )

        await kernel.load_active_instances()
        results = await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))

        self.assertEqual(results, [])
        self.assertEqual(await state_store.get_hot_state("algo-paused"), {})

    async def test_instance_failure_does_not_abort_other_instances(self):
        repository = InMemoryAlgoRepository()
        await repository.save_instance(
            AlgoInstance(
                instance_id="algo-good",
                algo_type="demo",
                dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
            )
        )
        await repository.save_instance(
            AlgoInstance(
                instance_id="algo-bad",
                algo_type="broken",
                dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
            )
        )
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        registry.register(BrokenAlgo)
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=InMemoryAlgoStateStore(),
            snapshot_builder=FakeSnapshotBuilder(),
        )

        await kernel.load_active_instances()
        results = await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))

        self.assertEqual(len(results), 2)
        self.assertTrue(any(result.get("error") == "boom" for result in results))
        self.assertTrue(any(result["action_count"] == 2 for result in results if "error" not in result))

    async def test_state_patch_can_stop_instance_after_trigger(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-stop",
            algo_type="stop-self",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)
        registry = AlgoRegistry()
        registry.register(StopSelfAlgo)
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=InMemoryAlgoStateStore(),
            snapshot_builder=FakeSnapshotBuilder(),
        )

        await kernel.load_active_instances()
        await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))
        updated = await repository.get_instance("algo-stop")

        self.assertEqual(updated.status.value, "stopped")

    async def test_service_lifecycle_guards_dispatch_and_refresh(self):
        service = AlgoRuntimeService(
            AlgoKernel(
                registry=AlgoRegistry(),
                repository=InMemoryAlgoRepository(),
                state_store=InMemoryAlgoStateStore(),
                snapshot_builder=FakeSnapshotBuilder(),
            )
        )

        with self.assertRaises(RuntimeError):
            await service.dispatch_trigger(TriggerEvent(type="manual"))

        with self.assertRaises(RuntimeError):
            await service.refresh_instances()

    async def test_failed_intent_execution_does_not_persist_checkpoint_or_state(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="demo",
            dependency_spec=DependencySpec(market_tokens={256265: "ltp"}, triggers={TriggerType.TICK}),
        )
        await repository.save_instance(instance)
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        state_store = InMemoryAlgoStateStore()
        kernel = AlgoKernel(
            registry=registry,
            repository=repository,
            state_store=state_store,
            snapshot_builder=FakeSnapshotBuilder(),
            intent_bridge=FailingIntentBridge(),
        )

        await kernel.load_active_instances()
        results = await kernel.dispatch_trigger(TriggerEvent(type="tick", token=256265))

        self.assertEqual(results[0]["error"], "intent failed")
        self.assertIsNone(await repository.get_checkpoint("algo-1"))
        self.assertEqual(await state_store.get_hot_state("algo-1"), {})
