import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kiteconnect import KiteConnect
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal, engine
from .redis_events import get_redis, publish_event, pubsub_iter
from .kite_session import KiteSession, get_session_account_id, make_account_id


logger = logging.getLogger(__name__)

RAW_EVENT_TABLES = {"order_events", "ws_order_events"}

TERMINAL_ORDER_STATUSES = {
    "COMPLETE",
    "CANCELLED",
    "REJECTED",
    "LAPSED",
}

EVENT_PROCESSOR_LOCK_ID = 87234101
POSITION_RECONCILE_LOCK_ID = 87234102


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return datetime.now(timezone.utc)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _position_key(exchange: str, tradingsymbol: str, product: str) -> str:
    return f"{exchange}:{tradingsymbol}:{product}"


class PositionPnL(BaseModel):
    model_config = ConfigDict(extra="allow")

    position_key: str
    account_id: str
    instrument_token: int
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int
    multiplier: int = 1
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0
    average_price: float = 0.0
    last_price: float = 0.0
    close_price: float = 0.0
    pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    day_change: float = 0.0
    day_change_percentage: float = 0.0
    last_reconciled_at: Optional[str] = None


class CanonicalOrderEventRuntime:
    def _event_fingerprint(self, source: str, payload: Dict[str, Any]) -> str:
        stable = {
            "source": source,
            "order_id": payload.get("order_id"),
            "status": payload.get("status"),
            "order_timestamp": payload.get("order_timestamp"),
            "exchange_update_timestamp": payload.get("exchange_update_timestamp"),
            "filled_quantity": _to_int(payload.get("filled_quantity")),
            "average_price": _to_float(payload.get("average_price")),
            "quantity": _to_int(payload.get("quantity")),
            "transaction_type": payload.get("transaction_type"),
            "product": payload.get("product"),
            "tradingsymbol": payload.get("tradingsymbol"),
        }
        encoded = json.dumps(stable, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def normalize_payload(self, source: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload.get("user_id") or payload.get("placed_by") or "unknown").strip() or "unknown"
        event_timestamp = _parse_timestamp(
            payload.get("exchange_update_timestamp")
            or payload.get("order_timestamp")
            or payload.get("exchange_timestamp")
        )
        exchange_update_timestamp = payload.get("exchange_update_timestamp")
        normalized = {
            "account_id": make_account_id(user_id) or "kite:unknown",
            "user_id": user_id,
            "order_id": str(payload.get("order_id") or "").strip(),
            "status": str(payload.get("status") or "UNKNOWN").strip() or "UNKNOWN",
            "event_timestamp": event_timestamp,
            "exchange_update_timestamp": _parse_timestamp(exchange_update_timestamp) if exchange_update_timestamp else None,
            "exchange": payload.get("exchange"),
            "tradingsymbol": payload.get("tradingsymbol"),
            "instrument_token": _to_int(payload.get("instrument_token")),
            "product": payload.get("product"),
            "transaction_type": payload.get("transaction_type"),
            "quantity": _to_int(payload.get("quantity")),
            "filled_quantity": _to_int(payload.get("filled_quantity")),
            "average_price": _to_float(payload.get("average_price")),
            "payload_json": payload,
        }
        normalized["event_fingerprint"] = self._event_fingerprint(source, payload)
        return normalized

    async def ingest_event(
        self,
        *,
        source: str,
        raw_table: str,
        payload: Dict[str, Any],
        corr_id: str,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        own_db = db is None
        session = db or SessionLocal()
        if raw_table not in RAW_EVENT_TABLES:
            raise ValueError(f"Unsupported raw event table: {raw_table}")
        normalized = self.normalize_payload(source, payload)
        if normalized["account_id"] == "kite:unknown":
            fallback_session = session.execute(
                text(
                    """
                    SELECT broker_user_id
                    FROM kite_sessions
                    WHERE broker_user_id IS NOT NULL
                    ORDER BY CASE WHEN session_id = 'system' THEN 0 ELSE 1 END, created_at DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
            fallback_user_id = str(fallback_session[0]).strip() if fallback_session and fallback_session[0] else ""
            if fallback_user_id:
                normalized["account_id"] = make_account_id(fallback_user_id) or normalized["account_id"]
                normalized["user_id"] = fallback_user_id
        raw_event_id: Optional[str] = None
        canonical_id: Optional[int] = None

        try:
            raw_insert_sql = text(
                f"""
                INSERT INTO {raw_table} (
                    id, order_id, user_id, status, event_timestamp, received_at,
                    exchange, tradingsymbol, instrument_token, transaction_type,
                    quantity, filled_quantity, average_price, payload_json, event_fingerprint
                ) VALUES (
                    :id, :order_id, :user_id, :status, :event_timestamp, NOW(),
                    :exchange, :tradingsymbol, :instrument_token, :transaction_type,
                    :quantity, :filled_quantity, :average_price, CAST(:payload_json AS JSONB), :event_fingerprint
                )
                ON CONFLICT (event_fingerprint) WHERE event_fingerprint IS NOT NULL DO NOTHING
                RETURNING id
                """
            )
            raw_result = session.execute(
                raw_insert_sql,
                {
                    "id": payload.get("_raw_event_id") or str(uuid.uuid4()),
                    "order_id": normalized["order_id"],
                    "user_id": normalized["user_id"],
                    "status": normalized["status"],
                    "event_timestamp": normalized["event_timestamp"],
                    "exchange": normalized["exchange"],
                    "tradingsymbol": normalized["tradingsymbol"],
                    "instrument_token": normalized["instrument_token"],
                    "transaction_type": normalized["transaction_type"],
                    "quantity": normalized["quantity"],
                    "filled_quantity": normalized["filled_quantity"],
                    "average_price": normalized["average_price"],
                    "payload_json": json.dumps(payload),
                    "event_fingerprint": normalized["event_fingerprint"],
                },
            )
            raw_row = raw_result.fetchone()
            if raw_row:
                raw_event_id = str(raw_row[0])
            else:
                raw_lookup = session.execute(
                    text(f"SELECT id FROM {raw_table} WHERE event_fingerprint = :event_fingerprint LIMIT 1"),
                    {"event_fingerprint": normalized["event_fingerprint"]},
                ).fetchone()
                raw_event_id = str(raw_lookup[0]) if raw_lookup else None

            canonical_insert = session.execute(
                text(
                    """
                    INSERT INTO canonical_order_events (
                        account_id, source, source_event_key, raw_event_table, raw_event_id,
                        order_id, status, event_timestamp, exchange_update_timestamp,
                        exchange, tradingsymbol, instrument_token, product,
                        transaction_type, quantity, filled_quantity, average_price, payload_json
                    ) VALUES (
                        :account_id, :source, :source_event_key, :raw_event_table, :raw_event_id,
                        :order_id, :status, :event_timestamp, :exchange_update_timestamp,
                        :exchange, :tradingsymbol, :instrument_token, :product,
                        :transaction_type, :quantity, :filled_quantity, :average_price, CAST(:payload_json AS JSONB)
                    )
                    ON CONFLICT (source, source_event_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "account_id": normalized["account_id"],
                    "source": source,
                    "source_event_key": normalized["event_fingerprint"],
                    "raw_event_table": raw_table,
                    "raw_event_id": raw_event_id,
                    "order_id": normalized["order_id"],
                    "status": normalized["status"],
                    "event_timestamp": normalized["event_timestamp"],
                    "exchange_update_timestamp": normalized["exchange_update_timestamp"],
                    "exchange": normalized["exchange"],
                    "tradingsymbol": normalized["tradingsymbol"],
                    "instrument_token": normalized["instrument_token"],
                    "product": normalized["product"],
                    "transaction_type": normalized["transaction_type"],
                    "quantity": normalized["quantity"],
                    "filled_quantity": normalized["filled_quantity"],
                    "average_price": normalized["average_price"],
                    "payload_json": json.dumps(payload),
                },
            )
            canonical_row = canonical_insert.fetchone()
            if canonical_row:
                canonical_id = int(canonical_row[0])
            else:
                existing = session.execute(
                    text(
                        "SELECT id FROM canonical_order_events WHERE source = :source AND source_event_key = :source_event_key LIMIT 1"
                    ),
                    {"source": source, "source_event_key": normalized["event_fingerprint"]},
                ).fetchone()
                canonical_id = int(existing[0]) if existing else None

            if own_db:
                session.commit()
            else:
                session.flush()
            return {
                "canonical_event_id": canonical_id,
                "raw_event_id": raw_event_id,
                "duplicate": canonical_row is None,
                "account_id": normalized["account_id"],
                "order_id": normalized["order_id"],
            }
        except Exception:
            if own_db:
                session.rollback()
            logger.error(
                "Failed to ingest order event",
                extra={"correlation_id": corr_id, "source": source, "order_id": normalized.get("order_id")},
                exc_info=True,
            )
            raise
        finally:
            if own_db:
                session.close()

    async def ingest_webhook_event(self, payload: BaseModel, corr_id: str, db: Session) -> Dict[str, Any]:
        return await self.ingest_event(
            source="webhook",
            raw_table="order_events",
            payload=payload.model_dump(),
            corr_id=corr_id,
            db=db,
        )

    async def ingest_ws_event(self, payload: Dict[str, Any], corr_id: str) -> Dict[str, Any]:
        return await self.ingest_event(
            source="ws",
            raw_table="ws_order_events",
            payload=payload,
            corr_id=corr_id,
            db=None,
        )

    def _claim_pending_events(self, db: Session, limit: int) -> Sequence[Any]:
        result = db.execute(
            text(
                """
                WITH claimed AS (
                    SELECT id
                    FROM canonical_order_events
                    WHERE processing_state = 'pending'
                    ORDER BY created_at ASC
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE canonical_order_events c
                SET processing_state = 'processing',
                    process_attempts = c.process_attempts + 1,
                    processing_started_at = NOW()
                FROM claimed
                WHERE c.id = claimed.id
                RETURNING c.id, c.account_id, c.order_id, c.status, c.event_timestamp,
                          c.exchange_update_timestamp, c.exchange, c.tradingsymbol,
                          c.instrument_token, c.product, c.transaction_type,
                          c.quantity, c.filled_quantity, c.average_price
                """
            ),
            {"limit": limit},
        )
        return result.fetchall()

    def _upsert_projection_from_event(self, db: Session, row: Any) -> None:
        existing = db.execute(
            text(
                """
                SELECT latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, needs_reconcile
                FROM order_state_projection
                WHERE account_id = :account_id AND order_id = :order_id
                FOR UPDATE
                """
            ),
            {"account_id": row.account_id, "order_id": row.order_id},
        ).fetchone()

        event_timestamp = _parse_timestamp(row.event_timestamp)
        filled_quantity = _to_int(row.filled_quantity)
        terminal = str(row.status or "").upper() in TERMINAL_ORDER_STATUSES

        if not existing:
            db.execute(
                text(
                    """
                    INSERT INTO order_state_projection (
                        account_id, order_id, latest_canonical_event_id, latest_status,
                        latest_event_timestamp, last_seen_filled_quantity,
                        dirty_for_trade_sync, needs_reconcile, terminal,
                        exchange, tradingsymbol, instrument_token, product, transaction_type, updated_at
                    ) VALUES (
                        :account_id, :order_id, :latest_canonical_event_id, :latest_status,
                        :latest_event_timestamp, :last_seen_filled_quantity,
                        :dirty_for_trade_sync, :needs_reconcile, :terminal,
                        :exchange, :tradingsymbol, :instrument_token, :product, :transaction_type, NOW()
                    )
                    """
                ),
                {
                    "account_id": row.account_id,
                    "order_id": row.order_id,
                    "latest_canonical_event_id": row.id,
                    "latest_status": row.status,
                    "latest_event_timestamp": event_timestamp,
                    "last_seen_filled_quantity": filled_quantity,
                    "dirty_for_trade_sync": filled_quantity > 0 or terminal,
                    "needs_reconcile": False,
                    "terminal": terminal,
                    "exchange": row.exchange,
                    "tradingsymbol": row.tradingsymbol,
                    "instrument_token": row.instrument_token,
                    "product": row.product,
                    "transaction_type": row.transaction_type,
                },
            )
            return

        prev_event_timestamp = _parse_timestamp(existing[0])
        prev_filled_quantity = _to_int(existing[1])
        dirty_for_trade_sync = bool(existing[2])
        needs_reconcile = bool(existing[3])

        filled_increased = filled_quantity > prev_filled_quantity
        if filled_quantity < prev_filled_quantity:
            needs_reconcile = True
        dirty_for_trade_sync = dirty_for_trade_sync or filled_increased or terminal

        latest_status = row.status if event_timestamp >= prev_event_timestamp else None
        db.execute(
            text(
                """
                UPDATE order_state_projection
                SET latest_canonical_event_id = CASE WHEN :is_latest THEN :latest_canonical_event_id ELSE latest_canonical_event_id END,
                    latest_status = CASE WHEN :is_latest THEN :latest_status ELSE latest_status END,
                    latest_event_timestamp = CASE WHEN :is_latest THEN :latest_event_timestamp ELSE latest_event_timestamp END,
                    last_seen_filled_quantity = GREATEST(last_seen_filled_quantity, :last_seen_filled_quantity),
                    dirty_for_trade_sync = :dirty_for_trade_sync,
                    needs_reconcile = :needs_reconcile,
                    terminal = terminal OR :terminal,
                    exchange = COALESCE(:exchange, exchange),
                    tradingsymbol = COALESCE(:tradingsymbol, tradingsymbol),
                    instrument_token = COALESCE(:instrument_token, instrument_token),
                    product = COALESCE(:product, product),
                    transaction_type = COALESCE(:transaction_type, transaction_type),
                    updated_at = NOW()
                WHERE account_id = :account_id AND order_id = :order_id
                """
            ),
            {
                "is_latest": event_timestamp >= prev_event_timestamp,
                "latest_canonical_event_id": row.id,
                "latest_status": latest_status,
                "latest_event_timestamp": event_timestamp,
                "last_seen_filled_quantity": filled_quantity,
                "dirty_for_trade_sync": dirty_for_trade_sync,
                "needs_reconcile": needs_reconcile,
                "terminal": terminal,
                "exchange": row.exchange,
                "tradingsymbol": row.tradingsymbol,
                "instrument_token": row.instrument_token,
                "product": row.product,
                "transaction_type": row.transaction_type,
                "account_id": row.account_id,
                "order_id": row.order_id,
            },
        )

    async def process_pending_events(self, batch_size: int = 100) -> int:
        db = SessionLocal()
        try:
            if not _try_advisory_lock(db, EVENT_PROCESSOR_LOCK_ID):
                db.rollback()
                return 0
            claimed = self._claim_pending_events(db, batch_size)
            db.commit()
            processed = 0
            for row in claimed:
                try:
                    self._upsert_projection_from_event(db, row)
                    db.execute(
                        text(
                            "UPDATE canonical_order_events SET processing_state = 'processed', processed_at = NOW(), last_error = NULL WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
                    db.commit()
                    processed += 1
                except Exception as exc:
                    db.rollback()
                    db.execute(
                        text(
                            "UPDATE canonical_order_events SET processing_state = 'failed', last_error = :last_error WHERE id = :id"
                        ),
                        {"id": row.id, "last_error": str(exc)[:1000]},
                    )
                    db.commit()
                    logger.error("Failed to process canonical order event %s", row.id, exc_info=True)
            return processed
        finally:
            try:
                _release_advisory_lock(db, EVENT_PROCESSOR_LOCK_ID)
            except Exception as exc:
                logger.error("Failed to release canonical event processor lock: %s", exc, exc_info=True)
            finally:
                db.close()

    async def sync_dirty_orders(
        self,
        kite: KiteConnect,
        positions_service: "RealTimePositionsService",
        batch_size: int = 25,
    ) -> int:
        db = await _acquire_advisory_lock_session(POSITION_RECONCILE_LOCK_ID, timeout_seconds=5.0)
        if db is None:
            return 0
        invalidate_connection = False
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_id, order_id
                    FROM order_state_projection
                    WHERE dirty_for_trade_sync = TRUE OR needs_reconcile = TRUE
                    ORDER BY updated_at ASC
                    LIMIT :limit
                    """
                ),
                {"limit": batch_size},
            ).fetchall()
            synced = 0
            for row in rows:
                order_id = row.order_id
                account_id = row.account_id
                try:
                    trades = await asyncio.to_thread(kite.order_trades, order_id)
                    inserted = self._store_trade_fills(db, account_id, order_id, trades or [])
                    applied = self._apply_pending_trade_fills(db, account_id, order_id)
                    db.execute(
                        text(
                            """
                            UPDATE order_state_projection
                            SET dirty_for_trade_sync = FALSE,
                                needs_reconcile = FALSE,
                                updated_at = NOW()
                            WHERE account_id = :account_id AND order_id = :order_id
                            """
                        ),
                        {"account_id": account_id, "order_id": order_id},
                    )
                    db.commit()
                    if inserted or applied:
                        await positions_service.sync_account_cache_from_db(account_id)
                        await positions_service.publish_snapshot(account_id, reason="trade_sync")
                    synced += 1
                except Exception as exc:
                    db.rollback()
                    logger.warning(
                        "Failed to sync dirty order trades",
                        extra={"account_id": account_id, "order_id": order_id, "error": str(exc)},
                        exc_info=True,
                    )
            return synced
        finally:
            try:
                _release_advisory_lock(db, POSITION_RECONCILE_LOCK_ID)
            except Exception as exc:
                invalidate_connection = True
                logger.error("Failed to release position runtime lock after dirty sync: %s", exc, exc_info=True)
            finally:
                _close_locked_session(db, invalidate_connection=invalidate_connection)

    def _store_trade_fills(
        self,
        db: Session,
        account_id: str,
        order_id: Optional[str],
        trades: Sequence[Dict[str, Any]],
        *,
        mark_applied: bool = False,
    ) -> int:
        inserted = 0
        for trade in trades:
            trade_id = str(trade.get("trade_id") or "").strip()
            if not trade_id:
                continue
            effective_order_id = str(trade.get("order_id") or order_id or "").strip()
            fill_timestamp = _parse_timestamp(
                trade.get("fill_timestamp") or trade.get("exchange_timestamp") or trade.get("order_timestamp")
            )
            result = db.execute(
                text(
                    """
                    INSERT INTO order_trade_fills (
                        account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol,
                        product, transaction_type, quantity, price, fill_timestamp, payload_json,
                        applied_to_position, applied_at
                    ) VALUES (
                        :account_id, :trade_id, :order_id, :instrument_token, :exchange, :tradingsymbol,
                        :product, :transaction_type, :quantity, :price, :fill_timestamp, CAST(:payload_json AS JSONB),
                        :applied_to_position, :applied_at
                    )
                    ON CONFLICT (account_id, trade_id) DO NOTHING
                    RETURNING trade_id
                    """
                ),
                {
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "order_id": effective_order_id,
                    "instrument_token": _to_int(trade.get("instrument_token")),
                    "exchange": trade.get("exchange"),
                    "tradingsymbol": trade.get("tradingsymbol"),
                    "product": trade.get("product") or "UNKNOWN",
                    "transaction_type": trade.get("transaction_type") or "UNKNOWN",
                    "quantity": _to_int(trade.get("quantity")),
                    "price": _to_float(trade.get("average_price") or trade.get("price")),
                    "fill_timestamp": fill_timestamp,
                    "payload_json": json.dumps(trade, default=str),
                    "applied_to_position": mark_applied,
                    "applied_at": datetime.now(timezone.utc) if mark_applied else None,
                },
            )
            if result.fetchone():
                inserted += 1
        return inserted

    def _apply_pending_trade_fills(self, db: Session, account_id: str, order_id: str) -> int:
        rows = db.execute(
            text(
                """
                SELECT trade_id, instrument_token, exchange, tradingsymbol, product,
                       transaction_type, quantity, price, fill_timestamp
                FROM order_trade_fills
                WHERE account_id = :account_id
                  AND order_id = :order_id
                  AND applied_to_position = FALSE
                ORDER BY fill_timestamp ASC, trade_id ASC
                FOR UPDATE
                """
            ),
            {"account_id": account_id, "order_id": order_id},
        ).fetchall()
        applied = 0
        current_reconcile_version_row = db.execute(
            text(
                "SELECT COALESCE(MAX(reconcile_version), 0) FROM account_positions WHERE account_id = :account_id"
            ),
            {"account_id": account_id},
        ).fetchone()
        current_reconcile_version = max(
            _to_int(current_reconcile_version_row[0] if current_reconcile_version_row else 0),
            int(datetime.now(timezone.utc).timestamp() * 1000),
        )

        for row in rows:
            quantity = _to_int(row.quantity)
            price = _to_float(row.price)
            is_buy = str(row.transaction_type or "").upper() == "BUY"

            existing = db.execute(
                text(
                    """
                    SELECT net_quantity, buy_quantity, sell_quantity, buy_value, sell_value,
                           version, reconcile_version, average_price, realized_pnl
                    FROM account_positions
                    WHERE account_id = :account_id AND instrument_token = :instrument_token AND product = :product
                    FOR UPDATE
                    """
                ),
                {
                    "account_id": account_id,
                    "instrument_token": row.instrument_token,
                    "product": row.product,
                },
            ).fetchone()

            if existing:
                current_quantity = _to_int(existing[0])
                buy_quantity = _to_int(existing[1]) + (quantity if is_buy else 0)
                sell_quantity = _to_int(existing[2]) + (0 if is_buy else quantity)
                buy_value = _to_float(existing[3]) + (price * quantity if is_buy else 0.0)
                sell_value = _to_float(existing[4]) + (0.0 if is_buy else price * quantity)
                version = _to_int(existing[5]) + 1
                reconcile_version = max(_to_int(existing[6]), current_reconcile_version)
                current_average_price = _to_float(existing[7])
                realized_pnl = _to_float(existing[8])
                signed_trade_quantity = quantity if is_buy else -quantity
                same_direction = current_quantity == 0 or (current_quantity > 0 and is_buy) or (current_quantity < 0 and not is_buy)
                net_quantity = current_quantity + signed_trade_quantity

                if same_direction:
                    existing_abs_quantity = abs(current_quantity)
                    trade_abs_quantity = abs(signed_trade_quantity)
                    total_abs_quantity = existing_abs_quantity + trade_abs_quantity
                    if total_abs_quantity > 0:
                        average_price = (
                            (existing_abs_quantity * current_average_price) + (trade_abs_quantity * price)
                        ) / total_abs_quantity
                    else:
                        average_price = 0.0
                else:
                    closed_quantity = min(abs(current_quantity), abs(signed_trade_quantity))
                    realized_pnl += (price - current_average_price) * closed_quantity * (1 if current_quantity > 0 else -1)
                    if net_quantity == 0:
                        average_price = 0.0
                    elif (current_quantity > 0 and net_quantity > 0) or (current_quantity < 0 and net_quantity < 0):
                        average_price = current_average_price
                    else:
                        average_price = price
                db.execute(
                    text(
                        """
                        UPDATE account_positions
                        SET net_quantity = :net_quantity,
                            buy_quantity = :buy_quantity,
                            sell_quantity = :sell_quantity,
                            buy_value = :buy_value,
                            sell_value = :sell_value,
                            average_price = :average_price,
                            realized_pnl = :realized_pnl,
                            last_trade_price = :last_trade_price,
                            last_trade_at = :last_trade_at,
                            reconcile_version = :reconcile_version,
                            last_updated_source = 'fill_apply',
                            version = :version,
                            updated_at = NOW()
                        WHERE account_id = :account_id AND instrument_token = :instrument_token AND product = :product
                        """
                    ),
                    {
                        "net_quantity": net_quantity,
                        "buy_quantity": buy_quantity,
                        "sell_quantity": sell_quantity,
                        "buy_value": buy_value,
                        "sell_value": sell_value,
                        "average_price": average_price,
                        "realized_pnl": realized_pnl,
                        "last_trade_price": price,
                        "last_trade_at": _parse_timestamp(row.fill_timestamp),
                        "reconcile_version": reconcile_version,
                        "version": version,
                        "account_id": account_id,
                        "instrument_token": row.instrument_token,
                        "product": row.product,
                    },
                )
            else:
                db.execute(
                    text(
                        """
                        INSERT INTO account_positions (
                            account_id, instrument_token, product, exchange, tradingsymbol,
                            net_quantity, buy_quantity, sell_quantity, buy_value, sell_value,
                            average_price, realized_pnl, last_trade_price, last_trade_at,
                            reconcile_version, last_updated_source, version, updated_at
                        ) VALUES (
                            :account_id, :instrument_token, :product, :exchange, :tradingsymbol,
                            :net_quantity, :buy_quantity, :sell_quantity, :buy_value, :sell_value,
                            :average_price, :realized_pnl, :last_trade_price, :last_trade_at,
                            :reconcile_version, 'fill_apply', 1, NOW()
                        )
                        """
                    ),
                    {
                        "account_id": account_id,
                        "instrument_token": row.instrument_token,
                        "product": row.product,
                        "exchange": row.exchange or "",
                        "tradingsymbol": row.tradingsymbol or str(row.instrument_token),
                        "net_quantity": quantity if is_buy else -quantity,
                        "buy_quantity": quantity if is_buy else 0,
                        "sell_quantity": 0 if is_buy else quantity,
                        "buy_value": price * quantity if is_buy else 0.0,
                        "sell_value": 0.0 if is_buy else price * quantity,
                        "average_price": price,
                        "realized_pnl": 0.0,
                        "last_trade_price": price,
                        "last_trade_at": _parse_timestamp(row.fill_timestamp),
                        "reconcile_version": current_reconcile_version,
                    },
                )

            db.execute(
                text(
                    """
                    UPDATE order_trade_fills
                    SET applied_to_position = TRUE,
                        applied_at = NOW()
                    WHERE account_id = :account_id AND trade_id = :trade_id
                    """
                ),
                {"account_id": account_id, "trade_id": row.trade_id},
            )
            applied += 1

        return applied


class RealTimePositionsService:
    def __init__(self):
        self.base_key_prefix = "positions:base:"
        self.ltp_key_prefix = "positions:ltp:"
        self.tracked_tokens_prefix = "positions:tracked_tokens:"
        self.token_keys_prefix = "positions:token_keys:"
        self.token_accounts_prefix = "positions:token_accounts:"

    def _base_key(self, account_id: str) -> str:
        return f"{self.base_key_prefix}{account_id}"

    def _ltp_key(self, account_id: str) -> str:
        return f"{self.ltp_key_prefix}{account_id}"

    def _tracked_tokens_key(self, account_id: str) -> str:
        return f"{self.tracked_tokens_prefix}{account_id}"

    def _token_keys_key(self, account_id: str, instrument_token: int) -> str:
        return f"{self.token_keys_prefix}{account_id}:{instrument_token}"

    def _token_accounts_key(self, instrument_token: int) -> str:
        return f"{self.token_accounts_prefix}{instrument_token}"

    def _channel(self, account_id: str) -> str:
        return f"positions.events:{account_id}"

    def _resolve_account_id(self, session_or_account_id: str) -> Optional[str]:
        if session_or_account_id.startswith("kite:"):
            return session_or_account_id
        db = SessionLocal()
        try:
            return get_session_account_id(db, session_or_account_id)
        finally:
            db.close()

    async def initialize_positions(self, kite: KiteConnect, session_id: str, corr_id: str) -> Dict[str, PositionPnL]:
        account_id = self._resolve_account_id(session_id)
        if not account_id:
            profile = await asyncio.to_thread(kite.profile)
            broker_user_id = str(profile.get("user_id") or "").strip()
            if not broker_user_id:
                raise RuntimeError("Unable to resolve broker account for positions initialization")
            account_id = make_account_id(broker_user_id)
            db = SessionLocal()
            try:
                session = db.query(KiteSession).filter_by(session_id=session_id).first()
                if session and broker_user_id:
                    session.broker_user_id = broker_user_id
                    db.commit()
            finally:
                db.close()
        await self.reconcile_account_positions(kite, account_id, corr_id)
        return await self.get_positions(account_id, corr_id)

    async def reconcile_account_positions(self, kite: KiteConnect, account_id: str, corr_id: str) -> int:
        if not account_id:
            return 0
        positions_data = await asyncio.to_thread(kite.positions)
        trades = await asyncio.to_thread(kite.trades)
        db = await _acquire_advisory_lock_session(POSITION_RECONCILE_LOCK_ID, timeout_seconds=5.0)
        if db is None:
            return 0
        invalidate_connection = False
        try:
            rows = positions_data.get("net", []) if isinstance(positions_data, dict) else []
            reconcile_version = int(datetime.now(timezone.utc).timestamp() * 1000)
            count = 0
            for pos in rows:
                quantity = _to_int(pos.get("quantity"))
                exchange = str(pos.get("exchange") or "")
                tradingsymbol = str(pos.get("tradingsymbol") or pos.get("instrument_token") or "")
                product = str(pos.get("product") or "")
                average_price = _to_float(pos.get("average_price"))
                last_price = _to_float(pos.get("last_price"))
                total_pnl = _to_float(pos.get("pnl"))
                unrealized_pnl = _to_float(
                    pos.get("unrealised", pos.get("unrealized", (last_price - average_price) * quantity))
                )
                realized_pnl = _to_float(pos.get("realised", pos.get("realized", total_pnl - unrealized_pnl)))
                db.execute(
                    text(
                        """
                        INSERT INTO account_positions (
                            account_id, instrument_token, product, exchange, tradingsymbol,
                            net_quantity, buy_quantity, sell_quantity, buy_value, sell_value,
                            average_price, realized_pnl, last_price, close_price, last_reconciled_at,
                            reconcile_version, last_updated_source, version, updated_at
                        ) VALUES (
                            :account_id, :instrument_token, :product, :exchange, :tradingsymbol,
                            :net_quantity, :buy_quantity, :sell_quantity, :buy_value, :sell_value,
                            :average_price, :realized_pnl, :last_price, :close_price, NOW(), :reconcile_version, 'reconcile', 1, NOW()
                        )
                        ON CONFLICT (account_id, instrument_token, product) DO UPDATE
                        SET exchange = EXCLUDED.exchange,
                            tradingsymbol = EXCLUDED.tradingsymbol,
                            net_quantity = EXCLUDED.net_quantity,
                            buy_quantity = EXCLUDED.buy_quantity,
                            sell_quantity = EXCLUDED.sell_quantity,
                            buy_value = EXCLUDED.buy_value,
                            sell_value = EXCLUDED.sell_value,
                            average_price = EXCLUDED.average_price,
                            realized_pnl = EXCLUDED.realized_pnl,
                            last_price = EXCLUDED.last_price,
                            close_price = EXCLUDED.close_price,
                            last_reconciled_at = EXCLUDED.last_reconciled_at,
                            reconcile_version = EXCLUDED.reconcile_version,
                            last_updated_source = EXCLUDED.last_updated_source,
                            version = account_positions.version + 1,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "account_id": account_id,
                        "instrument_token": _to_int(pos.get("instrument_token")),
                        "product": product,
                        "exchange": exchange,
                        "tradingsymbol": tradingsymbol,
                        "net_quantity": quantity,
                        "buy_quantity": _to_int(pos.get("buy_quantity")),
                        "sell_quantity": _to_int(pos.get("sell_quantity")),
                        "buy_value": _to_float(pos.get("buy_value")),
                        "sell_value": _to_float(pos.get("sell_value")),
                        "average_price": average_price,
                        "realized_pnl": realized_pnl,
                        "last_price": last_price,
                        "close_price": _to_float(pos.get("close_price")),
                        "reconcile_version": reconcile_version,
                    },
                )
                if quantity != 0:
                    count += 1

            db.execute(
                text(
                    """
                    DELETE FROM account_positions
                    WHERE account_id = :account_id
                      AND last_updated_source = 'reconcile'
                      AND reconcile_version < :reconcile_version
                    """
                ),
                {"account_id": account_id, "reconcile_version": reconcile_version},
            )

            order_event_runtime._store_trade_fills(db, account_id, None, trades or [], mark_applied=True)
            db.commit()
        finally:
            try:
                _release_advisory_lock(db, POSITION_RECONCILE_LOCK_ID)
            except Exception as exc:
                invalidate_connection = True
                logger.error("Failed to release position reconcile lock: %s", exc, exc_info=True)
            finally:
                _close_locked_session(db, invalidate_connection=invalidate_connection)

        await self.sync_account_cache_from_db(account_id)
        await self.publish_snapshot(account_id, reason="reconcile")
        logger.info(
            "Reconciled account positions from broker",
            extra={"correlation_id": corr_id, "account_id": account_id, "position_count": count},
        )
        return count

    async def sync_account_cache_from_db(self, account_id: str) -> None:
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_id, instrument_token, product, exchange, tradingsymbol,
                           net_quantity, buy_quantity, sell_quantity, buy_value, sell_value,
                           average_price, realized_pnl, last_price, close_price, last_reconciled_at
                    FROM account_positions
                    WHERE account_id = :account_id
                      AND net_quantity <> 0
                    ORDER BY exchange, tradingsymbol, product
                    """
                ),
                {"account_id": account_id},
            ).fetchall()
        finally:
            db.close()

        redis = get_redis()
        old_tokens = await redis.smembers(self._tracked_tokens_key(account_id))
        pipe = redis.pipeline()
        for token in old_tokens:
            pipe.srem(self._token_accounts_key(_to_int(token)), account_id)
            pipe.delete(self._token_keys_key(account_id, _to_int(token)))

        pipe.delete(self._base_key(account_id))
        pipe.delete(self._tracked_tokens_key(account_id))
        pipe.delete(self._ltp_key(account_id))

        base_mapping: Dict[str, str] = {}
        ltp_mapping: Dict[str, float] = {}
        for row in rows:
            exchange = str(row.exchange or "")
            tradingsymbol = str(row.tradingsymbol or row.instrument_token)
            product = str(row.product or "")
            position_key = _position_key(exchange, tradingsymbol, product)
            payload = {
                "position_key": position_key,
                "account_id": account_id,
                "instrument_token": _to_int(row.instrument_token),
                "tradingsymbol": tradingsymbol,
                "exchange": exchange,
                "product": product,
                "quantity": _to_int(row.net_quantity),
                "multiplier": 1,
                "buy_quantity": _to_int(row.buy_quantity),
                "sell_quantity": _to_int(row.sell_quantity),
                "buy_value": _to_float(row.buy_value),
                "sell_value": _to_float(row.sell_value),
                "average_price": _to_float(row.average_price),
                "realized_pnl": _to_float(row.realized_pnl),
                "last_price": _to_float(row.last_price),
                "close_price": _to_float(row.close_price),
                "last_reconciled_at": row.last_reconciled_at.isoformat() if row.last_reconciled_at else None,
            }
            base_mapping[position_key] = json.dumps(payload)
            ltp_mapping[position_key] = payload["last_price"]
            token = payload["instrument_token"]
            pipe.sadd(self._tracked_tokens_key(account_id), token)
            pipe.sadd(self._token_accounts_key(token), account_id)
            pipe.sadd(self._token_keys_key(account_id, token), position_key)

        if base_mapping:
            pipe.hset(self._base_key(account_id), mapping=base_mapping)
        if ltp_mapping:
            pipe.hset(self._ltp_key(account_id), mapping={k: str(v) for k, v in ltp_mapping.items()})

        await pipe.execute()

    async def get_positions(self, session_or_account_id: str, corr_id: str) -> Dict[str, PositionPnL]:
        account_id = self._resolve_account_id(session_or_account_id)
        if not account_id:
            return {}
        redis = get_redis()
        base = await redis.hgetall(self._base_key(account_id))
        if not base:
            await self.sync_account_cache_from_db(account_id)
            base = await redis.hgetall(self._base_key(account_id))
        if not base:
            return {}
        ltps = await redis.hgetall(self._ltp_key(account_id))
        positions: Dict[str, PositionPnL] = {}
        for key, raw in base.items():
            payload = json.loads(raw)
            last_price = _to_float(ltps.get(key), _to_float(payload.get("last_price")))
            quantity = _to_int(payload.get("quantity"))
            buy_value = _to_float(payload.get("buy_value"))
            sell_value = _to_float(payload.get("sell_value"))
            close_price = _to_float(payload.get("close_price"))
            average_price = _to_float(payload.get("average_price"))
            realized_pnl = _to_float(payload.get("realized_pnl"))
            unrealized_pnl = (last_price - average_price) * quantity
            pnl = realized_pnl + unrealized_pnl
            day_change = (last_price - close_price) * quantity if close_price else 0.0
            day_change_percentage = ((last_price - close_price) / close_price) * 100 if close_price else 0.0
            merged_payload = {
                **payload,
                "last_price": last_price,
                "close_price": close_price,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "pnl": pnl,
                "day_change": day_change,
                "day_change_percentage": day_change_percentage,
            }
            positions[key] = PositionPnL(**merged_payload)
        return positions

    async def publish_snapshot(self, account_id: str, reason: str = "snapshot") -> None:
        positions = await self.get_positions(account_id, corr_id=reason)
        await publish_event(
            self._channel(account_id),
            {
                "type": "snapshot",
                "reason": reason,
                "account_id": account_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "positions": {k: v.model_dump() for k, v in positions.items()},
            },
        )

    async def process_ticks(self, ticks: Sequence[Dict[str, Any]], corr_id: str) -> None:
        redis = get_redis()
        changed_by_account: Dict[str, Set[str]] = {}
        for tick in ticks:
            token = _to_int(tick.get("instrument_token"))
            last_price = _to_float(tick.get("last_price"))
            if not token or last_price <= 0:
                continue
            accounts = await redis.smembers(self._token_accounts_key(token))
            for account_id in accounts:
                position_keys = await redis.smembers(self._token_keys_key(account_id, token))
                if not position_keys:
                    continue
                await redis.hset(
                    self._ltp_key(account_id),
                    mapping={position_key: str(last_price) for position_key in position_keys},
                )
                changed_by_account.setdefault(account_id, set()).update(position_keys)

        for account_id, position_keys in changed_by_account.items():
            positions = await self.get_positions(account_id, corr_id)
            changed = {key: positions[key].model_dump() for key in position_keys if key in positions}
            if changed:
                await publish_event(
                    self._channel(account_id),
                    {
                        "type": "delta",
                        "reason": "tick",
                        "account_id": account_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "positions": changed,
                    },
                )

    async def subscribe_to_positions(
        self,
        session_or_account_id: str,
        corr_id: str,
    ) -> AsyncGenerator[str, None]:
        account_id = self._resolve_account_id(session_or_account_id)
        if not account_id:
            raise RuntimeError("Unable to resolve account for realtime position stream")

        initial_positions = await self.get_positions(account_id, corr_id)
        yield f"data: {json.dumps({'type': 'snapshot', 'account_id': account_id, 'positions': {k: v.model_dump() for k, v in initial_positions.items()}})}\n\n"

        async for message in pubsub_iter(self._channel(account_id)):
            if isinstance(message, dict) and message.get("event") == "heartbeat":
                yield ": heartbeat\n\n"
                continue
            yield f"data: {json.dumps(message)}\n\n"


def _try_advisory_lock(db: Session, lock_id: int) -> bool:
    row = db.execute(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}).fetchone()
    return bool(row[0]) if row else False


def _release_advisory_lock(db: Session, lock_id: int) -> None:
    db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})


async def _acquire_advisory_lock_session(lock_id: int, timeout_seconds: float = 5.0) -> Optional[Session]:
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while True:
        connection = engine.connect()
        db = Session(bind=connection)
        db.info["_advisory_lock_connection"] = connection
        acquired = False
        try:
            if _try_advisory_lock(db, lock_id):
                acquired = True
                return db
            db.rollback()
        finally:
            if not acquired:
                _close_locked_session(db)
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.1)


def _close_locked_session(db: Optional[Session], *, invalidate_connection: bool = False) -> None:
    if db is None:
        return
    connection = db.info.pop("_advisory_lock_connection", None)
    try:
        db.close()
    finally:
        if connection is not None:
            try:
                if invalidate_connection:
                    connection.invalidate()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass


async def refresh_processing_stuck_rows() -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE canonical_order_events
                SET processing_state = 'pending'
                WHERE processing_state = 'processing'
                  AND COALESCE(processing_started_at, created_at) < NOW() - INTERVAL '2 minutes'
                """
            )
        )
        db.commit()
    finally:
        db.close()


order_event_runtime = CanonicalOrderEventRuntime()
realtime_positions_service = RealTimePositionsService()
