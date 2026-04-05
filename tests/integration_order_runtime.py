import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from redis.asyncio import from_url as redis_from_url
    from sqlalchemy import text

    from broker_api.order_runtime import CanonicalOrderEventRuntime, RealTimePositionsService
    from tests.runtime_support import (
        TEST_REDIS_URL,
        apply_schema,
        create_test_session_factory,
        flush_test_redis,
        integration_env_ready,
        truncate_runtime_tables,
    )
    INTEGRATION_IMPORTS_READY = True
except Exception:
    redis_from_url = None
    text = None
    CanonicalOrderEventRuntime = None
    RealTimePositionsService = None
    TEST_REDIS_URL = None
    apply_schema = None
    create_test_session_factory = None
    flush_test_redis = None
    integration_env_ready = lambda: False
    truncate_runtime_tables = None
    INTEGRATION_IMPORTS_READY = False


@unittest.skipUnless(INTEGRATION_IMPORTS_READY and integration_env_ready(), "Integration env not configured")
class OrderRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.redis_client = redis_from_url(TEST_REDIS_URL, decode_responses=True)
        self.runtime = CanonicalOrderEventRuntime()
        self.positions_service = RealTimePositionsService()
        self.account_id = "kite:AB1234"

    async def asyncTearDown(self):
        await self.redis_client.aclose()

    async def test_ingest_process_and_trade_sync_flow(self):
        payload = {
            "user_id": "AB1234",
            "order_id": "OID-100",
            "status": "UPDATE",
            "order_timestamp": "2026-04-05 09:15:00",
            "exchange_update_timestamp": "2026-04-05 09:15:01",
            "exchange": "NSE",
            "tradingsymbol": "SBIN",
            "instrument_token": 12345,
            "product": "MIS",
            "transaction_type": "BUY",
            "quantity": 1,
            "filled_quantity": 1,
            "average_price": 101.5,
        }

        with patch("broker_api.order_runtime.SessionLocal", self.SessionLocal):
            first = await self.runtime.ingest_event(
                source="webhook",
                raw_table="order_events",
                payload=payload,
                corr_id="integration-1",
            )
            duplicate = await self.runtime.ingest_event(
                source="webhook",
                raw_table="order_events",
                payload=payload,
                corr_id="integration-2",
            )
            processed = await self.runtime.process_pending_events(batch_size=10)

        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(processed, 1)

        db = self.SessionLocal()
        try:
            projection = db.execute(
                text(
                    "SELECT latest_status, last_seen_filled_quantity, dirty_for_trade_sync FROM order_state_projection WHERE account_id = :account_id AND order_id = :order_id"
                ),
                {"account_id": self.account_id, "order_id": "OID-100"},
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(projection[0], "UPDATE")
        self.assertEqual(projection[1], 1)
        self.assertTrue(projection[2])

        fake_kite = SimpleNamespace(
            order_trades=lambda order_id: [
                {
                    "trade_id": "TR-1",
                    "order_id": order_id,
                    "instrument_token": 12345,
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 1,
                    "average_price": 101.5,
                    "fill_timestamp": "2026-04-05 09:15:01",
                }
            ]
        )

        with patch("broker_api.order_runtime.SessionLocal", self.SessionLocal), patch(
            "broker_api.order_runtime.engine",
            self.engine,
        ), patch("broker_api.order_runtime.get_redis", return_value=self.redis_client), patch(
            "broker_api.order_runtime.publish_event",
            AsyncMock(),
        ):
            synced = await self.runtime.sync_dirty_orders(fake_kite, self.positions_service, batch_size=10)

        self.assertEqual(synced, 1)

        db = self.SessionLocal()
        try:
            trade_fill = db.execute(
                text(
                    "SELECT applied_to_position FROM order_trade_fills WHERE account_id = :account_id AND trade_id = :trade_id"
                ),
                {"account_id": self.account_id, "trade_id": "TR-1"},
            ).fetchone()
            position = db.execute(
                text(
                    "SELECT net_quantity, average_price FROM account_positions WHERE account_id = :account_id AND instrument_token = :instrument_token AND product = :product"
                ),
                {"account_id": self.account_id, "instrument_token": 12345, "product": "MIS"},
            ).fetchone()
            projection_after_sync = db.execute(
                text(
                    "SELECT dirty_for_trade_sync, needs_reconcile FROM order_state_projection WHERE account_id = :account_id AND order_id = :order_id"
                ),
                {"account_id": self.account_id, "order_id": "OID-100"},
            ).fetchone()
        finally:
            db.close()

        self.assertTrue(trade_fill[0])
        self.assertEqual(position[0], 1)
        self.assertEqual(float(position[1]), 101.5)
        self.assertFalse(projection_after_sync[0])
        self.assertFalse(projection_after_sync[1])

    async def test_reconcile_account_positions_replaces_stale_rows(self):
        db = self.SessionLocal()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO account_positions (
                        account_id, instrument_token, product, exchange, tradingsymbol,
                        net_quantity, buy_quantity, sell_quantity, buy_value, sell_value,
                        average_price, realized_pnl, last_price, close_price, reconcile_version,
                        last_updated_source, version, updated_at
                    ) VALUES (
                        :account_id, 99999, 'MIS', 'NSE', 'OLDPOS',
                        1, 1, 0, 10, 0,
                        10, 0, 10, 10, 1,
                        'reconcile', 1, NOW()
                    )
                    """
                ),
                {"account_id": self.account_id},
            )
            db.commit()
        finally:
            db.close()

        fake_kite = SimpleNamespace(
            positions=lambda: {
                "net": [
                    {
                        "instrument_token": 12345,
                        "exchange": "NSE",
                        "tradingsymbol": "SBIN",
                        "product": "MIS",
                        "quantity": 2,
                        "buy_quantity": 2,
                        "sell_quantity": 0,
                        "buy_value": 200.0,
                        "sell_value": 0.0,
                        "average_price": 100.0,
                        "last_price": 120.0,
                        "close_price": 110.0,
                        "pnl": 40.0,
                    }
                ]
            },
            trades=lambda: [
                {
                    "trade_id": "TR-RECON-1",
                    "order_id": "OID-RECON-1",
                    "instrument_token": 12345,
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 2,
                    "average_price": 100.0,
                    "fill_timestamp": "2026-04-05 09:20:00",
                }
            ],
        )

        with patch("broker_api.order_runtime.SessionLocal", self.SessionLocal), patch(
            "broker_api.order_runtime.engine",
            self.engine,
        ), patch("broker_api.order_runtime.get_redis", return_value=self.redis_client), patch(
            "broker_api.order_runtime.publish_event",
            AsyncMock(),
        ):
            count = await self.positions_service.reconcile_account_positions(fake_kite, self.account_id, corr_id="reconcile")

        self.assertEqual(count, 1)

        db = self.SessionLocal()
        try:
            positions = db.execute(
                text(
                    "SELECT instrument_token, net_quantity FROM account_positions WHERE account_id = :account_id ORDER BY instrument_token"
                ),
                {"account_id": self.account_id},
            ).fetchall()
            fills = db.execute(
                text(
                    "SELECT applied_to_position FROM order_trade_fills WHERE account_id = :account_id AND trade_id = :trade_id"
                ),
                {"account_id": self.account_id, "trade_id": "TR-RECON-1"},
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(positions, [(12345, 2)])
        self.assertTrue(fills[0])
