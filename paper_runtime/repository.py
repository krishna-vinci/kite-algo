from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal
from .models import (
    PaperAccount,
    PaperFundLedgerEntry,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperTrade,
)


def _row_mapping(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if isinstance(row, dict):
        return dict(row)
    return {
        key: getattr(row, key)
        for key in dir(row)
        if not key.startswith("_") and not callable(getattr(row, key))
    }


def _decode_json_field(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class SqlAlchemyPaperRepository:
    def __init__(self, session_factory: sessionmaker | Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    @contextmanager
    def unit_of_work(self) -> Iterator["PaperRepositoryUnitOfWork"]:
        db = self.session_factory()
        try:
            yield PaperRepositoryUnitOfWork(repository=self, db=db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_account(self, account: PaperAccount) -> PaperAccount:
        db = self.session_factory()
        try:
            row = self._upsert_account_row(db, account)
            db.commit()
            return self._account_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_account(self, account_scope: str) -> Optional[PaperAccount]:
        db = self.session_factory()
        try:
            row = self._get_account_row(db, account_scope)
            return self._account_from_row(row) if row else None
        finally:
            db.close()

    def insert_order(self, order: PaperOrder) -> PaperOrder:
        db = self.session_factory()
        try:
            row = self._insert_order_row(db, order)
            db.commit()
            return self._order_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_order(self, order: PaperOrder) -> PaperOrder:
        db = self.session_factory()
        try:
            row = self._update_order_row(db, order)
            db.commit()
            if row is None:
                raise ValueError(f"paper order not found: {order.account_scope}/{order.order_id}")
            return self._order_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_order(self, account_scope: str, order_id: str) -> Optional[PaperOrder]:
        db = self.session_factory()
        try:
            row = self._get_order_row(db, account_scope, order_id)
            return self._order_from_row(row) if row else None
        finally:
            db.close()

    def list_orders(
        self,
        account_scope: str,
        *,
        instrument_token: Optional[int] = None,
        status: Optional[PaperOrderStatus | str] = None,
        transaction_type: Optional[str] = None,
        product: Optional[str] = None,
        limit: int = 200,
    ) -> List[PaperOrder]:
        db = self.session_factory()
        try:
            status_value = status.value if isinstance(status, PaperOrderStatus) else status
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                           quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                           placed_at, updated_at, completed_at, metadata_json
                    FROM public.paper_orders
                    WHERE account_scope = :account_scope
                      AND (:instrument_token IS NULL OR instrument_token = :instrument_token)
                      AND (:status IS NULL OR status = :status)
                      AND (:transaction_type IS NULL OR transaction_type = :transaction_type)
                      AND (:product IS NULL OR product = :product)
                    ORDER BY placed_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "account_scope": account_scope,
                    "instrument_token": instrument_token,
                    "status": status_value,
                    "transaction_type": transaction_type,
                    "product": product,
                    "limit": max(1, int(limit)),
                },
            ).fetchall()
            return [self._order_from_row(row) for row in rows]
        finally:
            db.close()

    def list_pending_orders_by_instrument(self, account_scope: str, instrument_token: int) -> List[PaperOrder]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                           quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                           placed_at, updated_at, completed_at, metadata_json
                    FROM public.paper_orders
                    WHERE account_scope = :account_scope
                      AND instrument_token = :instrument_token
                      AND status IN ('pending', 'open', 'partially_filled')
                    ORDER BY placed_at ASC
                    """
                ),
                {"account_scope": account_scope, "instrument_token": instrument_token},
            ).fetchall()
            return [self._order_from_row(row) for row in rows]
        finally:
            db.close()

    def list_pending_orders_for_instrument(self, instrument_token: int) -> List[PaperOrder]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                           quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                           placed_at, updated_at, completed_at, metadata_json
                    FROM public.paper_orders
                    WHERE instrument_token = :instrument_token
                      AND status IN ('pending', 'open', 'partially_filled')
                    ORDER BY placed_at ASC
                    """
                ),
                {"instrument_token": instrument_token},
            ).fetchall()
            return [self._order_from_row(row) for row in rows]
        finally:
            db.close()

    def list_active_market_tokens(self) -> List[int]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT DISTINCT instrument_token
                    FROM (
                        SELECT instrument_token
                        FROM public.paper_orders
                        WHERE status IN ('pending', 'open', 'partially_filled')
                        UNION
                        SELECT instrument_token
                        FROM public.paper_positions
                        WHERE net_quantity <> 0
                    ) active_tokens
                    ORDER BY instrument_token ASC
                    """
                )
            ).fetchall()
            return [int((row._mapping if hasattr(row, "_mapping") else row)["instrument_token"]) for row in rows]
        finally:
            db.close()

    def insert_trade(self, trade: PaperTrade) -> PaperTrade:
        db = self.session_factory()
        try:
            row = self._insert_trade_row(db, trade)
            db.commit()
            return self._trade_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_trades(
        self,
        account_scope: str,
        *,
        order_id: Optional[str] = None,
        instrument_token: Optional[int] = None,
        limit: int = 500,
    ) -> List[PaperTrade]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, trade_id, order_id, instrument_token, transaction_type, quantity, price, trade_timestamp, metadata_json
                    FROM public.paper_trades
                    WHERE account_scope = :account_scope
                      AND (:order_id IS NULL OR order_id = :order_id)
                      AND (:instrument_token IS NULL OR instrument_token = :instrument_token)
                    ORDER BY trade_timestamp DESC
                    LIMIT :limit
                    """
                ),
                {
                    "account_scope": account_scope,
                    "order_id": order_id,
                    "instrument_token": instrument_token,
                    "limit": max(1, int(limit)),
                },
            ).fetchall()
            return [self._trade_from_row(row) for row in rows]
        finally:
            db.close()

    def upsert_position(self, position: PaperPosition) -> PaperPosition:
        db = self.session_factory()
        try:
            row = self._upsert_position_row(db, position)
            db.commit()
            return self._position_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_position(self, account_scope: str, instrument_token: int, product: str) -> Optional[PaperPosition]:
        db = self.session_factory()
        try:
            row = self._get_position_row(db, account_scope, instrument_token, product)
            return self._position_from_row(row) if row else None
        finally:
            db.close()

    def list_positions(
        self,
        account_scope: str,
        *,
        instrument_token: Optional[int] = None,
        product: Optional[str] = None,
        only_open: bool = False,
    ) -> List[PaperPosition]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, instrument_token, product, exchange, tradingsymbol, net_quantity, average_price,
                           buy_quantity, sell_quantity, buy_value, sell_value, realized_pnl, unrealized_pnl, updated_at, metadata_json
                    FROM public.paper_positions
                    WHERE account_scope = :account_scope
                      AND (:instrument_token IS NULL OR instrument_token = :instrument_token)
                      AND (:product IS NULL OR product = :product)
                      AND (:only_open = FALSE OR net_quantity <> 0)
                    ORDER BY updated_at DESC
                    """
                ),
                {
                    "account_scope": account_scope,
                    "instrument_token": instrument_token,
                    "product": product,
                    "only_open": only_open,
                },
            ).fetchall()
            return [self._position_from_row(row) for row in rows]
        finally:
            db.close()

    def list_open_positions_for_instrument(self, instrument_token: int) -> List[PaperPosition]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT account_scope, instrument_token, product, exchange, tradingsymbol, net_quantity, average_price,
                           buy_quantity, sell_quantity, buy_value, sell_value, realized_pnl, unrealized_pnl, updated_at, metadata_json
                    FROM public.paper_positions
                    WHERE instrument_token = :instrument_token
                      AND net_quantity <> 0
                    ORDER BY updated_at DESC
                    """
                ),
                {"instrument_token": instrument_token},
            ).fetchall()
            return [self._position_from_row(row) for row in rows]
        finally:
            db.close()

    def append_fund_ledger_entry(self, entry: PaperFundLedgerEntry) -> PaperFundLedgerEntry:
        db = self.session_factory()
        try:
            row = self._append_fund_ledger_entry_row(db, entry)
            db.commit()
            return self._fund_ledger_from_row(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_fund_ledger(self, account_scope: str, *, limit: int = 200) -> List[PaperFundLedgerEntry]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT entry_id, account_scope, entry_type, amount, balance_after, reference_type, reference_id, notes, metadata_json, created_at
                    FROM public.paper_fund_ledger
                    WHERE account_scope = :account_scope
                    ORDER BY created_at DESC, entry_id DESC
                    LIMIT :limit
                    """
                ),
                {"account_scope": account_scope, "limit": max(1, int(limit))},
            ).fetchall()
            return [self._fund_ledger_from_row(row) for row in rows]
        finally:
            db.close()

    def clear_account_scope(self, account_scope: str) -> None:
        db = self.session_factory()
        try:
            for statement in (
                "DELETE FROM public.paper_position_lots WHERE account_scope = :account_scope",
                "DELETE FROM public.paper_trades WHERE account_scope = :account_scope",
                "DELETE FROM public.paper_orders WHERE account_scope = :account_scope",
                "DELETE FROM public.paper_positions WHERE account_scope = :account_scope",
                "DELETE FROM public.paper_fund_ledger WHERE account_scope = :account_scope",
            ):
                db.execute(text(statement), {"account_scope": account_scope})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _upsert_account_row(self, db: Session, account: PaperAccount) -> Any:
        return db.execute(
            text(
                """
                INSERT INTO public.paper_accounts (
                    account_scope,
                    currency,
                    starting_balance,
                    available_funds,
                    blocked_funds,
                    realized_pnl,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :account_scope,
                    :currency,
                    :starting_balance,
                    :available_funds,
                    :blocked_funds,
                    :realized_pnl,
                    CAST(:metadata_json AS JSONB),
                    :created_at,
                    :updated_at
                )
                ON CONFLICT (account_scope) DO UPDATE SET
                    currency = EXCLUDED.currency,
                    starting_balance = EXCLUDED.starting_balance,
                    available_funds = EXCLUDED.available_funds,
                    blocked_funds = EXCLUDED.blocked_funds,
                    realized_pnl = EXCLUDED.realized_pnl,
                    metadata_json = EXCLUDED.metadata_json,
                    updated_at = EXCLUDED.updated_at
                RETURNING account_scope, currency, starting_balance, available_funds, blocked_funds, realized_pnl, metadata_json, created_at, updated_at
                """
            ),
            {
                "account_scope": account.account_scope,
                "currency": account.currency,
                "starting_balance": account.starting_balance,
                "available_funds": account.available_funds,
                "blocked_funds": account.blocked_funds,
                "realized_pnl": account.realized_pnl,
                "metadata_json": _json_dumps(account.metadata),
                "created_at": account.created_at,
                "updated_at": account.updated_at,
            },
        ).fetchone()

    def _get_account_row(self, db: Session, account_scope: str) -> Any:
        return db.execute(
            text(
                """
                SELECT account_scope, currency, starting_balance, available_funds, blocked_funds, realized_pnl, metadata_json, created_at, updated_at
                FROM public.paper_accounts
                WHERE account_scope = :account_scope
                """
            ),
            {"account_scope": account_scope},
        ).fetchone()

    def _insert_order_row(self, db: Session, order: PaperOrder) -> Any:
        return db.execute(
            text(
                """
                INSERT INTO public.paper_orders (
                    account_scope,
                    order_id,
                    instrument_token,
                    exchange,
                    tradingsymbol,
                    product,
                    transaction_type,
                    order_type,
                    quantity,
                    filled_quantity,
                    pending_quantity,
                    price,
                    trigger_price,
                    average_price,
                    status,
                    placed_at,
                    updated_at,
                    completed_at,
                    metadata_json
                ) VALUES (
                    :account_scope,
                    :order_id,
                    :instrument_token,
                    :exchange,
                    :tradingsymbol,
                    :product,
                    :transaction_type,
                    :order_type,
                    :quantity,
                    :filled_quantity,
                    :pending_quantity,
                    :price,
                    :trigger_price,
                    :average_price,
                    :status,
                    :placed_at,
                    :updated_at,
                    :completed_at,
                    CAST(:metadata_json AS JSONB)
                )
                RETURNING account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                          quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                          placed_at, updated_at, completed_at, metadata_json
                """
            ),
            {
                "account_scope": order.account_scope,
                "order_id": order.order_id,
                "instrument_token": order.instrument_token,
                "exchange": order.exchange,
                "tradingsymbol": order.tradingsymbol,
                "product": order.product,
                "transaction_type": _enum_value(order.transaction_type),
                "order_type": _enum_value(order.order_type),
                "quantity": order.quantity,
                "filled_quantity": order.filled_quantity,
                "pending_quantity": order.pending_quantity,
                "price": order.price,
                "trigger_price": order.trigger_price,
                "average_price": order.average_price,
                "status": _enum_value(order.status),
                "placed_at": order.placed_at,
                "updated_at": order.updated_at,
                "completed_at": order.completed_at,
                "metadata_json": _json_dumps(order.metadata),
            },
        ).fetchone()

    def _update_order_row(self, db: Session, order: PaperOrder) -> Any:
        return db.execute(
            text(
                """
                UPDATE public.paper_orders
                SET exchange = :exchange,
                    tradingsymbol = :tradingsymbol,
                    product = :product,
                    transaction_type = :transaction_type,
                    order_type = :order_type,
                    quantity = :quantity,
                    filled_quantity = :filled_quantity,
                    pending_quantity = :pending_quantity,
                    price = :price,
                    trigger_price = :trigger_price,
                    average_price = :average_price,
                    status = :status,
                    updated_at = :updated_at,
                    completed_at = :completed_at,
                    metadata_json = CAST(:metadata_json AS JSONB)
                WHERE account_scope = :account_scope
                  AND order_id = :order_id
                RETURNING account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                          quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                          placed_at, updated_at, completed_at, metadata_json
                """
            ),
            {
                "account_scope": order.account_scope,
                "order_id": order.order_id,
                "exchange": order.exchange,
                "tradingsymbol": order.tradingsymbol,
                "product": order.product,
                "transaction_type": _enum_value(order.transaction_type),
                "order_type": _enum_value(order.order_type),
                "quantity": order.quantity,
                "filled_quantity": order.filled_quantity,
                "pending_quantity": order.pending_quantity,
                "price": order.price,
                "trigger_price": order.trigger_price,
                "average_price": order.average_price,
                "status": _enum_value(order.status),
                "updated_at": order.updated_at,
                "completed_at": order.completed_at,
                "metadata_json": _json_dumps(order.metadata),
            },
        ).fetchone()

    def _get_order_row(self, db: Session, account_scope: str, order_id: str) -> Any:
        return db.execute(
            text(
                """
                SELECT account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, order_type,
                       quantity, filled_quantity, pending_quantity, price, trigger_price, average_price, status,
                       placed_at, updated_at, completed_at, metadata_json
                FROM public.paper_orders
                WHERE account_scope = :account_scope AND order_id = :order_id
                """
            ),
            {"account_scope": account_scope, "order_id": order_id},
        ).fetchone()

    def _insert_trade_row(self, db: Session, trade: PaperTrade) -> Any:
        return db.execute(
            text(
                """
                INSERT INTO public.paper_trades (
                    account_scope,
                    trade_id,
                    order_id,
                    instrument_token,
                    transaction_type,
                    quantity,
                    price,
                    trade_timestamp,
                    metadata_json
                ) VALUES (
                    :account_scope,
                    :trade_id,
                    :order_id,
                    :instrument_token,
                    :transaction_type,
                    :quantity,
                    :price,
                    :trade_timestamp,
                    CAST(:metadata_json AS JSONB)
                )
                RETURNING account_scope, trade_id, order_id, instrument_token, transaction_type, quantity, price, trade_timestamp, metadata_json
                """
            ),
            {
                "account_scope": trade.account_scope,
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "instrument_token": trade.instrument_token,
                "transaction_type": _enum_value(trade.transaction_type),
                "quantity": trade.quantity,
                "price": trade.price,
                "trade_timestamp": trade.trade_timestamp,
                "metadata_json": _json_dumps(trade.metadata),
            },
        ).fetchone()

    def _upsert_position_row(self, db: Session, position: PaperPosition) -> Any:
        return db.execute(
            text(
                """
                INSERT INTO public.paper_positions (
                    account_scope,
                    instrument_token,
                    product,
                    exchange,
                    tradingsymbol,
                    net_quantity,
                    average_price,
                    buy_quantity,
                    sell_quantity,
                    buy_value,
                    sell_value,
                    realized_pnl,
                    unrealized_pnl,
                    updated_at,
                    metadata_json
                ) VALUES (
                    :account_scope,
                    :instrument_token,
                    :product,
                    :exchange,
                    :tradingsymbol,
                    :net_quantity,
                    :average_price,
                    :buy_quantity,
                    :sell_quantity,
                    :buy_value,
                    :sell_value,
                    :realized_pnl,
                    :unrealized_pnl,
                    :updated_at,
                    CAST(:metadata_json AS JSONB)
                )
                ON CONFLICT (account_scope, instrument_token, product) DO UPDATE SET
                    exchange = EXCLUDED.exchange,
                    tradingsymbol = EXCLUDED.tradingsymbol,
                    net_quantity = EXCLUDED.net_quantity,
                    average_price = EXCLUDED.average_price,
                    buy_quantity = EXCLUDED.buy_quantity,
                    sell_quantity = EXCLUDED.sell_quantity,
                    buy_value = EXCLUDED.buy_value,
                    sell_value = EXCLUDED.sell_value,
                    realized_pnl = EXCLUDED.realized_pnl,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    updated_at = EXCLUDED.updated_at,
                    metadata_json = EXCLUDED.metadata_json
                RETURNING account_scope, instrument_token, product, exchange, tradingsymbol, net_quantity, average_price,
                          buy_quantity, sell_quantity, buy_value, sell_value, realized_pnl, unrealized_pnl, updated_at, metadata_json
                """
            ),
            {
                "account_scope": position.account_scope,
                "instrument_token": position.instrument_token,
                "product": position.product,
                "exchange": position.exchange,
                "tradingsymbol": position.tradingsymbol,
                "net_quantity": position.net_quantity,
                "average_price": position.average_price,
                "buy_quantity": position.buy_quantity,
                "sell_quantity": position.sell_quantity,
                "buy_value": position.buy_value,
                "sell_value": position.sell_value,
                "realized_pnl": position.realized_pnl,
                "unrealized_pnl": position.unrealized_pnl,
                "updated_at": position.updated_at,
                "metadata_json": _json_dumps(position.metadata),
            },
        ).fetchone()

    def _get_position_row(self, db: Session, account_scope: str, instrument_token: int, product: str) -> Any:
        return db.execute(
            text(
                """
                SELECT account_scope, instrument_token, product, exchange, tradingsymbol, net_quantity, average_price,
                       buy_quantity, sell_quantity, buy_value, sell_value, realized_pnl, unrealized_pnl, updated_at, metadata_json
                FROM public.paper_positions
                WHERE account_scope = :account_scope
                  AND instrument_token = :instrument_token
                  AND product = :product
                """
            ),
            {
                "account_scope": account_scope,
                "instrument_token": instrument_token,
                "product": product,
            },
        ).fetchone()

    def _append_fund_ledger_entry_row(self, db: Session, entry: PaperFundLedgerEntry) -> Any:
        return db.execute(
            text(
                """
                INSERT INTO public.paper_fund_ledger (
                    account_scope,
                    entry_type,
                    amount,
                    balance_after,
                    reference_type,
                    reference_id,
                    notes,
                    metadata_json,
                    created_at
                ) VALUES (
                    :account_scope,
                    :entry_type,
                    :amount,
                    :balance_after,
                    :reference_type,
                    :reference_id,
                    :notes,
                    CAST(:metadata_json AS JSONB),
                    :created_at
                )
                RETURNING entry_id, account_scope, entry_type, amount, balance_after, reference_type, reference_id, notes, metadata_json, created_at
                """
            ),
            {
                "account_scope": entry.account_scope,
                "entry_type": _enum_value(entry.entry_type),
                "amount": entry.amount,
                "balance_after": entry.balance_after,
                "reference_type": entry.reference_type,
                "reference_id": entry.reference_id,
                "notes": entry.notes,
                "metadata_json": _json_dumps(entry.metadata),
                "created_at": entry.created_at,
            },
        ).fetchone()

    def _account_from_row(self, row: Any) -> PaperAccount:
        payload = _row_mapping(row)
        return PaperAccount(
            account_scope=str(payload["account_scope"]),
            currency=str(payload.get("currency") or "INR"),
            starting_balance=payload.get("starting_balance") or Decimal("0"),
            available_funds=payload.get("available_funds") or Decimal("0"),
            blocked_funds=payload.get("blocked_funds") or Decimal("0"),
            realized_pnl=payload.get("realized_pnl") or Decimal("0"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def _order_from_row(self, row: Any) -> PaperOrder:
        payload = _row_mapping(row)
        return PaperOrder(
            account_scope=str(payload["account_scope"]),
            order_id=str(payload["order_id"]),
            instrument_token=int(payload["instrument_token"]),
            exchange=str(payload.get("exchange") or "NSE"),
            tradingsymbol=payload.get("tradingsymbol"),
            product=str(payload.get("product") or "MIS"),
            transaction_type=str(payload["transaction_type"]),
            order_type=str(payload.get("order_type") or "market"),
            quantity=int(payload["quantity"]),
            filled_quantity=int(payload.get("filled_quantity") or 0),
            pending_quantity=payload.get("pending_quantity"),
            price=payload.get("price"),
            trigger_price=payload.get("trigger_price"),
            average_price=payload.get("average_price"),
            status=str(payload.get("status") or "pending"),
            placed_at=payload.get("placed_at"),
            updated_at=payload.get("updated_at"),
            completed_at=payload.get("completed_at"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _trade_from_row(self, row: Any) -> PaperTrade:
        payload = _row_mapping(row)
        return PaperTrade(
            account_scope=str(payload["account_scope"]),
            trade_id=str(payload["trade_id"]),
            order_id=str(payload["order_id"]),
            instrument_token=int(payload["instrument_token"]),
            transaction_type=str(payload["transaction_type"]),
            quantity=int(payload["quantity"]),
            price=payload["price"],
            trade_timestamp=payload.get("trade_timestamp"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _position_from_row(self, row: Any) -> PaperPosition:
        payload = _row_mapping(row)
        return PaperPosition(
            account_scope=str(payload["account_scope"]),
            instrument_token=int(payload["instrument_token"]),
            product=str(payload.get("product") or "MIS"),
            exchange=str(payload.get("exchange") or "NSE"),
            tradingsymbol=payload.get("tradingsymbol"),
            net_quantity=int(payload.get("net_quantity") or 0),
            average_price=payload.get("average_price") or Decimal("0"),
            buy_quantity=int(payload.get("buy_quantity") or 0),
            sell_quantity=int(payload.get("sell_quantity") or 0),
            buy_value=payload.get("buy_value") or Decimal("0"),
            sell_value=payload.get("sell_value") or Decimal("0"),
            realized_pnl=payload.get("realized_pnl") or Decimal("0"),
            unrealized_pnl=payload.get("unrealized_pnl") or Decimal("0"),
            updated_at=payload.get("updated_at"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _fund_ledger_from_row(self, row: Any) -> PaperFundLedgerEntry:
        payload = _row_mapping(row)
        return PaperFundLedgerEntry(
            entry_id=payload.get("entry_id"),
            account_scope=str(payload["account_scope"]),
            entry_type=str(payload["entry_type"]),
            amount=payload["amount"],
            balance_after=payload.get("balance_after"),
            reference_type=payload.get("reference_type"),
            reference_id=payload.get("reference_id"),
            notes=payload.get("notes"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at"),
        )


class PaperRepositoryUnitOfWork:
    def __init__(self, *, repository: SqlAlchemyPaperRepository, db: Session) -> None:
        self.repository = repository
        self.db = db

    def get_account(self, account_scope: str) -> Optional[PaperAccount]:
        row = self.repository._get_account_row(self.db, account_scope)
        return self.repository._account_from_row(row) if row else None

    def upsert_account(self, account: PaperAccount) -> PaperAccount:
        row = self.repository._upsert_account_row(self.db, account)
        return self.repository._account_from_row(row)

    def get_position(self, account_scope: str, instrument_token: int, product: str) -> Optional[PaperPosition]:
        row = self.repository._get_position_row(self.db, account_scope, instrument_token, product)
        return self.repository._position_from_row(row) if row else None

    def insert_order(self, order: PaperOrder) -> PaperOrder:
        row = self.repository._insert_order_row(self.db, order)
        return self.repository._order_from_row(row)

    def update_order(self, order: PaperOrder) -> PaperOrder:
        row = self.repository._update_order_row(self.db, order)
        if row is None:
            raise ValueError(f"paper order not found: {order.account_scope}/{order.order_id}")
        return self.repository._order_from_row(row)

    def insert_trade(self, trade: PaperTrade) -> PaperTrade:
        row = self.repository._insert_trade_row(self.db, trade)
        return self.repository._trade_from_row(row)

    def upsert_position(self, position: PaperPosition) -> PaperPosition:
        row = self.repository._upsert_position_row(self.db, position)
        return self.repository._position_from_row(row)

    def append_fund_ledger_entry(self, entry: PaperFundLedgerEntry) -> PaperFundLedgerEntry:
        row = self.repository._append_fund_ledger_entry_row(self.db, entry)
        return self.repository._fund_ledger_from_row(row)
