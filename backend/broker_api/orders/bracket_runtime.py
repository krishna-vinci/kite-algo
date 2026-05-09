from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal
from backend.broker_api.timeline.worker_timeline import worker_timeline_store
from backend.shared.serialization import _json_dumps, _json_loads, _row_mapping, _to_int, _utcnow

logger = logging.getLogger(__name__)
_BRACKET_EXECUTOR_WAKE_EVENT: Optional[asyncio.Event] = None

BRACKET_ACTIVE_STATES = {"entry_submitting", "entry_working", "arming_exits", "armed", "cancelling"}
BRACKET_STATES = {
    "entry_submitting",
    "entry_working",
    "arming_exits",
    "armed",
    "cancelling",
    "completed",
    "partial",
    "failed",
}
_JSON_CAST_CLAUSE = "CAST(:{name} AS JSONB)"


def _json_bind_clause(db: Session, name: str) -> str:
    bind = db.bind
    dialect_name = str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()
    if dialect_name == "sqlite":
        return f":{name}"
    return _JSON_CAST_CLAUSE.format(name=name)


class BracketRuntimeStore:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    def create_bracket_intent(
        self,
        db: Session,
        *,
        strategy_run_id: str,
        account_id: str,
        config: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        bracket_intent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        bracket_id = bracket_intent_id or f"brk_{uuid.uuid4().hex}"
        db.execute(
            text(
                f"""
                INSERT INTO public.bracket_intents (
                    bracket_intent_id,
                    strategy_run_id,
                    account_id,
                    status,
                    action_required,
                    action_reason,
                    config_json,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :bracket_intent_id,
                    :strategy_run_id,
                    :account_id,
                    'entry_submitting',
                    FALSE,
                    NULL,
                    {_json_bind_clause(db, 'config_json')},
                    {_json_bind_clause(db, 'metadata_json')},
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "bracket_intent_id": bracket_id,
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "config_json": _json_dumps(config or {}),
                "metadata_json": _json_dumps(metadata or {}),
            },
        )
        return self.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_id) or {
            "bracket_intent_id": bracket_id,
            "strategy_run_id": strategy_run_id,
            "account_id": account_id,
            "status": "entry_submitting",
            "action_required": False,
            "action_reason": None,
            "config": config or {},
            "metadata": metadata or {},
        }

    def list_bracket_intents_for_run(self, db: Session, *, strategy_run_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = db.execute(
            text(
                """
                SELECT *
                FROM public.bracket_intents
                WHERE strategy_run_id = :strategy_run_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"strategy_run_id": strategy_run_id, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
        return [self._intent_view(row) for row in rows]

    def get_bracket_intent(self, db: Session, *, strategy_run_id: str, bracket_intent_id: str) -> Optional[Dict[str, Any]]:
        row = db.execute(
            text(
                """
                SELECT *
                FROM public.bracket_intents
                WHERE strategy_run_id = :strategy_run_id
                  AND bracket_intent_id = :bracket_intent_id
                """
            ),
            {"strategy_run_id": strategy_run_id, "bracket_intent_id": bracket_intent_id},
        ).fetchone()
        return self._intent_view(row) if row else None

    def update_bracket_status(
        self,
        db: Session,
        *,
        bracket_intent_id: str,
        status: str,
        action_required: Optional[bool] = None,
        action_reason: Optional[str] = None,
        metadata_patch: Optional[Dict[str, Any]] = None,
        entry_basket_execution_id: Optional[str] = None,
    ) -> None:
        if status not in BRACKET_STATES:
            raise ValueError(f"Unsupported bracket status '{status}'")
        metadata_patch = dict(metadata_patch or {})
        row = db.execute(
            text("SELECT metadata_json FROM public.bracket_intents WHERE bracket_intent_id = :bracket_intent_id"),
            {"bracket_intent_id": bracket_intent_id},
        ).fetchone()
        current_metadata = _json_loads(_row_mapping(row).get("metadata_json") if row else None, {})
        current_metadata.update(metadata_patch)
        db.execute(
            text(
                f"""
                UPDATE public.bracket_intents
                SET status = :status,
                    action_required = COALESCE(:action_required, action_required),
                    action_reason = :action_reason,
                    metadata_json = {_json_bind_clause(db, 'metadata_json')},
                    entry_basket_execution_id = COALESCE(:entry_basket_execution_id, entry_basket_execution_id),
                    closed_at = CASE WHEN :status IN ('completed', 'partial', 'failed') THEN CURRENT_TIMESTAMP ELSE closed_at END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE bracket_intent_id = :bracket_intent_id
                """
            ),
            {
                "bracket_intent_id": bracket_intent_id,
                "status": status,
                "action_required": action_required,
                "action_reason": action_reason,
                "metadata_json": _json_dumps(current_metadata),
                "entry_basket_execution_id": entry_basket_execution_id,
            },
        )

    def enqueue_action(
        self,
        db: Session,
        *,
        bracket_intent_id: str,
        strategy_run_id: str,
        account_id: str,
        action_type: str,
        payload: Optional[Dict[str, Any]] = None,
        action_id: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        aid = action_id or f"bact_{uuid.uuid4().hex}"
        db.execute(
            text(
                f"""
                INSERT INTO public.bracket_actions (
                    action_id,
                    bracket_intent_id,
                    strategy_run_id,
                    account_id,
                    action_type,
                    status,
                    attempt_count,
                    next_attempt_at,
                    payload_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :action_id,
                    :bracket_intent_id,
                    :strategy_run_id,
                    :account_id,
                    :action_type,
                    'pending',
                    0,
                    :next_attempt_at,
                    {_json_bind_clause(db, 'payload_json')},
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (action_id) DO NOTHING
                """
            ),
            {
                "action_id": aid,
                "bracket_intent_id": bracket_intent_id,
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "action_type": action_type,
                "next_attempt_at": next_attempt_at,
                "payload_json": _json_dumps(payload or {}),
            },
        )
        wake = get_bracket_executor_wakeup_event()
        if wake is not None:
            wake.set()
        return self.get_action(db, action_id=aid) or {
            "action_id": aid,
            "bracket_intent_id": bracket_intent_id,
            "strategy_run_id": strategy_run_id,
            "account_id": account_id,
            "action_type": action_type,
            "status": "pending",
            "attempt_count": 0,
            "payload": payload or {},
        }

    def get_action(self, db: Session, *, action_id: str) -> Optional[Dict[str, Any]]:
        row = db.execute(
            text("SELECT * FROM public.bracket_actions WHERE action_id = :action_id"),
            {"action_id": action_id},
        ).fetchone()
        return self._action_view(row) if row else None

    def list_actions_for_intent(self, db: Session, *, bracket_intent_id: str) -> List[Dict[str, Any]]:
        rows = db.execute(
            text(
                """
                SELECT *
                FROM public.bracket_actions
                WHERE bracket_intent_id = :bracket_intent_id
                ORDER BY created_at ASC
                """
            ),
            {"bracket_intent_id": bracket_intent_id},
        ).fetchall()
        return [self._action_view(row) for row in rows]

    def claim_pending_actions(self, db: Session, *, limit: int = 10) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        try:
            rows = db.execute(
                text(
                    """
                    WITH candidates AS (
                        SELECT action_id
                        FROM public.bracket_actions
                        WHERE status = 'pending'
                          AND COALESCE(next_attempt_at, CURRENT_TIMESTAMP) <= CURRENT_TIMESTAMP
                        ORDER BY created_at ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE public.bracket_actions ba
                    SET status = 'claimed',
                        claimed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    FROM candidates
                    WHERE ba.action_id = candidates.action_id
                    RETURNING ba.*
                    """
                ),
                {"limit": safe_limit},
            ).fetchall()
        except Exception:
            # SQLite fallback for tests (production path remains FOR UPDATE SKIP LOCKED)
            rows = db.execute(
                text(
                    """
                    SELECT action_id
                    FROM public.bracket_actions
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT :limit
                    """
                ),
                {"limit": safe_limit},
            ).fetchall()
            claimed: List[Dict[str, Any]] = []
            for row in rows:
                action_id = str(_row_mapping(row).get("action_id") or "")
                if not action_id:
                    continue
                db.execute(
                    text(
                        """
                        UPDATE public.bracket_actions
                        SET status = 'claimed',
                            claimed_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE action_id = :action_id
                        """
                    ),
                    {"action_id": action_id},
                )
                fetched = self.get_action(db, action_id=action_id)
                if fetched:
                    claimed.append(fetched)
            return claimed
        return [self._action_view(row) for row in rows]

    def mark_action_succeeded(self, db: Session, *, action_id: str, result_payload: Optional[Dict[str, Any]] = None) -> None:
        db.execute(
            text(
                f"""
                UPDATE public.bracket_actions
                SET status = 'succeeded',
                    payload_json = {_json_bind_clause(db, 'payload_json')},
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = :action_id
                """
            ),
            {"action_id": action_id, "payload_json": _json_dumps(result_payload or {})},
        )

    def mark_action_retry(self, db: Session, *, action_id: str, error_payload: Dict[str, Any], next_attempt_at: datetime) -> None:
        db.execute(
            text(
                f"""
                UPDATE public.bracket_actions
                SET status = 'pending',
                    attempt_count = attempt_count + 1,
                    next_attempt_at = :next_attempt_at,
                    error_json = {_json_bind_clause(db, 'error_json')},
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = :action_id
                """
            ),
            {
                "action_id": action_id,
                "next_attempt_at": next_attempt_at,
                "error_json": _json_dumps(error_payload),
            },
        )

    def mark_action_failed(self, db: Session, *, action_id: str, error_payload: Dict[str, Any]) -> None:
        row = db.execute(
            text(
                f"""
                UPDATE public.bracket_actions
                SET status = 'failed',
                    attempt_count = attempt_count + 1,
                    error_json = {_json_bind_clause(db, 'error_json')},
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = :action_id
                RETURNING bracket_intent_id
                """
            ),
            {"action_id": action_id, "error_json": _json_dumps(error_payload)},
        ).fetchone()
        if not row:
            return
        bracket_intent_id = str(_row_mapping(row).get("bracket_intent_id") or "")
        if bracket_intent_id:
            self.update_bracket_status(
                db,
                bracket_intent_id=bracket_intent_id,
                status="arming_exits",
                action_required=True,
                action_reason="exit_placement_failed",
            )

    def has_active_bracket_for_run(self, strategy_run_id: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.bracket_intents
                    WHERE strategy_run_id = :strategy_run_id
                      AND status = ANY(:states)
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id, "states": list(BRACKET_ACTIVE_STATES)},
            ).fetchone()
            return bool(row)
        finally:
            db.close()

    def has_pending_actions_for_run(self, strategy_run_id: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.bracket_actions
                    WHERE strategy_run_id = :strategy_run_id
                      AND status IN ('pending', 'claimed')
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            return bool(row)
        finally:
            db.close()

    def request_cancel_bracket(self, db: Session, *, strategy_run_id: str, bracket_intent_id: str) -> Dict[str, Any]:
        intent = self.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
        if not intent:
            raise KeyError(bracket_intent_id)
        self.update_bracket_status(
            db,
            bracket_intent_id=bracket_intent_id,
            status="cancelling",
            action_required=False,
            action_reason=None,
        )
        self.enqueue_action(
            db,
            bracket_intent_id=bracket_intent_id,
            strategy_run_id=strategy_run_id,
            account_id=str(intent.get("account_id") or ""),
            action_type="cancel_bracket",
            payload={},
        )
        return self.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id) or intent

    def apply_order_event_observation(self, db: Session, *, canonical_event: Any) -> List[Dict[str, Any]]:
        event = _row_mapping(canonical_event)
        account_id = str(event.get("account_id") or "")
        order_id = str(event.get("order_id") or "")
        status = str(event.get("status") or "").upper()
        if not account_id or not order_id:
            return []

        link = db.execute(
            text(
                """
                SELECT strategy_run_id, bracket_intent_id
                FROM public.live_order_intents
                WHERE account_id = :account_id
                  AND broker_order_id = :order_id
                  AND bracket_intent_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"account_id": account_id, "order_id": order_id},
        ).fetchone()
        if not link:
            return []
        mapped_link = _row_mapping(link)
        strategy_run_id = str(mapped_link.get("strategy_run_id") or "")
        bracket_intent_id = str(mapped_link.get("bracket_intent_id") or "")
        if not strategy_run_id or not bracket_intent_id:
            return []

        intent_row = db.execute(
            text("SELECT config_json, status, account_id FROM public.bracket_intents WHERE bracket_intent_id = :bracket_intent_id"),
            {"bracket_intent_id": bracket_intent_id},
        ).fetchone()
        if not intent_row:
            return []
        intent = _row_mapping(intent_row)
        config = _json_loads(intent.get("config_json"), {})
        entry_order_id = str(((config.get("entry") or {}).get("broker_order_id") or "")).strip()
        current_status = str(intent.get("status") or "")
        emitted: List[Dict[str, Any]] = []

        if entry_order_id and order_id == entry_order_id:
            quantity_requested = _to_int(((config.get("entry") or {}).get("quantity") or 0))
            filled_quantity = _to_int(event.get("filled_quantity"))
            if status in {"REJECTED", "CANCELLED", "LAPSED"} and filled_quantity == 0:
                self.update_bracket_status(
                    db,
                    bracket_intent_id=bracket_intent_id,
                    status="failed",
                    action_required=False,
                    action_reason="entry_terminal",
                )
            elif filled_quantity > 0 and quantity_requested > 0 and filled_quantity >= quantity_requested:
                if current_status not in {"arming_exits", "armed", "completed", "partial", "failed"}:
                    self.update_bracket_status(
                        db,
                        bracket_intent_id=bracket_intent_id,
                        status="arming_exits",
                        action_required=False,
                        action_reason=None,
                    )
                    stoploss = dict(config.get("stoploss") or {})
                    if stoploss:
                        self.enqueue_action(
                            db,
                            bracket_intent_id=bracket_intent_id,
                            strategy_run_id=strategy_run_id,
                            account_id=account_id,
                            action_type="place_stoploss",
                            payload=stoploss,
                        )
                    target = dict(config.get("target") or {})
                    if target:
                        self.enqueue_action(
                            db,
                            bracket_intent_id=bracket_intent_id,
                            strategy_run_id=strategy_run_id,
                            account_id=account_id,
                            action_type="place_target",
                            payload=target,
                        )
                    event_row = worker_timeline_store.append_event(
                        db=db,
                        strategy_run_id=strategy_run_id,
                        account_id=account_id,
                        basket_execution_id=None,
                        event_kind="execution",
                        event_source="bracket_runtime",
                        event_type="bracket.state_changed",
                        related_resource_type="bracket_intent",
                        related_resource_id=bracket_intent_id,
                        summary=None,
                        payload={
                            "bracket_intent_id": bracket_intent_id,
                            "status": "arming_exits",
                        },
                    )
                    emitted.append(event_row)
            elif status in {"CANCELLED", "REJECTED", "LAPSED"} and 0 < filled_quantity < quantity_requested:
                self.update_bracket_status(
                    db,
                    bracket_intent_id=bracket_intent_id,
                    status="partial",
                    action_required=True,
                    action_reason="entry_partial_terminal",
                )

        stoploss_order_id = str(((config.get("stoploss") or {}).get("broker_order_id") or "")).strip()
        target_order_id = str(((config.get("target") or {}).get("broker_order_id") or "")).strip()
        if stoploss_order_id and order_id == stoploss_order_id and status == "COMPLETE":
            self.update_bracket_status(db, bracket_intent_id=bracket_intent_id, status="cancelling")
            if target_order_id:
                self.enqueue_action(
                    db,
                    bracket_intent_id=bracket_intent_id,
                    strategy_run_id=strategy_run_id,
                    account_id=account_id,
                    action_type="cancel_target",
                    payload={"order_id": target_order_id},
                )
        if target_order_id and order_id == target_order_id and status == "COMPLETE":
            self.update_bracket_status(db, bracket_intent_id=bracket_intent_id, status="cancelling")
            if stoploss_order_id:
                self.enqueue_action(
                    db,
                    bracket_intent_id=bracket_intent_id,
                    strategy_run_id=strategy_run_id,
                    account_id=account_id,
                    action_type="cancel_stoploss",
                    payload={"order_id": stoploss_order_id},
                )

        return emitted

    def mark_projection_inconsistent_if_linked(self, db: Session, *, canonical_event: Any) -> None:
        event = _row_mapping(canonical_event)
        account_id = str(event.get("account_id") or "")
        order_id = str(event.get("order_id") or "")
        if not account_id or not order_id:
            return
        row = db.execute(
            text(
                """
                SELECT bracket_intent_id
                FROM public.live_order_intents
                WHERE account_id = :account_id
                  AND broker_order_id = :order_id
                  AND bracket_intent_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"account_id": account_id, "order_id": order_id},
        ).fetchone()
        bracket_intent_id = str(_row_mapping(row).get("bracket_intent_id") or "")
        if not bracket_intent_id:
            return
        self.update_bracket_status(
            db,
            bracket_intent_id=bracket_intent_id,
            status="arming_exits",
            action_required=True,
            action_reason="projection_inconsistent",
        )

    def _intent_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "bracket_intent_id": payload.get("bracket_intent_id"),
            "strategy_run_id": payload.get("strategy_run_id"),
            "account_id": payload.get("account_id"),
            "entry_basket_execution_id": payload.get("entry_basket_execution_id"),
            "status": payload.get("status"),
            "action_required": bool(payload.get("action_required")),
            "action_reason": payload.get("action_reason"),
            "config": _json_loads(payload.get("config_json"), {}),
            "metadata": _json_loads(payload.get("metadata_json"), {}),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
        }

    def _action_view(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "action_id": payload.get("action_id"),
            "bracket_intent_id": payload.get("bracket_intent_id"),
            "strategy_run_id": payload.get("strategy_run_id"),
            "account_id": payload.get("account_id"),
            "action_type": payload.get("action_type"),
            "status": payload.get("status"),
            "attempt_count": _to_int(payload.get("attempt_count")),
            "next_attempt_at": payload.get("next_attempt_at"),
            "claimed_at": payload.get("claimed_at"),
            "payload": _json_loads(payload.get("payload_json"), {}),
            "error": _json_loads(payload.get("error_json"), None),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }


async def execute_bracket_action_once(
    *,
    action: Dict[str, Any],
    store: BracketRuntimeStore,
    place_order_fn,
    cancel_order_fn,
    max_attempts: int = 3,
) -> None:
    db = SessionLocal()
    try:
        action_id = str(action.get("action_id") or "")
        bracket_intent_id = str(action.get("bracket_intent_id") or "")
        action_type = str(action.get("action_type") or "")
        account_id = str(action.get("account_id") or "")
        strategy_run_id = str(action.get("strategy_run_id") or "")
        payload = dict(action.get("payload") or {})
        attempt_count = _to_int(action.get("attempt_count")) + 1

        if action_type in {"place_stoploss", "place_target"}:
            suffix = "stoploss" if action_type == "place_stoploss" else "target"
            idempotency_key = f"bracket:{bracket_intent_id}:{suffix}:{attempt_count}"
            result = await place_order_fn(
                strategy_run_id=strategy_run_id,
                account_id=account_id,
                bracket_intent_id=bracket_intent_id,
                action_type=action_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            store.mark_action_succeeded(db, action_id=action_id, result_payload=result)
            intent = store.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
            cfg = dict((intent or {}).get("config") or {})
            if action_type == "place_stoploss":
                cfg.setdefault("stoploss", {})["broker_order_id"] = result.get("order_id")
            else:
                cfg.setdefault("target", {})["broker_order_id"] = result.get("order_id")
            db.execute(
                text(
                    f"""
                    UPDATE public.bracket_intents
                    SET config_json = {_json_bind_clause(db, 'config_json')},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE bracket_intent_id = :bracket_intent_id
                    """
                ),
                {"bracket_intent_id": bracket_intent_id, "config_json": _json_dumps(cfg)},
            )
            pending = db.execute(
                text(
                    """
                    SELECT 1
                    FROM public.bracket_actions
                    WHERE bracket_intent_id = :bracket_intent_id
                      AND status IN ('pending', 'claimed')
                      AND action_type IN ('place_stoploss', 'place_target')
                    LIMIT 1
                    """
                ),
                {"bracket_intent_id": bracket_intent_id},
            ).fetchone()
            if not pending:
                store.update_bracket_status(
                    db,
                    bracket_intent_id=bracket_intent_id,
                    status="armed",
                    action_required=False,
                    action_reason=None,
                )
        elif action_type in {"cancel_stoploss", "cancel_target"}:
            order_id = str(payload.get("order_id") or "").strip()
            if order_id:
                await cancel_order_fn(account_id=account_id, order_id=order_id)
            store.mark_action_succeeded(db, action_id=action_id, result_payload={"cancelled_order_id": order_id})
            store.update_bracket_status(db, bracket_intent_id=bracket_intent_id, status="completed")
        elif action_type == "cancel_bracket":
            intent = store.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
            cfg = dict((intent or {}).get("config") or {})
            for key in ("entry", "stoploss", "target"):
                order_id = str((cfg.get(key) or {}).get("broker_order_id") or "").strip()
                if order_id:
                    await cancel_order_fn(account_id=account_id, order_id=order_id)
            store.mark_action_succeeded(db, action_id=action_id, result_payload={"cancelled": True})
            store.update_bracket_status(db, bracket_intent_id=bracket_intent_id, status="completed")
        else:
            store.mark_action_failed(db, action_id=action_id, error_payload={"error": f"unknown_action:{action_type}"})

        db.commit()
    except Exception as exc:
        db.rollback()
        attempt_count = _to_int(action.get("attempt_count")) + 1
        if attempt_count >= max_attempts:
            store.mark_action_failed(db, action_id=str(action.get("action_id") or ""), error_payload={"error": str(exc)})
        else:
            next_attempt_at = _utcnow() + timedelta(seconds=min(10, 2 * attempt_count))
            store.mark_action_retry(
                db,
                action_id=str(action.get("action_id") or ""),
                error_payload={"error": str(exc)},
                next_attempt_at=next_attempt_at,
            )
        db.commit()
    finally:
        db.close()


async def run_bracket_executor_once(
    *,
    store: BracketRuntimeStore,
    place_order_fn,
    cancel_order_fn,
    claim_limit: int = 10,
) -> int:
    db = SessionLocal()
    try:
        actions = store.claim_pending_actions(db, limit=claim_limit)
        db.commit()
    finally:
        db.close()

    for action in actions:
        await execute_bracket_action_once(
            action=action,
            store=store,
            place_order_fn=place_order_fn,
            cancel_order_fn=cancel_order_fn,
        )
    return len(actions)


bracket_runtime_store = BracketRuntimeStore()


def get_bracket_executor_wakeup_event() -> asyncio.Event:
    global _BRACKET_EXECUTOR_WAKE_EVENT
    if _BRACKET_EXECUTOR_WAKE_EVENT is None:
        _BRACKET_EXECUTOR_WAKE_EVENT = asyncio.Event()
    return _BRACKET_EXECUTOR_WAKE_EVENT
