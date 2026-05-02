from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal

from .models import (
    BenchmarkDailyPrice,
    BenchmarkDefinition,
    JournalAttachment,
    JournalDecisionEvent,
    JournalEpisode,
    JournalEquityPoint,
    JournalExecutionContext,
    JournalExecutionEnvironment,
    JournalExecutionFact,
    JournalExecutionIntent,
    JournalMetricSnapshot,
    JournalNote,
    JournalNoteRevision,
    JournalRule,
    JournalRuleEvidence,
    JournalRun,
    JournalRunLeg,
    JournalStrategyDeployment,
    JournalStrategyTemplate,
    JournalStrategyVariant,
    JournalSourceLink,
    JournalTimelineEvent,
    ProjectionState,
)
from .v2.environment import resolve_environment_key


def _row_mapping(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
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


def _require_nonblank_str(name: str, value: str | None) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{name} is required")
    return cleaned


def _require_uuid_str(name: str, value: str | None) -> str:
    cleaned = _require_nonblank_str(name, value)
    try:
        UUID(cleaned)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc
    return cleaned


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

    def count_runs(self, *, strategy_family: Optional[str] = None, execution_mode: Optional[str] = None, status: Optional[str] = None, review_state: Optional[str] = None) -> int:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public.journal_runs
                    WHERE (:strategy_family IS NULL OR strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR execution_mode = :execution_mode)
                      AND (:status IS NULL OR status = :status)
                      AND (:review_state IS NULL OR review_state = :review_state)
                    """
                ),
                {
                    "strategy_family": strategy_family,
                    "execution_mode": execution_mode,
                    "status": status,
                    "review_state": review_state,
                },
            )
            return int(row.scalar_one())
        finally:
            db.close()

    def list_runs(self, *, strategy_family: Optional[str] = None, execution_mode: Optional[str] = None, status: Optional[str] = None, review_state: Optional[str] = None, updated_after: Optional[datetime] = None, limit: int = 100, offset: int = 0) -> List[JournalRun]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_runs
                    WHERE (:strategy_family IS NULL OR strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR execution_mode = :execution_mode)
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
                    "execution_mode": execution_mode,
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

    def list_v1_review_note_candidates(
        self,
        *,
        limit: int = 100,
        environment_mode: str | None = None,
        account_scope: str | None = None,
    ) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        id,
                        execution_mode,
                        account_ref,
                        metadata_json ->> 'review_notes' AS review_notes,
                        ended_at,
                        updated_at
                    FROM public.journal_runs
                    WHERE COALESCE(metadata_json ->> 'review_notes', '') <> ''
                      AND (:environment_mode IS NULL OR execution_mode = :environment_mode)
                      AND (:account_scope IS NULL OR account_ref = :account_scope)
                    ORDER BY COALESCE(ended_at, updated_at, started_at) ASC, id ASC
                    LIMIT :limit
                    """
                ),
                {
                    "environment_mode": environment_mode,
                    "account_scope": account_scope,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def list_strategy_templates_for_environment(self, *, environment_id: str) -> list[dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT DISTINCT
                        st.id AS template_id,
                        st.strategy_family,
                        st.template_key,
                        st.display_name
                    FROM public.journal_execution_contexts ctx
                    INNER JOIN public.journal_strategy_templates st ON st.id = ctx.strategy_template_id
                    WHERE ctx.environment_id = CAST(:environment_id AS uuid)
                    ORDER BY st.strategy_family ASC, st.template_key ASC
                    """
                ),
                {"environment_id": environment_id},
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
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

    def find_v2_execution_fact_by_source(self, *, source_type: str, source_fact_key: str) -> JournalExecutionFact | None:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_facts
                    WHERE source_type = :source_type
                      AND source_fact_key = :source_fact_key
                      AND environment_id IS NOT NULL
                    LIMIT 1
                    """
                ),
                {
                    "source_type": source_type,
                    "source_fact_key": source_fact_key,
                },
            ).mappings().first()
            return self._execution_fact_from_row(row) if row else None
        finally:
            db.close()

    def claim_v2_projection_source(self, *, source_type: str, source_fact_key: str) -> bool:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_v2_projection_claims (
                        source_type,
                        source_fact_key,
                        status
                    ) VALUES (
                        :source_type,
                        :source_fact_key,
                        'processing'
                    )
                    ON CONFLICT (source_type, source_fact_key) DO UPDATE
                    SET status = 'processing',
                        updated_at = NOW()
                    WHERE public.journal_v2_projection_claims.status = 'failed'
                       OR public.journal_v2_projection_claims.updated_at < NOW() - INTERVAL '5 minutes'
                    RETURNING source_fact_key
                    """
                ),
                {
                    "source_type": source_type,
                    "source_fact_key": source_fact_key,
                },
            ).mappings().first()
            db.commit()
            return row is not None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_v2_projection_source(self, *, source_type: str, source_fact_key: str, status: str) -> None:
        normalized_status = str(status or "").strip()
        if normalized_status not in {"processing", "projected", "failed"}:
            raise ValueError("projection claim status must be processing, projected, or failed")
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE public.journal_v2_projection_claims
                    SET status = :status,
                        updated_at = NOW()
                    WHERE source_type = :source_type
                      AND source_fact_key = :source_fact_key
                    """
                ),
                {
                    "source_type": source_type,
                    "source_fact_key": source_fact_key,
                    "status": normalized_status,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_unprojected_live_fills(self, *, batch_size: int = 100) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT otf.*
                    FROM public.order_trade_fills otf
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.journal_execution_facts jef
                        WHERE jef.source_type IN ('live_fill', 'broker_import')
                          AND jef.source_fact_key = otf.account_id || ':' || otf.trade_id
                    )
                    ORDER BY otf.fill_timestamp ASC, otf.trade_id ASC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, int(batch_size))},
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def find_live_order_intent(self, *, account_id: str, order_id: str) -> Optional[Dict[str, Any]]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.live_order_intents
                    WHERE account_id = :account_id
                      AND broker_order_id = :order_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"account_id": account_id, "order_id": order_id},
            ).mappings().first()
            if not row:
                tag_row = db.execute(
                    text(
                        """
                        SELECT payload_json ->> 'tag' AS client_order_ref
                        FROM public.canonical_order_events
                        WHERE account_id = :account_id
                          AND order_id = :order_id
                          AND payload_json ? 'tag'
                        ORDER BY event_timestamp DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"account_id": account_id, "order_id": order_id},
                ).fetchone()
                client_order_ref = str(tag_row[0]).strip() if tag_row and tag_row[0] else ""
                if not client_order_ref:
                    return None
                row = db.execute(
                    text(
                        """
                        UPDATE public.live_order_intents
                        SET broker_order_id = COALESCE(broker_order_id, :order_id),
                            status = CASE WHEN status = 'pending' THEN 'placed' ELSE status END,
                            updated_at = NOW()
                        WHERE account_id = :account_id
                          AND client_order_ref = :client_order_ref
                        RETURNING *
                        """
                    ),
                    {"account_id": account_id, "order_id": order_id, "client_order_ref": client_order_ref},
                ).mappings().first()
                if not row:
                    db.rollback()
                    return None
                db.commit()
            payload = _row_mapping(row)
            payload["attribution_json"] = _decode_json_field(payload.get("attribution_json")) or {}
            payload["cost_contract_json"] = _decode_json_field(payload.get("cost_contract_json")) or {}
            payload["error_json"] = _decode_json_field(payload.get("error_json")) or {}
            return payload
        finally:
            db.close()

    def ensure_live_strategy_run_for_intent(self, *, intent: Dict[str, Any]) -> str:
        journal_run_id = intent.get("journal_run_id")
        if journal_run_id:
            return str(journal_run_id)

        account_id = str(intent.get("account_id") or intent.get("account_ref") or "")
        strategy_run_id = str(intent.get("strategy_run_id") or "").strip()
        if not account_id or not strategy_run_id:
            raise ValueError("live order intent requires account_id and strategy_run_id")

        db = self.session_factory()
        try:
            existing_link = db.execute(
                text(
                    """
                    SELECT run_id
                    FROM public.journal_source_links
                    WHERE source_type = 'live_order'
                      AND source_key = :strategy_run_id
                    ORDER BY linked_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()
            if existing_link:
                run_id = str(existing_link[0])
            else:
                created = db.execute(
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
                            review_state,
                            source_summary_json,
                            metadata_json
                        ) VALUES (
                            :strategy_family,
                            :strategy_name,
                            :entry_surface,
                            :execution_mode,
                            :account_ref,
                            'open',
                            'NIFTY50',
                            'margin_used',
                            'pending',
                            CAST(:source_summary_json AS jsonb),
                            CAST(:metadata_json AS jsonb)
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "strategy_family": intent.get("strategy_family") or "discretionary_strategy",
                        "strategy_name": intent.get("strategy_name") or strategy_run_id,
                        "entry_surface": intent.get("entry_surface") or "live_order",
                        "execution_mode": intent.get("execution_mode") or "live",
                        "account_ref": account_id,
                        "source_summary_json": _json_dumps({"source": "live_order_intent", "strategy_run_id": strategy_run_id}),
                        "metadata_json": _json_dumps({"created_by": "live_fill_projector", "live_order_intent": intent}),
                    },
                ).fetchone()
                if not created:
                    raise RuntimeError("failed to create live strategy journal run")
                run_id = str(created[0])
                db.execute(
                    text(
                        """
                        INSERT INTO public.journal_source_links (run_id, source_type, source_key, source_key_2, linked_at)
                        VALUES (CAST(:run_id AS uuid), 'live_order', :strategy_run_id, NULL, NOW())
                        ON CONFLICT (source_type, source_key, COALESCE(source_key_2, '')) DO UPDATE
                        SET run_id = EXCLUDED.run_id,
                            linked_at = EXCLUDED.linked_at
                        """
                    ),
                    {"run_id": run_id, "strategy_run_id": strategy_run_id},
                )

            client_order_ref = intent.get("client_order_ref")
            if client_order_ref:
                db.execute(
                    text(
                        """
                        UPDATE public.live_order_intents
                        SET journal_run_id = CAST(:run_id AS uuid),
                            updated_at = NOW()
                        WHERE client_order_ref = :client_order_ref
                          AND journal_run_id IS NULL
                        """
                    ),
                    {"run_id": run_id, "client_order_ref": client_order_ref},
                )
            db.commit()
            return run_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_imported_broker_run(self, *, account_id: str) -> str:
        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT id
                    FROM public.journal_runs
                    WHERE strategy_family = 'discretionary_strategy'
                      AND strategy_name = 'Imported Broker Activity'
                      AND execution_mode = 'live'
                      AND entry_surface = 'broker_import'
                      AND account_ref = :account_id
                      AND status = 'open'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ),
                {"account_id": account_id},
            ).fetchone()
            if existing:
                return str(existing[0])

            created = db.execute(
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
                        review_state,
                        source_summary_json,
                        metadata_json
                    ) VALUES (
                        'discretionary_strategy',
                        'Imported Broker Activity',
                        'broker_import',
                        'live',
                        :account_id,
                        'open',
                        'NIFTY50',
                        'notional',
                        'pending',
                        CAST(:source_summary_json AS jsonb),
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "account_id": account_id,
                    "source_summary_json": _json_dumps({"source": "broker_import"}),
                    "metadata_json": _json_dumps({"created_by": "live_fill_projector"}),
                },
            ).fetchone()
            if not created:
                raise RuntimeError("failed to create imported broker journal run")
            db.commit()
            return str(created[0])
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def find_open_live_runs_for_instrument(self, *, account_id: str, instrument_token: int, product: str) -> List[Dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        jr.id AS run_id,
                        SUM(CASE WHEN UPPER(jef.side) = 'BUY' THEN jef.quantity ELSE -jef.quantity END) AS net_quantity
                    FROM public.journal_runs jr
                    INNER JOIN public.journal_execution_facts jef ON jef.run_id = jr.id
                    WHERE jr.execution_mode = 'live'
                      AND jr.status = 'open'
                      AND jr.account_ref = :account_id
                      AND COALESCE(jef.payload_json -> 'broker_fill' ->> 'instrument_token', '') = :instrument_token
                      AND COALESCE(jef.payload_json -> 'broker_fill' ->> 'product', '') = :product
                      AND jef.source_type = 'live_fill'
                    GROUP BY jr.id
                    HAVING SUM(CASE WHEN UPPER(jef.side) = 'BUY' THEN jef.quantity ELSE -jef.quantity END) <> 0
                    """
                ),
                {
                    "account_id": account_id,
                    "instrument_token": str(int(instrument_token)),
                    "product": product,
                },
            ).mappings().all()
            return [_row_mapping(row) for row in rows]
        finally:
            db.close()

    def mark_run_externally_closed_if_flat(self, *, run_id: str) -> None:
        db = self.session_factory()
        try:
            net_row = db.execute(
                text(
                    """
                    SELECT COALESCE(SUM(CASE WHEN UPPER(side) = 'BUY' THEN quantity ELSE -quantity END), 0) AS net_quantity
                    FROM public.journal_execution_facts
                    WHERE run_id = CAST(:run_id AS uuid)
                      AND source_type = 'live_fill'
                    """
                ),
                {"run_id": run_id},
            ).fetchone()
            net_quantity = int(net_row[0] or 0) if net_row else 0
            if net_quantity != 0:
                return
            db.execute(
                text(
                    """
                    UPDATE public.journal_runs
                    SET status = 'closed',
                        review_state = 'pending',
                        ended_at = COALESCE(ended_at, NOW()),
                        metadata_json = COALESCE(metadata_json, '{}'::jsonb) || CAST(:external_exit_json AS jsonb),
                        updated_at = NOW()
                    WHERE id = CAST(:run_id AS uuid)
                    """
                ),
                {
                    "run_id": run_id,
                    "external_exit_json": _json_dumps(
                        {"external_exit": {"detected_at": datetime.utcnow().isoformat(), "source": "broker_reconcile"}}
                    ),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
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
                        environment_id,
                        episode_id,
                        intent_id,
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
                        position_effect,
                        payload_json
                    ) VALUES (
                        CAST(:run_id AS uuid),
                        CAST(:environment_id AS uuid),
                        CAST(:episode_id AS uuid),
                        CAST(:intent_id AS uuid),
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
                        :position_effect,
                        CAST(:payload_json AS jsonb)
                    )
                    ON CONFLICT (source_type, source_fact_key) DO UPDATE
                    SET run_id = EXCLUDED.run_id,
                        environment_id = COALESCE(EXCLUDED.environment_id, public.journal_execution_facts.environment_id),
                        episode_id = COALESCE(EXCLUDED.episode_id, public.journal_execution_facts.episode_id),
                        intent_id = COALESCE(EXCLUDED.intent_id, public.journal_execution_facts.intent_id),
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
                        position_effect = COALESCE(EXCLUDED.position_effect, public.journal_execution_facts.position_effect),
                        payload_json = EXCLUDED.payload_json
                    RETURNING id
                    """
                ),
                {
                    "run_id": fact.run_id,
                    "environment_id": fact.environment_id,
                    "episode_id": fact.episode_id,
                    "intent_id": fact.intent_id,
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
                    "position_effect": fact.position_effect,
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
        execution_mode: Optional[str] = None,
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
                      AND jms.time_window = 'since_inception'
                    WHERE (:strategy_family IS NULL OR jr.strategy_family = :strategy_family)
                      AND (:execution_mode IS NULL OR jr.execution_mode = :execution_mode)
                    GROUP BY jr.strategy_family, COALESCE(jr.strategy_name, 'Unspecified')
                    ORDER BY latest_started_at DESC NULLS LAST
                    LIMIT :limit
                    """
                ),
                {
                    "strategy_family": strategy_family,
                    "execution_mode": execution_mode,
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
                      AND jms.time_window = 'since_inception'
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
            if snapshot.environment_id is None:
                conflict_clause = """
                    ON CONFLICT (subject_type, subject_id, time_window, calc_version)
                    WHERE environment_id IS NULL
                    DO UPDATE
                    SET computed_at = EXCLUDED.computed_at,
                        metrics_json = EXCLUDED.metrics_json,
                        identity_rule_version = EXCLUDED.identity_rule_version,
                        grouping_rule_version = EXCLUDED.grouping_rule_version
                """
            else:
                conflict_clause = """
                    ON CONFLICT (
                        environment_id,
                        subject_type,
                        subject_id,
                        time_window,
                        calc_version,
                        identity_rule_version,
                        grouping_rule_version
                    )
                    WHERE environment_id IS NOT NULL
                    DO UPDATE
                    SET computed_at = EXCLUDED.computed_at,
                        metrics_json = EXCLUDED.metrics_json
                """

            row = db.execute(
                text(
                    f"""
                    INSERT INTO public.journal_metric_snapshots (
                        environment_id,
                        subject_type,
                        subject_id,
                        time_window,
                        calc_version,
                        identity_rule_version,
                        grouping_rule_version,
                        computed_at,
                        metrics_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        :subject_type,
                        :subject_id,
                        :window,
                        :calc_version,
                        :identity_rule_version,
                        :grouping_rule_version,
                        :computed_at,
                        CAST(:metrics_json AS jsonb)
                    )
                    {conflict_clause}
                    RETURNING id
                    """
                ),
                {
                    "environment_id": snapshot.environment_id,
                    "subject_type": snapshot.subject_type,
                    "subject_id": snapshot.subject_id,
                    "window": snapshot.window,
                    "calc_version": snapshot.calc_version,
                    "identity_rule_version": snapshot.identity_rule_version,
                    "grouping_rule_version": snapshot.grouping_rule_version,
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

    def ensure_execution_environment(
        self,
        *,
        mode: str,
        account_scope: str,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int = 1,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        resolved = resolve_environment_key(
            mode=mode,
            account_scope=account_scope,
            broker_user_id=broker_user_id,
            paper_account_key=paper_account_key,
            environment_epoch=environment_epoch,
            display_name=display_name,
            metadata=metadata,
        )
        normalized_mode = str(resolved.mode.value if hasattr(resolved.mode, "value") else resolved.mode)
        normalized_scope = resolved.account_scope
        broker_user_id = resolved.broker_user_id
        paper_account_key = resolved.paper_account_key
        environment_epoch = resolved.environment_epoch
        display_name = resolved.display_name
        metadata = resolved.metadata

        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_environments
                    WHERE mode = :mode
                      AND account_scope = :account_scope
                      AND COALESCE(broker_user_id, '') = COALESCE(:broker_user_id, '')
                      AND COALESCE(paper_account_key, '') = COALESCE(:paper_account_key, '')
                      AND environment_epoch = :environment_epoch
                    LIMIT 1
                    """
                ),
                {
                    "mode": normalized_mode,
                    "account_scope": normalized_scope,
                    "broker_user_id": broker_user_id,
                    "paper_account_key": paper_account_key,
                    "environment_epoch": int(environment_epoch),
                },
            ).mappings().first()

            if existing:
                payload = _row_mapping(existing)
                env_id = str(payload["id"])
                metadata_json: str | None = None
                if metadata is not None:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **metadata,
                    }
                    metadata_json = _json_dumps(merged_metadata)
                db.execute(
                    text(
                        """
                        UPDATE public.journal_execution_environments
                        SET display_name = COALESCE(:display_name, display_name),
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END
                        WHERE id = CAST(:environment_id AS uuid)
                        """
                    ),
                    {
                        "environment_id": env_id,
                        "display_name": display_name,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return env_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_execution_environments (
                        mode,
                        account_scope,
                        broker_user_id,
                        paper_account_key,
                        environment_epoch,
                        display_name,
                        metadata_json
                    ) VALUES (
                        :mode,
                        :account_scope,
                        :broker_user_id,
                        :paper_account_key,
                        :environment_epoch,
                        :display_name,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "mode": normalized_mode,
                    "account_scope": normalized_scope,
                    "broker_user_id": broker_user_id,
                    "paper_account_key": paper_account_key,
                    "environment_epoch": int(environment_epoch),
                    "display_name": display_name,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            environment_id = str(inserted.scalar_one())
            db.commit()
            return environment_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_strategy_template(
        self,
        *,
        template_key: str,
        strategy_family: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_template_key = str(template_key or "").strip()
        normalized_strategy_family = str(strategy_family or "").strip()
        if not normalized_template_key:
            raise ValueError("template_key is required")
        if not normalized_strategy_family:
            raise ValueError("strategy_family is required")

        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_templates
                    WHERE template_key = :template_key
                    LIMIT 1
                    """
                ),
                {"template_key": normalized_template_key},
            ).mappings().first()

            if existing:
                payload = _row_mapping(existing)
                template_id = str(payload["id"])
                metadata_json: str | None = None
                if metadata is not None:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **metadata,
                    }
                    metadata_json = _json_dumps(merged_metadata)

                db.execute(
                    text(
                        """
                        UPDATE public.journal_strategy_templates
                        SET strategy_family = :strategy_family,
                            display_name = COALESCE(:display_name, display_name),
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                            updated_at = NOW()
                        WHERE id = CAST(:template_id AS uuid)
                        """
                    ),
                    {
                        "template_id": template_id,
                        "strategy_family": normalized_strategy_family,
                        "display_name": display_name,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return template_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_strategy_templates (
                        strategy_family,
                        template_key,
                        display_name,
                        metadata_json
                    ) VALUES (
                        :strategy_family,
                        :template_key,
                        :display_name,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "strategy_family": normalized_strategy_family,
                    "template_key": normalized_template_key,
                    "display_name": display_name,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            template_id = str(inserted.scalar_one())
            db.commit()
            return template_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_strategy_variant(
        self,
        *,
        template_id: str,
        variant_key: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_variant_key = str(variant_key or "").strip()
        if not normalized_variant_key:
            raise ValueError("variant_key is required")

        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_variants
                    WHERE template_id = CAST(:template_id AS uuid)
                      AND variant_key = :variant_key
                    LIMIT 1
                    """
                ),
                {
                    "template_id": template_id,
                    "variant_key": normalized_variant_key,
                },
            ).mappings().first()

            if existing:
                payload = _row_mapping(existing)
                variant_id = str(payload["id"])
                metadata_json: str | None = None
                if metadata is not None:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **metadata,
                    }
                    metadata_json = _json_dumps(merged_metadata)

                db.execute(
                    text(
                        """
                        UPDATE public.journal_strategy_variants
                        SET display_name = COALESCE(:display_name, display_name),
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                            updated_at = NOW()
                        WHERE id = CAST(:variant_id AS uuid)
                        """
                    ),
                    {
                        "variant_id": variant_id,
                        "display_name": display_name,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return variant_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_strategy_variants (
                        template_id,
                        variant_key,
                        display_name,
                        metadata_json
                    ) VALUES (
                        CAST(:template_id AS uuid),
                        :variant_key,
                        :display_name,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "template_id": template_id,
                    "variant_key": normalized_variant_key,
                    "display_name": display_name,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            variant_id = str(inserted.scalar_one())
            db.commit()
            return variant_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_strategy_deployment(
        self,
        *,
        template_id: str,
        deployment_key: str,
        variant_id: str | None = None,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_deployment_key = str(deployment_key or "").strip()
        if not normalized_deployment_key:
            raise ValueError("deployment_key is required")

        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_deployments
                    WHERE template_id = CAST(:template_id AS uuid)
                      AND deployment_key = :deployment_key
                    LIMIT 1
                    """
                ),
                {
                    "template_id": template_id,
                    "deployment_key": normalized_deployment_key,
                },
            ).mappings().first()

            if existing:
                payload = _row_mapping(existing)
                deployment_id = str(payload["id"])
                metadata_json: str | None = None
                if metadata is not None:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **metadata,
                    }
                    metadata_json = _json_dumps(merged_metadata)

                db.execute(
                    text(
                        """
                        UPDATE public.journal_strategy_deployments
                        SET variant_id = COALESCE(CAST(:variant_id AS uuid), variant_id),
                            display_name = COALESCE(:display_name, display_name),
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                            updated_at = NOW()
                        WHERE id = CAST(:deployment_id AS uuid)
                        """
                    ),
                    {
                        "deployment_id": deployment_id,
                        "variant_id": variant_id,
                        "display_name": display_name,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return deployment_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_strategy_deployments (
                        template_id,
                        variant_id,
                        deployment_key,
                        display_name,
                        metadata_json
                    ) VALUES (
                        CAST(:template_id AS uuid),
                        CAST(:variant_id AS uuid),
                        :deployment_key,
                        :display_name,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "template_id": template_id,
                    "variant_id": variant_id,
                    "deployment_key": normalized_deployment_key,
                    "display_name": display_name,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            deployment_id = str(inserted.scalar_one())
            db.commit()
            return deployment_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_strategy_template(self, template_id: str) -> JournalStrategyTemplate | None:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_strategy_templates WHERE id = CAST(:template_id AS uuid)"),
                {"template_id": template_id},
            ).mappings().first()
            return self._strategy_template_from_row(row) if row else None
        finally:
            db.close()

    def list_strategy_templates(self, *, strategy_family: str | None = None) -> list[JournalStrategyTemplate]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_templates
                    WHERE (:strategy_family IS NULL OR strategy_family = :strategy_family)
                    ORDER BY strategy_family ASC, template_key ASC
                    """
                ),
                {"strategy_family": strategy_family},
            ).mappings().all()
            return [self._strategy_template_from_row(row) for row in rows]
        finally:
            db.close()

    def list_strategy_variants(self, *, template_id: str) -> list[JournalStrategyVariant]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_variants
                    WHERE template_id = CAST(:template_id AS uuid)
                    ORDER BY variant_key ASC
                    """
                ),
                {"template_id": template_id},
            ).mappings().all()
            return [self._strategy_variant_from_row(row) for row in rows]
        finally:
            db.close()

    def list_strategy_deployments(self, *, template_id: str) -> list[JournalStrategyDeployment]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_strategy_deployments
                    WHERE template_id = CAST(:template_id AS uuid)
                    ORDER BY deployment_key ASC
                    """
                ),
                {"template_id": template_id},
            ).mappings().all()
            return [self._strategy_deployment_from_row(row) for row in rows]
        finally:
            db.close()

    def get_execution_environment(self, environment_id: str) -> JournalExecutionEnvironment | None:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_execution_environments WHERE id = CAST(:environment_id AS uuid)"),
                {"environment_id": environment_id},
            ).mappings().first()
            return self._execution_environment_from_row(row) if row else None
        finally:
            db.close()

    def list_execution_environments(self, mode: str | None = None) -> list[JournalExecutionEnvironment]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_environments
                    WHERE (:mode IS NULL OR mode = :mode)
                    ORDER BY mode ASC, account_scope ASC, environment_epoch ASC
                    """
                ),
                {"mode": mode},
            ).mappings().all()
            return [self._execution_environment_from_row(row) for row in rows]
        finally:
            db.close()

    def ensure_execution_context(
        self,
        *,
        environment_id: str,
        source_system: str,
        external_run_id: str,
        template_id: str | None = None,
        variant_id: str | None = None,
        deployment_id: str | None = None,
        raw_identity: dict[str, Any] | None = None,
        resolved_identity: dict[str, Any] | None = None,
        resolution_method: str | None = None,
        resolution_confidence: Any | None = None,
        identity_rule_version: str = "journal_v2_identity_v1",
        status: str = "open",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_source_system = str(source_system or "").strip()
        normalized_external_run_id = str(external_run_id or "").strip()
        if not normalized_source_system:
            raise ValueError("source_system is required")
        if not normalized_external_run_id:
            raise ValueError("external_run_id is required")

        db = self.session_factory()
        try:
            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_contexts
                    WHERE environment_id = CAST(:environment_id AS uuid)
                      AND source_system = :source_system
                      AND external_run_id = :external_run_id
                    LIMIT 1
                    """
                ),
                {
                    "environment_id": environment_id,
                    "source_system": normalized_source_system,
                    "external_run_id": normalized_external_run_id,
                },
            ).mappings().first()

            identity_updates: dict[str, Any] = {}
            if raw_identity is not None:
                identity_updates["raw_identity"] = raw_identity
            if resolved_identity is not None:
                identity_updates["resolved_identity"] = resolved_identity
            if resolution_method is not None:
                identity_updates["resolution_method"] = resolution_method
            if resolution_confidence is not None:
                identity_updates["resolution_confidence"] = resolution_confidence
            if identity_updates and identity_rule_version:
                identity_updates["identity_rule_version"] = identity_rule_version

            if existing:
                payload = _row_mapping(existing)
                context_id = str(payload["id"])

                metadata_json: str | None = None
                if metadata is not None or identity_updates:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **(metadata or {}),
                        **identity_updates,
                    }
                    metadata_json = _json_dumps(merged_metadata)

                db.execute(
                    text(
                        """
                        UPDATE public.journal_execution_contexts
                        SET strategy_template_id = COALESCE(CAST(:template_id AS uuid), strategy_template_id),
                            strategy_variant_id = COALESCE(CAST(:variant_id AS uuid), strategy_variant_id),
                            strategy_deployment_id = COALESCE(CAST(:deployment_id AS uuid), strategy_deployment_id),
                            status = COALESCE(:status, status),
                            closed_at = CASE WHEN :ended_at IS NULL THEN closed_at ELSE :ended_at END,
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END
                        WHERE id = CAST(:context_id AS uuid)
                        """
                    ),
                    {
                        "context_id": context_id,
                        "template_id": template_id,
                        "variant_id": variant_id,
                        "deployment_id": deployment_id,
                        "status": status,
                        "ended_at": ended_at,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return context_id

            insert_metadata = {
                **(metadata or {}),
                **identity_updates,
            }
            if identity_rule_version and "identity_rule_version" not in insert_metadata:
                insert_metadata["identity_rule_version"] = identity_rule_version

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_execution_contexts (
                        environment_id,
                        source_system,
                        external_run_id,
                        strategy_template_id,
                        strategy_variant_id,
                        strategy_deployment_id,
                        status,
                        opened_at,
                        closed_at,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        :source_system,
                        :external_run_id,
                        CAST(:template_id AS uuid),
                        CAST(:variant_id AS uuid),
                        CAST(:deployment_id AS uuid),
                        :status,
                        COALESCE(:opened_at, NOW()),
                        :closed_at,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": environment_id,
                    "source_system": normalized_source_system,
                    "external_run_id": normalized_external_run_id,
                    "template_id": template_id,
                    "variant_id": variant_id,
                    "deployment_id": deployment_id,
                    "status": status,
                    "opened_at": started_at,
                    "closed_at": ended_at,
                    "metadata_json": _json_dumps(insert_metadata),
                },
            )
            context_id = str(inserted.scalar_one())
            db.commit()
            return context_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_execution_context(self, context_id: str) -> JournalExecutionContext | None:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_execution_contexts WHERE id = CAST(:context_id AS uuid)"),
                {"context_id": context_id},
            ).mappings().first()
            return self._execution_context_from_row(row) if row else None
        finally:
            db.close()

    def ensure_episode(
        self,
        *,
        environment_id: str,
        execution_context_id: str,
        episode_seq: int | None = None,
        status: str = "draft",
        opened_at: datetime | None = None,
        closed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        db = self.session_factory()
        try:
            resolved_episode_seq = episode_seq
            if resolved_episode_seq is None:
                max_row = db.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(episode_seq), 0) AS max_episode_seq
                        FROM public.journal_episodes
                        WHERE execution_context_id = CAST(:execution_context_id AS uuid)
                        """
                    ),
                    {"execution_context_id": execution_context_id},
                ).mappings().first()
                max_episode_seq = int((_row_mapping(max_row).get("max_episode_seq") if max_row else 0) or 0)
                resolved_episode_seq = max_episode_seq + 1

            if int(resolved_episode_seq) < 1:
                raise ValueError("episode_seq must be >= 1")

            existing = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_episodes
                    WHERE execution_context_id = CAST(:execution_context_id AS uuid)
                      AND episode_seq = :episode_seq
                    LIMIT 1
                    """
                ),
                {
                    "execution_context_id": execution_context_id,
                    "episode_seq": int(resolved_episode_seq),
                },
            ).mappings().first()

            if existing:
                payload = _row_mapping(existing)
                episode_id = str(payload["id"])
                metadata_json: str | None = None
                if metadata is not None:
                    merged_metadata = {
                        **(_decode_json_field(payload.get("metadata_json")) or {}),
                        **metadata,
                    }
                    metadata_json = _json_dumps(merged_metadata)

                db.execute(
                    text(
                        """
                        UPDATE public.journal_episodes
                        SET status = :status,
                            closed_at = CASE WHEN :closed_at IS NULL THEN closed_at ELSE :closed_at END,
                            metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                            updated_at = NOW()
                        WHERE id = CAST(:episode_id AS uuid)
                        """
                    ),
                    {
                        "episode_id": episode_id,
                        "status": status,
                        "closed_at": closed_at,
                        "metadata_json": metadata_json,
                    },
                )
                db.commit()
                return episode_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_episodes (
                        environment_id,
                        execution_context_id,
                        episode_seq,
                        status,
                        opened_at,
                        closed_at,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        CAST(:execution_context_id AS uuid),
                        :episode_seq,
                        :status,
                        COALESCE(:opened_at, NOW()),
                        :closed_at,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": environment_id,
                    "execution_context_id": execution_context_id,
                    "episode_seq": int(resolved_episode_seq),
                    "status": status,
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            episode_id = str(inserted.scalar_one())
            db.commit()
            return episode_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_episode_status(
        self,
        episode_id: str,
        *,
        status: str,
        closed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db = self.session_factory()
        try:
            metadata_json: str | None = None
            if metadata is not None:
                existing = db.execute(
                    text("SELECT metadata_json FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid)"),
                    {"episode_id": episode_id},
                ).mappings().first()
                existing_metadata = _decode_json_field(_row_mapping(existing).get("metadata_json")) if existing else None
                metadata_json = _json_dumps({**(existing_metadata or {}), **metadata})

            db.execute(
                text(
                    """
                    UPDATE public.journal_episodes
                    SET status = :status,
                        closed_at = CASE WHEN :closed_at IS NULL THEN closed_at ELSE :closed_at END,
                        metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                        updated_at = NOW()
                    WHERE id = CAST(:episode_id AS uuid)
                    """
                ),
                {
                    "episode_id": episode_id,
                    "status": status,
                    "closed_at": closed_at,
                    "metadata_json": metadata_json,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_episodes(
        self,
        *,
        environment_id: str | None = None,
        execution_context_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JournalEpisode]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_episodes
                    WHERE (:environment_id IS NULL OR environment_id = CAST(:environment_id AS uuid))
                      AND (:execution_context_id IS NULL OR execution_context_id = CAST(:execution_context_id AS uuid))
                      AND (:status IS NULL OR status = :status)
                    ORDER BY opened_at DESC, episode_seq DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "environment_id": environment_id,
                    "execution_context_id": execution_context_id,
                    "status": status,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            return [self._episode_from_row(row) for row in rows]
        finally:
            db.close()

    def get_episode_detail(self, episode_id: str) -> JournalEpisode | None:
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid)"),
                {"episode_id": episode_id},
            ).mappings().first()
            return self._episode_from_row(row) if row else None
        finally:
            db.close()

    def list_closed_episodes(self, *, environment_id: str, limit: int = 1000) -> list[JournalEpisode]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_episodes
                    WHERE environment_id = CAST(:environment_id AS uuid)
                      AND status = 'closed'
                    ORDER BY COALESCE(closed_at, opened_at) ASC, episode_seq ASC
                    LIMIT :limit
                    """
                ),
                {
                    "environment_id": environment_id,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [self._episode_from_row(row) for row in rows]
        finally:
            db.close()

    def list_closed_episodes_for_template_and_mode(
        self,
        *,
        template_id: str,
        mode: str,
        limit: int = 5000,
    ) -> list[JournalEpisode]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT ep.*
                    FROM public.journal_episodes ep
                    INNER JOIN public.journal_execution_contexts ctx ON ctx.id = ep.execution_context_id
                    INNER JOIN public.journal_execution_environments env ON env.id = ep.environment_id
                    WHERE ep.status = 'closed'
                      AND env.mode = :mode
                      AND (
                        ctx.strategy_template_id = CAST(:template_id AS uuid)
                        OR COALESCE(ctx.metadata_json ->> 'strategy_template_id', '') = :template_id
                      )
                    ORDER BY COALESCE(ep.closed_at, ep.opened_at) ASC, ep.episode_seq ASC
                    LIMIT :limit
                    """
                ),
                {
                    "template_id": template_id,
                    "mode": mode,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [self._episode_from_row(row) for row in rows]
        finally:
            db.close()

    def list_closed_episodes_for_environment_template(
        self,
        *,
        environment_id: str,
        template_id: str,
        limit: int = 5000,
    ) -> list[JournalEpisode]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT ep.*
                    FROM public.journal_episodes ep
                    INNER JOIN public.journal_execution_contexts ctx ON ctx.id = ep.execution_context_id
                    WHERE ep.environment_id = CAST(:environment_id AS uuid)
                      AND ep.status = 'closed'
                      AND (
                        ctx.strategy_template_id = CAST(:template_id AS uuid)
                        OR COALESCE(ctx.metadata_json ->> 'strategy_template_id', '') = :template_id
                      )
                    ORDER BY COALESCE(ep.closed_at, ep.opened_at) ASC, ep.episode_seq ASC
                    LIMIT :limit
                    """
                ),
                {
                    "environment_id": environment_id,
                    "template_id": template_id,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [self._episode_from_row(row) for row in rows]
        finally:
            db.close()

    def list_closed_episodes_for_environment_deployment(
        self,
        *,
        environment_id: str,
        deployment_id: str,
        limit: int = 5000,
    ) -> list[JournalEpisode]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT ep.*
                    FROM public.journal_episodes ep
                    INNER JOIN public.journal_execution_contexts ctx ON ctx.id = ep.execution_context_id
                    WHERE ep.environment_id = CAST(:environment_id AS uuid)
                      AND ep.status = 'closed'
                      AND ctx.strategy_deployment_id = CAST(:deployment_id AS uuid)
                    ORDER BY COALESCE(ep.closed_at, ep.opened_at) ASC, ep.episode_seq ASC
                    LIMIT :limit
                    """
                ),
                {
                    "environment_id": environment_id,
                    "deployment_id": deployment_id,
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
            return [self._episode_from_row(row) for row in rows]
        finally:
            db.close()

    def create_unresolved_item(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None,
        source_system: str,
        reason: str,
        raw_identity: dict[str, Any] | None = None,
        candidate_mappings: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_unresolved_queue (
                        environment_id,
                        execution_context_id,
                        source_system,
                        reason,
                        raw_identity_json,
                        candidate_mappings_json,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        CAST(:execution_context_id AS uuid),
                        :source_system,
                        :reason,
                        CAST(:raw_identity_json AS jsonb),
                        CAST(:candidate_mappings_json AS jsonb),
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": environment_id,
                    "execution_context_id": execution_context_id,
                    "source_system": source_system,
                    "reason": reason,
                    "raw_identity_json": _json_dumps(raw_identity or {}),
                    "candidate_mappings_json": _json_dumps(candidate_mappings or []),
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            unresolved_id = str(row.scalar_one())
            db.commit()
            return unresolved_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_unresolved_items(self, *, environment_id: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_unresolved_queue
                    WHERE environment_id = CAST(:environment_id AS uuid)
                      AND status = 'open'
                    ORDER BY created_at DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "environment_id": environment_id,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            items: list[dict[str, Any]] = []
            for row in rows:
                payload = _row_mapping(row)
                payload["raw_identity"] = _decode_json_field(payload.get("raw_identity_json")) or {}
                payload["candidate_mappings"] = _decode_json_field(payload.get("candidate_mappings_json")) or []
                payload["metadata"] = _decode_json_field(payload.get("metadata_json")) or {}
                payload.pop("raw_identity_json", None)
                payload.pop("candidate_mappings_json", None)
                payload.pop("metadata_json", None)
                items.append(payload)
            return items
        finally:
            db.close()

    def list_execution_facts_for_episode(self, episode_id: str) -> list[JournalExecutionFact]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_execution_facts
                    WHERE episode_id = CAST(:episode_id AS uuid)
                    ORDER BY fill_timestamp ASC, id ASC
                    """
                ),
                {"episode_id": episode_id},
            ).mappings().all()
            return [self._execution_fact_from_row(row) for row in rows]
        finally:
            db.close()

    def create_execution_intent(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None = None,
        episode_id: str | None = None,
        channel: str | None = None,
        intent_type: str | None = None,
        idempotency_key: str | None = None,
        status: str = "pending",
        requested_at: datetime | None = None,
        resolved_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_idempotency_key: str | None
        if idempotency_key is None:
            normalized_idempotency_key = None
        else:
            normalized_idempotency_key = str(idempotency_key).strip()
            if not normalized_idempotency_key:
                raise ValueError("idempotency_key cannot be blank")

        db = self.session_factory()
        try:
            if normalized_idempotency_key is not None:
                existing = db.execute(
                    text(
                        """
                        SELECT *
                        FROM public.journal_execution_intents
                        WHERE environment_id = CAST(:environment_id AS uuid)
                          AND idempotency_key = :idempotency_key
                        LIMIT 1
                        """
                    ),
                    {
                        "environment_id": environment_id,
                        "idempotency_key": normalized_idempotency_key,
                    },
                ).mappings().first()
                if existing:
                    payload_row = _row_mapping(existing)
                    intent_id = str(payload_row["id"])
                    merged_result = {
                        **(_decode_json_field(payload_row.get("result_json")) or {}),
                        **(result or {}),
                    }
                    metadata_json: str | None = None
                    if metadata is not None:
                        merged_metadata = {
                            **(_decode_json_field(payload_row.get("metadata_json")) or {}),
                            **metadata,
                        }
                        metadata_json = _json_dumps(merged_metadata)

                    db.execute(
                        text(
                            """
                            UPDATE public.journal_execution_intents
                            SET status = :status,
                                resolved_at = CASE WHEN :resolved_at IS NULL THEN resolved_at ELSE :resolved_at END,
                                result_json = CAST(:result_json AS jsonb),
                                metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                                updated_at = NOW()
                            WHERE id = CAST(:intent_id AS uuid)
                            """
                        ),
                        {
                            "intent_id": intent_id,
                            "status": status,
                            "resolved_at": resolved_at,
                            "result_json": _json_dumps(merged_result),
                            "metadata_json": metadata_json,
                        },
                    )
                    db.commit()
                    return intent_id

            inserted = db.execute(
                text(
                    """
                    INSERT INTO public.journal_execution_intents (
                        environment_id,
                        execution_context_id,
                        episode_id,
                        channel,
                        intent_type,
                        idempotency_key,
                        status,
                        requested_at,
                        resolved_at,
                        payload_json,
                        result_json,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        CAST(:execution_context_id AS uuid),
                        CAST(:episode_id AS uuid),
                        :channel,
                        :intent_type,
                        :idempotency_key,
                        :status,
                        COALESCE(:requested_at, NOW()),
                        :resolved_at,
                        CAST(:payload_json AS jsonb),
                        CAST(:result_json AS jsonb),
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": environment_id,
                    "execution_context_id": execution_context_id,
                    "episode_id": episode_id,
                    "channel": channel,
                    "intent_type": intent_type,
                    "idempotency_key": normalized_idempotency_key,
                    "status": status,
                    "requested_at": requested_at,
                    "resolved_at": resolved_at,
                    "payload_json": _json_dumps(payload or {}),
                    "result_json": _json_dumps(result or {}),
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            intent_id = str(inserted.scalar_one())
            db.commit()
            return intent_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_execution_intent_status(
        self,
        intent_id: str,
        *,
        status: str,
        resolved_at: datetime | None = None,
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db = self.session_factory()
        try:
            existing = db.execute(
                text("SELECT result_json, metadata_json FROM public.journal_execution_intents WHERE id = CAST(:intent_id AS uuid)"),
                {"intent_id": intent_id},
            ).mappings().first()
            existing_payload = _row_mapping(existing) if existing else {}

            merged_result = {
                **(_decode_json_field(existing_payload.get("result_json")) or {}),
                **(result or {}),
            }
            metadata_json: str | None = None
            if metadata is not None:
                merged_metadata = {
                    **(_decode_json_field(existing_payload.get("metadata_json")) or {}),
                    **metadata,
                }
                metadata_json = _json_dumps(merged_metadata)

            db.execute(
                text(
                    """
                    UPDATE public.journal_execution_intents
                    SET status = :status,
                        resolved_at = CASE WHEN :resolved_at IS NULL THEN resolved_at ELSE :resolved_at END,
                        result_json = CAST(:result_json AS jsonb),
                        metadata_json = CASE WHEN :metadata_json IS NULL THEN metadata_json ELSE CAST(:metadata_json AS jsonb) END,
                        updated_at = NOW()
                    WHERE id = CAST(:intent_id AS uuid)
                    """
                ),
                {
                    "intent_id": intent_id,
                    "status": status,
                    "resolved_at": resolved_at,
                    "result_json": _json_dumps(merged_result),
                    "metadata_json": metadata_json,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_note(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        note_type: str,
        title: str,
        body_markdown: str,
        episode_id: str | None = None,
        body_text: str | None = None,
        body_json: dict[str, Any] | None = None,
        effective_at: datetime | None = None,
        author_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_environment_id = _require_uuid_str("environment_id", environment_id)
        normalized_subject_type = _require_nonblank_str("subject_type", subject_type)
        normalized_subject_id = _require_nonblank_str("subject_id", subject_id)
        normalized_note_type = _require_nonblank_str("note_type", note_type)
        normalized_title = _require_nonblank_str("title", title)
        normalized_body_markdown = _require_nonblank_str("body_markdown", body_markdown)
        normalized_episode_id = _require_uuid_str("episode_id", episode_id) if episode_id is not None else None

        db = self.session_factory()
        try:
            if normalized_episode_id is not None:
                episode_row = db.execute(
                    text("SELECT * FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid) LIMIT 1"),
                    {"episode_id": normalized_episode_id},
                ).mappings().first()
                if episode_row is None:
                    raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
                episode_payload = _row_mapping(episode_row)
                if str(episode_payload.get("environment_id")) != normalized_environment_id:
                    raise ValueError("episode_id does not belong to environment_id")

            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_notes (
                        environment_id,
                        subject_type,
                        subject_id,
                        episode_id,
                        note_type,
                        title,
                        body_markdown,
                        body_text,
                        body_json,
                        effective_at,
                        author_id,
                        tags_json,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        :subject_type,
                        :subject_id,
                        CAST(:episode_id AS uuid),
                        :note_type,
                        :title,
                        :body_markdown,
                        :body_text,
                        CAST(:body_json AS jsonb),
                        :effective_at,
                        :author_id,
                        CAST(:tags_json AS jsonb),
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": normalized_environment_id,
                    "subject_type": normalized_subject_type,
                    "subject_id": normalized_subject_id,
                    "episode_id": normalized_episode_id,
                    "note_type": normalized_note_type,
                    "title": normalized_title,
                    "body_markdown": normalized_body_markdown,
                    "body_text": body_text if body_text is not None else "",
                    "body_json": _json_dumps(body_json) if body_json is not None else None,
                    "effective_at": effective_at,
                    "author_id": author_id,
                    "tags_json": _json_dumps(tags or []),
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            note_id = str(row.scalar_one())
            db.commit()
            return note_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_note(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body_markdown: str | None = None,
        body_text: str | None = None,
        body_json: dict | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        editor_id: str | None = None,
        change_reason: str | None = None,
    ) -> None:
        normalized_note_id = _require_uuid_str("note_id", note_id)

        db = self.session_factory()
        try:
            existing_row = db.execute(
                text("SELECT * FROM public.journal_notes WHERE id = CAST(:note_id AS uuid) LIMIT 1 FOR UPDATE"),
                {"note_id": normalized_note_id},
            ).mappings().first()
            if not existing_row:
                raise ValueError(f"Unknown note_id: {normalized_note_id}")

            existing = _row_mapping(existing_row)
            existing_body_markdown = _require_nonblank_str("body_markdown", existing.get("body_markdown"))
            existing_body_text = str(existing.get("body_text") or "")

            revision_no_row = db.execute(
                text(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_revision_no
                    FROM public.journal_note_revisions
                    WHERE note_id = CAST(:note_id AS uuid)
                    """
                ),
                {"note_id": normalized_note_id},
            ).mappings().first()
            next_revision_no = int((_row_mapping(revision_no_row).get("next_revision_no") if revision_no_row else 1) or 1)

            db.execute(
                text(
                    """
                    INSERT INTO public.journal_note_revisions (
                        note_id,
                        revision_no,
                        body_markdown,
                        body_text,
                        editor_id,
                        edited_at,
                        change_reason,
                        metadata_json
                    ) VALUES (
                        CAST(:note_id AS uuid),
                        :revision_no,
                        :body_markdown,
                        :body_text,
                        :editor_id,
                        NOW(),
                        :change_reason,
                        CAST(:metadata_json AS jsonb)
                    )
                    """
                ),
                {
                    "note_id": normalized_note_id,
                    "revision_no": next_revision_no,
                    "body_markdown": existing_body_markdown,
                    "body_text": existing_body_text,
                    "editor_id": editor_id,
                    "change_reason": change_reason,
                    "metadata_json": _json_dumps(
                        {
                            "title": existing.get("title"),
                            "body_json": _decode_json_field(existing.get("body_json")),
                            "tags": _decode_json_field(existing.get("tags_json")) or [],
                        }
                    ),
                },
            )

            if title is not None:
                resolved_title = _require_nonblank_str("title", title)
            else:
                resolved_title = str(existing.get("title") or "")

            if body_markdown is not None:
                resolved_body_markdown = _require_nonblank_str("body_markdown", body_markdown)
            else:
                resolved_body_markdown = existing_body_markdown

            resolved_body_text = body_text
            if resolved_body_text is None:
                if body_markdown is not None:
                    resolved_body_text = ""
                else:
                    resolved_body_text = existing_body_text

            if body_json is not None:
                resolved_body_json = body_json
            else:
                resolved_body_json = _decode_json_field(existing.get("body_json"))

            if tags is not None:
                resolved_tags = tags
            else:
                resolved_tags = _decode_json_field(existing.get("tags_json")) or []

            if metadata is not None:
                resolved_metadata = {
                    **(_decode_json_field(existing.get("metadata_json")) or {}),
                    **metadata,
                }
            else:
                resolved_metadata = _decode_json_field(existing.get("metadata_json")) or {}

            db.execute(
                text(
                    """
                    UPDATE public.journal_notes
                    SET title = :title,
                        body_markdown = :body_markdown,
                        body_text = :body_text,
                        body_json = CAST(:body_json AS jsonb),
                        tags_json = CAST(:tags_json AS jsonb),
                        metadata_json = CAST(:metadata_json AS jsonb),
                        updated_at = NOW()
                    WHERE id = CAST(:note_id AS uuid)
                    """
                ),
                {
                    "note_id": normalized_note_id,
                    "title": resolved_title,
                    "body_markdown": resolved_body_markdown,
                    "body_text": str(resolved_body_text or ""),
                    "body_json": _json_dumps(resolved_body_json) if resolved_body_json is not None else None,
                    "tags_json": _json_dumps(resolved_tags),
                    "metadata_json": _json_dumps(resolved_metadata),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_note(self, note_id: str) -> JournalNote | None:
        normalized_note_id = _require_uuid_str("note_id", note_id)
        db = self.session_factory()
        try:
            row = db.execute(
                text("SELECT * FROM public.journal_notes WHERE id = CAST(:note_id AS uuid)"),
                {"note_id": normalized_note_id},
            ).mappings().first()
            return self._note_from_row(row) if row else None
        finally:
            db.close()

    def list_notes(
        self,
        environment_id: str,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        episode_id: str | None = None,
        note_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JournalNote]:
        normalized_environment_id = _require_uuid_str("environment_id", environment_id)
        normalized_episode_id = _require_uuid_str("episode_id", episode_id) if episode_id is not None else None
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_notes
                    WHERE environment_id = CAST(:environment_id AS uuid)
                      AND (:subject_type IS NULL OR subject_type = :subject_type)
                      AND (:subject_id IS NULL OR subject_id = :subject_id)
                      AND (:episode_id IS NULL OR episode_id = CAST(:episode_id AS uuid))
                      AND (:note_type IS NULL OR note_type = :note_type)
                    ORDER BY updated_at DESC, created_at DESC, id DESC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "environment_id": normalized_environment_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "episode_id": normalized_episode_id,
                    "note_type": note_type,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            return [self._note_from_row(row) for row in rows]
        finally:
            db.close()

    def list_note_revisions(self, note_id: str) -> list[JournalNoteRevision]:
        normalized_note_id = _require_uuid_str("note_id", note_id)
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_note_revisions
                    WHERE note_id = CAST(:note_id AS uuid)
                    ORDER BY revision_no ASC, id ASC
                    """
                ),
                {"note_id": normalized_note_id},
            ).mappings().all()
            return [self._note_revision_from_row(row) for row in rows]
        finally:
            db.close()

    def attach_file_metadata(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        storage_key: str,
        mime_type: str,
        note_id: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        ocr_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized_environment_id = _require_uuid_str("environment_id", environment_id)
        normalized_subject_type = _require_nonblank_str("subject_type", subject_type)
        normalized_subject_id = _require_nonblank_str("subject_id", subject_id)
        normalized_storage_key = _require_nonblank_str("storage_key", storage_key)
        normalized_mime_type = _require_nonblank_str("mime_type", mime_type)
        normalized_note_id = _require_uuid_str("note_id", note_id) if note_id is not None else None

        db = self.session_factory()
        try:
            if normalized_note_id is not None:
                note_row = db.execute(
                    text("SELECT * FROM public.journal_notes WHERE id = CAST(:note_id AS uuid) LIMIT 1"),
                    {"note_id": normalized_note_id},
                ).mappings().first()
                if note_row is None:
                    raise LookupError(f"Unknown note_id: {normalized_note_id}")
                note_payload = _row_mapping(note_row)
                if str(note_payload.get("environment_id")) != normalized_environment_id:
                    raise ValueError("note_id does not belong to environment_id")

            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_attachments (
                        environment_id,
                        subject_type,
                        subject_id,
                        note_id,
                        storage_key,
                        mime_type,
                        sha256,
                        size_bytes,
                        ocr_text,
                        metadata_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        :subject_type,
                        :subject_id,
                        CAST(:note_id AS uuid),
                        :storage_key,
                        :mime_type,
                        :sha256,
                        :size_bytes,
                        :ocr_text,
                        CAST(:metadata_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": normalized_environment_id,
                    "subject_type": normalized_subject_type,
                    "subject_id": normalized_subject_id,
                    "note_id": normalized_note_id,
                    "storage_key": normalized_storage_key,
                    "mime_type": normalized_mime_type,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "ocr_text": ocr_text,
                    "metadata_json": _json_dumps(metadata or {}),
                },
            )
            attachment_id = str(row.scalar_one())
            db.commit()
            return attachment_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def append_timeline_event(self, event: JournalTimelineEvent) -> str:
        normalized_environment_id = _require_uuid_str("environment_id", event.environment_id)
        normalized_subject_type = _require_nonblank_str("subject_type", event.subject_type)
        normalized_subject_id = _require_nonblank_str("subject_id", event.subject_id)
        normalized_event_type = _require_nonblank_str("event_type", event.event_type)
        normalized_episode_id = _require_uuid_str("episode_id", event.episode_id) if event.episode_id is not None else None
        normalized_context_id = _require_uuid_str("execution_context_id", event.execution_context_id) if event.execution_context_id is not None else None

        db = self.session_factory()
        try:
            if normalized_episode_id is not None:
                episode_row = db.execute(
                    text("SELECT * FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid) LIMIT 1"),
                    {"episode_id": normalized_episode_id},
                ).mappings().first()
                if episode_row is None:
                    raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
                episode_payload = _row_mapping(episode_row)
                if str(episode_payload.get("environment_id")) != normalized_environment_id:
                    raise ValueError("episode_id does not belong to environment_id")

            if normalized_context_id is not None:
                context_row = db.execute(
                    text("SELECT * FROM public.journal_execution_contexts WHERE id = CAST(:context_id AS uuid) LIMIT 1"),
                    {"context_id": normalized_context_id},
                ).mappings().first()
                if context_row is None:
                    raise LookupError(f"Unknown execution_context_id: {normalized_context_id}")
                context_payload = _row_mapping(context_row)
                if str(context_payload.get("environment_id")) != normalized_environment_id:
                    raise ValueError("execution_context_id does not belong to environment_id")

            row = db.execute(
                text(
                    """
                    INSERT INTO public.journal_timeline_events (
                        environment_id,
                        episode_id,
                        execution_context_id,
                        subject_type,
                        subject_id,
                        channel,
                        event_type,
                        actor_type,
                        correlation_id,
                        causation_id,
                        occurred_at,
                        payload_json
                    ) VALUES (
                        CAST(:environment_id AS uuid),
                        CAST(:episode_id AS uuid),
                        CAST(:execution_context_id AS uuid),
                        :subject_type,
                        :subject_id,
                        :channel,
                        :event_type,
                        :actor_type,
                        :correlation_id,
                        :causation_id,
                        :occurred_at,
                        CAST(:payload_json AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "environment_id": normalized_environment_id,
                    "episode_id": normalized_episode_id,
                    "execution_context_id": normalized_context_id,
                    "subject_type": normalized_subject_type,
                    "subject_id": normalized_subject_id,
                    "channel": event.channel,
                    "event_type": normalized_event_type,
                    "actor_type": event.actor_type,
                    "correlation_id": event.correlation_id,
                    "causation_id": event.causation_id,
                    "occurred_at": event.occurred_at,
                    "payload_json": _json_dumps(event.payload or {}),
                },
            )
            event_id = str(row.scalar_one())
            db.commit()
            return event_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_timeline_events(
        self,
        *,
        environment_id: str | None = None,
        episode_id: str | None = None,
        execution_context_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[JournalTimelineEvent]:
        normalized_environment_id = _require_uuid_str("environment_id", environment_id) if environment_id is not None else None
        normalized_episode_id = _require_uuid_str("episode_id", episode_id) if episode_id is not None else None
        normalized_context_id = _require_uuid_str("execution_context_id", execution_context_id) if execution_context_id is not None else None
        db = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_timeline_events
                    WHERE (:environment_id IS NULL OR environment_id = CAST(:environment_id AS uuid))
                      AND (:episode_id IS NULL OR episode_id = CAST(:episode_id AS uuid))
                      AND (:execution_context_id IS NULL OR execution_context_id = CAST(:execution_context_id AS uuid))
                      AND (:subject_type IS NULL OR subject_type = :subject_type)
                      AND (:subject_id IS NULL OR subject_id = :subject_id)
                      AND (:event_type IS NULL OR event_type = :event_type)
                    ORDER BY occurred_at ASC, id ASC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ),
                {
                    "environment_id": normalized_environment_id,
                    "episode_id": normalized_episode_id,
                    "execution_context_id": normalized_context_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "event_type": event_type,
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).mappings().all()
            return [self._timeline_event_from_row(row) for row in rows]
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

    def get_latest_metric_snapshot(
        self,
        *,
        subject_type: str,
        subject_id: str,
        window: Optional[str] = None,
        calc_version: Optional[str] = None,
        environment_id: Optional[str] = None,
        identity_rule_version: Optional[str] = None,
        grouping_rule_version: Optional[str] = None,
    ) -> Optional[JournalMetricSnapshot]:
        db = self.session_factory()
        try:
            row = db.execute(
                text(
                    """
                    SELECT *
                    FROM public.journal_metric_snapshots
                    WHERE subject_type = :subject_type
                      AND subject_id = :subject_id
                      AND (:window IS NULL OR time_window = :window)
                      AND (:calc_version IS NULL OR calc_version = :calc_version)
                      AND (
                            (:environment_id IS NULL AND environment_id IS NULL)
                            OR (:environment_id IS NOT NULL AND environment_id = CAST(:environment_id AS uuid))
                          )
                      AND (:identity_rule_version IS NULL OR identity_rule_version = :identity_rule_version)
                      AND (:grouping_rule_version IS NULL OR grouping_rule_version = :grouping_rule_version)
                    ORDER BY computed_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "window": window,
                    "calc_version": calc_version,
                    "environment_id": environment_id,
                    "identity_rule_version": identity_rule_version,
                    "grouping_rule_version": grouping_rule_version,
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

    def _execution_environment_from_row(self, row: Any) -> JournalExecutionEnvironment:
        payload = _row_mapping(row)
        return JournalExecutionEnvironment(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            mode=payload.get("mode"),
            account_scope=payload.get("account_scope"),
            broker_user_id=payload.get("broker_user_id"),
            paper_account_key=payload.get("paper_account_key"),
            environment_epoch=payload.get("environment_epoch") or 1,
            display_name=payload.get("display_name"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            retired_at=payload.get("retired_at"),
        )

    def _execution_context_from_row(self, row: Any) -> JournalExecutionContext:
        payload = _row_mapping(row)
        return JournalExecutionContext(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            source_system=payload.get("source_system"),
            external_run_id=payload.get("external_run_id"),
            strategy_template_id=str(payload.get("strategy_template_id")) if payload.get("strategy_template_id") is not None else None,
            strategy_variant_id=str(payload.get("strategy_variant_id")) if payload.get("strategy_variant_id") is not None else None,
            strategy_deployment_id=str(payload.get("strategy_deployment_id")) if payload.get("strategy_deployment_id") is not None else None,
            status=payload.get("status") or "active",
            opened_at=payload.get("opened_at") or datetime.utcnow(),
            closed_at=payload.get("closed_at"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
        )

    def _episode_from_row(self, row: Any) -> JournalEpisode:
        payload = _row_mapping(row)
        return JournalEpisode(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            execution_context_id=str(payload.get("execution_context_id")),
            episode_seq=int(payload.get("episode_seq") or 0),
            status=payload.get("status") or "draft",
            opened_at=payload.get("opened_at") or datetime.utcnow(),
            closed_at=payload.get("closed_at"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
        )

    def _execution_intent_from_row(self, row: Any) -> JournalExecutionIntent:
        payload = _row_mapping(row)
        return JournalExecutionIntent(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            execution_context_id=str(payload.get("execution_context_id")) if payload.get("execution_context_id") is not None else None,
            episode_id=str(payload.get("episode_id")) if payload.get("episode_id") is not None else None,
            channel=payload.get("channel"),
            intent_type=payload.get("intent_type"),
            idempotency_key=payload.get("idempotency_key"),
            status=payload.get("status") or "pending",
            requested_at=payload.get("requested_at") or datetime.utcnow(),
            resolved_at=payload.get("resolved_at"),
            payload=_decode_json_field(payload.get("payload_json")) or {},
            result=_decode_json_field(payload.get("result_json")) or {},
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
        )

    def _strategy_template_from_row(self, row: Any) -> JournalStrategyTemplate:
        payload = _row_mapping(row)
        return JournalStrategyTemplate(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            strategy_family=payload.get("strategy_family"),
            template_key=payload.get("template_key"),
            display_name=payload.get("display_name"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
        )

    def _strategy_variant_from_row(self, row: Any) -> JournalStrategyVariant:
        payload = _row_mapping(row)
        return JournalStrategyVariant(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            template_id=str(payload.get("template_id")),
            variant_key=payload.get("variant_key"),
            display_name=payload.get("display_name"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
        )

    def _strategy_deployment_from_row(self, row: Any) -> JournalStrategyDeployment:
        payload = _row_mapping(row)
        return JournalStrategyDeployment(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            template_id=str(payload.get("template_id")),
            variant_id=str(payload.get("variant_id")) if payload.get("variant_id") is not None else None,
            deployment_key=payload.get("deployment_key"),
            display_name=payload.get("display_name"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
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
            environment_id=str(payload.get("environment_id")) if payload.get("environment_id") is not None else None,
            episode_id=str(payload.get("episode_id")) if payload.get("episode_id") is not None else None,
            intent_id=str(payload.get("intent_id")) if payload.get("intent_id") is not None else None,
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
            position_effect=payload.get("position_effect"),
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
            environment_id=str(payload.get("environment_id")) if payload.get("environment_id") is not None else None,
            subject_type=payload.get("subject_type"),
            subject_id=payload.get("subject_id"),
            window=payload.get("time_window") or payload.get("window"),
            calc_version=payload.get("calc_version"),
            identity_rule_version=payload.get("identity_rule_version") or "v1_legacy",
            grouping_rule_version=payload.get("grouping_rule_version") or "v1_legacy",
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

    def _note_from_row(self, row: Any) -> JournalNote:
        payload = _row_mapping(row)
        return JournalNote(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            subject_type=str(payload.get("subject_type") or ""),
            subject_id=str(payload.get("subject_id") or ""),
            episode_id=str(payload.get("episode_id")) if payload.get("episode_id") is not None else None,
            note_type=str(payload.get("note_type") or ""),
            title=str(payload.get("title") or ""),
            body_markdown=str(payload.get("body_markdown") or ""),
            body_text=str(payload.get("body_text") or ""),
            body_json=_decode_json_field(payload.get("body_json")),
            effective_at=payload.get("effective_at"),
            author_id=payload.get("author_id"),
            tags=_decode_json_field(payload.get("tags_json")) or [],
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
            updated_at=payload.get("updated_at") or datetime.utcnow(),
            archived_at=payload.get("archived_at"),
        )

    def _note_revision_from_row(self, row: Any) -> JournalNoteRevision:
        payload = _row_mapping(row)
        return JournalNoteRevision(
            id=payload.get("id"),
            note_id=str(payload.get("note_id") or ""),
            revision_no=int(payload.get("revision_no") or 0),
            body_markdown=str(payload.get("body_markdown") or ""),
            body_text=str(payload.get("body_text") or ""),
            editor_id=payload.get("editor_id"),
            edited_at=payload.get("edited_at") or datetime.utcnow(),
            change_reason=payload.get("change_reason"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
        )

    def _attachment_from_row(self, row: Any) -> JournalAttachment:
        payload = _row_mapping(row)
        return JournalAttachment(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            subject_type=str(payload.get("subject_type") or ""),
            subject_id=str(payload.get("subject_id") or ""),
            note_id=str(payload.get("note_id")) if payload.get("note_id") is not None else None,
            storage_key=str(payload.get("storage_key") or ""),
            mime_type=str(payload.get("mime_type") or ""),
            sha256=payload.get("sha256"),
            size_bytes=payload.get("size_bytes"),
            ocr_text=payload.get("ocr_text"),
            metadata=_decode_json_field(payload.get("metadata_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
        )

    def _timeline_event_from_row(self, row: Any) -> JournalTimelineEvent:
        payload = _row_mapping(row)
        return JournalTimelineEvent(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            environment_id=str(payload.get("environment_id")),
            episode_id=str(payload.get("episode_id")) if payload.get("episode_id") is not None else None,
            execution_context_id=str(payload.get("execution_context_id")) if payload.get("execution_context_id") is not None else None,
            subject_type=str(payload.get("subject_type") or ""),
            subject_id=str(payload.get("subject_id") or ""),
            channel=payload.get("channel"),
            event_type=str(payload.get("event_type") or ""),
            actor_type=payload.get("actor_type") or "system",
            correlation_id=payload.get("correlation_id"),
            causation_id=payload.get("causation_id"),
            occurred_at=payload.get("occurred_at") or datetime.utcnow(),
            payload=_decode_json_field(payload.get("payload_json")) or {},
            created_at=payload.get("created_at") or datetime.utcnow(),
        )
