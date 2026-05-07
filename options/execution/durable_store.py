from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from sqlalchemy import text
from database import SessionLocal

from .models import OptionRunCreateRequest, OptionRunState


class DurableOptionRunStore:
    """DB-backed canonical option run store for durable execution/protection state."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._id_factory = id_factory or self._next_run_id

    def _next_run_id(self) -> str:
        return f"opt_run_{uuid.uuid4().hex}"

    @staticmethod
    def _to_json(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _normalize_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return []

    @staticmethod
    def _normalize_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _normalize_optional_dict(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            if parsed is None:
                return None
            return parsed if isinstance(parsed, dict) else None
        if isinstance(value, dict):
            return value
        return None

    @classmethod
    def _row_to_state(cls, row: dict[str, Any]) -> OptionRunState:
        return OptionRunState(
            strategy_run_id=str(row.get("strategy_run_id") or ""),
            strategy_name=str(row.get("strategy_name") or ""),
            product=row.get("product"),
            status=str(row.get("status") or "created"),
            legs=cls._normalize_list(row.get("legs")),
            protection=cls._normalize_optional_dict(row.get("protection")),
            metadata=cls._normalize_dict(row.get("metadata")),
            orders=cls._normalize_list(row.get("orders")),
            trades=cls._normalize_list(row.get("trades")),
            completed_legs=cls._normalize_list(row.get("completed_legs")),
            failed_legs=cls._normalize_list(row.get("failed_legs")),
            pending_legs=cls._normalize_list(row.get("pending_legs")),
        )

    @staticmethod
    def _require_id(strategy_run_id: str) -> None:
        if not strategy_run_id:
            raise ValueError("strategy_run_id is required")

    def create_run(self, request: OptionRunCreateRequest) -> OptionRunState:
        strategy_run_id = str(request.strategy_run_id or self._id_factory())
        if not strategy_run_id.startswith("opt_run_") and not request.strategy_run_id:
            strategy_run_id = f"opt_run_{strategy_run_id}"

        run = OptionRunState.from_create_request(request, strategy_run_id=strategy_run_id)
        session = self._session_factory()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO public.option_run_states (
                        strategy_run_id,
                        strategy_name,
                        product,
                        status,
                        legs,
                        protection,
                        metadata,
                        orders,
                        trades,
                        completed_legs,
                        failed_legs,
                        pending_legs
                    ) VALUES (
                        :strategy_run_id,
                        :strategy_name,
                        :product,
                        :status,
                        CAST(:legs AS jsonb),
                        CAST(:protection AS jsonb),
                        CAST(:metadata AS jsonb),
                        CAST(:orders AS jsonb),
                        CAST(:trades AS jsonb),
                        CAST(:completed_legs AS jsonb),
                        CAST(:failed_legs AS jsonb),
                        CAST(:pending_legs AS jsonb)
                    )
                    """
                ),
                {
                    "strategy_run_id": run.strategy_run_id,
                    "strategy_name": run.strategy_name,
                    "product": run.product,
                    "status": run.status,
                    "legs": self._to_json(run.legs),
                    "protection": self._to_json(run.protection),
                    "metadata": self._to_json(run.metadata),
                    "orders": self._to_json(run.orders),
                    "trades": self._to_json(run.trades),
                    "completed_legs": self._to_json(run.completed_legs),
                    "failed_legs": self._to_json(run.failed_legs),
                    "pending_legs": self._to_json(run.pending_legs),
                },
            )
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_runs(self) -> list[OptionRunState]:
        session = self._session_factory()
        try:
            rows = (
                session.execute(
                    text(
                        """
                        SELECT
                            strategy_run_id,
                            strategy_name,
                            product,
                            status,
                            legs,
                            protection,
                            metadata,
                            orders,
                            trades,
                            completed_legs,
                            failed_legs,
                            pending_legs
                        FROM public.option_run_states
                        ORDER BY updated_at DESC, strategy_run_id
                        """
                    )
                )
                .mappings()
                .all()
            )
            return [self._row_to_state(dict(row)) for row in rows]
        finally:
            session.close()

    def get_run(self, strategy_run_id: str) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            return self._get_run_in_session(session, strategy_run_id)
        finally:
            session.close()

    def get_run_in_session(self, session: Any, strategy_run_id: str) -> OptionRunState:
        self._require_id(strategy_run_id)
        return self._get_run_in_session(session, strategy_run_id)

    def _get_run_in_session(self, session: Any, strategy_run_id: str, *, for_update: bool = False) -> OptionRunState:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = (
            session.execute(
                text(
                    f"""
                    SELECT
                        strategy_run_id,
                        strategy_name,
                        product,
                        status,
                        legs,
                        protection,
                        metadata,
                        orders,
                        trades,
                        completed_legs,
                        failed_legs,
                        pending_legs
                    FROM public.option_run_states
                    WHERE strategy_run_id = :strategy_run_id
                    {lock_clause}
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"Option run not found: {strategy_run_id}")
        return self._row_to_state(dict(row))

    def save_run(self, run: OptionRunState) -> OptionRunState:
        self._require_id(run.strategy_run_id)
        session = self._session_factory()
        try:
            self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _update_run_in_session(self, session: Any, run: OptionRunState) -> None:
        result = session.execute(
            text(
                """
                UPDATE public.option_run_states
                SET
                    strategy_name = :strategy_name,
                    product = :product,
                    status = :status,
                    legs = CAST(:legs AS jsonb),
                    protection = CAST(:protection AS jsonb),
                    metadata = CAST(:metadata AS jsonb),
                    orders = CAST(:orders AS jsonb),
                    trades = CAST(:trades AS jsonb),
                    completed_legs = CAST(:completed_legs AS jsonb),
                    failed_legs = CAST(:failed_legs AS jsonb),
                    pending_legs = CAST(:pending_legs AS jsonb),
                    updated_at = NOW()
                WHERE strategy_run_id = :strategy_run_id
                """
            ),
            {
                "strategy_run_id": run.strategy_run_id,
                "strategy_name": run.strategy_name,
                "product": run.product,
                "status": run.status,
                "legs": self._to_json(run.legs),
                "protection": self._to_json(run.protection),
                "metadata": self._to_json(run.metadata),
                "orders": self._to_json(run.orders),
                "trades": self._to_json(run.trades),
                "completed_legs": self._to_json(run.completed_legs),
                "failed_legs": self._to_json(run.failed_legs),
                "pending_legs": self._to_json(run.pending_legs),
            },
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise KeyError(f"Option run not found: {run.strategy_run_id}")

    def record_orders(self, strategy_run_id: str, orders: list[dict]) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            run.orders.extend(list(orders))
            self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_trades(self, strategy_run_id: str, trades: list[dict]) -> OptionRunState:
        self._require_id(strategy_run_id)
        session = self._session_factory()
        try:
            run = self._get_run_in_session(session, strategy_run_id, for_update=True)
            run.trades.extend(list(trades))
            self._update_run_in_session(session, run)
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
