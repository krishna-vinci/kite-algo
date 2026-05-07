from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal
from shared.serialization import _json_dumps, _json_loads, _row_mapping, _to_int


class WorkerTimelineStore:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    def append_event(
        self,
        *,
        db: Session | None = None,
        strategy_run_id: str,
        account_id: str,
        basket_execution_id: str | None,
        event_kind: str,
        event_source: str,
        event_type: str,
        related_resource_type: str | None,
        related_resource_id: str | None,
        summary: str | None,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        owns_session = db is None
        session = db or self.session_factory()
        try:
            payload_expr = self._payload_insert_expr(session)
            row = session.execute(
                text(
                    f"""
                    INSERT INTO public.worker_execution_events (
                        strategy_run_id,
                        account_id,
                        basket_execution_id,
                        event_kind,
                        event_source,
                        event_type,
                        related_resource_type,
                        related_resource_id,
                        summary,
                        payload_json
                    ) VALUES (
                        :strategy_run_id,
                        :account_id,
                        :basket_execution_id,
                        :event_kind,
                        :event_source,
                        :event_type,
                        :related_resource_type,
                        :related_resource_id,
                        :summary,
                        {payload_expr}
                    )
                    RETURNING
                        cursor,
                        strategy_run_id,
                        account_id,
                        basket_execution_id,
                        event_kind,
                        event_source,
                        event_type,
                        related_resource_type,
                        related_resource_id,
                        summary,
                        payload_json,
                        created_at
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "account_id": account_id,
                    "basket_execution_id": basket_execution_id,
                    "event_kind": event_kind,
                    "event_source": event_source,
                    "event_type": event_type,
                    "related_resource_type": related_resource_type,
                    "related_resource_id": related_resource_id,
                    "summary": summary,
                    "payload_json": _json_dumps(payload),
                },
            ).fetchone()
            if owns_session:
                session.commit()
            return self._normalize_row(row)
        except Exception:
            if owns_session:
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def list_events(
        self,
        *,
        db: Session | None = None,
        strategy_run_id: str,
        after_cursor: int = 0,
        limit: int = 200,
        event_kind: str | None = None,
        event_source: str | None = None,
        event_type: str | None = None,
        related_resource_type: str | None = None,
        related_resource_id: str | None = None,
        basket_execution_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        owns_session = db is None
        session = db or self.session_factory()
        try:
            rows = session.execute(
                text(
                    """
                    SELECT
                        cursor,
                        strategy_run_id,
                        account_id,
                        basket_execution_id,
                        event_kind,
                        event_source,
                        event_type,
                        related_resource_type,
                        related_resource_id,
                        summary,
                        payload_json,
                        created_at
                    FROM public.worker_execution_events
                    WHERE strategy_run_id = :strategy_run_id
                      AND cursor > :after_cursor
                      AND (:event_kind IS NULL OR event_kind = :event_kind)
                      AND (:event_source IS NULL OR event_source = :event_source)
                      AND (:event_type IS NULL OR event_type = :event_type)
                      AND (:related_resource_type IS NULL OR related_resource_type = :related_resource_type)
                      AND (:related_resource_id IS NULL OR related_resource_id = :related_resource_id)
                      AND (:basket_execution_id IS NULL OR basket_execution_id = :basket_execution_id)
                    ORDER BY cursor ASC
                    LIMIT :limit
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "after_cursor": max(0, _to_int(after_cursor)),
                    "event_kind": event_kind,
                    "event_source": event_source,
                    "event_type": event_type,
                    "related_resource_type": related_resource_type,
                    "related_resource_id": related_resource_id,
                    "basket_execution_id": basket_execution_id,
                    "limit": max(1, min(int(limit), 1000)),
                },
            ).fetchall()
            return [self._normalize_row(row) for row in rows]
        finally:
            if owns_session:
                session.close()

    def get_latest_event_for_source(
        self,
        *,
        db: Session | None = None,
        strategy_run_id: str,
        event_kind: str,
        event_source: str,
    ) -> Dict[str, Any] | None:
        owns_session = db is None
        session = db or self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    SELECT
                        cursor,
                        strategy_run_id,
                        account_id,
                        basket_execution_id,
                        event_kind,
                        event_source,
                        event_type,
                        related_resource_type,
                        related_resource_id,
                        summary,
                        payload_json,
                        created_at
                    FROM public.worker_execution_events
                    WHERE strategy_run_id = :strategy_run_id
                      AND event_kind = :event_kind
                      AND event_source = :event_source
                    ORDER BY cursor DESC
                    LIMIT 1
                    """
                ),
                {
                    "strategy_run_id": strategy_run_id,
                    "event_kind": event_kind,
                    "event_source": event_source,
                },
            ).fetchone()
            if not row:
                return None
            return self._normalize_row(row)
        finally:
            if owns_session:
                session.close()

    def _payload_insert_expr(self, db: Session) -> str:
        bind = getattr(db, "bind", None)
        dialect = getattr(bind, "dialect", None)
        dialect_name = str(getattr(dialect, "name", ""))
        if dialect_name.startswith("postgres"):
            return "CAST(:payload_json AS JSONB)"
        return ":payload_json"

    def _normalize_row(self, row: Any) -> Dict[str, Any]:
        payload = _row_mapping(row)
        return {
            "cursor": _to_int(payload.get("cursor")),
            "strategy_run_id": payload.get("strategy_run_id"),
            "account_id": payload.get("account_id"),
            "basket_execution_id": payload.get("basket_execution_id"),
            "event_kind": payload.get("event_kind"),
            "event_source": payload.get("event_source"),
            "event_type": payload.get("event_type"),
            "related_resource_type": payload.get("related_resource_type"),
            "related_resource_id": payload.get("related_resource_id"),
            "summary": payload.get("summary"),
            "payload": _json_loads(payload.get("payload_json"), {}),
            "created_at": payload.get("created_at"),
        }


worker_timeline_store = WorkerTimelineStore()
