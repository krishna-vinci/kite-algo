import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import AlgoInstance  # noqa: E402
from algo_runtime.registry import AlgoRegistry, RegistryError  # noqa: E402


class DemoAlgo:
    ALGO_TYPE = "demo"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.kwargs = kwargs

    async def initialize(self, context):
        return None

    async def evaluate(self, snapshot, state):
        return []


class MissingEvaluateAlgo:
    ALGO_TYPE = "broken"

    async def initialize(self, context):
        return None


class AlgoRegistryTests(unittest.TestCase):
    def test_register_and_create_algo(self):
        registry = AlgoRegistry()
        registry.register(DemoAlgo)
        instance = AlgoInstance(instance_id="algo-1", algo_type="demo")

        created = registry.create(instance, foo="bar")

        self.assertIsInstance(created, DemoAlgo)
        self.assertEqual(created.instance.instance_id, "algo-1")
        self.assertEqual(created.kwargs["foo"], "bar")

    def test_duplicate_registration_raises(self):
        registry = AlgoRegistry()
        registry.register(DemoAlgo)

        with self.assertRaises(RegistryError):
            registry.register(DemoAlgo)

    def test_missing_required_methods_raise(self):
        registry = AlgoRegistry()

        with self.assertRaises(RegistryError):
            registry.register(MissingEvaluateAlgo)

    def test_get_unknown_algo_type_raises(self):
        registry = AlgoRegistry()

        with self.assertRaises(RegistryError):
            registry.get("unknown")
