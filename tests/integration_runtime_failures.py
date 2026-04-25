import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from redis.asyncio import from_url as redis_from_url
    from sqlalchemy import text

    from broker_api.order_runtime import CanonicalOrderEventRuntime, RealTimePositionsService
    from broker_api.websocket_manager import write_ticks_to_redis_overlay
    from tests.runtime_support import (
        apply_schema,
        create_test_session_factory,
        flush_test_redis,
        integration_env_ready,
        TEST_REDIS_URL,
        truncate_runtime_tables,
    )
    INTEGRATION_IMPORTS_READY = True
except Exception:
    redis_from_url = None
    text = None
    CanonicalOrderEventRuntime = None
    RealTimePositionsService = None
    write_ticks_to_redis_overlay = None
    apply_schema = None
    create_test_session_factory = None
    flush_test_redis = None
    integration_env_ready = lambda: False
    TEST_REDIS_URL = None
    truncate_runtime_tables = None
    INTEGRATION_IMPORTS_READY = False


@unittest.skipUnless(INTEGRATION_IMPORTS_READY and integration_env_ready(), "Integration env not configured")
class RuntimeFailureIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        apply_schema()
        cls.engine, cls.SessionLocal = create_test_session_factory()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    async def asyncSetUp(self):
        truncate_runtime_tables(self.SessionLocal)
        await flush_test_redis()
        self.runtime = CanonicalOrderEventRuntime()
        self.positions_service = RealTimePositionsService()

    async def test_process_pending_events_marks_row_failed_on_projection_error(self):
        payload = {
            "user_id": "AB1234",
            "order_id": "OID-FAIL-1",
            "status": "UPDATE",
            "order_timestamp": "2026-04-05 09:15:00",
            "exchange": "NSE",
            "tradingsymbol": "SBIN",
            "instrument_token": 12345,
            "product": "MIS",
            "transaction_type": "BUY",
            "quantity": 1,
            "filled_quantity": 1,
            "average_price": 100.0,
        }

        with patch("broker_api.order_runtime.SessionLocal", self.SessionLocal):
            await self.runtime.ingest_event(
                source="webhook",
                raw_table="order_events",
                payload=payload,
                corr_id="failure-ingest",
            )
            with patch.object(self.runtime, "_upsert_projection_from_event", side_effect=RuntimeError("boom")):
                processed = await self.runtime.process_pending_events(batch_size=10)

        self.assertEqual(processed, 0)

        db = self.SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT processing_state, last_error FROM canonical_order_events WHERE order_id = :order_id"
                ),
                {"order_id": "OID-FAIL-1"},
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(row[0], "failed")
        self.assertIn("boom", row[1])

    async def test_sync_dirty_orders_leaves_projection_dirty_when_trade_fetch_fails(self):
        db = self.SessionLocal()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO order_state_projection (
                        account_id, order_id, latest_canonical_event_id, latest_status,
                        latest_event_timestamp, last_seen_filled_quantity,
                        dirty_for_trade_sync, needs_reconcile, terminal,
                        exchange, tradingsymbol, instrument_token, product, transaction_type, updated_at
                    ) VALUES (
                        'kite:AB1234', 'OID-DIRTY-1', 1, 'UPDATE',
                        NOW(), 1,
                        TRUE, FALSE, FALSE,
                        'NSE', 'SBIN', 12345, 'MIS', 'BUY', NOW()
                    )
                    """
                )
            )
            db.commit()
        finally:
            db.close()

        fake_kite = SimpleNamespace(order_trades=lambda _order_id: (_ for _ in ()).throw(RuntimeError("trades unavailable")))
        redis_client = redis_from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            with patch("broker_api.order_runtime.SessionLocal", self.SessionLocal), patch(
                "broker_api.order_runtime.engine",
                self.engine,
            ), patch("broker_api.order_runtime.get_redis", return_value=redis_client), patch(
                "broker_api.order_runtime.publish_event",
                AsyncMock(),
            ):
                synced = await self.runtime.sync_dirty_orders(fake_kite, self.positions_service, batch_size=10)
        finally:
            await redis_client.aclose()

        self.assertEqual(synced, 0)
        db = self.SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT dirty_for_trade_sync, needs_reconcile FROM order_state_projection WHERE account_id = :account_id AND order_id = :order_id"
                ),
                {"account_id": "kite:AB1234", "order_id": "OID-DIRTY-1"},
            ).fetchone()
        finally:
            db.close()
        self.assertTrue(row[0])
        self.assertFalse(row[1])

    async def test_write_ticks_to_redis_overlay_swallows_real_connection_failures(self):
        dead_redis = redis_from_url("redis://127.0.0.1:56380/0", decode_responses=True)
        try:
            with patch("broker_api.websocket_manager.get_redis", return_value=dead_redis):
                await write_ticks_to_redis_overlay(
                    [{"instrument_token": 12345, "last_price": 101.0, "change": 1.2}]
                )
        finally:
            await dead_redis.aclose()
