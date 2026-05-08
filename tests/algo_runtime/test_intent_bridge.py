import unittest
from types import SimpleNamespace

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.intent_bridge import IntentBridge, KiteOrdersIntentHandler  # noqa: E402
from algo_runtime.models import NoopAction, NotifyAction, OrderIntent, StatePatchAction  # noqa: E402


class FakeOrderHandler:
    def __init__(self):
        self.calls = []

    async def handle(self, intent, *, context=None):
        self.calls.append((intent, context))
        return {"ok": True, "intent_type": intent.intent_type, "context": context}


class FakeSessionQuery:
    def __init__(self, session_obj):
        self.session_obj = session_obj

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self.session_obj


class FakeSessionFactory:
    def __init__(self, session_obj):
        self.session_obj = session_obj

    def __call__(self):
        return SimpleNamespace(query=lambda model: FakeSessionQuery(self.session_obj), close=lambda: None)


class FakeOrdersService:
    async def place_order(self, kite, req, corr_id, idempotency_key=None, session_id=None, response=None):
        return SimpleNamespace(model_dump=lambda mode=None: {"order_id": "OID-123", "session_id": session_id, "idempotency_key": idempotency_key})

    async def place_basket(self, kite, req, corr_id, session_id=None, idempotency_key=None, response=None):
        return SimpleNamespace(model_dump=lambda mode=None: {"status": "success", "session_id": session_id, "idempotency_key": idempotency_key})


class IntentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_dedupes_order_intents_by_key(self):
        handler = FakeOrderHandler()
        bridge = IntentBridge(order_intent_handler=handler)
        intent = OrderIntent(intent_type="place_order", payload={"session_id": "test-session-id", "order": {}}, dedupe_key="dup-1")

        first = await bridge.execute([intent])
        second = await bridge.execute([intent])

        self.assertEqual(len(handler.calls), 1)
        self.assertEqual(first["order_results"][0]["status"], "executed")
        self.assertEqual(second["order_results"][0]["status"], "deduped")

    async def test_split_actions_classifies_all_known_types(self):
        bridge = IntentBridge()

        order_intents, notifications, state_patches, noops = bridge.split_actions(
            [
                OrderIntent(intent_type="place_order", payload={}),
                NotifyAction(message="hello"),
                StatePatchAction(patch={"armed": True}),
                NoopAction(),
            ]
        )

        self.assertEqual(len(order_intents), 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(len(state_patches), 1)
        self.assertEqual(len(noops), 1)

    async def test_kite_orders_handler_uses_existing_orders_service(self):
        handler = KiteOrdersIntentHandler(
            orders_service=FakeOrdersService(),
            session_factory=FakeSessionFactory(SimpleNamespace(access_token="token-123")),
        )

        result = await handler.handle(
            OrderIntent(
                intent_type="place_order",
                payload={
                    "session_id": "test-session-id",
                    "order": {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 1,
                    },
                },
                dedupe_key="idem-123",
            )
        )

        self.assertEqual(result["result"]["order_id"], "OID-123")
        self.assertEqual(result["result"]["session_id"], "test-session-id")
        self.assertEqual(result["result"]["idempotency_key"], "idem-123")
