import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.order_runtime import (
    CanonicalOrderEventRuntime,
    RealTimePositionsService,
    _acquire_advisory_lock_session,
    _close_locked_session,
)


class FakeResult:
    def __init__(self, rows=None, one=None, scalar=None):
        self._rows = rows or []
        self._one = one
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def scalar_one_or_none(self):
        return self._scalar


class FakeLockSession:
    def __init__(self, connection):
        self.connection = connection
        self.info = {}
        self.rollback = MagicMock()
        self.close = MagicMock()


class FakeApplyDB:
    def __init__(self, positions, pending_trades):
        self.positions = positions
        self.pending_trades = pending_trades
        self.applied_trade_ids = set()

    def execute(self, statement, params=None):
        params = params or {}
        sql = str(statement)

        if "SELECT trade_id, instrument_token" in sql:
            rows = [
                SimpleNamespace(**trade)
                for trade in self.pending_trades
                if trade["account_id"] == params["account_id"]
                and trade["order_id"] == params["order_id"]
                and trade["trade_id"] not in self.applied_trade_ids
            ]
            return FakeResult(rows=rows)

        if "SELECT COALESCE(MAX(reconcile_version), 0)" in sql:
            versions = [position["reconcile_version"] for position in self.positions.values()] or [0]
            return FakeResult(one=(max(versions),))

        if "SELECT net_quantity, buy_quantity, sell_quantity, buy_value, sell_value" in sql:
            key = (params["account_id"], params["instrument_token"], params["product"])
            position = self.positions.get(key)
            if not position:
                return FakeResult(one=None)
            return FakeResult(
                one=(
                    position["net_quantity"],
                    position["buy_quantity"],
                    position["sell_quantity"],
                    position["buy_value"],
                    position["sell_value"],
                    position["version"],
                    position["reconcile_version"],
                    position["average_price"],
                    position["realized_pnl"],
                )
            )

        if "UPDATE account_positions" in sql:
            key = (params["account_id"], params["instrument_token"], params["product"])
            self.positions[key].update(
                {
                    "net_quantity": params["net_quantity"],
                    "buy_quantity": params["buy_quantity"],
                    "sell_quantity": params["sell_quantity"],
                    "buy_value": params["buy_value"],
                    "sell_value": params["sell_value"],
                    "average_price": params["average_price"],
                    "realized_pnl": params["realized_pnl"],
                    "reconcile_version": params["reconcile_version"],
                    "version": params["version"],
                }
            )
            return FakeResult()

        if "INSERT INTO account_positions" in sql:
            key = (params["account_id"], params["instrument_token"], params["product"])
            self.positions[key] = {
                "net_quantity": params["net_quantity"],
                "buy_quantity": params["buy_quantity"],
                "sell_quantity": params["sell_quantity"],
                "buy_value": params["buy_value"],
                "sell_value": params["sell_value"],
                "average_price": params["average_price"],
                "realized_pnl": params["realized_pnl"],
                "reconcile_version": params["reconcile_version"],
                "version": 1,
            }
            return FakeResult()

        if "UPDATE order_trade_fills" in sql:
            self.applied_trade_ids.add(params["trade_id"])
            return FakeResult()

        raise AssertionError(f"Unhandled SQL in test fake: {sql}")


class FakeRedisPipeline:
    def __init__(self, redis_client):
        self.redis_client = redis_client
        self.ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def sadd(self, key, *values):
        self.ops.append(("sadd", key, values))
        return self

    def srem(self, key, *values):
        self.ops.append(("srem", key, values))
        return self

    def delete(self, key):
        self.ops.append(("delete", key, ()))
        return self

    def hset(self, key, mapping):
        self.ops.append(("hset", key, mapping))
        return self

    async def execute(self):
        for op, key, payload in self.ops:
            if op == "sadd":
                await self.redis_client.sadd(key, *payload)
            elif op == "srem":
                for value in payload:
                    await self.redis_client.srem(key, value)
            elif op == "delete":
                await self.redis_client.delete(key)
            elif op == "hset":
                await self.redis_client.hset(key, mapping=payload)
        self.ops.clear()


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping):
        bucket = self.hashes.setdefault(key, {})
        bucket.update(mapping)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(str(value) for value in values)

    async def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(str(value))

    async def delete(self, key):
        self.hashes.pop(key, None)
        self.sets.pop(key, None)

    def pipeline(self):
        return FakeRedisPipeline(self)


class OrderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_advisory_lock_retries_with_fresh_sessions(self):
        connections = [MagicMock(), MagicMock(), MagicMock()]
        sessions = [FakeLockSession(connection) for connection in connections]

        with patch("broker_api.order_runtime.engine.connect", side_effect=connections), patch(
            "broker_api.order_runtime.Session",
            side_effect=sessions,
        ), patch(
            "broker_api.order_runtime._try_advisory_lock",
            side_effect=[False, False, True],
        ):
            session = await _acquire_advisory_lock_session(99, timeout_seconds=1.0)

        self.assertIs(session, sessions[2])
        sessions[0].rollback.assert_called_once()
        sessions[1].rollback.assert_called_once()
        sessions[0].close.assert_called_once()
        sessions[1].close.assert_called_once()
        sessions[2].close.assert_not_called()
        connections[0].close.assert_called_once()
        connections[1].close.assert_called_once()
        connections[2].close.assert_not_called()

    def test_close_locked_session_invalidates_connection_when_requested(self):
        connection = MagicMock()
        session = FakeLockSession(connection)
        session.info["_advisory_lock_connection"] = connection

        _close_locked_session(session, invalidate_connection=True)

        session.close.assert_called_once()
        connection.invalidate.assert_called_once()
        connection.close.assert_called_once()

    def test_apply_pending_trade_fills_handles_reduce_flip_and_close(self):
        runtime = CanonicalOrderEventRuntime()
        account_id = "kite:AB1234"
        key = (account_id, 12345, "MIS")
        db = FakeApplyDB(
            positions={
                key: {
                    "net_quantity": 10,
                    "buy_quantity": 10,
                    "sell_quantity": 0,
                    "buy_value": 1000.0,
                    "sell_value": 0.0,
                    "average_price": 100.0,
                    "realized_pnl": 0.0,
                    "reconcile_version": 1,
                    "version": 1,
                }
            },
            pending_trades=[
                {
                    "account_id": account_id,
                    "order_id": "OID-1",
                    "trade_id": "T1",
                    "instrument_token": 12345,
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN",
                    "product": "MIS",
                    "transaction_type": "SELL",
                    "quantity": 4,
                    "price": 110.0,
                    "fill_timestamp": "2026-04-05T09:16:00+00:00",
                },
                {
                    "account_id": account_id,
                    "order_id": "OID-1",
                    "trade_id": "T2",
                    "instrument_token": 12345,
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN",
                    "product": "MIS",
                    "transaction_type": "SELL",
                    "quantity": 10,
                    "price": 90.0,
                    "fill_timestamp": "2026-04-05T09:17:00+00:00",
                },
                {
                    "account_id": account_id,
                    "order_id": "OID-1",
                    "trade_id": "T3",
                    "instrument_token": 12345,
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN",
                    "product": "MIS",
                    "transaction_type": "BUY",
                    "quantity": 4,
                    "price": 80.0,
                    "fill_timestamp": "2026-04-05T09:18:00+00:00",
                },
            ],
        )

        applied = runtime._apply_pending_trade_fills(db, account_id, "OID-1")

        self.assertEqual(applied, 3)
        self.assertEqual(db.applied_trade_ids, {"T1", "T2", "T3"})
        final_position = db.positions[key]
        self.assertEqual(final_position["net_quantity"], 0)
        self.assertEqual(final_position["average_price"], 0.0)
        self.assertEqual(final_position["buy_quantity"], 14)
        self.assertEqual(final_position["sell_quantity"], 14)
        self.assertEqual(final_position["buy_value"], 1320.0)
        self.assertEqual(final_position["sell_value"], 1340.0)
        self.assertAlmostEqual(final_position["realized_pnl"], 20.0)

    async def test_get_positions_computes_short_side_pnl_from_overlay(self):
        account_id = "kite:AB1234"
        position_key = "NSE:SBIN:MIS"
        service = RealTimePositionsService()
        redis_client = FakeRedis()
        redis_client.hashes[service._base_key(account_id)] = {
            position_key: json.dumps(
                {
                    "position_key": position_key,
                    "account_id": account_id,
                    "instrument_token": 12345,
                    "tradingsymbol": "SBIN",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": -5,
                    "buy_quantity": 0,
                    "sell_quantity": 5,
                    "buy_value": 0.0,
                    "sell_value": 500.0,
                    "average_price": 100.0,
                    "realized_pnl": 10.0,
                    "last_price": 95.0,
                    "close_price": 95.0,
                    "last_reconciled_at": None,
                }
            )
        }
        redis_client.hashes[service._ltp_key(account_id)] = {position_key: "90.0"}

        with patch("broker_api.order_runtime.get_redis", return_value=redis_client):
            positions = await service.get_positions(account_id, corr_id="test")

        position = positions[position_key]
        self.assertEqual(position.last_price, 90.0)
        self.assertEqual(position.unrealized_pnl, 50.0)
        self.assertEqual(position.realized_pnl, 10.0)
        self.assertEqual(position.pnl, 60.0)
        self.assertEqual(position.day_change, 25.0)

    async def test_process_ticks_updates_overlay_and_publishes_delta(self):
        account_id = "kite:AB1234"
        position_key = "NSE:SBIN:MIS"
        service = RealTimePositionsService()
        redis_client = FakeRedis()
        redis_client.hashes[service._base_key(account_id)] = {
            position_key: json.dumps(
                {
                    "position_key": position_key,
                    "account_id": account_id,
                    "instrument_token": 12345,
                    "tradingsymbol": "SBIN",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 2,
                    "buy_quantity": 2,
                    "sell_quantity": 0,
                    "buy_value": 200.0,
                    "sell_value": 0.0,
                    "average_price": 100.0,
                    "realized_pnl": 0.0,
                    "last_price": 100.0,
                    "close_price": 100.0,
                    "last_reconciled_at": None,
                }
            )
        }
        redis_client.hashes[service._ltp_key(account_id)] = {position_key: "100.0"}
        redis_client.sets[service._token_accounts_key(12345)] = {account_id}
        redis_client.sets[service._token_keys_key(account_id, 12345)] = {position_key}
        publish_event = AsyncMock()

        with patch("broker_api.order_runtime.get_redis", return_value=redis_client), patch(
            "broker_api.order_runtime.publish_event",
            publish_event,
        ):
            await service.process_ticks([{"instrument_token": 12345, "last_price": 120.0}], corr_id="tick")

        self.assertEqual(redis_client.hashes[service._ltp_key(account_id)][position_key], "120.0")
        publish_event.assert_awaited_once()
        channel, payload = publish_event.await_args.args
        self.assertEqual(channel, service._channel(account_id))
        self.assertEqual(payload["reason"], "tick")
        self.assertEqual(payload["positions"][position_key]["pnl"], 40.0)


class InstrumentsRepositoryTests(unittest.TestCase):
    def test_session_factory_is_closed_after_each_query(self):
        session = MagicMock()
        session.execute.return_value = FakeResult(scalar=15)
        repo = InstrumentsRepository(db=lambda: session)

        lot_size = repo.get_lot_size(12345)

        self.assertEqual(lot_size, 15)
        session.execute.assert_called_once()
        session.close.assert_called_once()
