from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        return json.loads(value)
    return value


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


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


class WorkerExecutionLinksStore:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    def upsert_order_link(
        self,
        *,
        strategy_run_id: str,
        account_id: str,
        broker_order_id: str,
        client_order_ref: Optional[str] = None,
        basket_execution_id: Optional[str] = None,
        basket_leg_index: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> None:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            updated = session.execute(
                text(
                    """
                    UPDATE public.worker_live_execution_links
                    SET strategy_run_id = :strategy_run_id,
                        client_order_ref = COALESCE(:client_order_ref, client_order_ref),
                        basket_execution_id = COALESCE(:basket_execution_id, basket_execution_id),
                        basket_leg_index = COALESCE(:basket_leg_index, basket_leg_index),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = :account_id
                      AND broker_order_id = :broker_order_id
                      AND trade_id IS NULL
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "account_id": account_id,
                    "broker_order_id": broker_order_id,
                    "client_order_ref": client_order_ref,
                    "basket_execution_id": basket_execution_id,
                    "basket_leg_index": basket_leg_index,
                },
            )
            if int(getattr(updated, "rowcount", 0) or 0) <= 0:
                session.execute(
                    text(
                        """
                        INSERT INTO public.worker_live_execution_links (
                            strategy_run_id,
                            account_id,
                            broker_order_id,
                            trade_id,
                            client_order_ref,
                            basket_execution_id,
                            basket_leg_index,
                            created_at,
                            updated_at
                        ) VALUES (
                            :strategy_run_id,
                            :account_id,
                            :broker_order_id,
                            NULL,
                            :client_order_ref,
                            :basket_execution_id,
                            :basket_leg_index,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "strategy_run_id": strategy_run_id,
                        "account_id": account_id,
                        "broker_order_id": broker_order_id,
                        "client_order_ref": client_order_ref,
                        "basket_execution_id": basket_execution_id,
                        "basket_leg_index": basket_leg_index,
                    },
                )
            if owns_db:
                session.commit()
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def upsert_trade_links_for_order(
        self,
        *,
        account_id: str,
        broker_order_id: str,
        trades: Iterable[Dict[str, Any]],
        db: Optional[Session] = None,
    ) -> int:
        owns_db = db is None
        session = db or self.session_factory()
        inserted = 0
        try:
            order_link = session.execute(
                text(
                    """
                    SELECT strategy_run_id, client_order_ref, basket_execution_id, basket_leg_index
                    FROM public.worker_live_execution_links
                    WHERE account_id = :account_id
                      AND broker_order_id = :broker_order_id
                      AND trade_id IS NULL
                    LIMIT 1
                    """
                ),
                {"account_id": account_id, "broker_order_id": broker_order_id},
            ).fetchone()
            if not order_link:
                return 0

            link = _row_mapping(order_link)
            for trade in trades:
                trade_id = str((trade or {}).get("trade_id") or "").strip()
                if not trade_id:
                    continue
                updated = session.execute(
                    text(
                        """
                        UPDATE public.worker_live_execution_links
                        SET strategy_run_id = :strategy_run_id,
                            broker_order_id = :broker_order_id,
                            client_order_ref = COALESCE(:client_order_ref, client_order_ref),
                            basket_execution_id = COALESCE(:basket_execution_id, basket_execution_id),
                            basket_leg_index = COALESCE(:basket_leg_index, basket_leg_index),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = :account_id
                          AND trade_id = :trade_id
                        """
                    ),
                    {
                        "strategy_run_id": link.get("strategy_run_id"),
                        "account_id": account_id,
                        "broker_order_id": broker_order_id,
                        "trade_id": trade_id,
                        "client_order_ref": link.get("client_order_ref"),
                        "basket_execution_id": link.get("basket_execution_id"),
                        "basket_leg_index": link.get("basket_leg_index"),
                    },
                )
                if int(getattr(updated, "rowcount", 0) or 0) <= 0:
                    session.execute(
                        text(
                            """
                            INSERT INTO public.worker_live_execution_links (
                                strategy_run_id,
                                account_id,
                                broker_order_id,
                                trade_id,
                                client_order_ref,
                                basket_execution_id,
                                basket_leg_index,
                                created_at,
                                updated_at
                            ) VALUES (
                                :strategy_run_id,
                                :account_id,
                                :broker_order_id,
                                :trade_id,
                                :client_order_ref,
                                :basket_execution_id,
                                :basket_leg_index,
                                CURRENT_TIMESTAMP,
                                CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "strategy_run_id": link.get("strategy_run_id"),
                            "account_id": account_id,
                            "broker_order_id": broker_order_id,
                            "trade_id": trade_id,
                            "client_order_ref": link.get("client_order_ref"),
                            "basket_execution_id": link.get("basket_execution_id"),
                            "basket_leg_index": link.get("basket_leg_index"),
                        },
                    )
                    inserted += 1
            if owns_db:
                session.commit()
            return inserted
        except Exception:
            if owns_db:
                session.rollback()
            raise
        finally:
            if owns_db:
                session.close()

    def list_links_for_run(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.worker_live_execution_links
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    ORDER BY created_at ASC, link_id ASC
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchall()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def list_trade_links_for_run(self, strategy_run_id: str, account_id: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.worker_live_execution_links
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                      AND trade_id IS NOT NULL
                    ORDER BY created_at ASC, link_id ASC
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchall()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def has_execution_links_for_run(self, *, strategy_run_id: str, account_id: str, db: Optional[Session] = None) -> bool:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    SELECT 1
                    FROM public.worker_live_execution_links
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            return bool(row)
        finally:
            if owns_db:
                session.close()

    def list_attribution_refs_for_run(self, *, strategy_run_id: str, account_id: str, db: Optional[Session] = None) -> Dict[str, List[str]]:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                text(
                    """
                    SELECT broker_order_id, client_order_ref
                    FROM public.worker_live_execution_links
                    WHERE strategy_run_id = :strategy_run_id
                      AND account_id = :account_id
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchall()
            broker_order_ids = sorted(
                {
                    str(_row_mapping(row).get("broker_order_id") or "").strip()
                    for row in rows
                    if str(_row_mapping(row).get("broker_order_id") or "").strip()
                }
            )
            client_order_refs = sorted(
                {
                    str(_row_mapping(row).get("client_order_ref") or "").strip()
                    for row in rows
                    if str(_row_mapping(row).get("client_order_ref") or "").strip()
                }
            )
            return {"broker_order_ids": broker_order_ids, "client_order_refs": client_order_refs}
        finally:
            if owns_db:
                session.close()

    def has_unresolved_execution_for_run(self, *, strategy_run_id: str, account_id: str, db: Optional[Session] = None) -> bool:
        owns_db = db is None
        session = db or self.session_factory()
        try:
            net_row = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(
                        CASE
                            WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'BUY' THEN otf.quantity
                            WHEN UPPER(COALESCE(otf.transaction_type, '')) = 'SELL' THEN -otf.quantity
                            ELSE 0
                        END
                    ), 0) AS net_quantity
                    FROM public.worker_live_execution_links wl
                    INNER JOIN public.order_trade_fills otf
                      ON otf.account_id = wl.account_id
                     AND otf.trade_id = wl.trade_id
                    WHERE wl.strategy_run_id = :strategy_run_id
                      AND wl.account_id = :account_id
                      AND wl.trade_id IS NOT NULL
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            net_quantity = _to_int(_row_mapping(net_row).get("net_quantity"))
            if net_quantity != 0:
                return True

            non_terminal = session.execute(
                text(
                    """
                    SELECT 1
                    FROM public.worker_live_execution_links wl
                    LEFT JOIN public.order_state_projection osp
                      ON osp.account_id = wl.account_id
                     AND osp.order_id = wl.broker_order_id
                    WHERE wl.strategy_run_id = :strategy_run_id
                      AND wl.account_id = :account_id
                      AND wl.trade_id IS NULL
                      AND (
                        osp.order_id IS NULL
                        OR UPPER(COALESCE(osp.latest_status, '')) NOT IN ('COMPLETE', 'CANCELLED', 'REJECTED', 'LAPSED')
                      )
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id, "account_id": account_id},
            ).fetchone()
            return bool(non_terminal)
        finally:
            if owns_db:
                session.close()


worker_execution_links_store = WorkerExecutionLinksStore()
