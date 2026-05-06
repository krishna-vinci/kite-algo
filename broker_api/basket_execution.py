from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal


TERMINAL_LEG_STATES = {"submit_failed", "filled", "cancelled", "rejected", "partial_terminal"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


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


def normalize_leg_state(*, broker_status: str, filled_quantity: int, requested_quantity: int) -> str:
    status = str(broker_status or "").upper()
    filled = max(0, _to_int(filled_quantity))
    requested = max(0, _to_int(requested_quantity))
    terminal = status in {"COMPLETE", "CANCELLED", "REJECTED", "LAPSED"}

    if terminal and requested > 0 and filled >= requested:
        return "filled"
    if terminal and filled == 0 and status == "REJECTED":
        return "rejected"
    if terminal and filled == 0 and status in {"CANCELLED", "LAPSED"}:
        return "cancelled"
    if terminal and 0 < filled < requested:
        return "partial_terminal"
    return "working"


def recompute_basket_status(legs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    leg_rows = [dict(leg) for leg in legs]
    requested_leg_count = len(leg_rows)
    completed_leg_count = 0
    terminal_leg_count = 0
    total_requested_quantity = 0
    total_filled_quantity = 0
    has_working = False

    for leg in leg_rows:
        status = str(leg.get("status") or "")
        requested = max(0, _to_int(leg.get("requested_quantity")))
        filled = max(0, _to_int(leg.get("last_seen_filled_quantity")))
        total_requested_quantity += requested
        total_filled_quantity += filled
        if status == "working":
            has_working = True
        if status in TERMINAL_LEG_STATES:
            terminal_leg_count += 1
        if requested > 0 and filled >= requested:
            completed_leg_count += 1

    if has_working:
        status = "active"
    elif requested_leg_count > 0 and completed_leg_count == requested_leg_count:
        status = "completed"
    elif requested_leg_count > 0 and terminal_leg_count == requested_leg_count and total_filled_quantity == 0:
        status = "failed"
    else:
        status = "partial"

    return {
        "status": status,
        "requested_leg_count": requested_leg_count,
        "completed_leg_count": completed_leg_count,
        "terminal_leg_count": terminal_leg_count,
        "total_requested_quantity": total_requested_quantity,
        "total_filled_quantity": total_filled_quantity,
    }


class BasketExecutionStore:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    def create_live_basket_execution(
        self,
        db: Session,
        *,
        strategy_run_id: str,
        account_id: str,
        all_or_none: bool,
        orders: Sequence[Dict[str, Any]],
        basket_execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        basket_id = str(basket_execution_id or f"bex_{uuid.uuid4().hex}")
        normalized_orders = [dict(item) for item in orders]
        aggregate = recompute_basket_status(
            [
                {
                    "status": "pending_submit",
                    "requested_quantity": _to_int(order.get("quantity")),
                    "last_seen_filled_quantity": 0,
                }
                for order in normalized_orders
            ]
        )
        db.execute(
            text(
                """
                INSERT INTO public.basket_executions (
                    basket_execution_id,
                    strategy_run_id,
                    account_id,
                    execution_mode,
                    status,
                    all_or_none,
                    requested_leg_count,
                    completed_leg_count,
                    terminal_leg_count,
                    total_requested_quantity,
                    total_filled_quantity,
                    request_json,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :basket_execution_id,
                    :strategy_run_id,
                    :account_id,
                    'live',
                    'submitting',
                    :all_or_none,
                    :requested_leg_count,
                    :completed_leg_count,
                    :terminal_leg_count,
                    :total_requested_quantity,
                    :total_filled_quantity,
                    CAST(:request_json AS JSONB),
                    '{}'::jsonb,
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "basket_execution_id": basket_id,
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "all_or_none": bool(all_or_none),
                "requested_leg_count": aggregate["requested_leg_count"],
                "completed_leg_count": aggregate["completed_leg_count"],
                "terminal_leg_count": aggregate["terminal_leg_count"],
                "total_requested_quantity": aggregate["total_requested_quantity"],
                "total_filled_quantity": aggregate["total_filled_quantity"],
                "request_json": _json_dumps({"orders": normalized_orders, "all_or_none": bool(all_or_none)}),
            },
        )

        for index, order in enumerate(normalized_orders):
            db.execute(
                text(
                    """
                    INSERT INTO public.basket_execution_legs (
                        basket_execution_id,
                        leg_index,
                        status,
                        exchange,
                        tradingsymbol,
                        product,
                        transaction_type,
                        requested_quantity,
                        request_json,
                        created_at,
                        updated_at
                    ) VALUES (
                        :basket_execution_id,
                        :leg_index,
                        'pending_submit',
                        :exchange,
                        :tradingsymbol,
                        :product,
                        :transaction_type,
                        :requested_quantity,
                        CAST(:request_json AS JSONB),
                        NOW(),
                        NOW()
                    )
                    """
                ),
                {
                    "basket_execution_id": basket_id,
                    "leg_index": index,
                    "exchange": order.get("exchange"),
                    "tradingsymbol": order.get("tradingsymbol"),
                    "product": order.get("product"),
                    "transaction_type": order.get("transaction_type"),
                    "requested_quantity": _to_int(order.get("quantity")),
                    "request_json": _json_dumps(order),
                },
            )

        return self.get_basket_for_run(db, strategy_run_id=strategy_run_id, basket_execution_id=basket_id) or {
            "basket_execution_id": basket_id,
            "strategy_run_id": strategy_run_id,
            "status": "submitting",
        }

    def mark_leg_submit_failed(
        self,
        db: Session,
        *,
        basket_execution_id: str,
        leg_index: int,
        error_message: Optional[str] = None,
    ) -> None:
        db.execute(
            text(
                """
                UPDATE public.basket_execution_legs
                SET status = 'submit_failed',
                    latest_broker_status = 'SUBMIT_FAILED',
                    updated_at = NOW(),
                    request_json = CASE
                        WHEN :error_message IS NULL THEN request_json
                        ELSE jsonb_set(request_json, '{submit_error}', to_jsonb(:error_message::TEXT), true)
                    END
                WHERE basket_execution_id = :basket_execution_id
                  AND leg_index = :leg_index
                """
            ),
            {
                "basket_execution_id": basket_execution_id,
                "leg_index": int(leg_index),
                "error_message": error_message,
            },
        )

    def mark_leg_working(
        self,
        db: Session,
        *,
        basket_execution_id: str,
        leg_index: int,
        broker_order_id: str,
        client_order_ref: Optional[str],
    ) -> None:
        db.execute(
            text(
                """
                UPDATE public.basket_execution_legs
                SET status = 'working',
                    broker_order_id = :broker_order_id,
                    client_order_ref = COALESCE(:client_order_ref, client_order_ref),
                    latest_broker_status = 'OPEN',
                    updated_at = NOW()
                WHERE basket_execution_id = :basket_execution_id
                  AND leg_index = :leg_index
                """
            ),
            {
                "basket_execution_id": basket_execution_id,
                "leg_index": int(leg_index),
                "broker_order_id": str(broker_order_id),
                "client_order_ref": client_order_ref,
            },
        )

    def finalize_submission(
        self,
        db: Session,
        *,
        basket_execution_id: str,
        rollback_status: str = "none",
        action_required: bool = False,
        action_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = self._recompute_and_update(db, basket_execution_id=basket_execution_id)
        status = str(summary.get("status") or "failed")
        if status == "partial":
            status = "active"
        db.execute(
            text(
                """
                UPDATE public.basket_executions
                SET status = :status,
                    rollback_status = :rollback_status,
                    action_required = :action_required,
                    action_reason = :action_reason,
                    updated_at = NOW()
                WHERE basket_execution_id = :basket_execution_id
                """
            ),
            {
                "basket_execution_id": basket_execution_id,
                "status": status,
                "rollback_status": str(rollback_status or "none"),
                "action_required": bool(action_required),
                "action_reason": action_reason,
            },
        )
        row = db.execute(
            text("SELECT strategy_run_id FROM public.basket_executions WHERE basket_execution_id = :basket_execution_id"),
            {"basket_execution_id": basket_execution_id},
        ).fetchone()
        if row and str(rollback_status or "none") != "none":
            strategy_run_id = str(_row_mapping(row).get("strategy_run_id") or "")
            payload = {
                "basket_execution_id": basket_execution_id,
                "status": status,
                "rollback_status": str(rollback_status or "none"),
                "action_required": bool(action_required),
                "action_reason": action_reason,
            }
            self.append_worker_execution_event(
                db,
                strategy_run_id=strategy_run_id,
                account_id=self._basket_account_id(db, basket_execution_id=basket_execution_id),
                basket_execution_id=basket_execution_id,
                event_type="basket.rollback_recorded",
                payload=payload,
            )
        return self.get_basket_for_run(
            db,
            strategy_run_id=str(_row_mapping(row).get("strategy_run_id") if row else ""),
            basket_execution_id=basket_execution_id,
        ) or {"basket_execution_id": basket_execution_id, "status": status}

    def apply_order_event(self, db: Session, *, canonical_event: Any) -> List[Dict[str, Any]]:
        event = _row_mapping(canonical_event)
        account_id = str(event.get("account_id") or "")
        order_id = str(event.get("order_id") or "")
        if not account_id or not order_id:
            return []

        link = db.execute(
            text(
                """
                SELECT strategy_run_id, basket_execution_id, basket_leg_index
                FROM public.live_order_intents
                WHERE account_id = :account_id
                  AND broker_order_id = :order_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"account_id": account_id, "order_id": order_id},
        ).fetchone()
        if not link:
            return []

        mapped = _row_mapping(link)
        strategy_run_id = str(mapped.get("strategy_run_id") or "")
        basket_execution_id = mapped.get("basket_execution_id")
        basket_leg_index = mapped.get("basket_leg_index")

        payload = {
            "order_id": order_id,
            "status": event.get("status"),
            "filled_quantity": _to_int(event.get("filled_quantity")),
            "quantity": _to_int(event.get("quantity")),
            "average_price": event.get("average_price"),
            "event_timestamp": str(event.get("event_timestamp") or ""),
        }

        emitted: List[Dict[str, Any]] = []
        if basket_execution_id is None or basket_leg_index is None:
            cursor = self.append_worker_execution_event(
                db,
                strategy_run_id=strategy_run_id,
                account_id=account_id,
                basket_execution_id=None,
                event_type="order.updated",
                payload=payload,
            )
            emitted.append(
                {
                    "cursor": cursor,
                    "strategy_run_id": strategy_run_id,
                    "account_id": account_id,
                    "basket_execution_id": None,
                    "event_type": "order.updated",
                    "payload": payload,
                }
            )
            return emitted

        leg_row = db.execute(
            text(
                """
                SELECT requested_quantity
                FROM public.basket_execution_legs
                WHERE basket_execution_id = :basket_execution_id
                  AND leg_index = :leg_index
                """
            ),
            {"basket_execution_id": basket_execution_id, "leg_index": int(basket_leg_index)},
        ).fetchone()
        if leg_row:
            requested_quantity = _to_int(_row_mapping(leg_row).get("requested_quantity"))
            leg_status = normalize_leg_state(
                broker_status=str(event.get("status") or ""),
                filled_quantity=_to_int(event.get("filled_quantity")),
                requested_quantity=requested_quantity,
            )
            db.execute(
                text(
                    """
                    UPDATE public.basket_execution_legs
                    SET status = :status,
                        latest_broker_status = :latest_broker_status,
                        last_seen_filled_quantity = :last_seen_filled_quantity,
                        average_price = :average_price,
                        updated_at = NOW()
                    WHERE basket_execution_id = :basket_execution_id
                      AND leg_index = :leg_index
                    """
                ),
                {
                    "basket_execution_id": basket_execution_id,
                    "leg_index": int(basket_leg_index),
                    "status": leg_status,
                    "latest_broker_status": str(event.get("status") or ""),
                    "last_seen_filled_quantity": _to_int(event.get("filled_quantity")),
                    "average_price": event.get("average_price"),
                },
            )
            status_before_row = db.execute(
                text(
                    "SELECT status FROM public.basket_executions WHERE basket_execution_id = :basket_execution_id"
                ),
                {"basket_execution_id": basket_execution_id},
            ).fetchone()
            status_before = str(_row_mapping(status_before_row).get("status") or "")
            summary = self._recompute_and_update(db, basket_execution_id=basket_execution_id)
            status_after = str(summary.get("status") or status_before)

            leg_event = {
                **payload,
                "basket_leg_index": int(basket_leg_index),
                "leg_status": leg_status,
            }
            leg_cursor = self.append_worker_execution_event(
                db,
                strategy_run_id=strategy_run_id,
                account_id=account_id,
                basket_execution_id=str(basket_execution_id),
                event_type="basket.leg_updated",
                payload=leg_event,
            )
            emitted.append(
                {
                    "cursor": leg_cursor,
                    "strategy_run_id": strategy_run_id,
                    "account_id": account_id,
                    "basket_execution_id": str(basket_execution_id),
                    "event_type": "basket.leg_updated",
                    "payload": leg_event,
                }
            )

            if status_after != status_before:
                status_event = {
                    "basket_execution_id": str(basket_execution_id),
                    "status": status_after,
                    "requested_leg_count": summary.get("requested_leg_count"),
                    "completed_leg_count": summary.get("completed_leg_count"),
                    "terminal_leg_count": summary.get("terminal_leg_count"),
                    "total_requested_quantity": summary.get("total_requested_quantity"),
                    "total_filled_quantity": summary.get("total_filled_quantity"),
                }
                status_cursor = self.append_worker_execution_event(
                    db,
                    strategy_run_id=strategy_run_id,
                    account_id=account_id,
                    basket_execution_id=str(basket_execution_id),
                    event_type="basket.status_changed",
                    payload=status_event,
                )
                emitted.append(
                    {
                        "cursor": status_cursor,
                        "strategy_run_id": strategy_run_id,
                        "account_id": account_id,
                        "basket_execution_id": str(basket_execution_id),
                        "event_type": "basket.status_changed",
                        "payload": status_event,
                    }
                )

            if emitted:
                latest_cursor = int(emitted[-1].get("cursor") or 0)
                db.execute(
                    text(
                        """
                        UPDATE public.basket_executions
                        SET latest_event_cursor = :latest_event_cursor,
                            latest_event_at = NOW(),
                            updated_at = NOW()
                        WHERE basket_execution_id = :basket_execution_id
                        """
                    ),
                    {
                        "basket_execution_id": basket_execution_id,
                        "latest_event_cursor": latest_cursor,
                    },
                )

        return emitted

    def append_worker_execution_event(
        self,
        db: Session,
        *,
        strategy_run_id: str,
        account_id: str,
        basket_execution_id: Optional[str],
        event_type: str,
        payload: Dict[str, Any],
    ) -> int:
        row = db.execute(
            text(
                """
                INSERT INTO public.worker_execution_events (
                    strategy_run_id,
                    account_id,
                    basket_execution_id,
                    event_type,
                    payload_json
                ) VALUES (
                    :strategy_run_id,
                    :account_id,
                    :basket_execution_id,
                    :event_type,
                    CAST(:payload_json AS JSONB)
                )
                RETURNING cursor
                """
            ),
            {
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "basket_execution_id": basket_execution_id,
                "event_type": event_type,
                "payload_json": _json_dumps(payload),
            },
        ).fetchone()
        return _to_int(_row_mapping(row).get("cursor"), 0)

    def list_baskets_for_run(self, db: Session, *, strategy_run_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = db.execute(
            text(
                """
                SELECT *
                FROM public.basket_executions
                WHERE strategy_run_id = :strategy_run_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"strategy_run_id": strategy_run_id, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
        return [self._basket_view(row, db=db) for row in rows]

    def get_basket_for_run(self, db: Session, *, strategy_run_id: str, basket_execution_id: str) -> Optional[Dict[str, Any]]:
        row = db.execute(
            text(
                """
                SELECT *
                FROM public.basket_executions
                WHERE strategy_run_id = :strategy_run_id
                  AND basket_execution_id = :basket_execution_id
                """
            ),
            {"strategy_run_id": strategy_run_id, "basket_execution_id": basket_execution_id},
        ).fetchone()
        if not row:
            return None
        return self._basket_view(row, db=db)

    def list_worker_execution_events(
        self,
        db: Session,
        *,
        strategy_run_id: str,
        after_cursor: int = 0,
        limit: int = 200,
        basket_execution_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = db.execute(
            text(
                """
                SELECT cursor, strategy_run_id, account_id, basket_execution_id, event_type, payload_json, created_at
                FROM public.worker_execution_events
                WHERE strategy_run_id = :strategy_run_id
                  AND cursor > :after_cursor
                  AND (:basket_execution_id IS NULL OR basket_execution_id = :basket_execution_id)
                  AND (:event_type IS NULL OR event_type = :event_type)
                ORDER BY cursor ASC
                LIMIT :limit
                """
            ),
            {
                "strategy_run_id": strategy_run_id,
                "after_cursor": max(0, _to_int(after_cursor)),
                "basket_execution_id": basket_execution_id,
                "event_type": event_type,
                "limit": max(1, min(int(limit), 1000)),
            },
        ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            payload = _row_mapping(row)
            events.append(
                {
                    "cursor": _to_int(payload.get("cursor")),
                    "strategy_run_id": payload.get("strategy_run_id"),
                    "account_id": payload.get("account_id"),
                    "basket_execution_id": payload.get("basket_execution_id"),
                    "event_type": payload.get("event_type"),
                    "payload": _json_loads(payload.get("payload_json"), {}),
                    "created_at": payload.get("created_at"),
                }
            )
        return events

    def has_active_basket_execution(self, strategy_run_id: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.basket_executions
                    WHERE strategy_run_id = :strategy_run_id
                      AND status IN ('submitting', 'active')
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            return bool(row)
        finally:
            db.close()

    def mark_projection_inconsistent_if_linked(self, db: Session, *, canonical_event: Any) -> None:
        event = _row_mapping(canonical_event)
        account_id = str(event.get("account_id") or "")
        order_id = str(event.get("order_id") or "")
        if not account_id or not order_id:
            return
        link = db.execute(
            text(
                """
                SELECT basket_execution_id
                FROM public.live_order_intents
                WHERE account_id = :account_id
                  AND broker_order_id = :order_id
                  AND basket_execution_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"account_id": account_id, "order_id": order_id},
        ).fetchone()
        if not link:
            return
        basket_execution_id = _row_mapping(link).get("basket_execution_id")
        if not basket_execution_id:
            return
        db.execute(
            text(
                """
                UPDATE public.basket_executions
                SET action_required = TRUE,
                    action_reason = 'projection_inconsistent',
                    updated_at = NOW()
                WHERE basket_execution_id = :basket_execution_id
                """
            ),
            {"basket_execution_id": basket_execution_id},
        )

    def _basket_account_id(self, db: Session, *, basket_execution_id: str) -> str:
        row = db.execute(
            text(
                "SELECT account_id FROM public.basket_executions WHERE basket_execution_id = :basket_execution_id"
            ),
            {"basket_execution_id": basket_execution_id},
        ).fetchone()
        return str(_row_mapping(row).get("account_id") or "")

    def _recompute_and_update(self, db: Session, *, basket_execution_id: str) -> Dict[str, Any]:
        legs = db.execute(
            text(
                """
                SELECT status, requested_quantity, last_seen_filled_quantity
                FROM public.basket_execution_legs
                WHERE basket_execution_id = :basket_execution_id
                ORDER BY leg_index ASC
                """
            ),
            {"basket_execution_id": basket_execution_id},
        ).fetchall()
        summary = recompute_basket_status([_row_mapping(row) for row in legs])
        db.execute(
            text(
                """
                UPDATE public.basket_executions
                SET status = :status,
                    requested_leg_count = :requested_leg_count,
                    completed_leg_count = :completed_leg_count,
                    terminal_leg_count = :terminal_leg_count,
                    total_requested_quantity = :total_requested_quantity,
                    total_filled_quantity = :total_filled_quantity,
                    updated_at = NOW()
                WHERE basket_execution_id = :basket_execution_id
                """
            ),
            {
                "basket_execution_id": basket_execution_id,
                "status": summary["status"],
                "requested_leg_count": summary["requested_leg_count"],
                "completed_leg_count": summary["completed_leg_count"],
                "terminal_leg_count": summary["terminal_leg_count"],
                "total_requested_quantity": summary["total_requested_quantity"],
                "total_filled_quantity": summary["total_filled_quantity"],
            },
        )
        return summary

    def _basket_view(self, row: Any, *, db: Session) -> Dict[str, Any]:
        payload = _row_mapping(row)
        legs = db.execute(
            text(
                """
                SELECT *
                FROM public.basket_execution_legs
                WHERE basket_execution_id = :basket_execution_id
                ORDER BY leg_index ASC
                """
            ),
            {"basket_execution_id": payload.get("basket_execution_id")},
        ).fetchall()
        leg_payloads: List[Dict[str, Any]] = []
        for leg in legs:
            leg_map = _row_mapping(leg)
            leg_payloads.append(
                {
                    "basket_execution_id": leg_map.get("basket_execution_id"),
                    "leg_index": _to_int(leg_map.get("leg_index")),
                    "status": leg_map.get("status"),
                    "exchange": leg_map.get("exchange"),
                    "tradingsymbol": leg_map.get("tradingsymbol"),
                    "product": leg_map.get("product"),
                    "transaction_type": leg_map.get("transaction_type"),
                    "requested_quantity": _to_int(leg_map.get("requested_quantity")),
                    "broker_order_id": leg_map.get("broker_order_id"),
                    "client_order_ref": leg_map.get("client_order_ref"),
                    "latest_broker_status": leg_map.get("latest_broker_status"),
                    "last_seen_filled_quantity": _to_int(leg_map.get("last_seen_filled_quantity")),
                    "average_price": leg_map.get("average_price"),
                    "request": _json_loads(leg_map.get("request_json"), {}),
                    "created_at": leg_map.get("created_at"),
                    "updated_at": leg_map.get("updated_at"),
                }
            )
        return {
            "basket_execution_id": payload.get("basket_execution_id"),
            "strategy_run_id": payload.get("strategy_run_id"),
            "account_id": payload.get("account_id"),
            "execution_mode": payload.get("execution_mode"),
            "status": payload.get("status"),
            "all_or_none": bool(payload.get("all_or_none")),
            "action_required": bool(payload.get("action_required")),
            "action_reason": payload.get("action_reason"),
            "rollback_status": payload.get("rollback_status"),
            "requested_leg_count": _to_int(payload.get("requested_leg_count")),
            "completed_leg_count": _to_int(payload.get("completed_leg_count")),
            "terminal_leg_count": _to_int(payload.get("terminal_leg_count")),
            "total_requested_quantity": _to_int(payload.get("total_requested_quantity")),
            "total_filled_quantity": _to_int(payload.get("total_filled_quantity")),
            "latest_event_cursor": payload.get("latest_event_cursor"),
            "latest_event_at": payload.get("latest_event_at"),
            "request": _json_loads(payload.get("request_json"), {}),
            "metadata": _json_loads(payload.get("metadata_json"), {}),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "legs": leg_payloads,
        }


basket_execution_store = BasketExecutionStore(session_factory=SessionLocal)
