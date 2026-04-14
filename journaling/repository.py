from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal

from .models import (
    BenchmarkDailyPrice,
    BenchmarkDefinition,
    JournalDecisionEvent,
    JournalEquityPoint,
    JournalExecutionFact,
    JournalMetricSnapshot,
    JournalRule,
    JournalRuleEvidence,
    JournalRun,
    JournalRunLeg,
    JournalSourceLink,
    ProjectionState,
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
    if isinstance(value, date):
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


class JournalRepository:
    def __init__(self, session_factory: sessionmaker | Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_run(self, run: JournalRun) -> str:
        db = self.session_factory()
        try:
            result = db.execute(
                text(
                    """
                    INSERT INTO public.journal_runs (
                        strategy_family,
                        strategy_name,
                        entry_surface,
                        execution_mode,
                        account_ref,
                        status,
                        benchmark_id,
                        capital_basis_type,
                        capital_committed,
                        started_at,
                        ended_at,
                        review_state,
                        source_summary_json,
                        metadata_json
                    ) VALUES (
                        :strategy_family,
                        :strategy_name,
                        :entry_surface,
                        :execution_mode,
                        :account_ref,
                        :status,
                        :benchmark_id,
                        :capital_basis_type,
                        :capital_committed,
                        :started_at,
                        :ended_at,
                        :review_state,
                        CAST(:source_summary_json AS jsonb),
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "strategy_family": run.strategy_family,
                    "strategy_name": run.strategy_name,
                    "entry_surface": run.entry_surface,
                    "execution_mode": run.execution_mode,
                    "account_ref": run.account_ref,
                    "status": run.status,
                    "benchmark_id": run.benchmark_id,
                    "capital_basis_type": run.capital_basis_type,
                    "capital_committed": run.capital_committed,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "review_state": run.review_state,
                    "source_summary_json": _json_dumps(run.source_summary),
                    "metadata_json": _json_dumps(run.metadata),
                },
            )
            run_id = str(result.scalar_one())
            db.commit()
            return run_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_run(self, run_id: str, *, status: Optional[str] = None, review_state: Optional[str] = None, ended_at: Optional[datetime] = None, source_summary: Optional[Dict[str, Any]] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE public.journal_runs
                    SET strategy_name = COALESCE(:strategy_name, strategy_name),
                        entry_surface = COALESCE(:entry_surface, entry_surface),
                        status = COALESCE(:status, status),
                        review_state = COALESCE(:review_state, review_state),
                        ended_at = COALESCE(:ended_at, ended_at),
                        source_summary_json = CASE WHEN :source_summary_json IS NULL THEN source_summary_json ELSE CAST(:source_summary_json AS jsonb) END,
                        metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                        updated_at = NOW()
                    WHERE id = CAST(:run_id AS uuid)
                    """
                ),
                {
                    "run_id": run_id,
                    "strategy_name": None,
                    "entry_surface": None,
                    "status": status,
                    "review_state": review_state,
                    "ended_at": ended_at,
                    "source_summary_json": _json_dumps(source_summary) if source_summary is not None else None,
                    "metadata_json": _json_dumps(metadata) if metadata is not None else None,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_run_fields(
        self,
        run_id: str,
        *,
        strategy_name: Optional[str] = None,
        entry_surface: Optional[str] = None,
        status: Optional[str] = None,
        review_state: Optional[str] = None,
        ended_at: Optional[datetime] = None,
        source_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE public.journal_runs
                    SET strategy_name = COALESCE(:strategy_name, strategy_name),
                        entry_surface = COALESCE(:entry_surface, entry_surface),
                        status = COALESCE(:status, status),
                        review_state = COALESCE(:review_state, review_state),
                        ended_at = COALESCE(:ended_at, ended_at),
                        source_summary_json = CASE WHEN :source_summary_json IS NULL THEN source_summary_json ELSE CAST(:source_summary_json AS jsonb) END,
                        metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                        updated_at = NOW()
                    WHERE id = CAST(:run_id AS uuid)
                    """
                ),
                {
                    "run_id": run_id,
                    "strategy_name": strategy_name,
                    "entry_surface": entry_surface,
                    "status": status,
                    "review_state": review_state,
                    "ended_at": ended_at,
                    "source_summary_json": _json_dumps(source_summary) if source_summary is not None else None,
                    "metadata_json": _json_dumps(metadata) if metadata is not None else None,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_run(self, run_id: str) -> Optional[JournalRun]:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_runs WHERE id = CAST(:run_id AS uuid)"),
                {"run_id": run_id},
            ).mappings().first()
            return self._run_from_row(row) if row else None
        finally:
            db.close()

    def count_runs(self, *, strategy_family: Optional[str] = None, status: Optional[str] = None, review_state: Optional[str] = None) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public.journal_runs
                    WHERE (:strategy_family IS NULL OR strategy_family = :strategy_family)
                      AND (:status IS NULL OR status = :status)
                      AND (:review_state IS NULL OR review_state = :review_state)
                    """
                ),
                {
                    "strategy_family": strategy_family,
                    "status": status,
                    "review_state": review_state,
                },
            )
            return int(row.scalar_one())
        finally:
            db.close()

    def list_runs(self, *, strategy_family: Optional[str] = None, status: Optional[str] = None, review_state: Optional[str] = None, updated_after: Optional[datetime] = None, limit: int = 100, offset: int = 0) -> List[JournalRun]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_runs
                    WHERE (:strategy_family IS NULL OR strategy_family = :strategy_family)
                      AND (:status IS NULL OR status = :status)
                      AND (:review_state IS NULL OR review_state = :review_state)
                      AND (:updated_after IS NULL OR updated_at >= :updated_after)
                    ORDER BY started_at DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "strategy_family": strategy_family,
                    "status": status,
                    "review_state": review_state,
                    "updated_after": updated_after,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            return [self._run_from_row(row) for row in rows]
        finally:
            db.close()

    def list_run_legs(self, run_id: str) -> List[JournalRunLeg]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_run_legs
                    WHERE run_id = CAST(:run_id AS uuid)
                    ORDER BY id ASC
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            return [self._run_leg_from_row(row) for row in rows]
        finally:
            db.close()

    def list_source_links(self, run_id: str) -> List[JournalSourceLink]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_source_links
                    WHERE run_id = CAST(:run_id AS uuid)
                    ORDER BY linked_at DESC, id DESC
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            return [self._source_link_from_row(row) for row in rows]
        finally:
            db.close()

    def find_source_link(self, *, source_type: str, source_key: str, source_key_2: Optional[str] = None) -> Optional[JournalSourceLink]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_source_links
                    WHERE source_type = :source_type
                      AND source_key = :source_key
                      AND COALESCE(source_key_2, '') = COALESCE(:source_key_2, '')
                    LIMIT 1
                    """
                ),
                {
                    "source_type": source_type,
                    "source_key": source_key,
                    "source_key_2": source_key_2,
                },
            ).mappings().first()
            return self._source_link_from_row(row) if row else None
        finally:
            db.close()

    def list_execution_facts(self, run_id: str) -> List[JournalExecutionFact]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_facts
                    WHERE run_id = CAST(:run_id AS uuid)
                    ORDER BY fill_timestamp ASC, id ASC
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            return [self._execution_fact_from_row(row) for row in rows]
        finally:
            db.close()

    def list_decision_events(self, run_id: str) -> List[JournalDecisionEvent]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_decision_events
                    WHERE run_id = CAST(:run_id AS uuid)
                    ORDER BY occurred_at ASC, id ASC
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            return [self._decision_event_from_row(row) for row in rows]
        finally:
            db.close()

    def upsert_run_leg(self, run_id: str, leg: JournalRunLeg) -> int:
        db = self.session_factory()
        try:
            result = db.execute(
                text(
                    """
                    INSERT INTO public.journal_run_legs (
                        run_id,
                        instrument_token,
                        exchange,
                        tradingsymbol,
                        product,
                        leg_role,
                        direction,
                        opened_quantity,
                        closed_quantity,
                        net_quantity,
                        metadata_json
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        :instrument_token,
                        :exchange,
                        :tradingsymbol,
                        :product,
                        :leg_role,
                        :direction,
                        :opened_quantity,
                        :closed_quantity,
                        :net_quantity,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "run_id": run_id,
                    "instrument_token": leg.instrument_token,
                    "exchange": leg.exchange,
                    "tradingsymbol": leg.tradingsymbol,
                    "product": leg.product,
                    "leg_role": leg.leg_role,
                    "direction": leg.direction,
                    "opened_quantity": leg.opened_quantity,
                    "closed_quantity": leg.closed_quantity,
                    "net_quantity": leg.net_quantity,
                    "metadata_json": _json_dumps(leg.metadata),
                },
            )
            leg_id = int(result.scalar_one())
            db.commit()
            return leg_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def link_source(self, link: JournalSourceLink) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_source_links (
                        run_id,
                        source_type,
                        source_key,
                        source_key_2,
                        linked_at
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        :source_type,
                        :source_key,
                        :source_key_2,
                        :linked_at
                    )
                    ON CONFLICT (source_type, source_key, COALESCE(source_key_2, '')) DO UPDATE
                    SET run_id = EXCLUDED.run_id,
                        linked_at = EXCLUDED.linked_at
                    RETURNING id
                    """
                ),
                {
                    "run_id": link.run_id,
                    "source_type": link.source_type,
                    "source_key": link.source_key,
                    "source_key_2": link.source_key_2,
                    "linked_at": link.linked_at,
                },
            )
            link_id = int(row.scalar_one())
            db.commit()
            return link_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def insert_execution_fact(self, fact: JournalExecutionFact) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_execution_facts (
                        run_id,
                        leg_id,
                        source_type,
                        source_fact_key,
                        order_id,
                        trade_id,
                        fill_timestamp,
                        side,
                        quantity,
                        price,
                        gross_cash_flow,
                        fees_amount,
                        taxes_amount,
                        slippage_amount,
                        payload_json
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        :leg_id,
                        :source_type,
                        :source_fact_key,
                        :order_id,
                        :trade_id,
                        :fill_timestamp,
                        :side,
                        :quantity,
                        :price,
                        :gross_cash_flow,
                        :fees_amount,
                        :taxes_amount,
                        :slippage_amount,
                        CAST(:payload_json AS jsonb)
                    )
                    ON CONFLICT (source_type, source_fact_key) DO UPDATE
                    SET run_id = EXCLUDED.run_id,
                        leg_id = EXCLUDED.leg_id,
                        order_id = EXCLUDED.order_id,
                        trade_id = EXCLUDED.trade_id,
                        fill_timestamp = EXCLUDED.fill_timestamp,
                        side = EXCLUDED.side,
                        quantity = EXCLUDED.quantity,
                        price = EXCLUDED.price,
                        gross_cash_flow = EXCLUDED.gross_cash_flow,
                        fees_amount = EXCLUDED.fees_amount,
                        taxes_amount = EXCLUDED.taxes_amount,
                        slippage_amount = EXCLUDED.slippage_amount,
                        payload_json = EXCLUDED.payload_json
                    RETURNING id
                    """
                ),
                {
                    "run_id": fact.run_id,
                    "leg_id": fact.leg_id,
                    "source_type": fact.source_type,
                    "source_fact_key": fact.source_fact_key,
                    "order_id": fact.order_id,
                    "trade_id": fact.trade_id,
                    "fill_timestamp": fact.fill_timestamp,
                    "side": fact.side,
                    "quantity": fact.quantity,
                    "price": fact.price,
                    "gross_cash_flow": fact.gross_cash_flow,
                    "fees_amount": fact.fees_amount,
                    "taxes_amount": fact.taxes_amount,
                    "slippage_amount": fact.slippage_amount,
                    "payload_json": _json_dumps(fact.payload),
                },
            )
            fact_id = int(row.scalar_one())
            db.commit()
            return fact_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def append_decision_event(self, event: JournalDecisionEvent) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_decision_events (
                        run_id,
                        decision_type,
                        actor_type,
                        occurred_at,
                        summary,
                        context_json
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        :decision_type,
                        :actor_type,
                        :occurred_at,
                        :summary,
                        CAST(:context_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "run_id": event.run_id,
                    "decision_type": event.decision_type,
                    "actor_type": event.actor_type,
                    "occurred_at": event.occurred_at,
                    "summary": event.summary,
                    "context_json": _json_dumps(event.context),
                },
            )
            event_id = int(row.scalar_one())
            db.commit()
            return event_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_rule(self, rule: JournalRule) -> str:
        db = self.session_factory()
        try:
            if rule.id:
                db.execute(
                    text(
                        """
                        UPDATE public.journal_rules
                        SET family_scope = :family_scope,
                            strategy_scope = :strategy_scope,
                            title = :title,
                            rule_type = :rule_type,
                            enforcement_level = :enforcement_level,
                            status = :status,
                            version = :version,
                            description = :description,
                            metadata_json = CAST(:metadata_json AS jsonb),
                            updated_at = NOW()
                        WHERE id = CAST(:rule_id AS uuid)
                        """
                    ),
                    {
                        "rule_id": rule.id,
                        "family_scope": rule.family_scope,
                        "strategy_scope": rule.strategy_scope,
                        "title": rule.title,
                        "rule_type": rule.rule_type,
                        "enforcement_level": rule.enforcement_level,
                        "status": rule.status,
                        "version": rule.version,
                        "description": rule.description,
                        "metadata_json": _json_dumps(rule.metadata),
                    },
                )
                db.commit()
                return rule.id

            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_rules (
                        family_scope,
                        strategy_scope,
                        title,
                        rule_type,
                        enforcement_level,
                        status,
                        version,
                        description,
                        metadata_json,
                        created_at
                    ) VALUES (
                        :family_scope,
                        :strategy_scope,
                        :title,
                        :rule_type,
                        :enforcement_level,
                        :status,
                        :version,
                        :description,
                        CAST(:metadata_json AS jsonb),
                        :created_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "family_scope": rule.family_scope,
                    "strategy_scope": rule.strategy_scope,
                    "title": rule.title,
                    "rule_type": rule.rule_type,
                    "enforcement_level": rule.enforcement_level,
                    "status": rule.status,
                    "version": rule.version,
                    "description": rule.description,
                    "metadata_json": _json_dumps(rule.metadata),
                    "created_at": rule.created_at,
                },
            )
            rule_id = str(row.scalar_one())
            db.commit()
            return rule_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_rule(self, rule_id: str) -> Optional[JournalRule]:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_rules WHERE id = CAST(:rule_id AS uuid)"),
                {"rule_id": rule_id},
            ).mappings().first()
            return self._rule_from_row(row) if row else None
        finally:
            db.close()

    def list_rules(
        self,
        *,
        family_scope: Optional[str] = None,
        strategy_scope: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[JournalRule]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_rules
                    WHERE (:family_scope IS NULL OR family_scope = :family_scope)
                      AND (:strategy_scope IS NULL OR strategy_scope = :strategy_scope)
                      AND (:status IS NULL OR status = :status)
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "family_scope": family_scope,
                    "strategy_scope": strategy_scope,
                    "status": status,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [self._rule_from_row(row) for row in rows]
        finally:
            db.close()

    def count_trade_rows(
        self,
        *,
        run_id: Optional[str] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public.journal_execution_facts jef
                    INNER JOIN public.journal_runs jr ON jr.id = jef.run_id
                    WHERE (:run_id IS NULL OR jef.run_id = CAST(:run_id AS uuid))
                      AND (:strategy_family IS NULL OR jr.strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR jr.execution_mode = :execution_mode)
                      AND (:source_type IS NULL OR jef.source_type = :source_type)
                    """
                ),
                {
                    "run_id": run_id,
                    "strategy_family": strategy_family,
                    "execution_mode": execution_mode,
                    "source_type": source_type,
                },
            )
            return int(row.scalar_one())
        finally:
            db.close()

    def list_trade_rows(
        self,
        *,
        run_id: Optional[str] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        jef.id,
                        jef.run_id,
                        jr.strategy_family,
                        jr.strategy_name,
                        jr.entry_surface,
                        jr.execution_mode,
                        jr.status AS run_status,
                        COALESCE(jrl.tradingsymbol, jef.payload_json ->> 'tradingsymbol', jr.strategy_name) AS tradingsymbol,
                        jef.source_type,
                        jef.source_fact_key,
                        jef.order_id,
                        jef.trade_id,
                        jef.fill_timestamp,
                        jef.side,
                        jef.quantity,
                        jef.price,
                        jef.gross_cash_flow,
                        jef.fees_amount,
                        jef.taxes_amount,
                        jef.slippage_amount,
                        jef.payload_json
                    FROM public.journal_execution_facts jef
                    INNER JOIN public.journal_runs jr
                      ON jr.id = jef.run_id
                    LEFT JOIN public.journal_run_legs jrl
                      ON jrl.id = jef.leg_id
                    WHERE (:run_id IS NULL OR jef.run_id = CAST(:run_id AS uuid))
                      AND (:strategy_family IS NULL OR jr.strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR jr.execution_mode = :execution_mode)
                      AND (:source_type IS NULL OR jef.source_type = :source_type)
                    ORDER BY jef.fill_timestamp DESC, jef.id DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "run_id": run_id,
                    "strategy_family": strategy_family,
                    "execution_mode": execution_mode,
                    "source_type": source_type,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def list_strategy_rollups(
        self,
        *,
        strategy_family: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        jr.strategy_family,
                        COALESCE(jr.strategy_name, 'Unspecified') AS strategy_name,
                        COUNT(*) AS run_count,
                        COUNT(*) FILTER (WHERE jr.status = 'open') AS open_run_count,
                        COUNT(*) FILTER (WHERE jr.status = 'closed') AS closed_run_count,
                        COUNT(*) FILTER (WHERE jr.review_state IN ('pending', 'in_progress')) AS review_backlog_count,
                        MAX(jr.started_at) AS latest_started_at,
                        SUM(COALESCE(jms.metrics_json ->> 'net_pnl', '0')::numeric) AS net_pnl,
                        SUM(COALESCE(jms.metrics_json ->> 'total_fees', '0')::numeric) AS total_fees
                    FROM public.journal_runs jr
                    LEFT JOIN public.journal_metric_snapshots jms
                      ON jms.subject_type = 'run'
                     AND jms.subject_id = CAST(jr.id AS text)
                     AND jms.window = 'since_inception'
                    WHERE (:strategy_family IS NULL OR jr.strategy_family = :strategy_family)
                    GROUP BY jr.strategy_family, COALESCE(jr.strategy_name, 'Unspecified')
                    ORDER BY latest_started_at DESC NULLS LAST
                    LIMIT :limit
                    """
                ),
                {
                    "strategy_family": strategy_family,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def list_review_queue_rows(
        self,
        *,
        limit: int = 100,
        review_state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        jr.id,
                        jr.strategy_family,
                        jr.strategy_name,
                        jr.entry_surface,
                        jr.execution_mode,
                        jr.status,
                        jr.review_state,
                        jr.started_at,
                        jr.ended_at,
                        COUNT(DISTINCT jef.id) AS execution_fact_count,
                        COUNT(DISTINCT jde.id) AS decision_event_count,
                        COUNT(DISTINCT jsl.id) AS source_link_count,
                        COALESCE(jms.metrics_json ->> 'net_pnl', NULL) AS net_pnl
                    FROM public.journal_runs jr
                    LEFT JOIN public.journal_execution_facts jef ON jef.run_id = jr.id
                    LEFT JOIN public.journal_decision_events jde ON jde.run_id = jr.id
                    LEFT JOIN public.journal_source_links jsl ON jsl.run_id = jr.id
                    LEFT JOIN public.journal_metric_snapshots jms
                      ON jms.subject_type = 'run'
                     AND jms.subject_id = CAST(jr.id AS text)
                     AND jms.window = 'since_inception'
                    WHERE jr.review_state IN ('pending', 'in_progress')
                      AND (:review_state IS NULL OR jr.review_state = :review_state)
                    GROUP BY jr.id, jms.metrics_json
                    ORDER BY COALESCE(jr.ended_at, jr.started_at) DESC
                    LIMIT :limit
                    """
                ),
                {
                    "review_state": review_state,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def list_calendar_summary_rows(
        self,
        *,
        start_day: Optional[date] = None,
        end_day: Optional[date] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        limit: int = 366,
    ) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        DATE(jef.fill_timestamp) AS trading_day,
                        COUNT(*) AS trade_count,
                        COUNT(DISTINCT jef.run_id) AS run_count,
                        SUM(COALESCE(jef.gross_cash_flow, 0)) AS realized_pnl,
                        SUM(COALESCE(jef.fees_amount, 0) + COALESCE(jef.taxes_amount, 0) + COALESCE(jef.slippage_amount, 0)) AS total_fees,
                        SUM(CASE WHEN COALESCE(jef.gross_cash_flow, 0) > 0 THEN 1 ELSE 0 END) AS winning_trade_count,
                        SUM(CASE WHEN COALESCE(jef.gross_cash_flow, 0) < 0 THEN 1 ELSE 0 END) AS losing_trade_count
                    FROM public.journal_execution_facts jef
                    INNER JOIN public.journal_runs jr ON jr.id = jef.run_id
                    WHERE (:start_day IS NULL OR DATE(jef.fill_timestamp) >= :start_day)
                      AND (:end_day IS NULL OR DATE(jef.fill_timestamp) <= :end_day)
                      AND (:strategy_family IS NULL OR jr.strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR jr.execution_mode = :execution_mode)
                    GROUP BY DATE(jef.fill_timestamp)
                    ORDER BY trading_day DESC
                    LIMIT :limit
                    """
                ),
                {
                    "start_day": start_day,
                    "end_day": end_day,
                    "strategy_family": strategy_family,
                    "execution_mode": execution_mode,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def append_rule_evidence(self, evidence: JournalRuleEvidence) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_rule_evidence (
                        run_id,
                        rule_id,
                        result,
                        notes,
                        evidence_json,
                        created_at
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        CAST(:rule_id AS uuid),
                        :result,
                        :notes,
                        CAST(:evidence_json AS jsonb),
                        :created_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "run_id": evidence.run_id,
                    "rule_id": evidence.rule_id,
                    "result": evidence.result,
                    "notes": evidence.notes,
                    "evidence_json": _json_dumps(evidence.evidence),
                    "created_at": evidence.created_at,
                },
            )
            evidence_id = int(row.scalar_one())
            db.commit()
            return evidence_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_equity_point(self, point: JournalEquityPoint) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_equity_points (
                        subject_type,
                        subject_id,
                        interval,
                        as_of,
                        starting_equity,
                        ending_equity,
                        realized_pnl,
                        unrealized_pnl,
                        cash_flow,
                        fees,
                        return_pct,
                        benchmark_return_pct,
                        excess_return_pct
                    ) VALUES (
                        :subject_type,
                        :subject_id,
                        :interval,
                        :as_of,
                        :starting_equity,
                        :ending_equity,
                        :realized_pnl,
                        :unrealized_pnl,
                        :cash_flow,
                        :fees,
                        :return_pct,
                        :benchmark_return_pct,
                        :excess_return_pct
                    )
                    ON CONFLICT (subject_type, subject_id, interval, as_of) DO UPDATE
                    SET starting_equity = EXCLUDED.starting_equity,
                        ending_equity = EXCLUDED.ending_equity,
                        realized_pnl = EXCLUDED.realized_pnl,
                        unrealized_pnl = EXCLUDED.unrealized_pnl,
                        cash_flow = EXCLUDED.cash_flow,
                        fees = EXCLUDED.fees,
                        return_pct = EXCLUDED.return_pct,
                        benchmark_return_pct = EXCLUDED.benchmark_return_pct,
                        excess_return_pct = EXCLUDED.excess_return_pct
                    RETURNING id
                    """
                ),
                point.model_dump(mode="python"),
            )
            point_id = int(row.scalar_one())
            db.commit()
            return point_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def replace_metric_snapshot(self, snapshot: JournalMetricSnapshot) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_metric_snapshots (
                        subject_type,
                        subject_id,
                        window,
                        calc_version,
                        computed_at,
                        metrics_json
                    ) VALUES (
                        :subject_type,
                        :subject_id,
                        :window,
                        :calc_version,
                        :computed_at,
                        CAST(:metrics_json AS jsonb)
                    )
                    ON CONFLICT (subject_type, subject_id, window, calc_version) DO UPDATE
                    SET computed_at = EXCLUDED.computed_at,
                        metrics_json = EXCLUDED.metrics_json
                    RETURNING id
                    """
                ),
                {
                    "subject_type": snapshot.subject_type,
                    "subject_id": snapshot.subject_id,
                    "window": snapshot.window,
                    "calc_version": snapshot.calc_version,
                    "computed_at": snapshot.computed_at,
                    "metrics_json": _json_dumps(snapshot.metrics),
                },
            )
            snapshot_id = int(row.scalar_one())
            db.commit()
            return snapshot_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_benchmark_definition(self, definition: BenchmarkDefinition) -> str:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO public.benchmark_definitions (
                        benchmark_id,
                        name,
                        source_list,
                        instrument_token,
                        metadata_json,
                        updated_at
                    ) VALUES (
                        :benchmark_id,
                        :name,
                        :source_list,
                        :instrument_token,
                        CAST(:metadata_json AS jsonb),
                        NOW()
                    )
                    ON CONFLICT (benchmark_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        source_list = EXCLUDED.source_list,
                        instrument_token = EXCLUDED.instrument_token,
                        metadata_json = EXCLUDED.metadata_json,
                        updated_at = NOW()
                    """
                ),
                {
                    "benchmark_id": definition.benchmark_id,
                    "name": definition.name,
                    "source_list": definition.source_list,
                    "instrument_token": definition.instrument_token,
                    "metadata_json": _json_dumps(definition.metadata),
                },
            )
            db.commit()
            return definition.benchmark_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_benchmark_daily_price(self, price: BenchmarkDailyPrice) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO public.benchmark_daily_prices (
                        benchmark_id,
                        trading_day,
                        open,
                        high,
                        low,
                        close,
                        daily_return,
                        source
                    ) VALUES (
                        :benchmark_id,
                        :trading_day,
                        :open,
                        :high,
                        :low,
                        :close,
                        :daily_return,
                        :source
                    )
                    ON CONFLICT (benchmark_id, trading_day) DO UPDATE
                    SET open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        daily_return = EXCLUDED.daily_return,
                        source = EXCLUDED.source
                    """
                ),
                price.model_dump(mode="python"),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def set_projection_state(self, state: ProjectionState) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO public.journal_projection_state (
                        projector_name,
                        cursor_json,
                        updated_at
                    ) VALUES (
                        :projector_name,
                        CAST(:cursor_json AS jsonb),
                        :updated_at
                    )
                    ON CONFLICT (projector_name) DO UPDATE
                    SET cursor_json = EXCLUDED.cursor_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "projector_name": state.projector_name,
                    "cursor_json": _json_dumps(state.cursor),
                    "updated_at": state.updated_at,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_projection_state(self, projector_name: str) -> Optional[ProjectionState]:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_projection_state WHERE projector_name = :projector_name"),
                {"projector_name": projector_name},
            ).mappings().first()
            if not row:
                return None
            payload = _row_mapping(row)
            return ProjectionState(
                projector_name=payload["projector_name"],
                cursor=_decode_json_field(payload.get("cursor_json")) or {},
                updated_at=payload.get("updated_at") or datetime.utcnow(),
            )
        finally:
            db.close()

    def list_equity_points(self, *, subject_type: str, subject_id: str, interval: Optional[str] = None) -> List[JournalEquityPoint]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_equity_points
                    WHERE subject_type = :subject_type
                      AND subject_id = :subject_id
                      AND (:interval IS NULL OR interval = :interval)
                    ORDER BY as_of ASC, id ASC
                    """
                ),
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "interval": interval,
                },
            ).mappings().all()
            return [self._equity_point_from_row(row) for row in rows]
        finally:
            db.close()

    def delete_equity_points(self, *, subject_type: str, subject_id: str, interval: Optional[str] = None) -> None:
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    DELETE FROM public.journal_equity_points
                    WHERE subject_type = :subject_type
                      AND subject_id = :subject_id
                      AND (:interval IS NULL OR interval = :interval)
                    """
                ),
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "interval": interval,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_latest_metric_snapshot(self, *, subject_type: str, subject_id: str, window: Optional[str] = None, calc_version: Optional[str] = None) -> Optional[JournalMetricSnapshot]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_metric_snapshots
                    WHERE subject_type = :subject_type
                      AND subject_id = :subject_id
                      AND (:window IS NULL OR window = :window)
                      AND (:calc_version IS NULL OR calc_version = :calc_version)
                    ORDER BY computed_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "window": window,
                    "calc_version": calc_version,
                },
            ).mappings().first()
            return self._metric_snapshot_from_row(row) if row else None
        finally:
            db.close()

    def get_benchmark_definition(self, benchmark_id: str) -> Optional[BenchmarkDefinition]:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.benchmark_definitions WHERE benchmark_id = :benchmark_id"),
                {"benchmark_id": benchmark_id},
            ).mappings().first()
            return self._benchmark_definition_from_row(row) if row else None
        finally:
            db.close()

    def list_benchmark_prices(self, benchmark_id: str, *, start_day: Optional[date] = None, end_day: Optional[date] = None) -> List[BenchmarkDailyPrice]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.benchmark_daily_prices
                    WHERE benchmark_id = :benchmark_id
                      AND (:start_day IS NULL OR trading_day >= :start_day)
                      AND (:end_day IS NULL OR trading_day <= :end_day)
                    ORDER BY trading_day ASC
                    """
                ),
                {
                    "benchmark_id": benchmark_id,
                    "start_day": start_day,
                    "end_day": end_day,
                },
            ).mappings().all()
            return [self._benchmark_daily_price_from_row(row) for row in rows]
        finally:
            db.close()

    def _run_from_row(self, row: Any) -> JournalRun:
        payload = _row_mapping(row)
        return JournalRun(
            id=str(payload.get("id")),
            strategy_family=payload.get("strategy_family"),
            strategy_name=payload.get("strategy_name"),
            entry_surface=payload.get("entry_surface"),
            execution_mode=payload.get("execution_mode"),
            account_ref=payload.get("account_ref"),
            status=payload.get("status"),
            benchmark_id=payload.get("benchmark_id") or "NIFTY50",
            capital_basis_type=payload.get("capital_basis_type"),
            capital_committed=payload.get("capital_committed"),
            started_at=payload.get("started_at") or datetime.utcnow(),
            ended_at=payload.get("ended_at"),
            review_state=payload.get("review_state") or "pending",
            source_summary=_decode_json_field(payload.get("source_summary_json")) or {},
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _run_leg_from_row(self, row: Any) -> JournalRunLeg:
        payload = _row_mapping(row)
        return JournalRunLeg(
            id=payload.get("id"),
            run_id=str(payload.get("run_id")) if payload.get("run_id") is not None else None,
            instrument_token=payload.get("instrument_token"),
            exchange=payload.get("exchange"),
            tradingsymbol=payload.get("tradingsymbol"),
            product=payload.get("product"),
            leg_role=payload.get("leg_role"),
            direction=payload.get("direction"),
            opened_quantity=payload.get("opened_quantity") or 0,
            closed_quantity=payload.get("closed_quantity") or 0,
            net_quantity=payload.get("net_quantity") or 0,
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _source_link_from_row(self, row: Any) -> JournalSourceLink:
        payload = _row_mapping(row)
        return JournalSourceLink(
            id=payload.get("id"),
            run_id=str(payload.get("run_id")),
            source_type=payload.get("source_type"),
            source_key=payload.get("source_key"),
            source_key_2=payload.get("source_key_2"),
            linked_at=payload.get("linked_at") or datetime.utcnow(),
        )

    def _execution_fact_from_row(self, row: Any) -> JournalExecutionFact:
        payload = _row_mapping(row)
        return JournalExecutionFact(
            id=payload.get("id"),
            run_id=str(payload.get("run_id")),
            leg_id=payload.get("leg_id"),
            source_type=payload.get("source_type"),
            source_fact_key=payload.get("source_fact_key"),
            order_id=payload.get("order_id"),
            trade_id=payload.get("trade_id"),
            fill_timestamp=payload.get("fill_timestamp") or datetime.utcnow(),
            side=payload.get("side"),
            quantity=payload.get("quantity"),
            price=payload.get("price"),
            gross_cash_flow=payload.get("gross_cash_flow"),
            fees_amount=payload.get("fees_amount") or Decimal("0"),
            taxes_amount=payload.get("taxes_amount") or Decimal("0"),
            slippage_amount=payload.get("slippage_amount") or Decimal("0"),
            payload=_decode_json_field(payload.get("payload_json")) or {},
        )

    def _decision_event_from_row(self, row: Any) -> JournalDecisionEvent:
        payload = _row_mapping(row)
        return JournalDecisionEvent(
            id=payload.get("id"),
            run_id=str(payload.get("run_id")),
            decision_type=payload.get("decision_type"),
            actor_type=payload.get("actor_type"),
            occurred_at=payload.get("occurred_at") or datetime.utcnow(),
            summary=payload.get("summary"),
            context=_decode_json_field(payload.get("context_json")) or {},
        )

    def _equity_point_from_row(self, row: Any) -> JournalEquityPoint:
        payload = _row_mapping(row)
        return JournalEquityPoint(
            id=payload.get("id"),
            subject_type=payload.get("subject_type"),
            subject_id=payload.get("subject_id"),
            interval=payload.get("interval"),
            as_of=payload.get("as_of") or datetime.utcnow(),
            starting_equity=payload.get("starting_equity"),
            ending_equity=payload.get("ending_equity"),
            realized_pnl=payload.get("realized_pnl") or Decimal("0"),
            unrealized_pnl=payload.get("unrealized_pnl") or Decimal("0"),
            cash_flow=payload.get("cash_flow") or Decimal("0"),
            fees=payload.get("fees") or Decimal("0"),
            return_pct=payload.get("return_pct"),
            benchmark_return_pct=payload.get("benchmark_return_pct"),
            excess_return_pct=payload.get("excess_return_pct"),
        )

    def _metric_snapshot_from_row(self, row: Any) -> JournalMetricSnapshot:
        payload = _row_mapping(row)
        return JournalMetricSnapshot(
            id=payload.get("id"),
            subject_type=payload.get("subject_type"),
            subject_id=payload.get("subject_id"),
            window=payload.get("window"),
            calc_version=payload.get("calc_version"),
            computed_at=payload.get("computed_at") or datetime.utcnow(),
            metrics=_decode_json_field(payload.get("metrics_json")) or {},
        )

    def _rule_from_row(self, row: Any) -> JournalRule:
        payload = _row_mapping(row)
        return JournalRule(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            family_scope=payload.get("family_scope"),
            strategy_scope=payload.get("strategy_scope"),
            title=payload.get("title"),
            rule_type=payload.get("rule_type"),
            enforcement_level=payload.get("enforcement_level"),
            status=payload.get("status"),
            version=payload.get("version") or 1,
            description=payload.get("description"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
        )

    def _benchmark_definition_from_row(self, row: Any) -> BenchmarkDefinition:
        payload = _row_mapping(row)
        return BenchmarkDefinition(
            benchmark_id=payload.get("benchmark_id"),
            name=payload.get("name"),
            source_list=payload.get("source_list") or "Nifty50",
            instrument_token=payload.get("instrument_token"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _benchmark_daily_price_from_row(self, row: Any) -> BenchmarkDailyPrice:
        payload = _row_mapping(row)
        return BenchmarkDailyPrice(
            benchmark_id=payload.get("benchmark_id"),
            trading_day=payload.get("trading_day"),
            open=payload.get("open"),
            high=payload.get("high"),
            low=payload.get("low"),
            close=payload.get("close"),
            daily_return=payload.get("daily_return"),
            source=payload.get("source"),
        )
