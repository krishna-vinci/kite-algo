import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.intent_bridge import IntentBridge  # noqa: E402
from algo_runtime.models import ExecutionMode, OrderIntent  # noqa: E402
from paper_runtime.executor import DryRunIntentHandler  # noqa: E402


class FakeModeHandler:
    def __init__(self, mode_name: str):
        self.mode_name = mode_name
        self.calls = []

    async def handle(self, intent, *, context=None):
        self.calls.append({"intent": intent, "context": context})
        return {"mode": self.mode_name, "intent_type": intent.intent_type, "context": context}


class ThreeModeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_intent_bridge_routes_paper_mode_to_paper_handler(self):
        live = FakeModeHandler("live")
        paper = FakeModeHandler("paper")
        bridge = IntentBridge(
            live_order_intent_handler=live,
            paper_order_intent_handler=paper,
            dry_run_order_intent_handler=DryRunIntentHandler(),
        )
        intent = OrderIntent(intent_type="place_order", payload={"order": {}, "session_id": "ignored"}, dedupe_key="paper-1")

        result = await bridge.execute([intent], execution_mode=ExecutionMode.PAPER, context={"instance_id": "algo-paper"})

        self.assertEqual(result["execution_mode"], "paper")
        self.assertEqual(result["order_results"][0]["result"]["mode"], "paper")
        self.assertEqual(len(live.calls), 0)
        self.assertEqual(len(paper.calls), 1)
        self.assertEqual(paper.calls[0]["context"]["instance_id"], "algo-paper")

    async def test_intent_bridge_routes_dry_run_mode_without_mutation(self):
        bridge = IntentBridge(
            live_order_intent_handler=FakeModeHandler("live"),
            dry_run_order_intent_handler=DryRunIntentHandler(),
        )
        intent = OrderIntent(intent_type="place_basket", payload={"basket": {"orders": []}}, dedupe_key="dry-1")

        result = await bridge.execute([intent], execution_mode=ExecutionMode.DRY_RUN, context={"instance_id": "algo-dry"})

        self.assertEqual(result["execution_mode"], "dry_run")
        self.assertEqual(result["order_results"][0]["result"]["mode"], "dry_run")
        self.assertFalse(result["order_results"][0]["result"]["mutated_state"])

    async def test_dedupe_cache_is_scoped_by_mode_and_instance(self):
        live = FakeModeHandler("live")
        paper = FakeModeHandler("paper")
        bridge = IntentBridge(
            live_order_intent_handler=live,
            paper_order_intent_handler=paper,
            dry_run_order_intent_handler=DryRunIntentHandler(),
        )
        intent = OrderIntent(intent_type="place_order", payload={"order": {}}, dedupe_key="same-key")

        live_result = await bridge.execute([intent], execution_mode=ExecutionMode.LIVE, context={"instance_id": "algo-1"})
        paper_result = await bridge.execute([intent], execution_mode=ExecutionMode.PAPER, context={"instance_id": "algo-2"})

        self.assertEqual(live_result["order_results"][0]["status"], "executed")
        self.assertEqual(paper_result["order_results"][0]["status"], "executed")
        self.assertEqual(len(live.calls), 1)
        self.assertEqual(len(paper.calls), 1)
