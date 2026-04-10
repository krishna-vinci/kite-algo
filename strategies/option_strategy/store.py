from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal


class OptionStrategyStore:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def create_run(
        self,
        *,
        underlying: str,
        expiry: str,
        user_intent: str,
        inferred_structure: str,
        inferred_family: str,
        execution_mode: str,
        selected_legs: list[dict[str, Any]],
        canonical_strategy: Dict[str, Any],
        order_plan: Dict[str, Any],
        status: str = "planned",
        algo_instance_id: str | None = None,
    ) -> str:
        session = self._session_factory()
        try:
            result = session.execute(
                text(
                    """
                    INSERT INTO public.option_strategy_runs (
                        underlying,
                        expiry,
                        user_intent,
                        inferred_structure,
                        inferred_family,
                        execution_mode,
                        status,
                        selected_legs,
                        canonical_strategy,
                        order_plan,
                        algo_instance_id
                    ) VALUES (
                        :underlying,
                        :expiry,
                        :user_intent,
                        :inferred_structure,
                        :inferred_family,
                        :execution_mode,
                        :status,
                        CAST(:selected_legs AS jsonb),
                        CAST(:canonical_strategy AS jsonb),
                        CAST(:order_plan AS jsonb),
                        :algo_instance_id
                    )
                    RETURNING id
                    """
                ),
                {
                    "underlying": underlying,
                    "expiry": expiry,
                    "user_intent": user_intent,
                    "inferred_structure": inferred_structure,
                    "inferred_family": inferred_family,
                    "execution_mode": execution_mode,
                    "status": status,
                    "selected_legs": json.dumps(selected_legs),
                    "canonical_strategy": json.dumps(canonical_strategy),
                    "order_plan": json.dumps(order_plan),
                    "algo_instance_id": algo_instance_id,
                },
            )
            run_id = str(result.scalar_one())
            session.commit()
            return run_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_execution_result(self, run_id: str, *, status: str, execution_result: Dict[str, Any], algo_instance_id: str | None = None) -> None:
        session = self._session_factory()
        try:
            session.execute(
                text(
                    """
                    UPDATE public.option_strategy_runs
                    SET status = :status,
                        execution_result = CAST(:execution_result AS jsonb),
                        algo_instance_id = COALESCE(:algo_instance_id, algo_instance_id),
                        updated_at = NOW()
                    WHERE id = CAST(:run_id AS uuid)
                    """
                ),
                {
                    "run_id": run_id,
                    "status": status,
                    "execution_result": json.dumps(execution_result),
                    "algo_instance_id": algo_instance_id,
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT * FROM public.option_strategy_runs WHERE id = CAST(:run_id AS uuid)"),
                {"run_id": run_id},
            ).mappings().first()
            return dict(row) if row else None
        finally:
            session.close()
